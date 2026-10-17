"""Tests for the productivity bundle's memory in the app (AppController + MEMORY tab + JARVIS entries).

"remember that ..." saves at once with Undo; "forget that ..." with one, several or no match;
"what do you remember?"; the refusals; MEMORY's Add / Edit / Delete / Undo, also while an Ask
runs; suggestions with Remember this, Save, Dismiss and the 6th pushing the oldest out, and the
ones not offered (LIVE says why); entries that follow what MEMORY did; memory.json changed,
deleted or made unreadable by hand while Jarvis runs; the read-only and write-failure banners and
NOT SENT; an open edit across re-renders, refusals under their note, the keyboard focus, the Tab
order and the buttons' names; no note in an Ask's spoken answer, nothing spoken with speak_replies
off, sizes only with [live] text = false, "Memory given"; the logs and ACTIVITY never hold a note.
Qt runs offscreen; fakes only (nothing reaches Google, Notion or claude.exe); every note is
invented.
"""

from __future__ import annotations

import json
import logging
import threading
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton, QVBoxLayout, QWidget

from briefing_reader import hud, live, memory_view, ui
from briefing_reader.memory import MEMORY_FILE, SOURCE_TOLD, MemoryStore
from tests.ask_fakes import GatedProcess, stream
from tests.ui_fakes import AppHarness, fonts, plan, settle, wait_for

_app: QApplication | None = None
MEMORY_ON = "\n[productivity]\nenabled = true\n"
NOTE = "Zq7memo Ana is my project lead"
SUGGESTION = "Sam Example organizes the Jarvis test sync"
GROUNDED_COMMAND = "who organizes the Jarvis test sync? Sam Example, I think"


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_memory_app", "-platform", "offscreen"])
    fonts()


def suggestion_plan(text: str = SUGGESTION) -> list[str]:
    from tests.ask_fakes import plan_stream

    return plan_stream({"say": "Sam runs it.", "question": "", "lines": [], "remember": [text]})


class MemoryAppCase(unittest.TestCase):
    def make(self, *runs, config_extra: str = MEMORY_ON) -> AppHarness:
        app = AppHarness(self, runs=runs, config_extra=config_extra)
        app.reading(autoplay=False)
        app.c.ask.start()
        self.assertTrue(wait_for(lambda: app.c.ask.ready is not None), "readiness checked")
        self.c = app.c
        self.reading = app.c.window.reading
        self.view = self.reading.memory_view
        return app

    def type(self, text: str) -> None:
        bar = self.reading.command_bar
        bar.set_text(text)
        bar.input.returnPressed.emit()
        settle()

    def entries(self) -> list[tuple[str, str, str, str]]:
        return self.reading.conversation.entries()

    def last(self) -> hud._ConversationEntry:
        return self.reading.conversation.entry_widgets()[-1]

    def notes(self) -> list[str]:
        return [entry.text for entry in self.c.memory_store.entries()]

    def memory_steps(self) -> list[live.StepView]:
        tasks = [task for task in self.c.live.snapshot() if task.kind == live.TASK_MEMORY]
        return list(tasks[0].steps) if tasks else []

    def activity(self) -> list[str]:
        return [message for _when, _tag, message, _sub in self.reading.activity.entries()]


class RememberTests(MemoryAppCase):
    def test_remember_saves_at_once_with_undo(self) -> None:
        app = self.make()
        self.assertTrue(self.reading.memory_available())
        self.type(f"remember that {NOTE}")
        self.assertEqual(self.notes(), [NOTE])
        role, _when, text, _sub = self.entries()[-2]
        self.assertEqual((role, text), (hud.ROLE_YOU, f"remember that {NOTE}"))
        entry = self.last()
        self.assertEqual(entry.entry[2], f'Noted, sir: "{NOTE}."')
        self.assertEqual(entry.link()[0], ui.MEMORY_UNDO_TEXT)
        self.assertEqual([row.info().text for row in self.view.rows()], [NOTE])
        self.assertEqual(self.view.rows()[0].info().meta, f"TOLD {ui.DOT} OCT 4")
        self.assertTrue(self.reading.memory_unread())
        step = self.memory_steps()[-1]
        self.assertEqual((step.kind, step.title, step.status), (live.MEMORY_SAVE, "Saved a note (on this PC)",
                                                                live.STATUS_OK))
        self.assertIn(("Note", NOTE), [(f.label, f.value) for f in step.fields])
        self.assertEqual(self.activity()[0], "Memory: 1 note saved (1 in all)")
        self.assertIn(("result", "Noted, sir.", ""), app.voice.said)
        self.assertFalse(app.factory.runner.started)
        self.assertEqual(self.reading.command_bar.text(), "")
        self.assertIn("1 note", self.view.status())
        self.assertIn("0.0 of 4 KB sent", self.view.status())
        # Undo: gone again, the entry says so and loses its link.
        entry.link_button.click()
        settle()
        self.assertEqual(self.notes(), [])
        self.assertEqual(entry.entry[2], f'Forgotten: "{NOTE}."')
        self.assertEqual(entry.link()[0], "")
        self.assertEqual(self.memory_steps()[-1].title, "Removed a note")
        stored = MemoryStore(app.c.config.data_dir / MEMORY_FILE)
        self.assertEqual(stored.entries(), ())

    def test_remember_without_that_and_remember_to_stays_an_ask(self) -> None:
        app = self.make(plan())
        self.type("remember Ana is my project lead")
        self.assertEqual(self.notes(), ["Ana is my project lead"])
        self.type("remember to call Ana")
        self.assertTrue(wait_for(lambda: not self.c.ask.busy))
        self.assertEqual(len(app.factory.runner.started), 1)    # a normal Ask
        self.assertEqual(self.notes(), ["Ana is my project lead"])
        stdin = app.factory.runner.started[0].stdin
        self.assertIn("<memory>", stdin)
        self.assertIn("- Ana is my project lead\n</memory>\n<command>\nremember to call Ana\n</command>", stdin)

    def test_undo_after_memory_changed_the_note_does_nothing(self) -> None:
        self.make()
        for change in ("edit", "delete"):
            with self.subTest(change=change):
                self.type(f"remember that {NOTE} ({change})")
                entry = self.last()
                link_id = entry.link()[1]                                  # the entry's own Undo
                self.assertTrue(link_id.startswith(ui.MEMORY_UNDO_LINK))
                row = self.view.rows()[0]
                if change == "edit":
                    row.edit_button.click()
                    row.edit_field.setText(f"{NOTE} ({change}, changed)")
                    QTest.keyClick(row.edit_field, Qt.Key.Key_Return)
                else:
                    row.delete_button.click()
                settle()
                self.assertEqual(entry.entry[2], ui.MEMORY_CHANGED_TEXT)   # followed at once (R44)
                self.assertEqual(entry.link()[0], "")
                before = self.notes()
                self.c._on_conversation_link(link_id)                      # a click that came too late
                settle()
                self.assertEqual(self.notes(), before)
                self.assertEqual(entry.entry[2], ui.MEMORY_CHANGED_TEXT)

    def test_undo_of_a_forget_after_the_note_came_back_in_memory(self) -> None:
        self.make()
        self.type("remember that Ben runs the lab")
        self.type("forget that Ben runs the lab")
        entry = self.last()
        link_id = entry.link()[1]
        self.view.input.setText("Ben runs the lab")                       # added again in MEMORY
        self.view.add_button.click()
        settle()
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            self.c._on_conversation_link(link_id)                          # Undo of the forget
            settle()
        self.assertIn("Memory: undo ignored (already changed)", "\n".join(logs.output))
        self.assertEqual(entry.entry[2], ui.MEMORY_CHANGED_TEXT)
        self.assertEqual(entry.link()[0], "")
        self.assertEqual(self.notes(), ["Ben runs the lab"])                 # one copy, not two

    def test_forget_one_several_none(self) -> None:
        self.make()
        for note in ("Ana is my project lead", "Ana prefers mornings", "Ben runs the lab"):
            self.type(f"remember that {note}")
        self.reading.set_tab(hud.TAB_BRIEFING)
        self.type("forget about the moon")                       # none: the tab stays
        self.assertEqual(self.last().entry[2], "I have no note about that, sir.")
        self.assertEqual(self.reading.current_tab(), hud.TAB_BRIEFING)
        self.assertEqual(len(self.notes()), 3)
        self.type("forget about Ana")                             # several: MEMORY with the matches
        self.assertEqual(self.last().entry[2], "Which note, sir? I've opened MEMORY with the matches.")
        self.assertEqual(self.reading.current_tab(), hud.TAB_MEMORY)
        self.assertEqual(self.view.filter_words(), "Ana")
        self.assertEqual(sorted(row.info().text for row in self.view.rows()),
                         ["Ana is my project lead", "Ana prefers mornings"])
        self.view.show_all_button.click()
        settle()
        self.assertEqual(len(self.view.rows()), 3)
        self.type("forget that Ben runs the lab")                 # one: gone, with Undo
        entry = self.last()
        self.assertEqual(entry.entry[2], 'Forgotten, sir: "Ben runs the lab."')
        self.assertNotIn("Ben runs the lab", self.notes())
        entry.link_button.click()
        settle()
        self.assertIn("Ben runs the lab", self.notes())
        self.assertEqual(entry.entry[2], 'Restored: "Ben runs the lab."')
        self.assertEqual(self.memory_steps()[-1].title, "Restored a note")

    def test_show(self) -> None:
        app = self.make()
        before = len(self.entries())
        self.type("what do you remember?")
        self.assertEqual(self.reading.current_tab(), hud.TAB_MEMORY)
        self.assertEqual(len(self.entries()), before)            # no entry, no run
        self.assertFalse(app.factory.runner.started)

    def test_show_after_a_forget_with_several_matches_shows_every_note(self) -> None:
        self.make()
        for note in ("Ana is my project lead", "Ana prefers mornings", "Ben runs the lab"):
            self.type(f"remember that {note}")
        self.type("forget about Ana")                             # MEMORY opens filtered
        self.assertEqual(len(self.view.rows()), 2)
        self.reading.set_tab(hud.TAB_JARVIS)
        self.type("show my memory")
        self.assertEqual(self.reading.current_tab(), hud.TAB_MEMORY)
        self.assertEqual(self.view.filter_words(), "")
        self.assertEqual(len(self.view.rows()), 3)

    def test_refusals(self) -> None:
        app = self.make()
        self.type("remember that my PIN is 1234")
        self.assertEqual(self.last().entry[2], "I'd rather not keep passwords, codes or account numbers, sir; nothing "
                                               "was saved.")
        self.assertIn(("result", "I'd rather not keep that, sir.", ""), app.voice.said)
        self.assertNotIn("1234", " ".join(f.value for step in self.memory_steps() for f in step.fields))
        self.type("remember that " + "x" * 201)
        self.assertEqual(self.last().entry[2], "That's longer than I keep (200 characters), sir; nothing was saved.")
        self.type("remember that Ana is my project lead")
        self.type("remember that ana is my PROJECT lead!")
        self.assertEqual(self.last().entry[2], 'I already have that, sir: "Ana is my project lead"')
        self.c.memory_store.max_entries = 1
        self.type("remember that Ben runs the lab")
        entry = self.last()
        self.assertEqual(entry.entry[2], "Your memory is full (1 notes), sir - delete some under MEMORY first.")
        self.assertEqual(entry.link(), (ui.MEMORY_OPEN_TEXT, ui.MEMORY_OPEN_LINK))
        entry.link_button.click()
        settle()
        self.assertEqual(self.reading.current_tab(), hud.TAB_MEMORY)
        self.assertEqual(self.notes(), ["Ana is my project lead"])
        self.assertEqual(self.memory_steps()[-1].status, live.STATUS_BLOCKED)


class MemoryTabTests(MemoryAppCase):
    def test_add_edit_delete_undo(self) -> None:
        self.make()
        self.reading.set_tab(hud.TAB_MEMORY)
        self.view.input.setText("Gym on Tuesdays")
        self.view.add_button.click()
        self.view.input.setText("Ana prefers mornings")
        QTest.keyClick(self.view.input, Qt.Key.Key_Return)
        settle()
        self.assertEqual([row.info().text for row in self.view.rows()], ["Ana prefers mornings", "Gym on Tuesdays"])
        self.assertEqual(self.view.input.text(), "")
        self.view.input.setText("gym on tuesdays")
        self.view.add_button.click()
        self.assertEqual(self.view.refusal(), 'I already have that, sir: "Gym on Tuesdays"')
        row = self.view.rows()[1]
        row.edit_button.click()
        self.assertTrue(row.editing())
        QTest.keyClick(row.edit_field, Qt.Key.Key_Escape)
        self.assertFalse(row.editing())
        row.edit_button.click()
        row.edit_field.setText("Gym on Tuesdays and Thursdays")
        QTest.keyClick(row.edit_field, Qt.Key.Key_Return)
        settle()
        self.assertEqual(self.notes(), ["Ana prefers mornings", "Gym on Tuesdays and Thursdays"])
        self.assertTrue(self.view.rows()[1].info().meta.endswith(f"{ui.DOT} EDITED"))
        self.assertEqual(self.memory_steps()[-1].title, "Changed a note")
        self.view.rows()[0].delete_button.click()
        settle()
        self.assertEqual(self.notes(), ["Gym on Tuesdays and Thursdays"])
        placeholder = self.view.rows()[0]
        self.assertTrue(placeholder.info().deleted)
        self.assertEqual(placeholder.text_label.text(), hud.MEMORY_DELETED_TEXT)
        placeholder.undo_button.click()
        settle()
        self.assertEqual(self.notes(), ["Ana prefers mornings", "Gym on Tuesdays and Thursdays"])
        self.assertFalse(any(row.info().deleted for row in self.view.rows()))
        self.view.rows()[0].delete_button.click()
        settle()
        self.c._memory_drop_trash(self.view.rows()[0].info().id)   # the 10 s are over
        self.assertEqual([row.info().text for row in self.view.rows()], ["Gym on Tuesdays and Thursdays"])

    def test_add_while_an_ask_runs(self) -> None:
        gate = threading.Event()
        app = self.make(GatedProcess(plan(), gate))
        self.type("move my sync to Friday")
        self.assertTrue(self.c.ask.busy and self.reading.command_bar.input.isReadOnly())
        self.view.input.setText("Saved while Jarvis plans")
        self.view.add_button.click()
        settle()
        self.assertEqual(self.notes(), ["Saved while Jarvis plans"])
        gate.set()
        self.assertTrue(wait_for(lambda: not self.c.ask.busy))
        self.assertEqual(len(app.factory.runner.started), 1)

    def test_read_only_banner(self) -> None:
        app = self.make()
        path = app.c.config.data_dir / MEMORY_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{zq7memo not json")
        with self.assertLogs("briefing_reader.memory", level="WARNING"):
            self.c.memory_store = MemoryStore(path)
        self.c._render_memory()
        self.assertEqual(self.view.banner(), hud.MEMORY_READ_ONLY_TEXT)
        self.assertTrue(self.view.read_only())
        self.assertFalse(self.view.input.isEnabled() or self.view.add_button.isEnabled())
        self.type("remember that Ana is my project lead")
        self.assertEqual(self.last().entry[2], "memory.json can't be read, so I won't change it, sir.")
        self.type("forget about Ana")
        self.assertEqual(self.last().entry[2], "memory.json can't be read, so I won't change it, sir.")
        self.assertEqual(path.read_bytes(), b"{zq7memo not json")

    def test_bundle_off_no_tab(self) -> None:
        self.make(config_extra="")
        self.assertIsNone(self.c.memory_store)
        self.assertFalse(self.reading.memory_available())
        self.assertIsNone(self.c.ask.planner.memory)

    def test_memory_off_in_the_bundle_no_tab(self) -> None:
        self.make(config_extra=MEMORY_ON + "memory = false\n")
        self.assertIsNone(self.c.memory_store)
        self.assertFalse(self.reading.memory_available())


class SuggestionAppTests(MemoryAppCase):
    def ask_for_suggestion(self, app: AppHarness, command: str = GROUNDED_COMMAND) -> None:
        self.type(command)
        self.assertTrue(wait_for(lambda: not self.c.ask.busy))

    def test_remember_this_saves_it(self) -> None:
        app = self.make(suggestion_plan())
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            self.ask_for_suggestion(app)
        self.assertIn("remember", app.factory.runner.started[0].argv[app.factory.runner.started[0].argv.index(
            "--json-schema") + 1])
        entry = self.last()
        self.assertEqual(entry.entry[2], f'Shall I remember this, sir? "{SUGGESTION}."')
        self.assertEqual(entry.entry[3], ui.MEMORY_SUGGESTION_SUB)
        self.assertEqual(entry.link()[0], ui.MEMORY_REMEMBER_TEXT)
        self.assertEqual([info.text for info in self.view.suggestions()], [SUGGESTION])
        tile = self.view.suggestion_tile                   # Memory's focal tile: the question, Keep / Dismiss
        self.assertTrue(tile.isVisibleTo(self.view))
        self.assertEqual((tile.state(), tile.kicker()), ("pending", memory_view.SUGGESTION_KICKER))
        self.assertEqual(tile.question(), memory_view.question_text(SUGGESTION))
        self.assertNotIn(SUGGESTION, " ".join(app.voice.texts()))   # never spoken
        self.assertIn("Memory: a suggestion was offered", "\n".join(logs.output))
        self.assertEqual(self.notes(), [])
        entry.link_button.click()
        settle()
        self.assertEqual(self.notes(), [SUGGESTION])
        self.assertEqual(self.c.memory_store.entries()[0].source, "suggested")
        self.assertEqual(entry.entry[2], f'Remembered: "{SUGGESTION}."')
        self.assertEqual(entry.link()[0], ui.MEMORY_UNDO_TEXT)
        self.assertEqual(self.view.suggestions(), [])
        self.assertEqual(self.view.suggestion_tile.state(), "")    # kept from the conversation: no KEPT tile
        self.assertEqual(self.activity()[0], "Memory: suggestion saved")
        entry.link_button.click()                                   # Undo of the kept suggestion
        settle()
        self.assertEqual(self.notes(), [])

    def test_save_dismiss_and_the_sixth_in_memory(self) -> None:
        texts = [f"Sam Example runs the Jarvis test sync {word}" for word in
                 ("weekly", "monthly", "daily", "yearly", "hourly", "nightly", "biweekly")]
        app = self.make(*[suggestion_plan(text) for text in texts])
        entries = []
        for text in texts[:6]:
            self.ask_for_suggestion(app)
            entries.append(self.last())
        self.assertEqual(entries[0].entry[2], ui.MEMORY_PUSHED_TEXT)          # pushed out by the 6th
        self.assertEqual(entries[0].link()[0], "")
        self.assertEqual(entries[0].entry[3], "")                             # no "kept only on this PC"
        self.assertEqual(entries[5].entry[3], ui.MEMORY_SUGGESTION_SUB)       # still pending: it stays
        self.assertEqual([info.text for info in self.view.suggestions()], texts[1:6])
        tile = self.view.suggestion_tile                   # the oldest pending one is the focal tile
        self.assertEqual((tile.info().text, tile.kicker()), (texts[1], f"SUGGESTION {ui.DOT} 1 OF 5"))
        tile.keep_button.click()
        settle()
        self.assertEqual(entries[1].entry[2], f'Remembered: "{texts[1]}."')  # saved there: no link
        self.assertEqual(entries[1].link()[0], "")
        self.assertEqual(entries[1].entry[3], "")
        self.assertEqual((tile.state(), tile.info().text, tile.kicker()),     # the next one waits: no KEPT
                         ("pending", texts[2], f"SUGGESTION {ui.DOT} 1 OF 4"))
        tile.dismiss_button.click()
        settle()
        self.assertEqual(entries[2].entry[2], ui.MEMORY_NOT_KEPT_TEXT)
        self.assertEqual(entries[2].link()[0], "")
        self.assertEqual(entries[2].entry[3], "")
        dismissed = self.memory_steps()[-1]
        self.assertEqual(dismissed.title, "Dismissed a suggestion")
        self.assertIn(("Stored in", ui.MEMORY_DISMISSED_WORDS), [(f.label, f.value) for f in dismissed.fields])
        raw = (app.c.config.data_dir / MEMORY_FILE).read_text(encoding="ascii")
        self.assertNotIn("daily", raw)                                       # hashes only
        self.assertEqual(self.notes(), [texts[1]])
        # The same fact again (re-worded a little) is not offered once dismissed (R43).
        before = len(self.entries())
        app.factory.runner.runs.append(suggestion_plan(texts[2] + " again"))
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            self.ask_for_suggestion(app)
        self.assertNotIn("Shall I remember this", " ".join(entry[2] for entry in self.entries()[before:]))
        self.assertIn("Memory: a suggestion was dropped (dismissed)", "\n".join(logs.output))

    def test_a_kept_note_is_not_suggested_again(self) -> None:
        app = self.make(suggestion_plan())
        self.c.memory_store.add(SUGGESTION, source=SOURCE_TOLD)
        self.c._render_memory()
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            self.ask_for_suggestion(app)
        self.assertNotIn("Shall I remember this", self.last().entry[2])
        self.assertIn("Memory: a suggestion was dropped (duplicate)", "\n".join(logs.output))
        step = self.memory_steps()[-1]                    # LIVE says it was not offered, and why
        self.assertEqual((step.title, step.status), ("Suggestion not offered: you already have that note",
                                                     live.STATUS_WARN))
        self.assertIn(("Note", SUGGESTION), [(f.label, f.value) for f in step.fields])

    def test_the_same_suggestion_pending_is_not_offered_twice(self) -> None:
        app = self.make(suggestion_plan(), suggestion_plan())
        self.ask_for_suggestion(app)
        self.ask_for_suggestion(app)
        self.assertEqual(len(self.view.suggestions()), 1)
        self.assertEqual(self.memory_steps()[-1].title,
                         "Suggestion not offered: the same suggestion is already waiting")

    def test_no_suggestion_while_memory_json_is_read_only(self) -> None:
        app = self.make(suggestion_plan())
        path = app.c.config.data_dir / MEMORY_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{zq7memo not json")
        with self.assertLogs("briefing_reader.memory", level="WARNING"):
            self.c.memory_store.refresh()
        self.assertFalse(self.c.ask.planner._suggestions_on())
        self.ask_for_suggestion(app)
        argv = app.factory.runner.started[0].argv
        self.assertNotIn("remember", argv[argv.index("--json-schema") + 1])   # never asked for one
        self.assertEqual(self.view.suggestions(), [])
        self.assertEqual(path.read_bytes(), b"{zq7memo not json")

    def test_logs_and_activity_hold_no_note(self) -> None:
        app = self.make(suggestion_plan("Sam Example zq7memo the Jarvis test sync"))
        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            logging.getLogger("briefing_reader").debug("start")
            self.type(f"remember that {NOTE}")
            self.type("forget about zq7memo")
            self.ask_for_suggestion(app)
        text = "\n".join(logs.output)
        self.assertNotIn("zq7memo", text.casefold())
        self.assertIn("Memory: saved a note (told; 1 in all)", text)
        self.assertIn("Memory: removed a note (0 in all)", text)
        self.assertNotIn("zq7memo", " ".join(" ".join(row) for row in self.reading.activity.entries()).casefold())


def live_text(app: AppHarness) -> str:
    """Every text of every LIVE task: titles, summaries, fields, items and notes."""
    parts: list[str] = []
    for task in app.c.live.snapshot():
        parts += [task.title, task.summary]
        for step in task.steps:
            parts += [step.title, step.summary]
            parts += [f"{field.label} {field.value}" for field in step.fields]
            parts += [f"{item.text} {item.note}" for item in step.items]
            parts += [note.text for note in step.notes]
    return "\n".join(parts)


class MemoryFileTests(MemoryAppCase):
    """memory.json changed or deleted by hand while Jarvis runs: the planner, MEMORY and the entries
    follow the file (nothing the owner removed is sent or written back); a file that turns
    unreadable shows the banner at once."""

    def path(self) -> Path:
        return self.c.config.data_dir / MEMORY_FILE

    def test_a_deleted_file_reaches_neither_the_planner_nor_memory(self) -> None:
        app = self.make(plan())
        self.type(f"remember that {NOTE}")
        noted = self.last()
        self.path().unlink()                                       # the owner forgets everything
        self.type("move my sync to Friday")                        # the next Ask
        self.assertTrue(wait_for(lambda: not self.c.ask.busy))
        stdin = app.factory.runner.started[0].stdin
        self.assertNotIn("<memory>", stdin)
        self.assertNotIn("zq7memo", stdin.casefold())
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.view.rows(), [])
        self.assertEqual(noted.entry[2], ui.MEMORY_CHANGED_TEXT)  # its Undo would act on a gone note
        self.assertEqual(noted.link()[0], "")
        self.assertIn("Took memory.json as changed outside Jarvis (0 notes)",
                      [step.title for step in self.memory_steps()])
        request = next(task for task in self.c.live.snapshot() if task.kind == live.TASK_ASK).steps[0]
        self.assertEqual({f.label: f.value for f in request.fields}["Memory given"], "none")
        self.assertFalse(self.path().exists())                     # nothing was written back

    def test_memory_follows_a_hand_edit_when_shown_or_the_window_comes_back(self) -> None:
        self.make()
        for note in ("Ana is my project lead", "Ben runs the lab"):
            self.type(f"remember that {note}")
        data = json.loads(self.path().read_bytes())
        data["entries"] = [item for item in data["entries"] if item["text"] != "Ben runs the lab"]
        self.path().write_text(json.dumps(data), encoding="ascii")
        self.reading.set_tab(hud.TAB_MEMORY)
        self.assertEqual([row.info().text for row in self.view.rows()], ["Ana is my project lead"])
        self.assertEqual(self.c._memory_notes[0], ("Ana is my project lead",))
        self.path().unlink()
        self.c._on_window_active(True)
        self.assertEqual(self.view.rows(), [])
        self.assertEqual(self.c._memory_notes, ((), 0))
        self.view.input.setText("Gym on Tuesdays")
        self.view.add_button.click()
        settle()
        self.assertEqual([entry.text for entry in MemoryStore(self.path()).entries()], ["Gym on Tuesdays"])

    def test_a_file_made_unreadable_meanwhile_shows_the_banner_at_once(self) -> None:
        self.make()
        self.type(f"remember that {NOTE}")
        noted = self.last()
        self.reading.set_tab(hud.TAB_MEMORY)
        self.path().write_bytes(b"{zq7memo broken")
        with self.assertLogs("briefing_reader.memory", level="WARNING"):
            self.view.input.setText("Ben runs the lab")
            self.view.add_button.click()
            settle()
        self.assertEqual(self.view.refusal(), "memory.json can't be read, so I won't change it, sir.")
        self.assertEqual(self.view.banner(), hud.MEMORY_READ_ONLY_TEXT)
        self.assertTrue(self.view.read_only())
        self.assertFalse(self.view.input.isEnabled())
        self.assertEqual((self.view.rows(), self.c._memory_notes), ([], ((), 0)))   # as at a start: no notes
        noted.link_button.click()                                  # its Undo can't work now
        settle()
        self.assertEqual(noted.entry[2], f'Noted, sir: "{NOTE}."')   # its words stay ...
        self.assertEqual(noted.link()[0], "")                      # ... without a link
        self.assertEqual(self.last().entry[2], "memory.json can't be read, so I won't change it, sir.")
        self.assertEqual(self.path().read_bytes(), b"{zq7memo broken")


class MemoryPageStateTests(MemoryAppCase):
    """MEMORY's page states: NOT SENT past the 4 KB, the write-failure banner, an open edit that keeps
    its draft while rows move, refusals under the note they are about."""

    def test_not_sent_past_4_kb_and_the_write_failure_banner(self) -> None:
        self.make()
        store = self.c.memory_store
        for number in range(25):
            store.add(f"{number:03d} {chr(97 + number) * 3} " + "n" * 192, source=SOURCE_TOLD)
        self.c._render_memory()
        rows = self.view.rows()
        self.assertEqual(len(rows), 25)
        self.assertEqual([row.not_sent_label.isVisibleTo(row) for row in rows], [False] * 20 + [True] * 5)
        self.assertEqual([row.info().text[:3] for row in rows[-5:]], ["004", "003", "002", "001", "000"])
        self.assertIn(f"25 notes {ui.DOT} 4.0 of 4 KB sent.", self.view.status())
        self.assertEqual(self.view.banner(), "")
        with mock.patch("briefing_reader.memory.os.replace", side_effect=OSError("disk full")), \
                self.assertLogs("briefing_reader.memory", level="WARNING"):
            store.max_entries = 100
            self.view.input.setText("Gym on Tuesdays")
            self.view.add_button.click()
            settle()
        self.assertEqual(self.view.banner(), hud.MEMORY_NOT_SAVED_TEXT)
        self.assertEqual(self.notes()[0], "Gym on Tuesdays")         # kept for this session

    def test_an_open_edit_keeps_its_draft_while_rows_move(self) -> None:
        self.make()
        for note in ("Ana prefers mornings", "Ben runs the lab", "Cy prefers email"):
            self.type(f"remember that {note}")
        self.reading.set_tab(hud.TAB_MEMORY)
        edited: list[str] = []
        self.view.edited.connect(lambda entry_id, _text: edited.append(entry_id))
        row = next(row for row in self.view.rows() if row.info().text == "Ana prefers mornings")
        row.edit_button.click()
        settle()
        row.edit_field.setText("Ana prefers late mornings")         # the owner's draft
        self.view.input.setFocus(Qt.FocusReason.OtherFocusReason)
        settle()
        typing_in_add = self.view.input.hasFocus()                   # offscreen focus may be unavailable
        self.view.input.setText("Gym on Tuesdays")                   # a note added above the open edit
        QTest.keyClick(self.view.input, Qt.Key.Key_Return)
        settle()
        self.view.rows()[1].delete_button.click()                    # a Deleted placeholder above it ...
        settle()
        self.c._memory_drop_trash(self.view.rows()[1].info().id)     # ... that goes after its 10 s
        settle()
        self.assertEqual(self.notes(), ["Gym on Tuesdays", "Ben runs the lab", "Ana prefers mornings"])
        editing = [item for item in self.view.rows() if item.editing()]
        self.assertEqual(editing, [row])                             # the same note, the same field
        self.assertEqual(row.edit_field.text(), "Ana prefers late mornings")
        self.assertEqual(row.edit_field.selectedText(), "")
        if typing_in_add:
            self.assertIs(QApplication.focusWidget(), self.view.input)   # the keyboard stays where it was
        self.assertEqual(edited, [])
        QTest.keyClick(row.edit_field, Qt.Key.Key_Return)
        settle()
        self.assertIn("Ana prefers late mornings", self.notes())

    def test_a_refused_edit_or_undo_says_why_under_its_note(self) -> None:
        self.make()
        for note in ("Gym on Tuesdays", "Ana prefers mornings"):
            self.type(f"remember that {note}")
        self.reading.set_tab(hud.TAB_MEMORY)
        row = self.view.rows()[0]
        row.edit_button.click()
        row.edit_field.setText("gym on TUESDAYS")
        QTest.keyClick(row.edit_field, Qt.Key.Key_Return)
        settle()
        self.assertTrue(row.editing())
        self.assertEqual(self.view.refusal(row.info().id), 'I already have that, sir: "Gym on Tuesdays"')
        self.assertTrue(row.refusal_label.isVisibleTo(row))
        self.assertEqual(self.view.refusal(), "")                    # not under the Add field
        QTest.keyClick(row.edit_field, Qt.Key.Key_Escape)
        self.assertEqual(self.view.refusal(row.info().id), "")      # it goes with the edit
        # A failed Undo of a delete: the placeholder stays and says why.
        ana = row.info().id
        row.delete_button.click()
        settle()
        self.type("remember that Ana prefers mornings")              # the same note again, meanwhile
        placeholder = next(item for item in self.view.rows() if item.info().id == ana)
        placeholder.undo_button.click()
        settle()
        placeholder = next(item for item in self.view.rows() if item.info().id == ana)
        self.assertTrue(placeholder.info().deleted)
        self.assertEqual(self.view.refusal(ana), 'I already have that, sir: "Ana prefers mornings"')
        self.c._memory_drop_trash(ana)                               # its 10 s end
        self.assertNotIn(ana, [item.info().id for item in self.view.rows()])
        self.assertEqual(self.notes().count("Ana prefers mornings"), 1)

    def test_a_refused_edit_quotes_the_other_note_never_its_own(self) -> None:
        self.make()
        for note in ("Ana leads the Fabrikam project", "Ana leads the Contoso rollout"):
            self.type(f"remember that {note}")
        self.reading.set_tab(hud.TAB_MEMORY)
        row = self.view.rows()[0]                                     # the newer note: "...Contoso rollout"
        self.assertEqual(row.info().text, "Ana leads the Contoso rollout")
        row.edit_button.click()
        row.edit_field.setText("Ana leads the Contoso project")       # says the same as both notes
        QTest.keyClick(row.edit_field, Qt.Key.Key_Return)
        settle()
        self.assertTrue(row.editing())
        self.assertEqual(self.view.refusal(row.info().id),
                         'I already have that, sir: "Ana leads the Fabrikam project"')
        self.assertEqual(sorted(self.notes()), ["Ana leads the Contoso rollout", "Ana leads the Fabrikam project"])


class MemorySpeechAndLiveTests(MemoryAppCase):
    """What is spoken and what LIVE shows: no note in an Ask's spoken answer, nothing spoken with
    speak_replies off, sizes only with [live] text = false, and "Memory given" in "Your request"."""

    def test_an_ask_answer_that_repeats_a_note_is_not_spoken(self) -> None:
        app = self.make(plan(say="Zq7memo Ana is your project lead, sir.", lines=()),
                        plan(say="Your Friday is free.", lines=()))
        self.type(f"remember that {NOTE}")
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            self.type("who leads the sync?")
            self.assertTrue(wait_for(lambda: not self.c.ask.busy))
        self.assertIn("<memory>", app.factory.runner.started[0].stdin)
        self.assertIn("Zq7memo Ana is your project lead", self.last().entry[2])   # shown in full
        self.assertNotIn("zq7memo", " ".join(app.voice.texts()).casefold())     # never spoken
        self.assertIn("Here's what I found, sir.", app.voice.texts())
        self.assertIn("Ask reply not spoken as written (it repeats a memory note)", "\n".join(logs.output))
        self.type("is my Friday free?")                              # an answer without a note: as before
        self.assertTrue(wait_for(lambda: not self.c.ask.busy))
        self.assertIn("Your Friday is free.", app.voice.texts())

    def test_nothing_is_spoken_with_speak_replies_off(self) -> None:
        app = self.make(config_extra=MEMORY_ON + "\n[assistant]\nspeak_replies = false\n")
        self.type("remember that Ben runs the lab")
        self.type("forget that Ben runs the lab")
        self.type("remember that my PIN is 1234")
        self.assertEqual(self.last().entry[2], "I'd rather not keep passwords, codes or account numbers, sir; "
                                               "nothing was saved.")
        said = " ".join(app.voice.texts())
        for words in ("Noted", "Forgotten", "I'd rather not keep that"):
            self.assertNotIn(words, said)

    def test_live_text_off_shows_a_notes_size_only(self) -> None:
        app = self.make(config_extra=MEMORY_ON + "\n[live]\ntext = false\n")
        self.type(f"remember that {NOTE}")
        self.view.input.setText("Zq7memo Ben runs the lab")
        self.view.add_button.click()
        settle()
        steps = self.memory_steps()
        self.assertEqual([step.title for step in steps], ["Saved a note (on this PC)"] * 2)
        self.assertIn(("Note", f"{len(NOTE)} characters - not kept ([live] text = false)"),
                      [(f.label, f.value) for f in steps[0].fields])
        self.assertNotIn("zq7memo", live_text(app).casefold())

    def test_memory_given_in_your_request(self) -> None:
        app = self.make(plan(), stream("research_success"))
        self.type(f"remember that {NOTE}")
        self.type("move my sync to Friday")
        self.assertTrue(wait_for(lambda: not self.c.ask.busy))
        self.type("web: what time does the Example Museum open on Saturday")
        self.assertTrue(wait_for(lambda: not self.c.ask.busy))
        tasks = {task.kind: task for task in app.c.live.snapshot() if task.kind in (live.TASK_ASK, live.TASK_WEB)}
        given = {f.label: f.value for f in tasks[live.TASK_ASK].steps[0].fields}
        self.assertEqual(given["Memory given"], f"1 note, {len(NOTE)} characters")
        self.assertNotIn("Memory given", {f.label for f in tasks[live.TASK_WEB].steps[0].fields})

    def test_no_memory_given_while_the_bundle_is_off(self) -> None:
        app = self.make(plan(), config_extra="")
        self.type("move my sync to Friday")
        self.assertTrue(wait_for(lambda: not self.c.ask.busy))
        (task,) = [task for task in app.c.live.snapshot() if task.kind == live.TASK_ASK]
        self.assertNotIn("Memory given", {f.label for f in task.steps[0].fields})

    def test_memory_words(self) -> None:
        from briefing_reader.ask_ui import memory_words

        self.assertEqual(memory_words((), 0), "none")
        self.assertEqual(memory_words(("abc", "de"), 0), "2 notes, 5 characters")
        self.assertEqual(memory_words(("abc",), 1), "1 note, 3 characters (1 not sent: over the 4 KB limit)")
        self.assertEqual(memory_words(("x" * 200,) * 20, 3), "20 notes, 4,000 characters (3 not sent: over the "
                                                              "4 KB limit)")


class MemoryPageKeyboardTests(unittest.TestCase):
    """memory_view.MemoryStage alone: where the keyboard goes after Enter, Esc, Delete and Undo, the Tab order
    with rows made after the window, and button names that say which note."""

    def setUp(self) -> None:
        self.window = QWidget()
        layout = QVBoxLayout(self.window)
        self.bar = QLineEdit()                                     # a field outside MEMORY (the command bar)
        layout.addWidget(self.bar)
        self.page = memory_view.MemoryStage()
        layout.addWidget(self.page)
        self.after = QPushButton("Approve")                        # built after the page, like a card
        self.after.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        layout.addWidget(self.after)
        self.window.resize(600, 600)
        self.window.show()
        self.window.activateWindow()
        settle()
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.window.hide)

    def rows(self, ids: str, deleted: str = "") -> list[hud.MemoryRowInfo]:
        return [hud.MemoryRowInfo(i, f"note {i} text", f"TOLD {ui.DOT} OCT 4", deleted=i in deleted) for i in ids]

    def focus(self, widget: QWidget) -> bool:
        widget.setFocus(Qt.FocusReason.TabFocusReason)
        settle()
        return QApplication.focusWidget() is widget                # offscreen focus may be unavailable

    def test_focus_stays_in_its_row(self) -> None:
        page = self.page
        page.set_rows(self.rows("abc"))
        page.edited.connect(lambda _entry_id, _text: page.stop_editing())
        page.deleted.connect(lambda entry_id: page.set_rows(self.rows("abc", deleted=entry_id)))
        page.undoDelete.connect(lambda _entry_id: page.set_rows(self.rows("abc")))
        b = page.rows()[1]
        page.edit_row("b")
        settle()
        if QApplication.focusWidget() is not b.edit_field:
            self.skipTest("offscreen focus is unavailable here")
        QTest.keyClick(b.edit_field, Qt.Key.Key_Return)             # saved
        self.assertIs(QApplication.focusWidget(), b.edit_button)
        page.edit_row("b")
        QTest.keyClick(b.edit_field, Qt.Key.Key_Escape)             # cancelled
        self.assertIs(QApplication.focusWidget(), b.edit_button)
        a = page.rows()[0]
        self.assertTrue(self.focus(a.delete_button))
        QTest.keyClick(a.delete_button, Qt.Key.Key_Space)           # Delete: Undo, in the same row
        settle()
        self.assertTrue(a.info().deleted)
        self.assertIs(QApplication.focusWidget(), a.undo_button)
        QTest.keyClick(a.undo_button, Qt.Key.Key_Space)             # Undo: the note's Edit
        settle()
        self.assertIs(QApplication.focusWidget(), a.edit_button)
        page.set_rows(self.rows("bc"))                              # the row the keyboard is on goes
        self.assertIs(QApplication.focusWidget(), page.rows()[0].edit_button)
        self.assertTrue(self.focus(self.bar))                       # never out of a field outside MEMORY
        page.edit_row("c")
        self.bar.setFocus(Qt.FocusReason.OtherFocusReason)
        settle()
        page.set_rows(self.rows("abc"))
        page.stop_editing()
        self.assertIs(QApplication.focusWidget(), self.bar)

    def test_tab_goes_through_memory_in_order(self) -> None:
        page = self.page
        page.set_suggestions([hud.SuggestionRowInfo(1, "Sam Example runs the sync")])
        page.set_rows(self.rows("ab"))
        chain: list[QWidget] = []
        widget = page.input.nextInFocusChain()
        while widget is not page.input and len(chain) < 40:
            chain.append(widget)
            widget = widget.nextInFocusChain()
        tile = page.suggestion_tile
        mine = [page.add_button, page.show_all_button]
        for row in page.rows():
            mine += [row.edit_field, row.cancel_button, row.save_button, row.edit_button, row.delete_button,
                     row.undo_button]

        def next_stop(widget: QWidget) -> QWidget:
            widget = widget.nextInFocusChain()
            while not widget.focusPolicy() & Qt.FocusPolicy.TabFocus:
                widget = widget.nextInFocusChain()
            return widget

        self.assertIs(next_stop(tile.dismiss_button), tile.keep_button)          # the focal tile first:
        self.assertIs(next_stop(tile.keep_button), page.input)                   # Dismiss, Keep, then Add
        positions = [chain.index(widget) for widget in mine]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(positions[-1], chain.index(self.after))     # every note before what follows MEMORY
        if self.focus(page.add_button):
            QTest.keyClick(page.add_button, Qt.Key.Key_Tab)        # Show all is hidden: the first note's Edit
            self.assertIs(QApplication.focusWidget(), page.rows()[0].edit_button)

    def test_buttons_say_which_note(self) -> None:
        page = self.page
        page.set_suggestions([hud.SuggestionRowInfo(1, "Sam Example runs the sync")])
        page.set_rows(self.rows("a"))
        row, suggestion = page.rows()[0], page.suggestion_tile
        self.assertEqual(row.edit_button.accessibleName(), "Edit note: note a text")
        self.assertEqual(row.delete_button.accessibleName(), "Delete note: note a text")
        self.assertEqual(row.undo_button.accessibleName(), "Undo delete")
        self.assertEqual(suggestion.keep_button.accessibleName(), "Keep suggested note: Sam Example runs the sync")
        self.assertEqual(suggestion.dismiss_button.accessibleName(),
                         "Dismiss suggested note: Sam Example runs the sync")
        page.set_rows([hud.MemoryRowInfo("a", "Ana prefers mornings", f"TOLD {ui.DOT} OCT 4")])
        self.assertEqual(row.edit_button.accessibleName(), "Edit note: Ana prefers mornings")


if __name__ == "__main__":
    unittest.main()
