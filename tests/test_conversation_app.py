"""Ask remembers the conversation: the app (AppController + AskController + conversation.ConversationStore).

What was said before a request - the owner's earlier requests and Jarvis's words as the window showed
them - reaches the next Ask's planner as <conversation> (never a web research), and LIVE's "Your request"
says how many earlier exchanges that was (a count only). A memory shortcut's words and answers stay on
screen only (notes reach Ask through <memory>, with its own rules). The last day of the conversation is
kept on this PC (conversation.json: written a moment after it changes), shown again at a start (older than
a day left out, and pruned from the file), at most 100 entries; the Transcript's Clear conversation clears
the window and deletes the file. Nothing of it is logged. claude.exe is never started (tests.ask_fakes'
runner), nothing reaches Google or Notion, and Qt runs offscreen; every text is invented.
"""

from __future__ import annotations

import json
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QApplication

from briefing_reader import hud, live, ui
from briefing_reader.ask import planner as ask_planner
from briefing_reader.conversation import CONVERSATION_FILE, ROLE_JARVIS, ROLE_YOU, ConversationStore, Turn
from tests.ask_fakes import stream
from tests.ui_fakes import CAL_LINE, NOW, SAY, AppHarness, fonts, plan, settle, wait_for

_app: QApplication | None = None
FIRST = "block two hours on Tuesday for the Zq7talk essay"
MEMORY_ON = "\n[productivity]\nenabled = true\n"


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_conversation_app", "-platform", "offscreen"])
    fonts()
    ask_planner.cli.forget_probes()


def conversation_of(stdin: str) -> str:
    return stdin.split("<conversation>\n", 1)[1].split("\n</conversation>", 1)[0] if "<conversation>" in stdin else ""


class ConversationAppCase(unittest.TestCase):
    def make(self, *runs: Any, **kwargs: Any) -> AppHarness:
        app = AppHarness(self, runs=runs, **kwargs)
        app.reading()
        app.c.ask.start()
        self.assertTrue(wait_for(lambda: app.c.ask.ready is not None), "readiness checked")
        return app

    def submit(self, app: AppHarness, text: str) -> None:
        bar = app.c.window.reading.command_bar
        bar.set_text(text)
        bar.input.returnPressed.emit()
        settle()
        self.assertTrue(wait_for(lambda: not app.c.ask.busy, 10))
        settle()

    @staticmethod
    def path(app: AppHarness) -> Path:
        return app.config.data_dir / CONVERSATION_FILE

    @staticmethod
    def request_fields(app: AppHarness, kind: str = live.TASK_ASK) -> dict[str, str]:
        task = [task for task in app.c.live.snapshot() if task.kind == kind][-1]
        return {field.label: field.value for field in task.steps[0].fields}


class ConversationContextTests(ConversationAppCase):
    def test_the_next_ask_gets_what_was_said(self) -> None:
        app = self.make(plan(lines=(CAL_LINE,)), plan(lines=(CAL_LINE,)))
        self.submit(app, FIRST)
        first = app.factory.runner.started[0].stdin
        self.assertNotIn("<conversation>", first)                      # nothing was said before it
        self.assertEqual(self.request_fields(app)["Conversation given"], "none")
        self.submit(app, "and move it to 8 PM")
        rows = conversation_of(app.factory.runner.started[1].stdin).split("\n")
        self.assertEqual(rows[:2], [f"owner | 2026-10-04 13:52 | {FIRST}", f"jarvis | 2026-10-04 13:52 | {SAY}"])
        self.assertNotIn("and move it to 8 PM", conversation_of(app.factory.runner.started[1].stdin))   # <command>'s
        self.assertEqual(self.request_fields(app)["Conversation given"], "1 earlier exchange")
        self.assertIn("<command>\nand move it to 8 PM\n</command>", app.factory.runner.started[1].stdin)

    def test_web_research_never_gets_it_nor_gives_to_it(self) -> None:
        """ISO-4 both ways: the research gets no conversation, and a later Ask's conversation holds nothing
        the research was asked or found (its request and answer stay on screen and in the file, marked)."""
        app = self.make(plan(lines=(CAL_LINE,)), stream("research_success"), plan(lines=(CAL_LINE,)))
        self.submit(app, FIRST)
        self.submit(app, "web: what time does the Example Museum open on Saturday")
        research = app.factory.runner.started[1].stdin
        self.assertNotIn("<conversation>", research)
        self.assertNotIn("zq7talk", research.casefold())
        self.assertNotIn("Conversation given", self.request_fields(app, live.TASK_WEB))
        texts = [entry[2] for entry in app.c.window.reading.conversation.entries()]
        self.assertEqual(len(texts), 4)                                     # shown as ever
        self.submit(app, "and move it to 8 PM")
        later = conversation_of(app.factory.runner.started[2].stdin)
        self.assertIn(FIRST, later)
        self.assertNotIn("Example Museum", later)
        self.assertNotIn(texts[3][:30], later)                              # the research's answer
        self.assertEqual(self.request_fields(app)["Conversation given"], "1 earlier exchange")
        app.c._save_conversation()
        kept = json.loads(self.path(app).read_text(encoding="ascii"))["turns"]
        self.assertEqual([turn.get("context", True) for turn in kept], [True, True, False, False, True, True])

    def test_a_request_the_planner_hands_to_web_research_leaves_it_too(self) -> None:
        from tests.ask_fakes import plan_stream

        handed = plan_stream({"say": "I'll look that up.", "question": "", "lines": [],
                              "web_research": {"question": "Example Museum opening hours Saturday",
                                               "why": "opening hours are on the web"}})
        app = self.make(plan(lines=(CAL_LINE,)), handed, stream("research_success"), plan(lines=(CAL_LINE,)))
        self.submit(app, FIRST)
        self.submit(app, "what time does the Example Museum open on Saturday")
        self.assertIn(FIRST, conversation_of(app.factory.runner.started[1].stdin))   # the planner run got it
        self.submit(app, "and move it to 8 PM")
        later = conversation_of(app.factory.runner.started[3].stdin)
        self.assertIn(FIRST, later)
        self.assertNotIn("Example Museum", later)                         # neither the question nor the answer
        self.assertEqual(self.request_fields(app)["Conversation given"], "1 earlier exchange")

    def test_a_memory_shortcut_stays_on_screen_only(self) -> None:
        app = self.make(plan(lines=(CAL_LINE,)), config_extra=MEMORY_ON)
        self.submit(app, "remember that Zq7memo Ana is my project lead")
        texts = [entry[2] for entry in app.c.window.reading.conversation.entries()]
        self.assertIn("remember that Zq7memo Ana is my project lead", texts)   # shown
        self.submit(app, FIRST)
        stdin = app.factory.runner.started[0].stdin
        self.assertIn("- Zq7memo Ana is my project lead\n</memory>", stdin)   # through <memory> only
        self.assertNotIn("zq7memo", conversation_of(stdin).casefold())
        self.assertNotIn("<conversation>", stdin)                               # nothing else was said
        app.c._save_conversation()
        self.assertNotIn("zq7memo", self.path(app).read_text(encoding="ascii").casefold())   # never kept


class ConversationLogTests(unittest.TestCase):
    """hud.ConversationLog's part: ``changed`` when what an entry says changes (never for a link or a sub
    line), ``kept_entries()`` without the shown-only ones, each entry's time and a stamp of its own."""

    def test_changed_kept_entries_and_stamps(self) -> None:
        log = hud.ConversationLog()
        self.addCleanup(log.deleteLater)
        changes: list[int] = []
        log.changed.connect(lambda: changes.append(1))
        first = log.add(hud.ROLE_YOU, "move my 3pm to 4", when=NOW)
        second = log.add(hud.ROLE_JARVIS, "Noted, sir.", when=NOW, keep=False)
        third = log.add(hud.ROLE_JARVIS, "Done.", when=NOW - timedelta(hours=20), stamp="Yesterday 5:52 PM",
                        tone=hud.TONE_GOOD)
        self.assertEqual(len(changes), 3)
        log.update(first, link_text="Undo", link_id="x", sub="2 proposals")   # a link and a sub line: no
        self.assertEqual(len(changes), 3)
        log.update(third, text="Done and undone.")
        log.entry(third).set_content(tone=hud.TONE_WARN)                    # also straight on the entry
        self.assertEqual(len(changes), 5)
        log.update(first, context=False)                                     # never given to Ask (ISO-4)
        log.update(first, context=False)                                     # unchanged: no signal
        self.assertEqual(len(changes), 6)
        self.assertEqual(log.kept_entries(), [(hud.ROLE_YOU, NOW, "move my 3pm to 4", hud.TONE_DONE, False),
                                              (hud.ROLE_JARVIS, NOW - timedelta(hours=20), "Done and undone.",
                                               hud.TONE_WARN, True)])
        self.assertEqual(log.entry(third).stamp, "Yesterday 5:52 PM")
        self.assertEqual(log.entry(second).when, NOW)
        log.clear()
        self.assertEqual(len(changes), 7)
        log.clear()                                                          # nothing to clear: no signal
        self.assertEqual(len(changes), 7)


class ConversationKeptTests(ConversationAppCase):
    def test_kept_a_moment_after_it_changes(self) -> None:
        app = self.make(plan(lines=(CAL_LINE,)))
        self.assertFalse(self.path(app).exists())                       # nothing said yet: no file
        self.submit(app, FIRST)
        self.assertTrue(wait_for(lambda: self.path(app).exists(), 3))
        turns = json.loads(self.path(app).read_text(encoding="ascii"))["turns"]
        self.assertEqual([(turn["role"], turn["text"]) for turn in turns], [(ROLE_YOU, FIRST), (ROLE_JARVIS, SAY)])
        self.assertTrue(self.path(app).read_bytes().isascii())

    def test_shown_again_at_a_start_and_used_for_context(self) -> None:
        def earlier(config: Any) -> None:
            ConversationStore(config.data_dir / CONVERSATION_FILE, clock=lambda: NOW).save((
                Turn(ROLE_YOU, "too old to keep", NOW - timedelta(hours=25)),
                Turn(ROLE_YOU, "what's on tomorrow?", NOW - timedelta(hours=20)),
                Turn(ROLE_JARVIS, "Tomorrow you have Project sync at 2 PM.", NOW - timedelta(hours=20), "warn"),
                Turn(ROLE_YOU, FIRST, NOW - timedelta(minutes=5))))
            raw = json.loads((config.data_dir / CONVERSATION_FILE).read_text(encoding="ascii"))
            raw["turns"].insert(0, {"role": "you", "text": "older still", "at": (NOW - timedelta(hours=30)).isoformat(),
                                    "tone": ""})
            (config.data_dir / CONVERSATION_FILE).write_text(json.dumps(raw), encoding="ascii")

        app = self.make(plan(lines=(CAL_LINE,)), prepare=earlier)
        widgets = app.c.window.reading.conversation.entry_widgets()
        self.assertEqual([widget.entry[2] for widget in widgets],
                         ["what's on tomorrow?", "Tomorrow you have Project sync at 2 PM.", FIRST])
        self.assertEqual(widgets[0].stamp, "Yesterday 5:52 PM")         # an earlier day says so
        self.assertEqual(widgets[2].stamp, "1:47 PM")
        self.assertEqual(widgets[1].tone(), hud.TONE_WARN)
        on_disk = json.loads(self.path(app).read_text(encoding="ascii"))["turns"]
        self.assertEqual(len(on_disk), 3)                                # older than a day: gone at the start
        self.submit(app, "and the day after?")
        rows = conversation_of(app.factory.runner.started[0].stdin).split("\n")
        self.assertEqual(rows[0], "owner | 2026-10-03 17:52 | what's on tomorrow?")
        self.assertEqual(self.request_fields(app)["Conversation given"], "2 earlier exchanges")

    def test_at_most_100_entries(self) -> None:
        def many(config: Any) -> None:
            ConversationStore(config.data_dir / CONVERSATION_FILE, clock=lambda: NOW).save(tuple(
                Turn(ROLE_YOU if number % 2 == 0 else ROLE_JARVIS, f"said {number}",
                     NOW - timedelta(minutes=300 - number)) for number in range(130)))

        app = self.make(plan(lines=(CAL_LINE,)), prepare=many)
        texts = [entry[2] for entry in app.c.window.reading.conversation.entries()]
        self.assertEqual(len(texts), 100)
        self.assertEqual((texts[0], texts[-1]), ("said 30", "said 129"))
        self.submit(app, FIRST)
        app.c._save_conversation()
        self.assertEqual(len(json.loads(self.path(app).read_text(encoding="ascii"))["turns"]), 100)

    def test_the_hourly_save_drops_what_turned_a_day_old(self) -> None:
        app = self.make(plan(lines=(CAL_LINE,)))
        self.submit(app, FIRST)
        app.c._save_conversation()
        self.assertEqual(len(json.loads(self.path(app).read_text(encoding="ascii"))["turns"]), 2)
        self.assertEqual(app.c._conversation_prune_timer.interval(), 3600 * 1000)
        self.assertTrue(app.c._conversation_prune_timer.isActive())
        app.clock.advance(minutes=24 * 60 + 1)
        app.c._conversation_prune_timer.timeout.emit()
        self.assertEqual(json.loads(self.path(app).read_text(encoding="ascii"))["turns"], [])
        self.assertEqual(len(app.c.window.reading.conversation.entries()), 2)   # still on screen this session

    def test_clear_conversation(self) -> None:
        app = self.make(plan(lines=(CAL_LINE,)), plan(lines=(CAL_LINE,)))
        reading = app.c.window.reading
        button = reading.transcript_clear_button
        self.assertTrue(button.isHidden())                              # nothing to clear yet
        self.submit(app, FIRST)
        app.c._save_conversation()
        self.assertTrue(self.path(app).exists())
        self.assertFalse(button.isHidden())
        self.assertEqual(button.text(), ui.CLEAR_CONVERSATION_TEXT)
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            button.click()
            settle()
        self.assertEqual(reading.conversation.entries(), [])
        self.assertFalse(self.path(app).exists())                       # wiped on this PC too
        self.assertTrue(button.isHidden())
        self.assertIn("Conversation: cleared (2 entries)", "\n".join(logs.output))
        self.submit(app, "and move it to 8 PM")
        self.assertNotIn("<conversation>", app.factory.runner.started[1].stdin)   # nothing said before it now
        self.assertEqual(self.request_fields(app)["Conversation given"], "none")

    def test_the_logs_never_hold_what_was_said(self) -> None:
        def earlier(config: Any) -> None:
            ConversationStore(config.data_dir / CONVERSATION_FILE, clock=lambda: NOW).save((
                Turn(ROLE_YOU, "Zq7talk earlier words", NOW - timedelta(hours=2)),))

        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            app = self.make(plan(lines=(CAL_LINE,)), plan(lines=(CAL_LINE,)), prepare=earlier)
            self.submit(app, FIRST)
            self.submit(app, "and the Zq7talk follow-up")
            app.c._save_conversation()
            app.c.clear_conversation()
        text = "\n".join(logs.output)
        self.assertNotIn("zq7talk", text.casefold())
        self.assertIn("Conversation: 1 entry of the last day shown again", text)


if __name__ == "__main__":
    unittest.main()
