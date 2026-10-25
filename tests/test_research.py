"""Tests for briefing_reader.ask.research: routing, the isolated stdin, the URL rules, the init and
web-step guards, the answer and one research run end to end; and which of a run's web steps
research_live.WebSteps hands to the page snapshotter (a recording fake: no Qt, no page).

Every run is a recorded (hand-written, invented) stream from tests/fixtures/ask/research_*.jsonl
played by a fake process on a fake clock: claude.exe is never started and nothing touches the
network.
"""

from __future__ import annotations

import inspect
import json
import logging
import threading
import unittest
import unittest.mock
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from briefing_reader import live, pictures, snapshots
from briefing_reader.actions import link_host
from briefing_reader.ask import research as rs
from briefing_reader.ask import research_live
from briefing_reader.ask import stream as st
from briefing_reader.ask.research import (
    FETCH,
    SEARCH,
    ResearchAnswer,
    ResearchLimits,
    WebTracker,
    build_research_prompt,
    check_question,
    check_research_init,
    fetch_url_problem,
    forced_question,
    normalize_url,
    read_research_result,
    run_research,
)
from briefing_reader.ask.research_validate import ResearchReport, WebSource, validate
from tests.ask_fakes import (
    HOURS_URL,
    NOW,
    RESEARCH_ANSWER,
    RESEARCH_HITS,
    RESEARCH_QUERY,
    VISIT_URL,
    FakeClock,
    FakeProcess,
    FakeRunner,
    assistant,
    fetch_block,
    fetch_call,
    research_init,
    research_result,
    research_stream,
    search_call,
    search_result,
    stream,
    success_events,
    tool_result,
)

ZONE = "America/Los_Angeles"
QUESTION = "what time does the museum open on saturday"
GOLDEN = ("<today>\nWednesday 2026-10-07 09:30 (time zone America/Los_Angeles)\n</today>\n"
          "<limits>\nat most 4 web searches and 4 page reads; at most 5 sources; at most 3 suggestions\n</limits>\n"
          "<question>\nwhat time does the museum open on saturday\n</question>\n")
LSAQUO, RSAQUO = chr(0x2039), chr(0x203A)


def events(name: str) -> list[dict]:
    """A fixture's events after its init line (garbage lines left out)."""
    found = [st.parse_event(line) for line in stream(name)[1:]]
    return [event for event in found if event is not None]


class ForcedQuestionTests(unittest.TestCase):
    def test_the_prefix(self) -> None:
        for command, question in (("web: x", "x"), ("Web:x", "x"), ("  research:  x", "x"), ("RESEARCH: x", "x"),
                                  ("web : opening hours of the museum", "opening hours of the museum"),
                                  ("web:", ""), ("web:   ", ""), ("research:", ""), ("web: web: x", "web: x")):
            with self.subTest(command=command):
                self.assertEqual(forced_question(command), question)
        for command in ("webinar: x", "website: x", "the web: x", "web://x", "x web: y", "web x", "", "researcher: x",
                        "https://example.org", "web//: x"):
            with self.subTest(command=command):
                self.assertIsNone(forced_question(command))


class CheckQuestionTests(unittest.TestCase):
    COMMAND = "check the Budget review reply and what time the museum opens on saturday"

    def test_grounded_questions_pass(self) -> None:
        for question in ("museum opening hours saturday october 2026",   # neutral words, dates, a year
                         "Museum opens Saturday",                        # another case
                         "museum opening times sat 10 am",               # 3-letter day, a number <= 31, am
                         "what time does the museum open",               # stop words
                         "budget reviews museum",                        # another ending of a typed word
                         "museum hours 2025 2027"):                      # the years next to now's
            with self.subTest(question=question):
                check = check_question(question, self.COMMAND, NOW)
                self.assertTrue(check.ok, check.reason)
                self.assertEqual(check.unknown, ())
        typed = check_question("museum hours 1999", "museum hours in 1999", NOW)
        self.assertTrue(typed.ok)   # a number the owner typed

    def test_context_words_are_refused_with_the_words(self) -> None:
        check = check_question("Zq7 Calmarker museum opening hours", self.COMMAND, NOW)
        self.assertFalse(check.ok)
        self.assertEqual(check.unknown, ("calmarker", "zq7"))
        self.assertEqual(check.reason, "it named words you didn't type (calmarker, zq7)")
        self.assertEqual(check.text, "Zq7 Calmarker museum opening hours")
        self.assertEqual(check_question("museum hours 1999", self.COMMAND, NOW).unknown, ("1999",))
        many = check_question(" ".join(f"word{n}x" for n in range(12)), self.COMMAND, NOW)
        self.assertEqual(len(many.unknown), 8)
        self.assertNotIn("calmarker", repr(check))   # the repr names counts only

    def test_nothing_rides_on_a_typed_word_a_number_or_an_invisible_character(self) -> None:
        # T-ISO-1: a context datum glued to a typed word, spelled as numbers or hidden by an
        # invisible character never passes (the hint is checked as it would be sent).
        command = "what time does the museum open this weekend"
        for question, unknown in (("museum timedrsmith opensmithjohn hours", ("opensmithjohn", "timedrsmith")),
                                  ("museum hours 4 8 2 9 1 7", ("1", "2", "4", "7", "8", "9")),
                                  ("museum 5 5 5 0 1 3 4", ("0", "1", "3", "4", "5")),
                                  ("the\u2060museum", ("themuseum",)),
                                  ("museum hours wee\u200bkend open\u200bcalmarker", ("opencalmarker",)),
                                  ("museum hours sat 4 sat 8 sat 2", ("2",)),
                                  ("museums opensmith", ("opensmith",))):
            with self.subTest(question=question):
                check = check_question(question, command, NOW)
                self.assertFalse(check.ok)
                self.assertEqual(check.unknown, unknown)
        for question in ("museums opened opening hours", "museum closing time", "museum october 10 2026",
                         "museum 10 am saturday"):
            with self.subTest(question=question):
                self.assertTrue(check_question(question, command + " close", NOW).ok)
        self.assertTrue(rs.inflected("closing", "close"))
        self.assertTrue(rs.inflected("museum", "museums"))
        self.assertFalse(rs.inflected("timedrsmith", "time"))
        self.assertFalse(rs.inflected("opener", "open"))

    def test_addresses_links_and_length(self) -> None:
        for question in ("museum hours ana@example.com", "museum https://example.org", "museum www.example.org"):
            with self.subTest(question=question):
                check = check_question(question, self.COMMAND, NOW)
                self.assertFalse(check.ok)
                self.assertEqual(check.reason, "it names an address or a link you didn't type")
        typed = "museum hours on www.example.org"
        self.assertTrue(check_question("museum hours www.example.org", typed, NOW).ok)
        long = check_question("museum " * 40, self.COMMAND + " museum", NOW)
        self.assertFalse(long.ok)
        self.assertEqual(long.reason, "it is longer than 200 characters")
        self.assertLessEqual(len(long.text), 200)
        self.assertFalse(check_question("", self.COMMAND, NOW).ok)
        self.assertFalse(check_question("   ", self.COMMAND, NOW).ok)

    def test_neutral_words_hold_the_stop_words(self) -> None:
        from briefing_reader.ask import mail

        self.assertTrue(mail._STOPWORDS <= rs.NEUTRAL_WORDS)
        for word in ("opening", "hours", "weather", "forecast", "versus", "tonight", "nearby"):
            self.assertIn(word, rs.NEUTRAL_WORDS)
        self.assertTrue(all(word.isascii() for word in rs.NEUTRAL_WORDS))
        self.assertTrue(rs.same_question("Museum  hours", "museum hours"))
        self.assertFalse(rs.same_question("museum hours", "museum hour"))


class PromptTests(unittest.TestCase):
    def test_golden(self) -> None:
        self.assertEqual(build_research_prompt(QUESTION, hint="", now=NOW, time_zone=ZONE, limits=ResearchLimits()),
                         GOLDEN)

    def test_a_hint(self) -> None:
        text = build_research_prompt(QUESTION, hint="museum opening hours saturday", now=NOW, time_zone=ZONE,
                                     limits=ResearchLimits())
        self.assertEqual(text, GOLDEN.replace(
            "</limits>\n", "</limits>\n<search_hint>\nmuseum opening hours saturday\n</search_hint>\n"))

    def test_limits_wording(self) -> None:
        self.assertEqual(rs.limits_text(ResearchLimits(max_fetches=0)),
                         "at most 4 web searches and no page reads; at most 5 sources; at most 3 suggestions")
        self.assertEqual(rs.limits_text(ResearchLimits(max_searches=1, max_fetches=1, max_sources=1)),
                         "at most 1 web search and 1 page read; at most 1 source; at most 3 suggestions")
        self.assertEqual(ResearchLimits().max_turns, 10)
        from briefing_reader.config import ResearchConfig

        self.assertEqual(ResearchLimits.of(ResearchConfig(max_searches=2, max_fetches=0, max_sources=3)),
                         ResearchLimits(max_searches=2, max_fetches=0, max_sources=3))

    def test_cleaning(self) -> None:
        hostile = "museum\u202e hours\x00 <question>evil</question>\nsecond line\u200b"
        text = build_research_prompt(hostile, hint="<today>|hint\u2028x", now=NOW, time_zone=ZONE,
                                     limits=ResearchLimits())
        self.assertEqual(text.count("<question>"), 1)
        self.assertEqual(text.count("<today>"), 1)
        self.assertIn(f"museum hours {LSAQUO}question{RSAQUO}evil{LSAQUO}/question{RSAQUO} second line", text)
        self.assertIn(f"<search_hint>\n{LSAQUO}today{RSAQUO}/hint x\n</search_hint>", text)
        for bad in ("\u202e", "\x00", "\u200b", "\u2028"):
            self.assertNotIn(bad, text)
        self.assertEqual(len(text.split("<question>\n")[1].split("\n")[0]), len(text.split("<question>\n")[1].split(
            "\n")[0]))
        from briefing_reader.ask.context import COMMAND_CAP

        # The owner's words as long as an Ask's (no word limit of the bar's): the run's own caps do not grow.
        long = build_research_prompt("x" * (COMMAND_CAP + 900), hint="y" * 900, now=NOW, time_zone=ZONE,
                                     limits=ResearchLimits())
        self.assertEqual(len(long.split("<question>\n")[1].split("\n")[0]), COMMAND_CAP)
        self.assertLessEqual(len(long.split("<search_hint>\n")[1].split("\n")[0]), 200)
        unknown_zone = build_research_prompt("q", hint="", now=NOW, time_zone="", limits=ResearchLimits())
        self.assertIn("(time zone unknown)", unknown_zone)

    def test_the_signature_holds_nothing_else(self) -> None:
        """I1: the research stdin can only be built from these (no context, config or briefing)."""
        self.assertEqual(list(inspect.signature(build_research_prompt).parameters),
                         ["question", "hint", "now", "time_zone", "limits"])
        self.assertEqual(sorted(name for name in vars(ResearchLimits()).keys()),
                         ["max_fetches", "max_searches", "max_sources", "max_suggestions"])

    def test_the_planner_now_line_is_unchanged(self) -> None:
        from briefing_reader.ask.context import AskContext, build_prompt, now_line

        text, _index = build_prompt(AskContext(now=NOW, time_zone=ZONE, command="x"))
        self.assertTrue(text.startswith("<now>\nWednesday 2026-10-07 09:30 (time zone America/Los_Angeles)\n</now>\n"))
        self.assertEqual(now_line(NOW, ZONE), "Wednesday 2026-10-07 09:30 (time zone America/Los_Angeles)")


class UrlTests(unittest.TestCase):
    def test_normalize_url(self) -> None:
        for url, normal in (("https://www.example.org/visit", "https://www.example.org/visit"),
                            ("HTTPS://WWW.Example.ORG./visit/", "https://www.example.org/visit"),
                            ("https://example.org", "https://example.org/"),
                            ("https://example.org/", "https://example.org/"),
                            ("https://example.org:443/a#frag", "https://example.org/a"),
                            ("http://example.org:80/a", "http://example.org/a"),
                            ("https://example.org:8443/a", "https://example.org:8443/a"),
                            ("https://example.org/a/?b=1#x", "https://example.org/a?b=1"),
                            ("https://example.org/A/B", "https://example.org/A/B"),
                            ("https://[::1]/x", "https://[::1]/x"),
                            ("  https://example.org/a  ", "https://example.org/a"),
                            ("not a url", "not a url"), ("", "")):
            with self.subTest(url=url):
                self.assertEqual(normalize_url(url), normal)
        self.assertEqual(normalize_url(None), "")
        self.assertNotEqual(normalize_url("https://evil@example.org/"), normalize_url("https://example.org/"))

    def test_fetch_url_problem(self) -> None:
        for url in ("https://www.example.org/x", "http://example.org", "https://museum.example.net:443/a",
                    "http://example.org:80/a", "https://xn--bcher-kva.example/", "https://example.org/a?q=1#f"):
            with self.subTest(url=url):
                self.assertEqual(fetch_url_problem(url), "")
        refused = {
            "a local or private address": ("http://localhost/", "https://127.0.0.1/", "http://10.1.2.3/",
                                           "http://192.168.1.1:8080/admin", "http://[::1]/", "http://router.local/",
                                           "http://intranet/", "http://printer.lan/", "https://a.internal/x",
                                           "http://169.254.169.254/latest/meta-data", "http://0.0.0.0/"),
            "an address with a user name or password": ("https://user:pw@example.org/", "https://user@example.org/"),
            "an address with an unusual port": ("https://example.org:8443/",),
            "not an http or https address": ("file:///C:/Users/example/.env", "ftp://example.org/", "data:text/html,x",
                                             "javascript:alert(1)", "example.org/x"),
            "not a web address": ("", "   ", "https://example.org/a b", "https://example.org/\x00",
                                  "https://exa\\mple.org/", "https://8.8.8.8/", None, 3),
        }
        for reason, urls in refused.items():
            for url in urls:
                with self.subTest(url=url):
                    self.assertEqual(fetch_url_problem(url), reason)
        self.assertEqual(fetch_url_problem("https://example.org/" + "a" * 2100), "longer than 2,048 characters")


class InitTests(unittest.TestCase):
    def test_the_research_init(self) -> None:
        init = check_research_init(research_init(), ResearchLimits())
        self.assertEqual((init.api_key_source, init.tools, init.mcp_servers, init.structured_tool, init.permission_mode),
                         ("none", ("StructuredOutput", "WebFetch", "WebSearch"), 0, True, "dontAsk"))
        self.assertEqual((init.model, init.version), ("claude-sonnet-4-5-20250929", "2.1.294"))
        # WebFetch missing while allowed: search only, not a stop. No permissionMode said: fine.
        self.assertEqual(check_research_init(research_init(tools=["StructuredOutput", "WebSearch"]),
                                             ResearchLimits()).tools, ("StructuredOutput", "WebSearch"))
        no_mode = {key: value for key, value in research_init().items() if key != "permissionMode"}
        self.assertEqual(check_research_init(no_mode, ResearchLimits()).permission_mode, "")

    def test_the_init_fixtures_trip(self) -> None:
        for name, reason in (("research_init_bash", "tools"), ("research_init_mcp", "mcp_servers"),
                             ("research_api_key", "api_key_source"), ("research_init_mode", "permission_mode")):
            with self.subTest(name=name), self.assertRaises(st.GuardTrip) as caught:
                check_research_init(json.loads(stream(name)[0]), ResearchLimits())
            self.assertEqual(caught.exception.reason, reason)
        with self.assertRaises(st.RunStop) as stopped:
            check_research_init(json.loads(stream("research_init_no_search")[0]), ResearchLimits())
        self.assertEqual((stopped.exception.failure.kind, stopped.exception.failure.detail), (st.GUARD, "no_web_search"))
        self.assertIn("without its web search tool", stopped.exception.failure.message)

    def test_more_trips(self) -> None:
        cases = {
            "tools": [research_init(tools=["StructuredOutput", "WebSearch", "Read"]),
                      research_init(tools=["StructuredOutput", "WebSearch", "mcp__gmail__send"]),
                      research_init(tools="WebSearch"), research_init(tools=[1])],
            "no_structured_output": [research_init(tools=["WebSearch", "WebFetch"])],
            "not_init": [{"type": "assistant"}, research_init(subtype="status")],
            "mcp_servers": [research_init(mcp_servers=None)],
            "permission_mode": [research_init(permissionMode="bypassPermissions"), research_init(permissionMode="")],
        }
        for reason, cases_ in cases.items():
            for event in cases_:
                with self.subTest(reason=reason), self.assertRaises(st.GuardTrip) as caught:
                    check_research_init(event, ResearchLimits())
                self.assertEqual(caught.exception.reason, reason)
        # Page reads off ([research] max_fetches = 0): WebFetch is a tool too many.
        with self.assertRaises(st.GuardTrip) as caught:
            check_research_init(research_init(), ResearchLimits(max_fetches=0))
        self.assertEqual(caught.exception.reason, "tools")
        check_research_init(research_init(tools=["StructuredOutput", "WebSearch"]), ResearchLimits(max_fetches=0))


class TrackerTests(unittest.TestCase):
    """WebTracker over the recorded fixtures: every call and result in order, exact, with the caps
    and guards."""

    def track(self, name: str, limits: ResearchLimits = ResearchLimits(), question: str = "") -> tuple[
            WebTracker, list, Exception | None]:
        heard: list = []
        tracker = WebTracker(limits, question, on_call=lambda call: heard.append(("call", call)),
                             on_result=lambda result: heard.append(("result", result)))
        stopped = None
        for event in events(name):
            try:
                tracker.check(event)
            except (st.GuardTrip, st.RunStop) as exc:
                stopped = exc
                break
        return tracker, heard, stopped

    def test_success(self) -> None:
        tracker, heard, stopped = self.track("research_success")
        self.assertIsNone(stopped)
        self.assertEqual([(what, item.kind) for what, item in heard],
                         [("call", SEARCH), ("result", SEARCH), ("call", FETCH), ("result", FETCH)])
        search, search_done, fetch, fetch_done = (item for _what, item in heard)
        self.assertEqual((search.tool_id, search.number, search.query, search.server), ("toolu_s1", 1, RESEARCH_QUERY,
                                                                                          False))
        self.assertEqual((search_done.status, search_done.hits, search_done.groups, search_done.took_ms),
                         ("ok", RESEARCH_HITS, 1, 2400))
        self.assertIn("Links: [", search_done.text)
        self.assertEqual((fetch.url, fetch.prompt, fetch.found_by, fetch.number),
                         (VISIT_URL, "What are the opening hours on Saturday?", rs.FOUND_SEARCH, 1))
        self.assertEqual((fetch_done.status, fetch_done.code, fetch_done.code_text, fetch_done.size, fetch_done.took_ms,
                          fetch_done.final_url), ("ok", 200, "OK", 48213, 1840, VISIT_URL))
        self.assertTrue(fetch_done.text.startswith("The Example Museum opens at 10 AM"))
        log = tracker.log()
        self.assertEqual((log.searches, log.fetches, log.denied, log.hits, log.checkable), (1, 1, 0, 3, True))
        self.assertEqual(log.seen, frozenset(normalize_url(url) for _title, url in RESEARCH_HITS))
        self.assertEqual(log.title_of("https://www.example.org/visit/"), "Visit - Example Museum")
        self.assertEqual(log.title_of("https://example.com/none"), "")
        self.assertIs(log.result_of("toolu_f1"), fetch_done)
        for text in (repr(log), repr(search), repr(fetch_done)):
            for secret in ("Example Museum", "example.org", "museum"):
                self.assertNotIn(secret, text)

    def test_parallel_calls_are_closed_by_their_own_results(self) -> None:
        tracker, heard, _ = self.track("research_parallel")
        self.assertEqual([(what, item.tool_id) for what, item in heard],
                         [("call", "toolu_s1"), ("call", "toolu_s2"), ("result", "toolu_s2"), ("result", "toolu_s1")])
        second = heard[1][1]
        self.assertEqual((second.number, second.allowed_domains), (2, ("example.net",)))
        self.assertEqual(heard[2][1].hits, (("Hours and tickets", HOURS_URL),))

    def test_results_as_text_or_unreadable(self) -> None:
        tracker, heard, _ = self.track("research_text_results")
        result = heard[1][1]
        self.assertEqual((result.status, result.hits, result.groups), ("ok", RESEARCH_HITS, 0))
        self.assertTrue(tracker.log().checkable)
        tracker, heard, _ = self.track("research_unread")
        result = heard[1][1]
        self.assertEqual((result.status, result.hits), ("unread", ()))
        self.assertIn("opens at 10 AM", result.text)
        self.assertFalse(tracker.log().checkable)

    def test_denied_redirect_and_errors(self) -> None:
        tracker, heard, _ = self.track("research_denied")
        denied = heard[-1][1]
        self.assertEqual(denied.status, "denied")
        self.assertIn("haven't granted it yet", denied.message)
        self.assertEqual(tracker.log().denied, 1)
        tracker, heard, _ = self.track("research_redirect")
        redirect, final = heard[3][1], heard[5][1]
        self.assertEqual((redirect.status, redirect.code, redirect.redirect_to), ("redirect", 301,
                                                                                  "https://www.example.net/new"))
        self.assertEqual(final.status, "ok")
        self.assertIn(normalize_url("https://www.example.net/new"), tracker.log().seen)
        self.assertNotIn(normalize_url("https://www.example.org/old-hours/x"), tracker.log().seen)
        self.assertEqual(heard[4][1].found_by, rs.FOUND_OTHER)
        tracker, heard, _ = self.track("research_fetch_error")
        failed = heard[-1][1]
        self.assertEqual((failed.status, failed.code, failed.message), ("error", 404, "Request failed with status code 404"))
        self.assertNotIn(normalize_url(HOURS_URL + "/x"), tracker.log().seen)

    def test_the_caps_stop_the_run_at_the_call(self) -> None:
        tracker, heard, stopped = self.track("research_too_many_searches")
        self.assertIsInstance(stopped, st.RunStop)
        self.assertEqual((stopped.failure.kind, stopped.failure.detail), (st.RESEARCH_CAP, "searches"))
        self.assertEqual(stopped.failure.message, "Jarvis stopped the web research: it wanted a fifth web search "
                                                  "(at most 4: [research] max_searches); nothing was proposed")
        last = heard[-1][1]
        self.assertEqual((last.number, last.over), (5, True))
        self.assertEqual(tracker.log().searches, 5)
        tracker, heard, stopped = self.track("research_too_many_fetches")
        self.assertEqual((stopped.failure.kind, stopped.failure.detail), (st.RESEARCH_CAP, "fetches"))
        self.assertIn("it wanted a fifth page read (at most 4: [research] max_fetches)", stopped.failure.message)
        self.assertTrue(heard[-1][1].over)
        _, _, stopped = self.track("research_too_many_searches", ResearchLimits(max_searches=7))
        self.assertIsNone(stopped)
        _, heard, stopped = self.track("research_success", ResearchLimits(max_searches=1, max_fetches=1))
        self.assertIsNone(stopped)
        self.assertEqual(rs.cap_message(SEARCH, 8, 7), "Jarvis stopped the web research: it wanted an eighth web search "
                                                       "(at most 7: [research] max_searches); nothing was proposed")

    def test_a_local_fetch_stops_the_run(self) -> None:
        tracker, heard, stopped = self.track("research_local_fetch")
        self.assertEqual((stopped.failure.kind, stopped.failure.detail), (st.GUARD, "fetch_url"))
        self.assertIn("a local or private address", stopped.failure.message)
        refused = heard[-1][1]
        self.assertEqual((refused.url, refused.refused), ("http://192.168.1.1/admin", "a local or private address"))
        for url in ("http://localhost/x", "http://127.0.0.1/", "http://10.1.2.3/", "http://192.168.1.1:8080/",
                    "http://[::1]/", "http://router.local/", "http://intranet/", "http://printer.lan/",
                    "https://a.internal/", "https://user:pw@example.org/", "https://example.org:8443/",
                    "file:///C:/x", "javascript:alert(1)", "data:text/html,x", "ftp://example.org/", ""):
            with self.subTest(url=url):
                tracker = WebTracker(ResearchLimits())
                with self.assertRaises(st.RunStop) as caught:
                    tracker.check(fetch_call("toolu_x", url))
                self.assertEqual(caught.exception.failure.detail, "fetch_url")
                self.assertTrue(tracker.log().calls[0].refused)

    def test_tools_that_should_not_exist(self) -> None:
        _, _, stopped = self.track("research_bad_tool")
        self.assertIsInstance(stopped, st.GuardTrip)
        self.assertEqual(stopped.reason, "tool_use")
        for block in ({"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "dir"}},
                      {"type": "tool_use", "id": "t", "name": "mcp__gmail__send_email", "input": {}},
                      {"type": "tool_use", "id": "t", "name": "Task", "input": {}},
                      {"type": "tool_use", "id": "t", "name": "websearch", "input": {}},
                      {"type": "tool_use", "id": "t", "input": {}},
                      {"type": "server_tool_use", "id": "t", "name": "code_execution", "input": {}},
                      {"type": "mcp_tool_use", "id": "t", "name": "web_search", "input": {}}):
            with self.subTest(block=block.get("name")), self.assertRaises(st.GuardTrip):
                WebTracker(ResearchLimits()).check(assistant(block))
        # Page reads off: a WebFetch call is a tool that should not exist.
        with self.assertRaises(st.GuardTrip):
            WebTracker(ResearchLimits(max_fetches=0)).check(fetch_call("toolu_f1", VISIT_URL))
        # StructuredOutput and text are fine; a user event never trips.
        tracker = WebTracker(ResearchLimits())
        tracker.check(assistant({"type": "text", "text": "Done."},
                                {"type": "tool_use", "id": "a", "name": "StructuredOutput", "input": {}}))
        tracker.check(tool_result("a", "Structured output provided successfully"))
        self.assertEqual(tracker.log().calls, ())

    def test_nested_calls_count_like_top_level_ones(self) -> None:
        tracker, heard, stopped = self.track("research_nested")
        self.assertIsNone(stopped)
        self.assertEqual([(what, item.kind) for what, item in heard],
                         [("call", SEARCH), ("result", SEARCH), ("call", FETCH), ("result", FETCH)])
        nested_bad = assistant({"type": "tool_use", "id": "t", "name": "Read", "input": {}}, parent="toolu_task1")
        with self.assertRaises(st.GuardTrip):
            WebTracker(ResearchLimits()).check(nested_bad)

    def test_the_same_call_twice_counts_once_and_unknown_results_are_ignored(self) -> None:
        tracker = WebTracker(ResearchLimits(max_searches=1))
        tracker.check(search_call("toolu_s1", "q"))
        tracker.check(search_call("toolu_s1", "q"))   # the same tool id again: not a second search
        tracker.check(search_result("toolu_unknown", RESEARCH_HITS))
        tracker.check(search_result("toolu_s1", RESEARCH_HITS))
        tracker.check(search_result("toolu_s1", RESEARCH_HITS[:1]))   # a second result for it: ignored
        log = tracker.log()
        self.assertEqual((log.searches, len(log.calls), len(log.results), log.hits), (1, 1, 1, 3))

    def test_tool_use_result_only_with_one_result_in_the_event(self) -> None:
        tracker = WebTracker(ResearchLimits())
        tracker.check(assistant(fetch_block("toolu_f1", VISIT_URL), fetch_block("toolu_f2", HOURS_URL)))
        both = tool_result("toolu_f1", "page one", structured={"code": 500, "bytes": 1, "url": VISIT_URL})
        both["message"]["content"].append({"type": "tool_result", "tool_use_id": "toolu_f2", "content": "page two"})
        tracker.check(both)
        first, second = tracker.log().results
        self.assertEqual((first.status, first.code, first.text), ("ok", None, "page one"))   # 500 not applied to both
        self.assertEqual((second.status, second.text), ("ok", "page two"))

    def test_server_side_tools(self) -> None:
        tracker = WebTracker(ResearchLimits())
        tracker.check(assistant({"type": "server_tool_use", "id": "srv_1", "name": "web_search",
                                 "input": {"query": "museum"}},
                                {"type": "web_search_tool_result", "tool_use_id": "srv_1",
                                 "content": [{"type": "web_search_result", "title": "Visit", "url": VISIT_URL}]},
                                {"type": "server_tool_use", "id": "srv_2", "name": "web_fetch",
                                 "input": {"url": VISIT_URL}},
                                {"type": "web_fetch_tool_result", "tool_use_id": "srv_2",
                                 "content": {"type": "web_fetch_result", "url": VISIT_URL,
                                             "content": {"type": "document",
                                                         "source": {"type": "text", "data": "Opens at 10."}}}},
                                {"type": "server_tool_use", "id": "srv_3", "name": "web_search",
                                 "input": {"query": "x"}},
                                {"type": "web_search_tool_result", "tool_use_id": "srv_3",
                                 "content": {"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"}}))
        log = tracker.log()
        self.assertTrue(all(call.server for call in log.calls))
        self.assertEqual([(result.kind, result.status) for result in log.results],
                         [(SEARCH, "ok"), (FETCH, "ok"), (SEARCH, "error")])
        self.assertEqual(log.results[0].hits, (("Visit", VISIT_URL),))
        self.assertEqual(log.results[1].text, "Opens at 10.")
        self.assertEqual(log.results[2].message, "max_uses_exceeded")
        self.assertEqual(log.searches, 2)

    def test_malformed_events_never_raise(self) -> None:
        tracker = WebTracker(ResearchLimits())
        for event in ({"type": "assistant"}, {"type": "assistant", "message": "x"},
                      {"type": "assistant", "message": {"content": "x"}},
                      {"type": "assistant", "message": {"content": [None, 3, "x", {"type": "text"}]}},
                      {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
                      {"type": "user", "tool_use_result": [1]}, {"type": "result", "permission_denials": "x"},
                      {"type": "system", "subtype": "status"}, {"type": "rate_limit_event"}):
            tracker.check(event)
        tracker.check(search_call("toolu_s1", None))   # type: ignore[arg-type]
        tracker.check(tool_result("toolu_s1", 42, structured={"results": "x", "durationSeconds": "slow"}))
        result = tracker.log().results[0]
        self.assertEqual((result.status, result.took_ms), ("empty", None))
        self.assertEqual(tracker.log().calls[0].query, "")

    def test_found_by(self) -> None:
        tracker = WebTracker(ResearchLimits(), "what does example.net/hours say about the museum")
        tracker.check(search_call("toolu_s1", "q"))
        tracker.check(search_result("toolu_s1", RESEARCH_HITS[:1]))
        tracker.check(fetch_call("toolu_f1", VISIT_URL + "/"))
        tracker.check(fetch_call("toolu_f2", "https://www.example.net/hours"))
        tracker.check(fetch_call("toolu_f3", "https://collect.example.com/c?d=museum"))
        self.assertEqual([call.found_by for call in tracker.log().calls[1:]],
                         [rs.FOUND_SEARCH, rs.FOUND_OWNER, rs.FOUND_OTHER])
        _, heard, stopped = self.track("research_followed_link")
        self.assertIsNone(stopped)   # a public page a link led to: shown as such, not refused
        self.assertEqual(heard[4][1].found_by, rs.FOUND_OTHER)

    def test_a_broken_callback_changes_nothing(self) -> None:
        def broken(_value: object) -> None:
            raise RuntimeError("listener")

        quiet = WebTracker(ResearchLimits())
        noisy = WebTracker(ResearchLimits(), on_call=broken, on_result=broken)
        for event in events("research_success"):
            quiet.check(event)
            noisy.check(event)
        self.assertEqual(noisy.log(), quiet.log())

    def test_permission_denials_of_the_result_count(self) -> None:
        tracker = WebTracker(ResearchLimits())
        tracker.check(research_result(None, permission_denials=[{"tool_name": "WebSearch"}, {"tool_name": "WebFetch"}]))
        self.assertEqual(tracker.log().denied, 2)


class ReadResultTests(unittest.TestCase):
    INIT = check_research_init(research_init(), ResearchLimits())

    def test_success(self) -> None:
        answer = read_research_result(research_result(RESEARCH_ANSWER), self.INIT)
        self.assertIsInstance(answer, ResearchAnswer)
        self.assertEqual(answer.answer, RESEARCH_ANSWER["answer"])
        self.assertEqual(answer.sources, (("Visit - Example Museum", VISIT_URL), ("Hours and tickets", HOURS_URL)))
        self.assertEqual(answer.suggestions, tuple(RESEARCH_ANSWER["suggestions"]))
        self.assertNotIn("Museum", repr(answer))
        flat = read_research_result(research_result({"answer": "One.\nTwo\u2028three", "sources": [],
                                                     "suggestions": []}), self.INIT)
        self.assertEqual(flat.answer, "One. Two three")

    def test_shapes(self) -> None:
        good_source = {"title": "t", "url": VISIT_URL}
        bad = [{"answer": "a", "sources": []}, {"answer": "a", "suggestions": []}, {"sources": [], "suggestions": []},
               {"answer": 3, "sources": [], "suggestions": []}, {"answer": "a", "sources": {}, "suggestions": []},
               {"answer": "x" * 501, "sources": [], "suggestions": []},
               {"answer": "a", "sources": [good_source] * 7, "suggestions": []},
               {"answer": "a", "sources": ["https://example.org"], "suggestions": []},
               {"answer": "a", "sources": [{"title": "t"}], "suggestions": []},
               {"answer": "a", "sources": [{"title": 1, "url": VISIT_URL}], "suggestions": []},
               {"answer": "a", "sources": [{"title": "t" * 201, "url": VISIT_URL}], "suggestions": []},
               {"answer": "a", "sources": [{"title": "t", "url": "u" * 2001}], "suggestions": []},
               {"answer": "a", "sources": [], "suggestions": ["Todo: x"] * 4},
               {"answer": "a", "sources": [], "suggestions": [3]},
               {"answer": "a", "sources": [], "suggestions": ["x" * 1001]}, ["not", "a", "dict"], "text"]
        for data in bad:
            with self.subTest(data=str(data)[:50]):
                found = read_research_result(research_result(data), self.INIT)
                self.assertEqual((found.kind, found.detail), (st.GARBLED, "shape"))
        ok = read_research_result(research_result({"answer": "", "sources": [good_source] * 6,
                                                   "suggestions": ["a", "b", "c"]}), self.INIT)
        self.assertEqual((ok.answer, len(ok.sources), len(ok.suggestions)), ("", 6, 3))

    def test_subtypes_and_guards(self) -> None:
        self.assertEqual(read_research_result(research_result(None), self.INIT).detail, "no_structured_output")
        text_only = research_result(None, result=json.dumps(RESEARCH_ANSWER))
        self.assertEqual(read_research_result(text_only, self.INIT).kind, st.GARBLED)   # never a text fallback
        self.assertEqual(read_research_result(research_result(RESEARCH_ANSWER), None).detail, "no_init")
        for changes, kind in (({"subtype": "error_max_turns", "is_error": True}, st.MAX_TURNS),
                              ({"subtype": "error_max_structured_output_retries", "is_error": True}, st.SCHEMA_RETRIES),
                              ({"subtype": "error_during_execution", "is_error": True, "result": "odd"}, st.ERROR),
                              ({"is_error": True, "result": "Claude AI usage limit reached|1791900000"}, st.LIMIT),
                              ({"is_error": True, "result": "Invalid API key - please run /login"}, st.NOT_SIGNED_IN)):
            with self.subTest(kind=kind):
                self.assertEqual(read_research_result(research_result(RESEARCH_ANSWER, **changes), self.INIT).kind,
                                 kind)


class RunResearchTests(unittest.TestCase):
    """One research run end to end with a fake runner."""

    def setUp(self) -> None:
        self.clock = FakeClock()

    def run_it(self, run, *, limits: ResearchLimits = ResearchLimits(), progress=None, capture: bool = False,
               cancel: threading.Event | None = None, timeout_s: float = 120.0) -> st.RunResult:
        self.runner = FakeRunner(run, clock=self.clock)
        return run_research(self.runner, ["claude.exe", "-p"], "the stdin", env={"PATH": "x"}, cwd=Path("."),
                            timeout_s=timeout_s, limits=limits, question=QUESTION, cancel=cancel, clock=self.clock,
                            now=lambda: NOW, progress=progress, capture=capture)

    def process(self) -> FakeProcess:
        return self.runner.processes[-1]

    def test_success_and_the_progress_order(self) -> None:
        heard: list[tuple[str, object]] = []
        result = self.run_it(stream("research_success"), progress=lambda what, value: heard.append((what, value)))
        self.assertIsNone(result.failure)
        self.assertEqual(result.outcome, "ok")
        self.assertIsNone(result.plan)
        self.assertIsInstance(result.answer, ResearchAnswer)
        self.assertEqual(len(result.answer.sources), 2)
        self.assertEqual((result.web.searches, result.web.fetches), (1, 1))
        self.assertEqual([what for what, _value in heard],
                         ["started", "init", "web_call", "turn", "web_result", "web_call", "turn", "web_result", "turn",
                          "result"])
        self.assertEqual([value for what, value in heard if what == "turn"], [1, 2, 3])
        self.assertEqual(heard[2][1].query, RESEARCH_QUERY)
        self.assertEqual(heard[5][1].url, VISIT_URL)
        self.assertEqual((result.stats.turns, result.stats.input_tokens, result.stats.duration_ms), (4, 21000, 18400))
        self.assertEqual(self.runner.started[0].stdin, "the stdin")
        self.assertFalse(self.process().killed)
        self.assertEqual(result.reply, "")
        for secret in ("Museum", "example", RESEARCH_QUERY):
            self.assertNotIn(secret, repr(result))

    def test_capture_keeps_the_raw_answer(self) -> None:
        result = self.run_it(stream("research_success"), capture=True)
        self.assertEqual(json.loads(result.reply), RESEARCH_ANSWER)

    def test_a_raising_progress_changes_nothing(self) -> None:
        def broken(_what: str, _value: object) -> None:
            raise RuntimeError("listener")

        for name in ("research_success", "research_too_many_searches", "research_local_fetch", "research_init_mcp",
                     "research_garbled", "research_redirect"):
            with self.subTest(name=name):
                self.clock = FakeClock()
                plain = self.run_it(stream(name))
                self.clock = FakeClock()
                noisy = self.run_it(stream(name), progress=broken)
                self.assertEqual(noisy, plain)
                self.assertEqual(noisy.web, plain.web)

    def test_stops_kill_the_process(self) -> None:
        for name, kind, detail in (("research_too_many_searches", st.RESEARCH_CAP, "searches"),
                                   ("research_too_many_fetches", st.RESEARCH_CAP, "fetches"),
                                   ("research_local_fetch", st.GUARD, "fetch_url"),
                                   ("research_bad_tool", st.GUARD, "tool_use"),
                                   ("research_init_bash", st.GUARD, "tools"),
                                   ("research_init_mcp", st.GUARD, "mcp_servers"),
                                   ("research_api_key", st.GUARD, "api_key_source"),
                                   ("research_init_no_search", st.GUARD, "no_web_search"),
                                   ("research_init_mode", st.GUARD, "permission_mode"),
                                   ("research_overage", st.LIMIT, st.OVERAGE)):
            with self.subTest(name=name):
                heard: list[tuple[str, object]] = []
                result = self.run_it(stream(name), progress=lambda what, value: heard.append((what, value)))
                self.assertEqual((result.failure.kind, result.failure.detail), (kind, detail))
                self.assertTrue(self.process().killed)
                self.assertTrue(self.process().closed)
                self.assertIsNone(result.answer)
                self.assertEqual(heard[-1], ("stopped", result.failure))
                self.assertIsNotNone(result.web)
        overage = self.run_it(stream("research_overage"))
        self.assertTrue(overage.limit.overage)
        self.assertEqual(overage.failure.message, st.OVERAGE_MESSAGE)

    def test_research_wording(self) -> None:
        guard = self.run_it(stream("research_init_mcp")).failure
        self.assertEqual(guard.message, "Claude Code started the web research with something Jarvis does not allow, "
                                        "so it was stopped; nothing was proposed")
        self.assertEqual(self.run_it(stream("research_max_turns")).failure.message,
                         "The web research ran out of turns; nothing was proposed - try a narrower question")
        self.assertEqual(self.run_it(stream("research_garbled")).failure.message,
                         "The web research's answer could not be read; nothing was proposed")
        local = self.run_it(stream("research_local_fetch")).failure
        self.assertEqual(local.message, rs.FETCH_URL_MESSAGE)   # the specific stop keeps its words
        hanging = FakeProcess(stream("research_success")[:2], clock=self.clock, hang=True)
        timeout = self.run_it(hanging, timeout_s=60)
        self.assertEqual(timeout.failure.message, "The web research took too long (60 s: [research] timeout_seconds); "
                                                  "nothing was proposed")
        self.assertTrue(hanging.killed)
        errored = self.run_it(FakeProcess(stream("research_success")[:2], clock=self.clock, exit_code=3))
        self.assertEqual((errored.failure.kind, errored.failure.message),
                         (st.ERROR, "Claude Code failed during the web research; nothing was proposed"))
        retries = research_stream(research_result(None, subtype="error_max_structured_output_retries", is_error=True))
        self.assertEqual(self.run_it(retries).failure.message,
                         "The web research's answer could not be read; nothing was proposed")
        self.assertEqual(rs.research_failure(st.failure(st.CANCELLED, "cancel"), 120).message,
                         st.MESSAGES[st.CANCELLED])
        self.assertEqual(rs.research_failure(st.failure(st.NOT_SIGNED_IN), 120).message, st.MESSAGES[st.NOT_SIGNED_IN])

    def test_cancel_and_the_web_steps_so_far(self) -> None:
        cancel = threading.Event()
        lines = stream("research_success")[:4]   # init, search call, its result, the fetch call
        process = FakeProcess(lines, clock=self.clock, hang=True, cancel_after=6, cancel=cancel)
        heard: list[tuple[str, object]] = []
        result = self.run_it(process, cancel=cancel, progress=lambda what, value: heard.append((what, value)))
        self.assertEqual(result.failure.kind, st.CANCELLED)
        self.assertTrue(process.killed)
        self.assertEqual([what for what, _ in heard if what.startswith("web_")], ["web_call", "web_result", "web_call"])
        self.assertEqual((result.web.searches, result.web.fetches, len(result.web.results)), (1, 1, 1))
        self.assertLess(self.clock.t - 1000.0, 2.0)

    def test_every_ok_fixture_runs(self) -> None:
        for name in ("research_parallel", "research_text_results", "research_unread", "research_denied",
                     "research_redirect", "research_fetch_error", "research_injection", "research_injection_more",
                     "research_nested", "research_followed_link"):
            with self.subTest(name=name):
                result = self.run_it(stream(name))
                self.assertIsNone(result.failure, result.failure)
                self.assertIsInstance(result.answer, ResearchAnswer)

    def test_garbage_lines_are_skipped_never_logged(self) -> None:
        with self.assertLogs("briefing_reader.ask.stream", level="INFO") as logs:
            result = self.run_it(stream("research_garbage"))
        self.assertIsNone(result.failure)
        text = "\n".join(logs.output)
        self.assertIn("skipped 3 unreadable output line(s)", text)
        self.assertNotIn("Debugger", text)

    def test_nothing_is_logged_from_the_web(self) -> None:
        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            logging.getLogger("briefing_reader").debug("marker")
            for name in ("research_success", "research_injection", "research_local_fetch", "research_unread"):
                self.run_it(stream(name))
        text = "\n".join(logs.output)
        for secret in ("example", "Museum", "museum", "192.168", "Ignore previous", "toolu_", "admin"):
            self.assertNotIn(secret, text)

    def test_builders_shape(self) -> None:
        lines = research_stream(*success_events())
        self.assertEqual(lines, stream("research_success"))
        self.assertEqual(json.loads(lines[0])["cwd"], "C:/Users/example/AppData/Local/briefing-reader/research")
        later = NOW + timedelta(days=3)
        self.assertGreater(datetime(2026, 10, 10, 10, 0, tzinfo=NOW.tzinfo), NOW)
        self.assertLess(datetime(2026, 10, 10, 10, 0, tzinfo=NOW.tzinfo), later + timedelta(days=1))


class RecordingSnapshots:
    """snapshot_engine.PageSnapshotter's surface for WebSteps (no Qt, no page): records each call
    and, like the real one, shows a PENDING picture or an unavailable one by the snapshot rules.
    ``fail``: every call raises after it is recorded."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail = fail

    def request(self, step: live.LiveStep, url: str, *, task_id: int) -> None:
        view = step.view()
        self.calls.append(("request", view.kind if view is not None else "", url, task_id))
        if self.fail:
            raise RuntimeError("a broken snapshotter")
        reason = snapshots.snapshot_url_problem(url)
        step.picture(pictures.page_unavailable(url, reason) if reason else pictures.page_pending(url))

    def end_run(self, task_id: int) -> None:
        self.calls.append(("end_run", task_id))
        if self.fail:
            raise RuntimeError("a broken snapshotter")


class WebStepsPictureTests(unittest.TestCase):
    """research_live.WebSteps and page pictures: only a page the run READ (WebFetch came back OK) is
    handed to the snapshotter, with its web.fetch step and the run's LIVE task id; a search, a page
    read that failed, was denied or redirected, and a call Jarvis stopped never are. finished()
    ends the run's pictures. The snapshotter decides what may be opened; a broken one changes
    nothing; without one the steps are exactly as before."""

    PAGE = "https://www.example.org/visit"

    def setUp(self) -> None:
        self.stream = live.LiveStream()
        self.task = self.stream.task(live.TASK_WEB, "web research")
        self.snapper = RecordingSnapshots()

    def web(self, snapper: RecordingSnapshots | None = None, task: live.LiveTask | None = None) -> research_live.WebSteps:
        return research_live.WebSteps(task or self.task, ResearchLimits(), snapshots=snapper)

    @staticmethod
    def fetch(web: research_live.WebSteps, tool_id: str, url: str, status: str = rs.RESULT_OK, *,
              code: int | None = 200, number: int = 1, **call: Any) -> None:
        web.call(rs.WebCall(tool_id, FETCH, number, url=url, found_by=rs.FOUND_SEARCH, **call))
        if call.get("over") or call.get("refused"):
            return
        web.result(rs.WebResult(tool_id, FETCH, status, text="The museum opens at 10 AM.", code=code,
                                code_text="OK" if code == 200 else "Moved", size=1234, took_ms=800, final_url=url,
                                redirect_to="https://www.example.net/new" if status == rs.RESULT_REDIRECT else "",
                                message="no" if status in (rs.RESULT_ERROR, rs.RESULT_DENIED) else ""))

    def view(self, tool_id: str) -> live.StepView:
        return self.task.find(live.WEB_FETCH, key=f"web:{tool_id}").view()

    def test_only_a_page_read_that_came_back_ok_is_pictured(self) -> None:
        web = self.web(self.snapper)
        web.call(rs.WebCall("toolu_s1", SEARCH, 1, query="museum hours"))
        web.result(rs.WebResult("toolu_s1", SEARCH, rs.RESULT_OK, hits=(("Visit", self.PAGE),)))
        self.fetch(web, "toolu_f1", self.PAGE)
        self.fetch(web, "toolu_f2", "https://www.example.org/missing", rs.RESULT_ERROR, code=404, number=2)
        self.fetch(web, "toolu_f3", "https://www.example.org/denied", rs.RESULT_DENIED, code=None, number=3)
        self.fetch(web, "toolu_f4", "https://www.example.org/old", rs.RESULT_REDIRECT, code=301, number=4)
        self.fetch(web, "toolu_f5", "https://www.example.org/unread", rs.RESULT_UNREAD, code=None, number=5)
        self.fetch(web, "toolu_f6", "https://www.example.org/over", number=6, over=True)
        self.fetch(web, "toolu_f7", "https://192.168.1.1/admin", number=7, refused="a local or private address")
        self.assertEqual(self.snapper.calls, [("request", live.WEB_FETCH, self.PAGE, self.task.id)])
        read = self.view("toolu_f1")
        self.assertEqual((read.status, read.summary), (live.STATUS_OK, "200 - 1,234 bytes"))
        self.assertEqual((read.picture.kind, read.picture.state), (pictures.KIND_PAGE, live.PICTURE_PENDING))
        self.assertEqual(read.picture.note, snapshots.NOTE_OPENING)
        self.assertTrue(read.open_hint)
        for tool_id in ("toolu_f2", "toolu_f3", "toolu_f4", "toolu_f5", "toolu_f6", "toolu_f7"):
            self.assertIsNone(self.view(tool_id).picture, tool_id)
        search = self.task.find(live.WEB_SEARCH, key="web:toolu_s1").view()
        self.assertEqual(search.picture.kind, pictures.KIND_SEARCH)   # drawn from its results; never opened
        web.finished()
        self.assertEqual(self.snapper.calls[-1], ("end_run", self.task.id))

    def test_the_snapshotter_decides_what_may_be_opened(self) -> None:
        web = self.web(self.snapper)
        self.fetch(web, "toolu_f1", "http://www.example.org/plain")              # WebFetch may read http
        self.fetch(web, "toolu_f2", "https://www.google.com/search?q=museum", number=2)
        self.assertEqual([call[2] for call in self.snapper.calls],
                         ["http://www.example.org/plain", "https://www.google.com/search?q=museum"])
        plain, search = self.view("toolu_f1").picture, self.view("toolu_f2").picture
        self.assertEqual(plain.state, live.PICTURE_UNAVAILABLE)
        self.assertTrue(plain.note.startswith(pictures.UNAVAILABLE_PREFIX + snapshots.R_NOT_PUBLIC_HTTPS), plain.note)
        self.assertEqual(search.note, pictures.UNAVAILABLE_PREFIX + snapshots.R_SEARCH_PAGE)
        self.assertEqual(self.view("toolu_f1").status, live.STATUS_OK)        # the read itself is unchanged

    def test_without_a_snapshotter_or_with_a_broken_one_the_steps_are_the_same(self) -> None:
        def steps(snapper: RecordingSnapshots | None) -> tuple:
            stream = live.LiveStream()
            task = stream.task(live.TASK_WEB, "web research")
            web = self.web(snapper, task)
            self.fetch(web, "toolu_f1", self.PAGE)
            web.finished()
            return tuple((step.kind, step.status, step.summary, step.fields, step.blocks, step.picture)
                         for step in stream.snapshot()[0].steps)

        broken = RecordingSnapshots(fail=True)
        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            logging.getLogger("briefing_reader").debug("marker")
            self.assertEqual(steps(broken), steps(None))
        self.assertEqual([call[0] for call in broken.calls], ["request", "end_run"])
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)                                   # "Live view: ... failed (RuntimeError)"
        self.assertNotIn("example", text)
        self.web().finished()                                                 # no snapshotter: nothing to end

    def test_a_disabled_live_view_never_asks(self) -> None:
        disabled = live.LiveStream(enabled=False).task(live.TASK_WEB, "web research")
        web = self.web(self.snapper, disabled)
        self.fetch(web, "toolu_f1", self.PAGE)
        self.assertEqual(self.snapper.calls, [])


class BudgetSnapshots(RecordingSnapshots):
    """RecordingSnapshots with the real per-research budget (snapshots.RunBudget): past ``cap`` or a
    page already asked for, the picture is unavailable, as the real snapshotter shows it."""

    def __init__(self, cap: int = snapshots.MAX_PER_RUN) -> None:
        super().__init__()
        self.cap = cap
        self.budget = snapshots.RunBudget()

    def request(self, step: live.LiveStep, url: str, *, task_id: int) -> None:
        view = step.view()
        self.calls.append(("request", view.kind if view is not None else "", url, task_id))
        reason = snapshots.snapshot_url_problem(url) or self.budget.admit(task_id, url, self.cap)
        step.picture(pictures.page_unavailable(url, reason) if reason else pictures.page_pending(url))


def report_of(*sources: tuple[int, str, str, bool]) -> ResearchReport:
    """A checked answer's report with these shown sources: (number, title, url, checked)."""
    return ResearchReport(answer="It opens at 10 AM [1].", sources=tuple(
        WebSource(number, title, url, link_host(url), checked, f"card{number}")
        for number, title, url, checked in sources))


class WebStepsSearchPictureTests(unittest.TestCase):
    """A web search that came back is drawn on its web.search step as a compact results page
    (pictures.search_picture): the exact query, the results in order, the sites it was kept to; a
    search with no results or results Jarvis could not list says so; a failed or refused search
    gets none. [live] pictures decides; nothing is ever opened for it (no snapshotter call)."""

    def setUp(self) -> None:
        self.stream = live.LiveStream()
        self.task = self.stream.task(live.TASK_WEB, "web research")
        self.snapper = RecordingSnapshots()
        self.web = research_live.WebSteps(self.task, ResearchLimits(), snapshots=self.snapper)

    def search(self, tool_id: str, status: str, hits: tuple = (), *, number: int = 1, **call: Any) -> live.StepView:
        self.web.call(rs.WebCall(tool_id, SEARCH, number, query="Example Museum opening hours", **call))
        self.web.result(rs.WebResult(tool_id, SEARCH, status, hits=hits, text="Links: []" if hits else "",
                                     message="refused" if status in (rs.RESULT_DENIED, rs.RESULT_ERROR) else ""))
        return self.task.find(live.WEB_SEARCH, key=f"web:{tool_id}").view()

    def test_a_search_is_drawn_from_what_it_returned(self) -> None:
        hits = tuple((f"Result {n}", f"https://www{n}.example.org/page") for n in range(1, 13))
        view = self.search("toolu_s1", rs.RESULT_OK, hits, allowed_domains=("example.org",))
        picture = view.picture
        self.assertEqual((picture.kind, picture.state, picture.caption),
                         (pictures.KIND_SEARCH, live.PICTURE_READY, pictures.CAPTION_SEARCH))
        search = picture.search
        self.assertEqual(search.query, "Example Museum opening hours")
        self.assertEqual([(hit.title, hit.url, hit.host) for hit in search.hits[:2]],
                         [("Result 1", "https://www1.example.org/page", "www1.example.org"),
                          ("Result 2", "https://www2.example.org/page", "www2.example.org")])
        self.assertEqual((len(search.hits), search.more, search.only), (pictures.SEARCH_HITS_SHOWN, 2, "example.org"))
        self.assertTrue(view.open_hint)                                   # a step with a picture starts open
        self.assertEqual(self.snapper.calls, [])                           # nothing is opened for it
        # The picture lists the results: not once more as items, only as text in a closed block
        # (to copy or for a screen reader), before what WebSearch returned.
        self.assertEqual(view.items, ())
        self.assertEqual([block.label for block in view.blocks],
                         [research_live.RESULTS_BLOCK, research_live.SEARCH_BLOCK])
        listed = view.blocks[0]
        self.assertEqual(listed.text.splitlines(), [f"{title} - {url}" for title, url in hits])
        self.assertEqual((listed.start_open, listed.untrusted), (False, True))
        self.assertEqual({field.label: field.value for field in view.fields}["Results"], "12 pages listed")

    def test_nothing_found_unread_failed_and_refused(self) -> None:
        empty = self.search("toolu_s1", rs.RESULT_EMPTY).picture
        self.assertEqual((empty.search.hits, empty.search.unread), ((), False))
        unread = self.search("toolu_s2", rs.RESULT_UNREAD, number=2).picture
        self.assertTrue(unread.search.unread)
        for number, status in ((3, rs.RESULT_ERROR), (4, rs.RESULT_DENIED)):
            with self.subTest(status=status):
                self.assertIsNone(self.search(f"toolu_s{number}", status, number=number).picture)
        self.web.call(rs.WebCall("toolu_s5", SEARCH, 5, query="museum", over=True))   # stopped by Jarvis
        self.assertIsNone(self.task.find(live.WEB_SEARCH, key="web:toolu_s5").view().picture)

    def test_pictures_off_draws_none_and_text_off_keeps_no_snippet(self) -> None:
        off = live.LiveStream(pictures=False).task(live.TASK_WEB, "web research")
        web = research_live.WebSteps(off, ResearchLimits())
        web.call(rs.WebCall("toolu_s1", SEARCH, 1, query="museum"))
        web.result(rs.WebResult("toolu_s1", SEARCH, rs.RESULT_OK, hits=(("Visit", VISIT_URL),)))
        view = off.find(live.WEB_SEARCH, key="web:toolu_s1").view()
        self.assertIsNone(view.picture)
        self.assertEqual(view.status, live.STATUS_OK)
        self.assertEqual([item.text for item in view.items], [f"Visit - {VISIT_URL}"])   # no picture: items
        self.assertEqual(view.blocks, ())
        no_text = live.LiveStream(keep_text=False).task(live.TASK_WEB, "web research")
        web = research_live.WebSteps(no_text, ResearchLimits())
        web.call(rs.WebCall("toolu_s1", SEARCH, 1, query="museum"))
        web.result(rs.WebResult("toolu_s1", SEARCH, rs.RESULT_OK, hits=(("Visit", VISIT_URL),)))
        view = no_text.find(live.WEB_SEARCH, key="web:toolu_s1").view()
        self.assertEqual(view.picture.kind, pictures.KIND_SEARCH)
        self.assertEqual([item.text for item in view.items], [f"Visit - {VISIT_URL}"])   # a block keeps no text
        texts_off = live.LiveStream(keep_text=False).task(live.TASK_WEB, "web research")
        texts_off.step(live.WEB_SEARCH, "Web search 1").picture(
            pictures.search_picture("museum", [("Visit", VISIT_URL, "Open daily from 10 AM.")]))
        kept = texts_off.find(live.WEB_SEARCH).view().picture
        self.assertEqual((kept.search.hits[0].title, kept.search.hits[0].snippet), ("Visit", ""))

    def test_a_broken_picture_never_changes_the_step(self) -> None:
        # A picture that fails to draw leaves the step exactly as with pictures off (its results
        # as items); a drawn one only moves them into the closed block.
        def views(broken: bool, drawn: bool = True) -> tuple:
            stream = live.LiveStream(pictures=drawn)
            task = stream.task(live.TASK_WEB, "web research")
            web = research_live.WebSteps(task, ResearchLimits())
            web.call(rs.WebCall("toolu_s1", SEARCH, 1, query="museum"))
            if broken:
                with unittest.mock.patch.object(research_live, "search_picture", side_effect=RuntimeError("x")):
                    web.result(rs.WebResult("toolu_s1", SEARCH, rs.RESULT_OK, hits=(("Visit", VISIT_URL),)))
            else:
                web.result(rs.WebResult("toolu_s1", SEARCH, rs.RESULT_OK, hits=(("Visit", VISIT_URL),)))
            return tuple((step.kind, step.status, step.summary, step.fields, step.items, step.blocks, step.picture)
                         for step in stream.snapshot()[0].steps)

        self.assertEqual(views(True), views(False, drawn=False))
        ((_kind, status, summary, fields, items, blocks, picture),) = views(False)
        (broken,) = views(True)
        self.assertEqual((status, summary, fields), broken[1:4])
        self.assertEqual((items, len(blocks), picture.kind), ((), 1, pictures.KIND_SEARCH))


class CitedSourcePictureTests(unittest.TestCase):
    """WebSteps.cited: after the answer's check, each source it SHOWS (never a left-out one) gets a
    picture - the page read's when the run read that page (no second picture), else a web.source
    step of its own ("Source [2]: example.net") with the page's picture, asked after the reads and in
    citation order within the same cap per research; the check (research.validate) names each
    picture's step and links to it ("picture:<step id>"). Nothing without a snapshotter, the LIVE
    view or [live] text."""

    def setUp(self) -> None:
        self.stream = live.LiveStream()
        self.task = self.stream.task(live.TASK_WEB, "web research")

    def read(self, web: research_live.WebSteps, tool_id: str, url: str, number: int,
             final_url: str = "") -> None:
        web.call(rs.WebCall(tool_id, FETCH, number, url=url, found_by=rs.FOUND_SEARCH))
        web.result(rs.WebResult(tool_id, FETCH, rs.RESULT_OK, text="Open 10 AM.", code=200, code_text="OK",
                                size=10, took_ms=5, final_url=final_url or url))

    def steps(self, kind: str) -> list[live.StepView]:
        return [step for step in self.stream.snapshot()[0].steps if step.kind == kind]

    def test_reads_first_then_the_sources_in_citation_order_within_the_cap(self) -> None:
        snapper = BudgetSnapshots()
        web = research_live.WebSteps(self.task, ResearchLimits(), snapshots=snapper)
        self.read(web, "toolu_f1", VISIT_URL, 1)
        self.read(web, "toolu_f2", "https://www.example.org/old", 2, final_url="https://www.example.org/new")
        check = self.task.step(live.RESEARCH_VALIDATE, research_live.VALIDATE_TITLE)
        report = report_of((1, "Hours", HOURS_URL, True), (2, "Visit", VISIT_URL, True),
                           (3, "New", "https://www.example.org/new", True),       # where read 2 ended
                           (4, "Tickets", "https://www.example.com/tickets", False),   # Jarvis could not check it
                           (5, "Shop", "https://shop.example.com/", True),
                           (6, "Cafe", "https://cafe.example.com/", True))
        web.cited(report, check)
        self.assertEqual([call[1:3] for call in snapper.calls],
                         [(live.WEB_FETCH, VISIT_URL), (live.WEB_FETCH, "https://www.example.org/old"),
                          (live.WEB_SOURCE, HOURS_URL), (live.WEB_SOURCE, "https://shop.example.com/"),
                          (live.WEB_SOURCE, "https://cafe.example.com/")])   # never the unchecked one
        sources = self.steps(live.WEB_SOURCE)
        self.assertEqual([step.title for step in sources],
                         ["Source [1]: museum.example.net", "Source [4]: www.example.com",
                          "Source [5]: shop.example.com", "Source [6]: cafe.example.com"])
        self.assertEqual([step.picture.state for step in sources],
                         [live.PICTURE_PENDING, live.PICTURE_UNAVAILABLE, live.PICTURE_PENDING,
                          live.PICTURE_UNAVAILABLE])                                # the 6th: past 4
        self.assertEqual(sources[3].picture.note, pictures.UNAVAILABLE_PREFIX + snapshots.R_OVER_CAP)
        self.assertTrue(all(step.status == live.STATUS_OK and step.open_hint for step in sources))
        fields = {field.label: field.value for field in sources[1].fields}
        self.assertEqual(fields["Check"], research_live.UNCHECKED_WARNING)           # unchecked: said so ...
        self.assertEqual(sources[1].picture.note, pictures.UNAVAILABLE_PREFIX + research_live.UNCHECKED_NO_PICTURE)
        self.assertEqual(sources[1].summary, research_live.SOURCE_UNCHECKED_SUMMARY)   # ... and never opened
        fields = {field.label: field.value for field in sources[0].fields}
        self.assertEqual((fields["Cited as"], fields["URL"], fields["Read by the research"]),
                         ("[1] Hours", HOURS_URL, research_live.SOURCE_NOT_READ))
        self.assertEqual([sources[0].summary, sources[2].summary, sources[3].summary],
                         [research_live.SOURCE_SUMMARY] * 3)   # the refused 6th too: no visit claimed,
        self.assertNotIn("this PC", research_live.SOURCE_SUMMARY)   # its picture says whether one is made
        self.assertEqual(sources[0].picture.note, pictures.NOTE_OPENING)
        fetches = self.steps(live.WEB_FETCH)
        links = {field.label: (field.value, field.link) for field in check.view().fields}
        self.assertEqual(links, {
            "Picture [1]": ("Source [1]: museum.example.net", f"picture:{sources[0].id}"),
            "Picture [2]": ("Page read 1: www.example.org", f"picture:{fetches[0].id}"),
            "Picture [3]": ("Page read 2: www.example.org", f"picture:{fetches[1].id}"),
            "Picture [5]": ("Source [5]: shop.example.com", f"picture:{sources[2].id}"),
            "Picture [6]": ("Source [6]: cafe.example.com", f"picture:{sources[3].id}")})   # no link for [4]
        self.assertTrue(all(live.result_link(link) == link for _value, link in links.values()))
        web.cited(report, check)                                       # again: the same steps (keyed)
        self.assertEqual(len(self.steps(live.WEB_SOURCE)), 4)

    def test_the_check_names_each_pictures_step(self) -> None:
        snapper = BudgetSnapshots()
        web = research_live.WebSteps(self.task, ResearchLimits(), snapshots=snapper)
        self.read(web, "toolu_f1", VISIT_URL, 1)
        answer = ResearchAnswer("Open [1] [2] [3].", sources=(("Visit", VISIT_URL), ("Hours", HOURS_URL),
                                                              ("Local", "https://192.168.1.1/")))
        web_log = rs.WebLog(searches=1, fetches=1, reads=1, hits=2,
                            seen=frozenset({normalize_url(VISIT_URL), normalize_url(HOURS_URL)}))
        cards, report = validate(answer, web_log, now=NOW, max_sources=5, max_cards=8, page_ids=(), question="q",
                                 hint="", forced=True)
        research_live.validated(self.task, cards, report, answer, web_log, web_steps=web)
        (check,) = self.steps(live.RESEARCH_VALIDATE)
        self.assertEqual([item.note for item in check.items[:2]],
                         [f"{VISIT_URL} - picture: see the Page read 1 step",
                          f"{HOURS_URL} - picture: see the Source [2] step"])
        self.assertEqual(check.items[2].status_text, "LEFT OUT")
        self.assertNotIn("its picture", check.items[2].note)                         # never a left-out one
        self.assertEqual([call[2] for call in snapper.calls], [VISIT_URL, HOURS_URL])
        self.assertEqual(self.stream.snapshot()[0].steps[-1].kind, live.WEB_SOURCE)  # after the check

    def test_an_unchecked_source_is_never_opened_and_the_check_says_why(self) -> None:
        # A search came back as text Jarvis could not list (checkable=False): a source no search
        # listed and no page read loaded is shown with UNCHECKED_WARNING, as an Open card only. It
        # gets no picture (nothing is opened for it, no cap used); the checked one after it does.
        snapper = BudgetSnapshots()
        web = research_live.WebSteps(self.task, ResearchLimits(), snapshots=snapper)
        tickets = "https://www.example.com/tickets"
        answer = ResearchAnswer("Open [1] [2].", sources=(("Tickets", tickets), ("Hours", HOURS_URL)))
        web_log = rs.WebLog(searches=1, fetches=0, reads=0, hits=1, seen=frozenset({normalize_url(HOURS_URL)}),
                            checkable=False)
        cards, report = validate(answer, web_log, now=NOW, max_sources=5, max_cards=8, page_ids=(), question="q",
                                 hint="", forced=True)
        self.assertEqual([source.checked for source in report.sources], [False, True])
        research_live.validated(self.task, cards, report, answer, web_log, web_steps=web)
        self.assertEqual([call[1:3] for call in snapper.calls], [(live.WEB_SOURCE, HOURS_URL)])
        unchecked, checked = self.steps(live.WEB_SOURCE)
        self.assertEqual((unchecked.title, unchecked.picture.state, unchecked.picture.note),
                         ("Source [1]: www.example.com", live.PICTURE_UNAVAILABLE,
                          pictures.UNAVAILABLE_PREFIX + research_live.UNCHECKED_NO_PICTURE))
        self.assertTrue(unchecked.open_hint)                                  # its step shows why
        self.assertEqual(checked.picture.state, live.PICTURE_PENDING)
        (check,) = self.steps(live.RESEARCH_VALIDATE)
        self.assertEqual([item.note for item in check.items[:2]],
                         [f"{research_live.UNCHECKED_WARNING} - {tickets} - no picture (its address could not "
                          "be checked): see the Source [1] step",
                          f"{HOURS_URL} - picture: see the Source [2] step"])
        self.assertEqual(check.items[0].status, live.STATUS_WARN)
        links = {field.label: field.link for field in check.fields if field.link}
        self.assertEqual(links, {"Picture [2]": f"picture:{checked.id}"})     # no link to a picture never taken

    def test_a_lower_cap_refuses_with_its_own_number_and_the_check_stays_true(self) -> None:
        snapper = BudgetSnapshots(cap=2)          # min(4, [research] max_fetches = 2)
        web = research_live.WebSteps(self.task, ResearchLimits(max_fetches=2), snapshots=snapper)
        self.read(web, "toolu_f1", VISIT_URL, 1)
        answer = ResearchAnswer("Open [1] [2] [3].", sources=(
            ("Visit", VISIT_URL), ("Hours", HOURS_URL), ("Shop", "https://shop.example.com/")))
        web_log = rs.WebLog(searches=1, fetches=1, reads=1, hits=3, seen=frozenset(
            {normalize_url(VISIT_URL), normalize_url(HOURS_URL), normalize_url("https://shop.example.com/")}))
        cards, report = validate(answer, web_log, now=NOW, max_sources=5, max_cards=8, page_ids=(), question="q",
                                 hint="", forced=True)
        research_live.validated(self.task, cards, report, answer, web_log, web_steps=web)
        sources = self.steps(live.WEB_SOURCE)
        self.assertEqual([step.picture.state for step in sources], [live.PICTURE_PENDING, live.PICTURE_UNAVAILABLE])
        self.assertEqual(sources[1].picture.note, pictures.UNAVAILABLE_PREFIX + "at most 2 page pictures per research")
        (check,) = self.steps(live.RESEARCH_VALIDATE)
        self.assertEqual(check.items[2].note, "https://shop.example.com/ - picture: see the Source [3] step")
        links = {field.label: field.link for field in check.fields if field.link}
        self.assertEqual(links["Picture [3]"], f"picture:{sources[1].id}")   # its step says why there is none

    def test_nothing_without_a_snapshotter_the_live_view_or_text(self) -> None:
        report = report_of((1, "Hours", HOURS_URL, True))
        search_only = ResearchLimits(max_fetches=0)    # [research] max_fetches = 0: no page opened at all
        for name, task, snapper, limits in (
                ("no snapshotter", self.task, None, ResearchLimits()),
                ("text off", live.LiveStream(keep_text=False).task(live.TASK_WEB, "web"), BudgetSnapshots(),
                 ResearchLimits()),
                ("LIVE off", live.LiveStream(enabled=False).task(live.TASK_WEB, "web"), BudgetSnapshots(),
                 ResearchLimits()),
                ("search only", live.LiveStream().task(live.TASK_WEB, "web"), BudgetSnapshots(), search_only)):
            with self.subTest(name):
                web = research_live.WebSteps(task, limits, snapshots=snapper)
                check = task.step(live.RESEARCH_VALIDATE, research_live.VALIDATE_TITLE)
                web.cited(report, check)
                self.assertFalse(web.pictures_sources())
                if snapper is not None:
                    self.assertEqual(snapper.calls, [])
                view = task.find(live.WEB_SOURCE).view()
                self.assertIsNone(view)
                checked = check.view()
                self.assertFalse(checked is not None and any(field.link for field in checked.fields))

    def test_a_broken_snapshotter_changes_nothing_else(self) -> None:
        broken = RecordingSnapshots(fail=True)
        web = research_live.WebSteps(self.task, ResearchLimits(), snapshots=broken)
        check = self.task.step(live.RESEARCH_VALIDATE, research_live.VALIDATE_TITLE)
        web.cited(report_of((1, "Hours", HOURS_URL, True), (2, "Shop", "https://shop.example.com/", True)), check)
        self.assertEqual([call[0] for call in broken.calls], ["request", "request"])   # one failure stops nothing
        self.assertEqual(len(self.steps(live.WEB_SOURCE)), 2)
        web.finished()
        self.assertEqual(broken.calls[-1][0], "end_run")


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
