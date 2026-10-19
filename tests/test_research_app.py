"""Tests for web research in the app (ui.AppController with Ask on, ask_ui.AskController).

A "web:" request: LIVE comes up with the WEB task and shows each web search (its exact query) and
page read as it happens; the JARVIS entry is the answer with its numbered sources, a sub line of
counts and "See every step"; Jarvis says the answer without its [n] marks; ACTIVITY gets counts
only. The sources are ASK cards labelled with their site whose "Open page" opens the page only on
that click and only after the web rule is checked again (a tampered link is refused); Done / Deny
record the decision; a research Todo's block is added only on its Add block click (at once, then its Undo
deletes exactly that event). What the research found never reaches a later Ask (T-ISO-4). A request the planner hands
over stays in its ASK task. The logs never hold the question, a query, an address, a site, a title
or the answer. A page the research read gets its picture in LIVE through the app's page
snapshotter (tests.ui_fakes.FakeSnapshotter here: no web engine, no page is opened).

Qt runs offscreen; fakes only: claude.exe is never started (tests.ask_fakes' runner and research
streams), nothing reaches Google, Notion or the web, and every name, address and text is invented.
"""

from __future__ import annotations

import dataclasses
import sys
import threading
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from briefing_reader import ask_ui, hud, live, live_view, pictures, snapshots, ui
from briefing_reader.actions import IDN_WARNING, SOURCE_ASK, STATUS_DENIED, STATUS_DONE
from briefing_reader.ask import planner as ask_planner
from briefing_reader.ask.research_validate import entry_text
from briefing_reader.persona import research_reply_speech
from tests.ask_fakes import (
    HOURS_URL,
    RESEARCH_ANSWER,
    RESEARCH_HITS,
    RESEARCH_QUERY,
    VISIT_URL,
    GatedProcess,
    fetch_call,
    fetch_result,
    plan_stream,
    research_answer,
    research_stream,
    search_call,
    search_result,
    success_events,
)
from tests.ui_fakes import CAL_LINE, AppHarness, fonts, plan, settle, wait_for

_app: QApplication | None = None
QUESTION = "what time does the Example Museum open on Saturday"
WEB_COMMAND = f"web: {QUESTION}"
DOT = hud.MIDDLE_DOT


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_research_app", "-platform", "offscreen"])
    fonts()
    ask_planner.cli.forget_probes()


def research_run(*events) -> list[str]:
    """A research run's JSON lines (the research_success run by default)."""
    return research_stream(*(events or success_events()))


def answer_with(sources, suggestions=(), answer: str = RESEARCH_ANSWER["answer"], hits=RESEARCH_HITS) -> list[str]:
    """A run with one search listing ``hits`` and an answer with ``sources`` and ``suggestions``."""
    return research_run(search_call("toolu_s1", RESEARCH_QUERY), search_result("toolu_s1", hits),
                        research_answer(answer, sources, suggestions))


class ResearchAppCase(unittest.TestCase):
    config_extra = ""

    def make(self, *runs, extra: str | None = None) -> AppHarness:
        app = AppHarness(self, runs=runs, config_extra=self.config_extra if extra is None else extra)
        app.reading(autoplay=False)
        app.c.ask.start()
        self.assertTrue(wait_for(lambda: app.c.ask.ready is not None), "readiness checked")
        self.app, self.c, self.reading = app, app.c, app.c.window.reading
        self.outcomes: list[ask_planner.AskOutcome] = []
        app.c.ask.outcomeReady.connect(self.outcomes.append)
        return app

    def submit(self, text: str = WEB_COMMAND) -> None:
        self.reading.command_bar.set_text(text)
        self.reading.command_bar.input.returnPressed.emit()
        settle()

    def finish(self) -> ask_planner.AskOutcome:
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10))
        settle()
        return self.outcomes[-1]

    def drained(self) -> None:
        wait_for(lambda: not self.c._live_feed._timer.isActive(), 2)
        self.c._live_feed.drain()
        settle()

    def tasks(self, kind: str) -> list[live.TaskView]:
        return [task for task in self.c.live.snapshot() if task.kind == kind]

    def asked(self) -> list:
        return [action for action in self.c._actions if action.source == SOURCE_ASK]

    def card(self, title_start: str):
        return next(card for card in self.reading.approvals.cards() if card.title().startswith(title_start))


class WebRequestTests(ResearchAppCase):

    def test_live_shows_each_web_step_then_jarvis_has_the_answer_with_numbered_sources(self) -> None:
        gate = threading.Event()
        self.make(GatedProcess(research_run(), gate, after=2))     # held after the search call
        self.reading.set_tab(hud.TAB_BRIEFING)
        self.submit()
        self.assertEqual(self.reading.current_tab(), hud.TAB_LIVE)   # [live] auto_open = "tab": the WEB task
        self.assertEqual(self.reading.conversation.entries()[-1][0::2], ("you", WEB_COMMAND))
        task_id = self.c.ask.task_id
        self.assertTrue(task_id)
        log = self.reading.live_log

        def search_row():
            self.c._live_feed.drain()
            card = log.task_widget(task_id)
            if card is None:
                return None
            return next((step for step in card.step_widgets() if step.view.kind == live.WEB_SEARCH), None)

        self.assertTrue(wait_for(lambda: search_row() is not None, 5))
        row = search_row()
        self.assertEqual(log.task_widget(task_id).view.kind, live.TASK_WEB)
        self.assertEqual(row.view.status, live.STATUS_RUNNING)       # while Claude Code runs it
        self.assertFalse(row.is_open())                              # one line: the focal frame shows it
        self.assertEqual(self.reading.live_stage.focal.frame.step_id(), row.view.id)
        self.assertIn(f"Query (exact): {RESEARCH_QUERY}", live_view.exact_text(row.view))
        self.assertEqual(self.reading.live_status.state(), hud.LIVE_WORKING)
        gate.set()
        outcome = self.finish()
        self.drained()
        report = outcome.research
        self.assertIsNotNone(report)
        card = log.task_widget(task_id)
        self.assertEqual((card.view.status, card.view.summary), (live.STATUS_OK, "2 source cards + 1 proposal"))
        steps = {step.view.kind: step.view for step in card.step_widgets()}
        self.assertEqual((steps[live.WEB_SEARCH].status, steps[live.WEB_FETCH].status), (live.STATUS_OK, live.STATUS_OK))
        # JARVIS: the answer with its numbered sources (the whole entry_text), counts, See every step.
        entry = self.reading.conversation.entry_widgets()[-1]
        role, _stamp, text, sub = entry.entry
        self.assertEqual((role, text), ("jarvis", entry_text(report)))
        self.assertEqual(text, RESEARCH_ANSWER["answer"] + "\n\nSources:\n"
                               "[1] Visit - Example Museum (www.example.org)\n[2] Hours and tickets (museum.example.net)")
        self.assertTrue(sub.startswith(f"web research {DOT} 2 source cards + 1 proposal under NEEDS YOUR OK {DOT} "),
                        sub)
        self.assertTrue(sub.endswith(f" s {DOT} 1 search, 1 page read"), sub)
        self.assertEqual(entry.tone(), hud.TONE_DONE)
        self.assertEqual(entry.link(), (ui.SEE_STEPS_TEXT, f"live:{task_id}"))
        # Jarvis says it: the answer without its [n] marks, the sources on screen, the suggestion waiting.
        address = self.c.config.assistant.address
        expected = research_reply_speech(report.answer, sources=2, cards=1, address=address).text
        self.assertEqual(self.app.voice.said[-1], ("reply", expected, ""))
        self.assertEqual(self.app.voice.kinds(), ["ack", "reply"])
        self.assertNotIn("[1]", expected)
        self.assertIn("The sources are on screen", expected)
        # ACTIVITY: counts only (never the question, the answer, a title or a site).
        newest = self.reading.activity.entries()[0]
        self.assertEqual((newest[1], newest[2]), (hud.TAG_ASK, "Web research: 2 source cards + 1 proposal"))
        self.assertTrue(newest[3].endswith(f" s {DOT} 1 run {DOT} 1 search, 1 page read"), newest[3])
        row_text = " ".join(newest[1:])
        for secret in ("Museum", "example", "Saturday", "10 AM"):
            self.assertNotIn(secret, row_text)
        # The bar: the answer (its [n] marks kept), and the counts.
        bar = self.reading.command_bar
        self.assertEqual((bar.status(), bar.tone()), (RESEARCH_ANSWER["answer"], hud.TONE_DONE))
        self.assertTrue(bar.meta().startswith("2 SOURCE CARDS + 1 PROPOSAL UNDER ASK"), bar.meta())
        self.assertTrue(bar.meta().endswith("5 WEB RESEARCH RUNS LEFT THIS HOUR"), bar.meta())
        entry.link_button.click()                                     # every step of this research
        settle()
        self.assertEqual(self.reading.current_tab(), hud.TAB_LIVE)

    def test_with_auto_open_off_jarvis_comes_up(self) -> None:
        self.make(research_run(), extra='\n[live]\nauto_open = "off"\n')
        self.reading.set_tab(hud.TAB_BRIEFING)
        self.submit()
        self.assertEqual(self.reading.current_tab(), hud.TAB_JARVIS)
        self.finish()
        self.assertEqual(self.reading.current_tab(), hud.TAB_JARVIS)

    def test_a_refused_research_says_why(self) -> None:
        self.make(extra="\n[research]\nenabled = false\n")
        self.submit()
        self.finish()
        entry = self.reading.conversation.entry_widgets()[-1]
        self.assertEqual((entry.entry[2], entry.tone()), (ask_planner.RESEARCH_OFF_MESSAGE, hud.TONE_WARN))
        self.assertEqual(self.app.voice.texts()[-1], "Sir, web research is turned off.")
        self.assertFalse(self.app.factory.runner.started)
        self.assertEqual(self.asked(), [])

    def test_the_research_limit_is_said_as_the_web_research_limit(self) -> None:
        self.make()
        for _ in range(6):   # [research] max_per_hour reached; the Ask caps still allow a run
            self.c.ask.planner.usage.start(kind="research")
        self.submit()
        outcome = self.finish()
        self.assertEqual(outcome.kind, ask_planner.RESEARCH_LIMIT)
        entry = self.reading.conversation.entry_widgets()[-1]
        self.assertTrue(entry.entry[2].startswith("Web research limit reached"), entry.entry[2])
        self.assertEqual(entry.tone(), hud.TONE_WARN)
        self.assertEqual(self.app.voice.texts()[-1], "Sir, we've reached the web research limit for now.")
        self.assertFalse(self.app.factory.runner.started)

    def test_page_reads_that_returned_nothing_are_not_counted_as_read(self) -> None:
        denied = research_run(search_call("toolu_s1", RESEARCH_QUERY),
                              search_result("toolu_s1", RESEARCH_HITS),
                              fetch_call("toolu_f1", VISIT_URL),
                              fetch_result("toolu_f1", error="Claude requested permissions to use WebFetch, but you "
                                                             "haven't granted it yet."),
                              research_answer("It opens at 10 AM [1].", [{"title": "Visit", "url": VISIT_URL}], []))
        self.make(denied)
        self.submit()
        self.finish()
        sub = self.reading.conversation.entries()[-1][3]
        self.assertTrue(sub.endswith(f"1 search, 0 page reads, 1 not read"), sub)
        self.assertTrue(self.reading.activity.entries()[0][3].endswith("1 search, 0 page reads, 1 not read"))

    def test_notes_the_entry_does_not_show_are_added(self) -> None:
        from briefing_reader.ask.research_validate import REFUSED_TOOLS_NOTE

        denied = research_run(search_call("toolu_s1", RESEARCH_QUERY),
                              search_result("toolu_s1", RESEARCH_HITS),
                              fetch_call("toolu_f1", VISIT_URL),
                              fetch_result("toolu_f1", error="Claude requested permissions to use WebFetch, but you "
                                                             "haven't granted it yet."),
                              research_answer("I couldn't read the page.", [], []))
        self.make(denied)
        self.submit()
        outcome = self.finish()
        self.assertEqual(outcome.message, REFUSED_TOOLS_NOTE)
        text = self.reading.conversation.entries()[-1][2]
        self.assertEqual(text, f"I couldn't read the page.\n\nSources: none\n{REFUSED_TOOLS_NOTE}")
        self.assertIn("I couldn't find a source I could show you", self.app.voice.texts()[-1])


class SourceCardTests(ResearchAppCase):

    def setUp(self) -> None:
        self.make(research_run())
        self.submit()
        self.outcome = self.finish()

    def test_the_cards_under_ask(self) -> None:
        reading = self.reading
        self.assertEqual(reading.approvals.group_headers(), [("ASK", 3), ("BRIEFING", 2)])
        labels = [card.kind_label.full_text() for card in reading.approvals.cards()]
        # The label shows the end of the host, which names the site; the detail the whole host.
        self.assertEqual(labels, [f"ASK {DOT} TODO", f"ASK {DOT} WEB {DOT} WWW.EXAMPLE.ORG",
                                  f"ASK {DOT} WEB {DOT} \u2026EXAMPLE.NET", "CALENDAR", "TODO"])
        self.assertTrue(self.card("[2]").detail_label.text().startswith(f"museum.example.net {DOT} web source"))
        first = self.card("[1]")
        self.assertEqual(first.title(), "[1] Visit - Example Museum")
        self.assertEqual(first.detail_label.text(), f"www.example.org {DOT} web source {DOT} opens in your browser")
        self.assertTrue(first.source_button.isVisibleTo(first))
        self.assertEqual(first.source_button.text(), "Open page")
        self.assertEqual(first.approve_button.text(), "Done")
        self.assertTrue(all(action.web for action in self.asked()))        # the Todo links to a source
        todo = self.card("Visit the Example Museum")
        self.assertEqual(todo.approve_button.text(), "Add block")
        # Its page may be any public site: the card names it and the tool reads Open page.
        self.assertTrue(todo.detail_label.text().endswith(f"{DOT} opens www.example.org"), todo.detail_label.text())
        self.assertEqual(todo.source_button.text(), "Open page")

    def test_open_page_opens_only_on_its_click_and_only_the_cards_page(self) -> None:
        with mock.patch.object(ui.QDesktopServices, "openUrl") as opened:
            settle()
            self.c._refresh_actions_ui()
            self.assertEqual(opened.call_count, 0)                     # never by itself
            second = self.card("[2]")
            second.source_button.click()
            settle()
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(opened.call_args[0][0].toString(), HOURS_URL)
            self.card("Visit the Example Museum").source_button.click()   # the Todo's source link
            settle()
            self.assertEqual(opened.call_args[0][0].toString(), VISIT_URL)
            self.assertEqual(opened.call_count, 2)

    def test_a_tampered_link_is_never_opened(self) -> None:
        web = next(action for action in self.asked() if action.kind == "open")
        for link in ("https://localhost/x", "http://www.example.org/visit", "https://user:pw@www.example.org/visit",
                     "https://10.0.0.1/", "https://printer.lan/", "https://www.example.org:8443/visit",
                     "https://intranet/x", "javascript:alert(1)", "https://www.example.org/a/../visit"):
            with self.subTest(link):
                tampered = dataclasses.replace(web, link=link)
                self.c._set_source(SOURCE_ASK, [tampered if action.id == web.id else action
                                                for action in self.c._source_lists[SOURCE_ASK]])
                with mock.patch.object(ui.QDesktopServices, "openUrl") as opened:
                    self.c.open_action_source(web.id)
                    settle()
                self.assertEqual(opened.call_count, 0)
        # The web rule is for web cards only: the same page on an ordinary Ask card keeps the allowlist.
        plain = dataclasses.replace(web, web=False, link=VISIT_URL)
        self.c._set_source(SOURCE_ASK, [plain])
        with mock.patch.object(ui.QDesktopServices, "openUrl") as opened:
            self.c.open_action_source(plain.id)
        self.assertEqual(opened.call_count, 0)

    def test_done_and_deny_record_the_decision(self) -> None:
        first, second = self.card("[1]"), self.card("[2]")
        ids = (first.action_id, second.action_id)
        with mock.patch.object(ui.QDesktopServices, "openUrl") as opened:
            first.approve_button.click()
            settle()
            second.deny_button.click()
            settle()
        self.assertEqual(opened.call_count, 0)                         # Done never opens anything
        self.assertEqual(self.c._store.get(ids[0])["status"], STATUS_DONE)
        self.assertEqual(self.c._store.get(ids[1])["status"], STATUS_DENIED)
        pending = [action.id for action in self.c._pending_actions()]
        self.assertFalse(set(ids) & set(pending))

    def test_a_research_todo_adds_its_block_only_on_its_click(self) -> None:
        todo = self.card("Visit the Example Museum")
        personal = self.app.calendars["personal"]
        self.assertNotIn(("create", "Visit the Example Museum"), personal.calls)   # nothing by itself
        todo.approve_button.click()
        settle()
        self.assertIsNone(self.c._send_hold)                                     # no countdown: at once
        self.assertTrue(wait_for(lambda: ("create", "Visit the Example Museum") in personal.calls, 5))
        self.assertEqual(personal.calls.count(("create", "Visit the Example Museum")), 1)

    def test_undo_deletes_exactly_the_added_block(self) -> None:
        todo = self.card("Visit the Example Museum")
        personal = self.app.calendars["personal"]
        personal.stamps = True                                 # Google answers with its version: Undo is offered
        todo.approve_button.click()
        self.assertTrue(wait_for(lambda: todo.status() == hud.CARD_ADDED and todo.undo_text() == "Undo", 5))
        (event_id,) = [key for key in personal.events if key.startswith("evt-new")]
        self.c._undos[todo.action_id].shown -= 5
        todo.undo_button.click()
        self.assertTrue(wait_for(lambda: todo.status() == hud.CARD_PENDING, 5))
        self.assertEqual([call for call in personal.calls if call[0] == "undo_create"], [("undo_create", event_id)])
        self.assertEqual(personal.calls.count(("create", "Visit the Example Museum")), 1)


class CalendarSuggestionTests(ResearchAppCase):

    def test_a_research_event_is_added_only_on_approve(self) -> None:
        line = "Calendar: Example Museum visit | 2026-10-10 10:00-12:00 |  | Example Museum | from the web"
        self.make(answer_with([{"title": "Visit", "url": VISIT_URL}], [line], answer="It opens at 10 AM [1]."))
        self.submit()
        outcome = self.finish()
        self.assertEqual(outcome.research.dropped, ())
        event = self.card("Example Museum visit")
        personal = self.app.calendars["personal"]
        created = []
        create = personal.create_event

        def recorded(action, interactive: bool = True):  # type: ignore[no-untyped-def]
            created.append(action)
            return create(action, interactive=interactive)

        personal.create_event = recorded
        self.assertEqual(event.approve_button.text(), "Approve")
        self.assertNotIn(("create", "Example Museum visit"), personal.calls)   # nothing by itself
        event.approve_button.click()
        settle()
        self.assertIsNone(self.c._send_hold)                                     # no countdown: at once
        self.assertTrue(wait_for(lambda: ("create", "Example Museum visit") in personal.calls, 5))
        self.assertEqual(personal.calls.count(("create", "Example Museum visit")), 1)
        # Only what the card showed went into the calendar: the place, never the web's notes.
        (written,) = created
        self.assertEqual((written.title, written.where, written.notes), ("Example Museum visit", "Example Museum", ""))


class IdnTests(ResearchAppCase):

    def test_an_international_domain_shows_the_warning(self) -> None:
        idn = "https://xn--bcher-kva.example.com/hours"
        hits = (("Hours", idn),)
        self.make(answer_with([{"title": "Hours", "url": idn}], answer="It opens at 10 AM [1].", hits=hits))
        self.submit()
        self.finish()
        card = self.card("[1]")
        self.assertEqual(card.kind_label.full_text(), f"ASK {DOT} WEB {DOT} \u2026EXAMPLE.COM")
        # Next to the address and above Open page (not in the note under Deny / Done).
        self.assertIn(IDN_WARNING, card.detail_label.text())
        self.assertIn("xn--bcher-kva.example.com", card.detail_label.text())
        self.assertNotIn(IDN_WARNING, card.note())
        with mock.patch.object(ui.QDesktopServices, "openUrl") as opened:
            card.source_button.click()
            settle()
        self.assertEqual(bytes(opened.call_args[0][0].toEncoded()).decode("ascii"), idn)   # on the click


class IsolationTests(ResearchAppCase):

    def test_t_iso_4_what_the_research_found_never_reaches_a_later_ask(self) -> None:
        marker = "ZQ7WEBTITLE"
        sources = [{"title": f"{marker} Visit", "url": VISIT_URL}]
        suggestion = (f"Todo: title={marker} museum | due=2026-10-10 18:00 | block= | acct= | link={VISIT_URL}",)
        self.make(answer_with(sources, suggestion, answer=f"{marker} opens at 10 AM [1]."), plan(lines=(CAL_LINE,)))
        self.submit()
        self.finish()
        self.assertTrue(any(marker in action.title for action in self.asked()))
        briefing, page_ids = self.c._ask_context()
        self.assertFalse(set(page_ids) & {action.id for action in self.asked()})
        self.submit("block two hours on Tuesday")
        self.finish()
        started = self.app.factory.runner.started
        self.assertEqual(len(started), 2)
        self.assertIn("<question>", started[0].stdin)                  # the research's own stdin
        later = started[1].stdin
        self.assertIn("<command>", later)                              # a planner run
        for secret in (marker, marker.casefold(), VISIT_URL, "www.example.org", QUESTION):
            self.assertNotIn(secret, later)


class RoutedTests(ResearchAppCase):
    config_extra = '\n[live]\nauto_open = "off"\n'

    def test_a_request_the_planner_hands_over_stays_in_its_ask_task(self) -> None:
        handed = plan_stream({"say": "I'll look that up.", "question": "", "lines": [],
                              "web_research": {"question": "Example Museum opening hours Saturday",
                                               "why": "opening hours are on the web"}})
        self.make(handed, research_run())
        self.reading.set_tab(hud.TAB_BRIEFING)
        self.submit(QUESTION)
        self.assertEqual(self.reading.current_tab(), hud.TAB_JARVIS)   # auto_open = "off"
        outcome = self.finish()
        self.assertEqual(self.tasks(live.TASK_WEB), [])
        (task,) = self.tasks(live.TASK_ASK)
        kinds = [step.kind for step in task.steps]
        self.assertLess(kinds.index(live.PLANNER_RUN), kinds.index(live.RESEARCH_INPUT))
        self.assertEqual(kinds[kinds.index(live.RESEARCH_INPUT):],
                         [live.RESEARCH_INPUT, live.RESEARCH_RUN, live.WEB_SEARCH, live.WEB_FETCH,
                          live.RESEARCH_VALIDATE, live.WEB_SOURCE, live.ASK_CARDS])
        self.assertEqual((task.status, task.summary), (live.STATUS_OK, "2 source cards + 1 proposal"))
        planner_run = next(step for step in task.steps if step.kind == live.PLANNER_RUN)
        self.assertEqual(planner_run.summary, "asked for web research - opening hours are on the web")
        self.assertEqual(self.reading.conversation.entries()[-1][2], entry_text(outcome.research))
        newest = self.reading.activity.entries()[0]
        self.assertEqual(newest[2], "Web research: 2 source cards + 1 proposal")
        self.assertIn(f"{DOT} 2 runs {DOT}", newest[3])
        research = self.app.factory.runner.started[1]
        self.assertIn("WebSearch,WebFetch", research.argv)
        self.assertTrue(research.stdin.startswith("<today>\n"))
        self.assertIn(f"<question>\n{QUESTION}\n</question>", research.stdin)


class LogTests(ResearchAppCase):

    def test_the_logs_never_hold_the_question_a_query_a_page_or_the_answer(self) -> None:
        self.make(research_run())
        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            self.submit()
            self.finish()
            with mock.patch.object(ui.QDesktopServices, "openUrl"):
                self.card("[1]").source_button.click()
                settle()
        text = "\n".join(logs.output)
        for secret in (QUESTION, RESEARCH_QUERY, VISIT_URL, HOURS_URL, "example.org", "example.net", "Example Museum",
                       "Hours and tickets", RESEARCH_ANSWER["answer"][:40], "10 AM"):
            self.assertNotIn(secret, text)
        self.assertIn("Research: run ok", text)
        self.assertIn("Open the link of action", text)


class PagePictureTests(ResearchAppCase):
    """Page pictures ([live] page_snapshots): the app hands its snapshotter (here tests.ui_fakes'
    FakeSnapshotter: no web engine, no page) to the planner; a "web:" run's page read gets its
    picture on its LIVE step (PENDING, then READY), asked from the Ask's thread with the WEB task's
    id; Cancel, Clear and quitting reach the snapshotter; with page_snapshots or text off there is
    no page picture."""

    def fetch_step(self, task_id: int):
        card = self.reading.live_log.task_widget(task_id)
        return next(step for step in card.step_widgets() if step.view.kind == live.WEB_FETCH)

    def test_the_page_a_web_request_read_is_pictured_in_live(self) -> None:
        self.make(research_run())
        snaps = self.app.snapshots
        self.assertIs(self.c._snapshots, snaps)
        self.assertIs(self.c.ask.planner.page_snapshots, snaps)
        self.submit()
        self.finish()
        self.drained()
        # The WEB task's id from LIVE (ask.task_id is 0 again once the run ended, which a fast run can do while
        # submit() settles: reading it there raced the run).
        web, = self.tasks(live.TASK_WEB)
        task_id = web.id
        self.assertTrue(task_id)
        # The page read, then (after the check) the cited source no page read pictured.
        self.assertEqual(snaps.requests, [(VISIT_URL, task_id), (HOURS_URL, task_id)])
        self.assertEqual(snaps.ended, [task_id])
        self.assertEqual(snaps.threads, ["ask", "ask"])                # asked from the Ask's thread
        step = self.fetch_step(task_id)
        self.assertEqual((step.view.status, step.view.picture.state), (live.STATUS_OK, live.PICTURE_PENDING))
        # One line in the timeline (the owner's choice): picked, the page's picture is the focal frame's.
        self.assertFalse(step.is_open())
        self.assertIsNone(step.picture_widget())                       # no thumbnail
        frame = self.reading.live_stage.focal.frame
        step.row.click()
        settle()
        self.assertEqual((frame.step_id(), frame.state()), (step.view.id, live_view.PENDING))
        self.assertIn(snapshots.NOTE_OPENING, frame.accessibleName())
        snaps.complete(0)
        self.drained()
        step = self.fetch_step(task_id)
        self.assertEqual(step.view.picture.state, live.PICTURE_READY)
        self.assertEqual((frame.step_id(), frame.state()), (step.view.id, live_view.READY))
        self.assertEqual(frame.step().picture.caption, pictures.page_caption("www.example.org"))
        frame.click()                                                  # Enter / click: larger, inside Jarvis
        settle()
        viewer = self.reading.live_log.picture_viewer()
        self.assertTrue(viewer is not None and viewer.isVisible())
        self.assertEqual(viewer.step_id(), step.view.id)
        viewer.close()
        settle()
        # The cited source: its own step with its picture; the check's "Picture [2]" link shows it.
        card = self.reading.live_log.task_widget(task_id)
        source = next(step for step in card.step_widgets() if step.view.kind == live.WEB_SOURCE)
        self.assertEqual(source.view.title, "Source [2]: museum.example.net")
        self.assertEqual(source.view.picture.state, live.PICTURE_PENDING)
        self.assertFalse(source.is_open())
        check = next(step for step in card.step_widgets() if step.view.kind == live.RESEARCH_VALIDATE)
        link = next(field.link for field in check.view.fields if field.label == "Picture [2]")
        self.assertEqual(link, f"{live.PICTURE_LINK}{source.view.id}")
        self.reading.live_log.follow_link(link)                        # not READY yet: the step is the focal one
        settle()
        self.assertFalse(viewer.isVisible())
        self.assertEqual(frame.step_id(), source.view.id)
        self.assertFalse(source.is_open())
        snaps.complete(1)
        self.drained()
        outside: list[str] = []
        self.reading.live_log.linkClicked.connect(outside.append)
        self.reading.live_log.follow_link(link)                        # READY: shown larger, inside Jarvis
        settle()
        self.assertEqual(outside, [])                                  # an internal link: it never leaves LIVE
        self.assertTrue(viewer.isVisible())
        self.assertEqual(viewer.step_id(), source.view.id)
        viewer.close()
        settle()
        # Clear: the snapshotter drops what nobody would see any more.
        self.c.clear_live()
        self.assertEqual(snaps.cleared, 1)
        with mock.patch.object(ui, "_remove_audio_dir_after"):
            self.c.shutdown()
        self.assertEqual(snaps.shutdowns, 1)

    def test_cancel_stops_the_runs_pictures_at_once(self) -> None:
        gate = threading.Event()
        self.make(GatedProcess(research_run(), gate, after=2))     # held after the search call
        self.submit()
        task_id = self.c.ask.task_id
        self.assertTrue(wait_for(lambda: self.tasks(live.TASK_WEB)
                                 and any(step.kind == live.WEB_SEARCH for step in self.tasks(live.TASK_WEB)[0].steps)))
        self.reading.command_bar.button.click()                       # Cancel
        self.assertEqual(self.app.snapshots.cancelled, [task_id])     # before the run has even stopped
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 5))
        settle()
        self.assertEqual(self.app.snapshots.cancelled, [task_id, task_id])   # and again once it has
        self.assertEqual(self.app.snapshots.ended, [task_id])
        self.assertEqual(self.app.snapshots.requests, [])

    def test_page_snapshots_off_means_no_snapshotter(self) -> None:
        self.make(research_run(), extra="\n[live]\npage_snapshots = false\n")
        self.assertIsNone(self.c._snapshots)
        self.assertIsNone(self.c.ask.planner.page_snapshots)
        self.submit()
        task_id = self.c.ask.task_id
        self.finish()
        self.drained()
        self.assertEqual(self.app.snapshots.requests, [])
        self.assertIsNone(self.fetch_step(task_id).view.picture)
        self.c.clear_live()                                           # nothing to tell: no error
        self.assertEqual(self.app.snapshots.cleared, 0)

    def test_text_off_gives_no_page_picture(self) -> None:
        # The snapshotter refuses (R_TEXT_OFF: no page is opened) and LIVE keeps no page picture at all
        # with [live] text = false (a page picture is page text).
        self.make(research_run(), extra="\n[live]\ntext = false\n")
        self.assertFalse(self.app.snapshots.keep_text)
        self.submit()
        task_id = self.c.ask.task_id
        self.finish()
        self.drained()
        self.assertEqual(self.app.snapshots.requests, [(VISIT_URL, task_id)])
        self.assertEqual(self.app.snapshots.pending, [False])         # refused: nothing to open
        self.assertIsNone(self.fetch_step(task_id).view.picture)

    def test_live_off_means_no_snapshotter(self) -> None:
        self.make(research_run(), extra="\n[live]\nenabled = false\n")
        self.assertIsNone(self.c._snapshots)
        self.submit()
        self.finish()
        self.assertEqual(self.app.snapshots.requests, [])

    def test_the_app_builds_the_real_one_with_the_research_cap(self) -> None:
        from briefing_reader import snapshot_engine

        self.make(extra="\n[research]\nmax_fetches = 2\n")
        self.c._snapshot_factory = None
        real = self.c._create_snapshotter(self.c.config.live)
        self.addCleanup(real.deleteLater)
        self.addCleanup(real.shutdown)
        self.assertIsInstance(real, snapshot_engine.PageSnapshotter)
        self.assertIs(real.parent(), self.c)
        self.assertEqual((real._max_per_run, real._keep_text), (2, True))
        self.assertTrue(real.idle())                                  # nothing starts until a page is read
        self.assertNotIn("PySide6.QtWebEngineCore", sys.modules)       # the web engine is not even loaded

    def test_a_factory_that_fails_leaves_the_research_as_it_was(self) -> None:
        def broken(_controller):
            raise RuntimeError("no pictures today")

        with self.assertLogs("briefing_reader.ui", level="WARNING") as logs:
            app = AppHarness(self, runs=(research_run(),), snapshot_factory=broken)
        self.assertIsNone(app.c._snapshots)
        self.assertIn("Page pictures could not be set up (RuntimeError)", "\n".join(logs.output))


class ControllerKindTests(unittest.TestCase):

    def test_running_kind_of_a_web_request(self) -> None:
        self.assertEqual(ask_ui.forced_question("Research:  x"), "x")
        self.assertIsNone(ask_ui.forced_question("website: x"))


if __name__ == "__main__":
    unittest.main()
