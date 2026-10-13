"""Tests for briefing_reader.live: the LIVE view's event stream (Qt-free).

Lifecycle, statuses, ordering across threads, the debounced listener and change feed, bounds,
cleaning (controls and bidi made visible, secrets redacted, token shapes hidden), [live] text =
false, never raising, safe repr, and the word helpers. Every value is invented.
"""

from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from briefing_reader import config as config_module
from briefing_reader import live
from briefing_reader.actions import parse_action_line
from briefing_reader.ask import cli
from briefing_reader.live import (
    NO_STEP,
    NO_TASK,
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_RUNNING,
    STATUS_WARN,
    Field,
    Item,
    LiveLimits,
    LiveStream,
)

PDT = timezone(timedelta(hours=-7))
WALL = datetime(2026, 10, 7, 14, 41, 7, tzinfo=PDT)


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def make(**kwargs) -> tuple[LiveStream, Clock]:
    clock = Clock()
    kwargs.setdefault("clock", clock)
    kwargs.setdefault("wall", lambda: WALL)
    return LiveStream(**kwargs), clock


def only(stream: LiveStream) -> live.TaskView:
    views = stream.snapshot()
    assert len(views) == 1, views
    return views[0]


class LifecycleTests(unittest.TestCase):
    def test_task_and_steps(self) -> None:
        stream, clock = make()
        task = stream.task(live.TASK_ASK, "  Reply to   Sam\n about the budget ")
        self.assertIsNot(task, NO_TASK)
        self.assertFalse(task.finished)
        step = task.step(live.ASK_REQUEST, "Your request", status=STATUS_OK, fields=[("You typed", "hello")])
        clock.t += 2
        planner = task.step(live.PLANNER_RUN, "Planner run 1", fields=[Field("Model", "sonnet", mono=True)])
        planner.note("Claude Code started")
        planner.block("Exact text sent to Claude Code (stdin)", "line 1\nline 2\tx", untrusted=True)
        view = only(stream)
        self.assertEqual(view.title, "Reply to Sam about the budget")   # whitespace collapsed
        self.assertEqual((view.kind, view.status, view.tag), (live.TASK_ASK, STATUS_RUNNING, "ASK"))
        first, second = view.steps
        self.assertEqual((first.kind, first.status, first.ended), (live.ASK_REQUEST, STATUS_OK, 100.0))
        self.assertEqual(first.fields, (Field("You typed", "hello"),))
        self.assertEqual((second.status, second.ended, second.started), (STATUS_RUNNING, None, 102.0))
        self.assertEqual(second.fields[0], Field("Model", "sonnet", mono=True))
        self.assertEqual(second.notes[0].text, "Claude Code started")
        block = second.blocks[0]
        self.assertEqual((block.text, block.chars, block.cut, block.kept, block.untrusted),
                         ("line 1\nline 2\tx", 15, 0, True, True))
        self.assertLess(first.seq, second.seq)
        self.assertEqual(second.at, WALL)
        clock.t += 5
        self.assertEqual(second.elapsed(clock()), 5.0)
        planner.done(status_text="answered", summary="2 proposals")
        view = only(stream)
        self.assertEqual((view.steps[1].status, view.steps[1].status_text, view.steps[1].summary),
                         (STATUS_OK, "ANSWERED", "2 proposals"))
        self.assertEqual(view.steps[1].elapsed(999.0), 5.0)
        task.finish(STATUS_OK, summary="2 cards")
        view = only(stream)
        self.assertEqual((view.status, view.summary, view.ended), (STATUS_OK, "2 cards", 107.0))
        self.assertTrue(task.finished)

    def test_every_status_and_status_words(self) -> None:
        stream, clock = make()
        task = stream.task(live.TASK_ACTION, "Reply work: Re: Budget")
        for status in (STATUS_OK, STATUS_WARN, STATUS_BLOCKED, STATUS_FAILED, STATUS_CANCELLED):
            task.step(live.ACTION_CHECKS, status, status=status)
        words = [live.status_word(step) for step in only(stream).steps]
        self.assertEqual(words, ["DONE", "CHECK", "BLOCKED", "FAILED", "CANCELLED"])
        step = task.step(live.ACTION_CALL, "call", status_text="sent!")
        self.assertEqual(live.status_word(only(stream).steps[-1]), "SENT")
        step.update(status_text="already there")
        self.assertEqual(only(stream).steps[-1].status_text, "ALREADY THERE")
        countdown = task.step(live.ACTION_COUNTDOWN, "Undo countdown", status_text="SENDING", deadline=clock() + 6.5)
        view = only(stream).steps[-1]
        self.assertEqual(live.status_word(view, clock()), "SENDING IN 7 S")
        self.assertEqual(live.status_word(view), "SENDING")
        countdown.done(STATUS_CANCELLED, status_text="UNDONE", summary="nothing was sent")
        view = only(stream).steps[-1]
        self.assertEqual((live.status_word(view, clock()), view.deadline), ("UNDONE", None))

    def test_done_sets_the_end_once(self) -> None:
        stream, clock = make()
        step = stream.task(live.TASK_ASK, "x").step(live.MAIL_SEARCH, "search")
        clock.t = 103
        step.done(STATUS_WARN, status_text="NOTHING FOUND")
        clock.t = 110
        step.done(STATUS_OK, summary="later")
        view = only(stream).steps[0]
        self.assertEqual((view.status, view.ended, view.summary), (STATUS_OK, 103, "later"))
        step.update(status=STATUS_RUNNING)   # never back to running once ended
        self.assertEqual(only(stream).steps[0].status, STATUS_OK)

    def test_finish_closes_running_steps_and_freezes(self) -> None:
        stream, clock = make()
        task = stream.task(live.TASK_ASK, "x")
        done = task.step(live.ASK_CHECKS, "checks")
        done.done()
        running = task.step(live.PLANNER_RUN, "Planner run 1")
        clock.t += 3
        task.finish(STATUS_CANCELLED, summary="Jarvis closed")
        view = only(stream)
        self.assertEqual([step.status for step in view.steps], [STATUS_OK, STATUS_CANCELLED])
        self.assertEqual((view.steps[1].summary, view.steps[1].ended), ("Jarvis closed", 103.0))
        self.assertEqual(view.steps[0].summary, "")
        revision = view.revision
        # Frozen: nothing more changes it.
        running.note("late")
        running.field("Took", "1 s")
        running.block("Planner's reply (raw)", "{}")
        running.done(STATUS_OK)
        self.assertIs(task.step(live.ASK_CARDS, "Answer"), NO_STEP)
        task.update(title="other")
        task.finish(STATUS_OK)
        view = only(stream)
        self.assertEqual(view.revision, revision)
        self.assertEqual((view.status, view.title, len(view.steps)), (STATUS_CANCELLED, "x", 2))

    def test_find_current_and_task_note(self) -> None:
        stream, _clock = make()
        task = stream.task(live.TASK_ASK, "x")
        first = task.step(live.MAIL_THREAD, "Read email thread", key="work:t1")
        second = task.step(live.MAIL_THREAD, "Read email thread", key="work:t2")
        self.assertEqual(task.find(live.MAIL_THREAD).id, second.id)
        self.assertEqual(task.find(live.MAIL_THREAD, "work:t1").id, first.id)
        self.assertIs(task.find(live.PLANNER_RUN), NO_STEP)
        second.done()
        self.assertEqual(task.current().id, first.id)
        task.note("Cancel clicked")
        self.assertEqual(only(stream).steps[0].notes[0].text, "Cancel clicked")
        first.done()
        task.note("after")   # no running step: the last one gets it
        self.assertEqual(only(stream).steps[1].notes[0].text, "after")

    def test_rename_block_keeps_the_text(self) -> None:
        stream, _clock = make()
        step = stream.task(live.TASK_ASK, "a").step(live.PLANNER_RUN, "Planner run 1")
        step.block("not sent yet", "the prompt")
        size = stream._total
        step.rename_block("not sent yet", "sent")
        (block,) = step.view().blocks
        self.assertEqual((block.label, block.text), ("sent", "the prompt"))
        self.assertEqual(stream._total, size - len("not sent yet") + len("sent"))
        version = stream.version
        step.rename_block("not sent yet", "sent")      # gone already: nothing
        step.block("other", "x")
        step.rename_block("other", "sent")             # the new name is taken: nothing
        self.assertEqual([block.label for block in step.view().blocks], ["sent", "other"])
        self.assertEqual(stream.version, version + 1)  # only block() changed it
        live.NO_STEP.rename_block("a", "b")

    def test_fields_replace_by_label_and_items(self) -> None:
        stream, _clock = make()
        step = stream.task(live.TASK_ASK, "x").step(live.CALENDAR_READ, "Calendar",
                                                    fields=[("Window", "a"), ("Window", "b")])
        step.field("work", "reading...")
        step.field("personal", "12 events")
        step.field("work", "3 events")
        step.item("Wed Oct 7 2:00-3:00 PM - Jarvis test sync", status=STATUS_OK, note="NEW recipient", mono=True)
        view = only(stream).steps[0]
        self.assertEqual([(f.label, f.value) for f in view.fields],
                         [("Window", "b"), ("work", "3 events"), ("personal", "12 events")])
        self.assertEqual(view.items[0], Item("Wed Oct 7 2:00-3:00 PM - Jarvis test sync", STATUS_OK, "",
                                             "NEW recipient", True))

    def test_open_hint_table(self) -> None:
        ok, warn = Item("a", STATUS_OK), Item("b", STATUS_WARN, "DROPPED")
        self.assertTrue(live.open_hint(live.CALENDAR_READ, STATUS_RUNNING))
        for status in (STATUS_WARN, STATUS_BLOCKED, STATUS_FAILED):
            self.assertTrue(live.open_hint(live.CALENDAR_READ, status))
        for status in (STATUS_OK, STATUS_CANCELLED):
            self.assertFalse(live.open_hint(live.CALENDAR_READ, status))
        self.assertTrue(live.open_hint(live.ACTION_PAYLOAD, STATUS_OK))
        self.assertTrue(live.open_hint(live.ASK_CARDS, STATUS_OK))
        self.assertFalse(live.open_hint(live.ASK_VALIDATE, STATUS_OK, [ok]))
        self.assertTrue(live.open_hint(live.ASK_VALIDATE, STATUS_OK, [ok, warn]))
        stream, _clock = make()
        task = stream.task(live.TASK_ASK, "x")
        task.step(live.ASK_REQUEST, "req", status=STATUS_OK)
        task.step(live.PLANNER_RUN, "run")
        self.assertEqual([step.open_hint for step in only(stream).steps], [False, True])


class RollingTaskTests(unittest.TestCase):
    def test_background_task_is_reused_pinned_and_steps_replace_in_place(self) -> None:
        stream, clock = make()
        ask = stream.task(live.TASK_ASK, "first")
        background = stream.task(live.TASK_BACKGROUND, live.BACKGROUND_TITLE, key=live.BACKGROUND_KEY)
        again = stream.task(live.TASK_BACKGROUND, live.BACKGROUND_TITLE, key=live.BACKGROUND_KEY)
        self.assertEqual(background.id, again.id)
        agenda = background.step(live.AGENDA_READ, "Agenda", key="agenda")
        background.step(live.CLAUDE_CHECK, "Checked Claude Code", key="claude", status=STATUS_OK)
        views = stream.snapshot()
        self.assertEqual([view.id for view in views], [background.id, ask.id])   # pinned first
        self.assertTrue(views[0].pinned and views[0].compact)
        self.assertEqual(views[0].status, STATUS_RUNNING)    # a step runs
        agenda.done(STATUS_OK)
        self.assertEqual(stream.snapshot()[0].status, STATUS_OK)
        clock.t += 60
        replaced = again.step(live.AGENDA_READ, "Agenda again", key="agenda", status=STATUS_FAILED)
        view = stream.snapshot()[0]
        self.assertEqual(len(view.steps), 2)
        self.assertEqual((view.steps[0].id, view.steps[0].title, view.steps[0].status),
                         (agenda.id, "Agenda again", STATUS_FAILED))
        self.assertEqual(replaced.id, agenda.id)
        background.finish(STATUS_OK)        # never finished
        stream.clear()                      # and never removed
        self.assertEqual(stream.snapshot()[0].id, background.id)
        self.assertFalse(background.finished)
        self.assertEqual(stream.running(), 1)   # the Ask; never the background task

    def test_clear_empties_the_rolling_task_of_its_finished_steps(self) -> None:
        stream, _clock = make()
        background = stream.task(live.TASK_BACKGROUND, live.BACKGROUND_TITLE, key=live.BACKGROUND_KEY)
        background.step(live.EVENT_CHECK, "Checked 1 card's events", key="peek", status=STATUS_OK,
                        items=[live.Item("Team sync - Mon Oct 5 - 2:00-3:00 PM")])
        running = background.step(live.AGENDA_READ, "Agenda", key="agenda")
        before = stream._total
        version = stream.version
        stream.clear()
        view = only(stream)
        self.assertEqual([step.id for step in view.steps], [running.id])   # what still runs stays
        self.assertLess(stream._total, before)
        self.assertIn(view.id, [task.id for task in stream.changes(version).tasks])
        running.done(STATUS_OK)
        stream.clear()
        view = only(stream)
        self.assertEqual((view.steps, view.steps_more, view.status), ((), 0, STATUS_OK))
        self.assertEqual(stream._total, len(live.BACKGROUND_TITLE))
        version = stream.version
        stream.clear()                      # nothing left to clear: no change
        self.assertEqual(stream.version, version)


class FeedTests(unittest.TestCase):
    def test_listener_once_per_burst_until_changes(self) -> None:
        stream, _clock = make()
        calls: list[int] = []
        stream.set_listener(lambda: calls.append(1))
        task = stream.task(live.TASK_ASK, "x")
        step = task.step(live.PLANNER_RUN, "run")
        for number in range(500):
            step.note(f"note {number}")
        self.assertEqual(len(calls), 1)
        changes = stream.changes(0)
        self.assertEqual([view.id for view in changes.tasks], [task.id])
        step.note("more")
        self.assertEqual(len(calls), 2)
        stream.changes(changes.version)
        stream.set_listener(None)
        step.note("quiet")
        self.assertEqual(len(calls), 2)

    def test_changes_only_changed_tasks_and_removed_ids(self) -> None:
        stream, _clock = make()
        first = stream.task(live.TASK_ASK, "one")
        second = stream.task(live.TASK_ASK, "two")
        version = stream.changes(0).version
        second.step(live.ASK_REQUEST, "req", status=STATUS_OK)
        changes = stream.changes(version)
        self.assertEqual(([view.id for view in changes.tasks], changes.removed, changes.reset),
                         ([second.id], (), False))
        self.assertEqual(stream.changes(changes.version).tasks, ())
        first.finish(STATUS_OK)
        version = stream.changes(changes.version).version
        stream.clear()
        changes = stream.changes(version)
        self.assertEqual((changes.removed, changes.tasks), ((first.id,), ()))
        self.assertEqual([view.id for view in stream.snapshot()], [second.id])   # running stays whole

    def test_reset_when_since_is_older_than_the_removal_log(self) -> None:
        stream, _clock = make(limits=LiveLimits(max_removed_log=3))
        tasks = [stream.task(live.TASK_ASK, str(number)) for number in range(6)]
        version = stream.changes(0).version
        for task in tasks:
            task.finish(STATUS_OK)
        stream.clear()
        changes = stream.changes(version)
        self.assertTrue(changes.reset)
        self.assertEqual(changes.tasks, ())
        self.assertFalse(stream.changes(changes.version).reset)
        self.assertTrue(stream.changes(changes.version + 100).reset)   # a cursor from elsewhere

    def test_views_are_rebuilt_only_when_changed(self) -> None:
        stream, _clock = make()
        task = stream.task(live.TASK_ASK, "x")
        task.step(live.ASK_REQUEST, "req", status=STATUS_OK)
        first = stream.snapshot()[0]
        self.assertIs(stream.snapshot()[0], first)
        task.update(summary="y")
        self.assertIsNot(stream.snapshot()[0], first)
        self.assertIs(stream.snapshot()[0].steps[0], first.steps[0])


class ThreadTests(unittest.TestCase):
    def test_eight_threads(self) -> None:
        stream = LiveStream(limits=LiveLimits(max_steps=600, max_notes=600, max_total_chars=50_000_000))
        task = stream.task(live.TASK_ASK, "burst")
        calls: list[int] = []
        stream.set_listener(lambda: calls.append(1))
        errors: list[BaseException] = []
        views: list[tuple] = []
        start = threading.Barrier(9)

        def work(number: int) -> None:
            try:
                start.wait()
                step = task.step(live.PLANNER_RUN, f"thread {number}")
                for index in range(500):
                    step.note(f"{number}:{index}")
                for index in range(50):
                    task.step(live.MAIL_THREAD, f"t{number}", key=f"{number}:{index % 5}")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(number,)) for number in range(8)]
        for thread in threads:
            thread.start()
        start.wait()
        for _ in range(20):
            views.append(stream.snapshot())
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        view = stream.snapshot()[0]
        seqs = [step.seq for step in view.steps]
        self.assertEqual(len(seqs), len(set(seqs)))
        self.assertEqual(len(view.steps), 8 + 8 * 5)   # keyed steps replaced in place
        for step in view.steps:
            if step.kind != live.PLANNER_RUN:
                continue
            numbers = [int(note.text.split(":")[1]) for note in step.notes]
            self.assertEqual(numbers, sorted(numbers))
            self.assertEqual(len(numbers), 500)
        for snapshot in views:   # every snapshot during the burst is whole and consistent
            for task_view in snapshot:
                self.assertTrue(all(isinstance(step, live.StepView) for step in task_view.steps))
        self.assertLessEqual(len(calls), 1)   # never re-armed: changes() was not called


class BoundsTests(unittest.TestCase):
    def test_tasks_running_never_dropped_and_briefings_keep_three(self) -> None:
        stream, _clock = make(limits=LiveLimits(max_tasks=4))
        running = stream.task(live.TASK_ASK, "running")
        finished = []
        for number in range(5):
            task = stream.task(live.TASK_ACTION, f"done {number}")
            task.finish(STATUS_OK)
            finished.append(task)
        ids = [view.id for view in stream.snapshot()]
        self.assertEqual(len(ids), 4)
        self.assertIn(running.id, ids)
        self.assertEqual(ids[1:], [task.id for task in finished[-3:]])
        stream2, _clock = make()
        briefings = [stream2.task(live.TASK_BRIEFING, "Briefing from Notion") for _ in range(5)]
        for task in briefings:
            task.finish(STATUS_OK)
        stream2.task(live.TASK_BRIEFING, "Briefing from Notion")   # trimming runs on the next mutation
        kept = [view.id for view in stream2.snapshot()]
        self.assertEqual(len(kept), 3)
        self.assertNotIn(briefings[0].id, kept)
        self.assertTrue(all(view.compact for view in stream2.snapshot()))

    def test_steps_items_notes_fields(self) -> None:
        stream, _clock = make(limits=LiveLimits(max_steps=3, max_items=2, max_notes=4, max_fields=2))
        task = stream.task(live.TASK_ASK, "x")
        steps = [task.step(live.MAIL_THREAD, str(number)) for number in range(5)]
        self.assertIs(steps[3], NO_STEP)
        step = steps[0]
        for number in range(5):
            step.item(f"item {number}")
            step.note(f"note {number}")
            step.field(f"label {number}", "v")
        step.field("label 0", "replaced")   # replacing past the cap still works
        view = only(stream)
        self.assertEqual((len(view.steps), view.steps_more), (3, 2))
        first = view.steps[0]
        self.assertEqual(([item.text for item in first.items], first.items_more), (["item 0", "item 1"], 3))
        self.assertEqual([note.text for note in first.notes], ["note 0", "note 2", "note 3", "note 4"])
        self.assertEqual(first.notes_more, 1)
        self.assertEqual([(f.label, f.value) for f in first.fields], [("label 0", "replaced"), ("label 1", "v")])

    def test_block_cut_and_total_budget(self) -> None:
        stream, _clock = make(limits=LiveLimits(max_block_chars=10, max_total_chars=200))
        task = stream.task(live.TASK_ASK, "x")
        step = task.step(live.PLANNER_RUN, "run")
        step.block("stdin", "0123456789abcdef")
        block = only(stream).steps[0].blocks[0]
        self.assertEqual((block.text, block.chars, block.cut), ("0123456789", 16, 6))
        task.finish(STATUS_OK)
        other = stream.task(live.TASK_ACTION, "y")
        other.step(live.ACTION_PAYLOAD, "payload").block("Message", "z" * 10)
        for number in range(20):
            other.step(live.ACTION_CHECKS, "c" * 20)
        ids = [view.id for view in stream.snapshot()]
        self.assertEqual(ids, [other.id])   # the finished task went to stay under the budget

    def test_caps_on_lines(self) -> None:
        stream, _clock = make()
        task = stream.task(live.TASK_ASK, "t" * 500, summary="s" * 500)
        step = task.step(live.ASK_CHECKS, "x", fields=[("l" * 100, "v" * 3000)])
        step.item("i" * 900, note="n" * 900)
        step.note("o" * 900)
        view = only(stream)
        self.assertEqual((len(view.title), len(view.summary)), (live.TITLE_CAP, live.SUMMARY_CAP))
        self.assertTrue(view.title.endswith("..."))
        field = view.steps[0].fields[0]
        self.assertEqual((len(field.label), len(field.value)), (live.LABEL_CAP, live.FIELD_CAP))
        item = view.steps[0].items[0]
        self.assertEqual((len(item.text), len(item.note)), (live.ITEM_CAP, live.ITEM_CAP))
        self.assertEqual(len(view.steps[0].notes[0].text), live.NOTE_CAP)


class CleaningTests(unittest.TestCase):
    def test_controls_and_bidi_are_made_visible(self) -> None:
        stream, _clock = make()
        step = stream.task(live.TASK_ASK, "evil\u202etxt.exe").step(live.MAIL_THREAD, "x")
        step.field("Subject", "a\r\nb\nc\x07d\u200be\u2028f")
        step.block("Text given to the planner", "line\r\nnext\ttab\x1b[31m\u202eabc\u2066")
        view = only(stream)
        self.assertEqual(view.title, "evil<U+202E>txt.exe")
        self.assertEqual(view.steps[0].fields[0].value, "a \u21b5 b \u21b5 c<U+0007>d<U+200B>e<U+2028>f")
        self.assertEqual(view.steps[0].blocks[0].text, "line\nnext\ttab<U+001B>[31m<U+202E>abc<U+2066>")

    def test_secrets_and_token_shapes(self) -> None:
        secret = "liveview-secret-value-1234"
        config_module.register_secret(secret)
        stream, _clock = make()
        step = stream.task(live.TASK_ASK, f"has {secret}").step(live.ACTION_CALL, "x")
        step.field("Error", "Bearer abcdefghijklmnop and ya29.a0AfH6SMBxyz12345 and sk-ant-api03-abcdefghij")
        step.note("token 1//0abcdefghijklmnopqrstuvwx GOCSPX-abcdefghijkl")
        step.block("raw", f"{secret} ya29.a0AfH6SMBxyz12345 data")
        step.item(f"secret_{'a' * 24}")
        view = only(stream)
        text = repr([view.title, view.steps[0].fields, view.steps[0].notes, view.steps[0].items])
        everything = " ".join([view.title, view.steps[0].fields[0].value, view.steps[0].notes[0].text,
                               view.steps[0].items[0].text, view.steps[0].blocks[0].text])
        self.assertNotIn(secret, everything)
        for shape in ("abcdefghijklmnop", "ya29.a0AfH6SMBxyz12345 and", "sk-ant-api03", "1//0abc", "GOCSPX-a",
                      "a" * 24):
            self.assertNotIn(shape, everything.replace(view.steps[0].blocks[0].text, ""))
        self.assertIn("[hidden]", view.steps[0].fields[0].value)
        # A block is data: redacted (registered secrets) but no token-shape filter.
        self.assertEqual(view.steps[0].blocks[0].text, "[REDACTED] ya29.a0AfH6SMBxyz12345 data")
        self.assertNotIn(secret, text)

    def test_keep_text_false_keeps_sizes_only(self) -> None:
        stream, _clock = make(keep_text=False)
        step = stream.task(live.TASK_ASK, "x").step(live.PLANNER_RUN, "run")
        step.block("Exact text sent to Claude Code (stdin)", "secret plans " * 10)
        block = only(stream).steps[0].blocks[0]
        self.assertEqual((block.text, block.chars, block.kept), ("", 130, False))

    def test_links_follow_rule_l(self) -> None:
        stream, _clock = make()
        step = stream.task(live.TASK_ACTION, "x").step(live.ACTION_CALL, "x")
        step.field("Thread", "Gmail", link="https://mail.google.com/mail/#all/18c0ffee00000001")
        step.field("Event", "Calendar", link="https://www.google.com/calendar/event?eid=abc")
        step.field("Event2", "Calendar", link="https://calendar.google.com/calendar/event?eid=abc")
        step.field("Answer", "in the JARVIS tab", link="tab:jarvis")
        step.field("Web", "page", link="https://evil.example/x")
        step.field("Docs", "doc", link="https://docs.google.com/document/d/1")
        step.field("Plain", "http", link="http://mail.google.com/x")
        links = [field.link for field in only(stream).steps[0].fields]
        self.assertEqual(links, ["https://mail.google.com/mail/#all/18c0ffee00000001",
                                 "https://www.google.com/calendar/event?eid=abc",
                                 "https://calendar.google.com/calendar/event?eid=abc", "tab:jarvis", "", "", ""])


class SafetyTests(unittest.TestCase):
    def test_never_raises(self) -> None:
        stream, _clock = make()

        def broken() -> None:
            raise RuntimeError("listener")

        stream.set_listener(broken)
        task = stream.task(live.TASK_ASK, object())                # not a string
        step = task.step(live.ASK_CHECKS, None, fields=[("a", 3), "junk", ("b",)], items=[5, Item("ok")])
        step.field(None, ["x"])
        step.item(12)
        step.note(b"bytes")
        step.block("b", None)
        step.update(status="nonsense", deadline="soon")
        task.finish("nonsense")
        self.assertEqual(only(stream).status, STATUS_OK)

        class Unprintable:
            def __str__(self) -> str:
                raise ValueError("no")

        stream.task(live.TASK_ASK, Unprintable())
        self.assertEqual(stream.snapshot()[-1].title, "<Unprintable>")

    def test_no_task_and_no_step_accept_everything(self) -> None:
        for handle in (NO_TASK,):
            self.assertTrue(handle.finished)
            self.assertFalse(handle.enabled)
            self.assertIs(handle.step(live.ASK_CHECKS, "x"), NO_STEP)
            self.assertIs(handle.find(live.ASK_CHECKS), NO_STEP)
            handle.update(title="x")
            handle.note("x")
            handle.finish(STATUS_OK)
        NO_STEP.update(title="x", status=STATUS_OK, deadline=1.0)
        NO_STEP.field("a", "b")
        NO_STEP.item("x")
        NO_STEP.note("x")
        NO_STEP.block("a", "b")
        NO_STEP.done()
        self.assertFalse(NO_STEP.active)

    def test_disabled_and_closed(self) -> None:
        stream, _clock = make(enabled=False)
        self.assertIs(stream.task(live.TASK_ASK, "x"), NO_TASK)
        self.assertEqual(stream.snapshot(), ())
        stream, _clock = make()
        task = stream.task(live.TASK_ASK, "x")
        step = task.step(live.PLANNER_RUN, "run")
        calls: list[int] = []
        stream.set_listener(lambda: calls.append(1))
        stream.close()
        step.note("after close")
        task.finish(STATUS_OK)
        self.assertIs(stream.task(live.TASK_ASK, "y"), NO_TASK)
        self.assertEqual((stream.snapshot(), calls, stream.running()), ((), [], 0))
        stream.set_listener(lambda: calls.append(1))
        self.assertEqual(calls, [])

    def test_repr_names_no_content(self) -> None:
        stream, _clock = make()
        task = stream.task(live.TASK_ASK, "LIVEMARK-TITLE")
        step = task.step(live.MAIL_THREAD, "LIVEMARK-STEP", summary="LIVEMARK-SUM",
                         fields=[("LIVEMARK-L", "LIVEMARK-V")], items=[Item("LIVEMARK-I", note="LIVEMARK-N")])
        step.note("LIVEMARK-NOTE")
        step.block("LIVEMARK-BL", "LIVEMARK-BLOCK")
        view = stream.snapshot()[0]
        payload = live.Payload("LIVEMARK-P", (Field("LIVEMARK-F", "LIVEMARK-FV"),), "Message", "LIVEMARK-BODY")
        texts = [repr(view), str(view), repr(view.steps[0]), repr(task), repr(step), repr(stream),
                 repr(stream.changes(0)), repr(view.steps[0].fields), repr(view.steps[0].items),
                 repr(view.steps[0].notes), repr(view.steps[0].blocks), repr(payload), str(payload)]
        for text in texts:
            self.assertNotIn("LIVEMARK", text)
        self.assertIn("kind='ask'", repr(view))
        self.assertIn("status='running'", repr(view.steps[0]))

    def test_when_text(self) -> None:
        stream, _clock = make()
        self.assertEqual(stream.when_text(WALL), "2:41 PM")
        self.assertEqual(stream.when_text(WALL, seconds=True), "2:41:07 PM")
        stream.hour24 = True
        self.assertEqual(stream.when_text(WALL), "14:41")
        self.assertEqual(stream.when_text(WALL, seconds=True), "14:41:07")


class WordTests(unittest.TestCase):
    def test_elapsed_text(self) -> None:
        for seconds, text in ((0, "0.0 s"), (0.44, "0.4 s"), (8.27, "8.2 s"), (12.9, "12 s"), (65, "1:05"),
                              (3723, "1:02:03"), (-3, "0.0 s"), (float("nan"), "0.0 s")):
            self.assertEqual(live.elapsed_text(seconds), text)

    def test_notify_words(self) -> None:
        self.assertEqual(live.notify_words("all"), "everyone on the event gets Google's email (sendUpdates=all)")
        for value in ("external", "externalOnly"):
            self.assertEqual(live.notify_words(value),
                             "only guests outside your organization get an email (sendUpdates=externalOnly)")
        self.assertEqual(live.notify_words("none"), "nobody gets an email (sendUpdates=none)")
        self.assertEqual(live.notify_words("odd"), live.notify_words("all"))

    def test_command_line_text_names_no_folder(self) -> None:
        exe = r"C:\Users\someone\.local\bin\claude.exe"
        schema = cli.schema_text(allow_search=True)
        argv = cli.build_argv(exe, model="sonnet", max_turns=4, schema=schema, hardened=True)
        text = live.command_line_text(argv)
        self.assertTrue(text.startswith("claude -p --output-format stream-json --verbose --model sonnet "))
        self.assertIn(f"--json-schema <answer schema: {len(schema):,} characters>", text)
        self.assertIn("--system-prompt-file planner_prompt.md", text)
        self.assertIn("--settings ask_settings.json", text)
        self.assertIn('--tools ""', text)
        self.assertIn('--mcp-config {"mcpServers":{}}', text)
        self.assertTrue(text.endswith("--safe-mode --restricted"))
        for folder in ("someone", "Users", ".local", str(cli.ASSETS), "briefing_reader", "assets", "\\", "/ask"):
            self.assertNotIn(folder, text)
        self.assertEqual(live.command_line_text(["x", "/home/me/file.json", "a b"]), 'claude file.json "a b"')


class PrivacyTests(unittest.TestCase):
    """P1, P3, P7 in the app (tests.ui_fakes.AppHarness, offscreen, fakes only): an Ask that reads a
    thread holding a marker after a search with a marked word, and an approved Email with a marked
    body. The markers are on screen (the stream) but in no log line and no file; no secret is in
    the stream; repr() of the stream's objects names no content."""

    BODY = "LIVEMARK-BODY-7f3"
    WORD = "livemarkq"
    SEND = "LIVEMARK-SEND-2c9"
    SECRETS = ("sk-ant-dummy-not-for-the-child", "owner@example.edu")

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        from tests import ui_fakes

        cls.qt = QApplication.instance() or QApplication(["test_live", "-platform", "offscreen"])
        ui_fakes.fonts()

    def test_markers_are_shown_but_never_logged_or_saved(self) -> None:
        from briefing_reader.ask import planner as ask_planner
        from tests.ask_fakes import FakeReader, gmail_message, gmail_thread, plan_stream
        from tests.ui_fakes import EMAIL_LINE, GUEST, AppHarness, settle, wait_for

        ask_planner.cli.forget_probes()
        thread = gmail_thread("thr0000777", [gmail_message(
            "msg0000777", sender="Ana Lima <ana@example.edu>", to="me@example.edu", subject="Budget review",
            body=f"Here is the budget: {self.BODY}", msgid="<CAB7@mail.example.edu>", at_ms=1791200000000)])
        reader = FakeReader("work", {"thr0000777": thread}, found=["thr0000777"])
        first = plan_stream({"say": "Let me look.", "lines": [],
                             "gmail_search": {"account": "work", "query": f"subject:{self.WORD}", "why": "your words"}})
        second = plan_stream({"say": "I read it.", "lines": []})
        app = AppHarness(self, runs=(first, second), readers={"work": reader, "personal": FakeReader("personal")})
        app.reading(autoplay=False)
        c = app.c
        c.ask.start()
        self.assertTrue(wait_for(lambda: c.ask.ready is not None))
        mail = parse_action_line(EMAIL_LINE.replace(GUEST, "ana@example.edu").replace("Moved to Friday",
                                                                                         self.SEND))
        command = f"what does the {self.WORD} budget email say"
        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            c.window.reading.command_bar.set_text(command)
            c.window.reading.command_bar.input.returnPressed.emit()
            self.assertTrue(wait_for(lambda: not c.ask.busy, 10))
            settle()
            c._set_source("briefing", list(c._source_lists["briefing"]) + [mail])
            settle()
            c.window.reading.action_card(mail.id).approve_button.click()
            settle()
            self.assertIsNotNone(c._countdown)
            c._countdown.deadline = time.monotonic() - 0.001
            c._on_countdown_tick()
            self.assertTrue(wait_for(lambda: mail.id not in c._jobs and mail.id not in c._running, 5))
            settle()
        self.assertEqual(reader.searches, [f"subject:{self.WORD}"])
        self.assertEqual(len(app.senders["work"].sent), 1)
        exe = str(app.root / "bin" / "claude.exe")
        # P7: no marker, address, secret or path in any log line (DEBUG included).
        text = "\n".join(logs.output)
        for private in (self.BODY, self.WORD, self.SEND, "ana@example.edu", "me@example.edu", exe, *self.SECRETS):
            self.assertNotIn(private, text)
        # P1: no file Jarvis wrote holds a marker.
        for path in app.root.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                for marker in (self.BODY, self.WORD, self.SEND):
                    self.assertNotIn(marker.encode("utf-8"), data, path.name)
        # On screen: the stream shows the markers ...
        shown = self.everything(c.live)
        for marker in (self.BODY, self.WORD, self.SEND):
            self.assertIn(marker, shown)
        # ... but no secret (P3): no environment value, nothing of the auth status, no path.
        for secret in (*self.SECRETS, exe, str(app.root), str(ask_planner.cli.ASSETS), "C:/Windows",
                       str(app.config.data_dir), "total_cost_usd", "session_id"):
            self.assertNotIn(secret, shown)
        # repr() of every view names no content.
        texts = repr(c.live.snapshot()) + repr(c.live.changes(0)) + "".join(
            repr(step) + repr(step.fields) + repr(step.items) + repr(step.notes) + repr(step.blocks)
            for task in c.live.snapshot() for step in task.steps)
        for marker in (self.BODY, self.WORD, self.SEND, "ana@example.edu", "Budget review"):
            self.assertNotIn(marker, texts)

    @staticmethod
    def everything(stream: live.LiveStream) -> str:
        parts: list[str] = []
        for task in stream.snapshot():
            parts += [task.title, task.summary]
            for step in task.steps:
                parts += [step.title, step.summary, step.status_text]
                parts += [f"{field.label} {field.value} {field.link}" for field in step.fields]
                parts += [f"{item.text} {item.note}" for item in step.items]
                parts += [note.text for note in step.notes]
                parts += [f"{block.label} {block.text}" for block in step.blocks]
        return "\n".join(parts)


class MemoryTaskTests(unittest.TestCase):
    """The productivity bundle's rolling MEMORY task and its memory.save steps."""

    def test_memory_task_and_step_kinds_are_known(self) -> None:
        self.assertIn(live.TASK_MEMORY, live.TASK_KINDS)
        self.assertIn(live.MEMORY_SAVE, live.STEP_KINDS)
        self.assertEqual(live.TASK_TAGS[live.TASK_MEMORY], "MEMORY")
        self.assertNotIn(live.TASK_MEMORY, live.ATTENTION_KINDS)     # never opens LIVE by itself
        stream, _clock = make()
        memory = stream.task(live.TASK_MEMORY, live.MEMORY_TITLE, key=live.MEMORY_KEY)
        again = stream.task(live.TASK_MEMORY, live.MEMORY_TITLE, key=live.MEMORY_KEY)
        self.assertEqual(memory.id, again.id)
        memory.step(live.MEMORY_SAVE, "Saved a note (on this PC)", status=STATUS_OK,
                    fields=[("Note", "Ana is my project lead"), ("Stored in", "memory.json (on this PC)")])
        view = stream.snapshot()[0]
        self.assertEqual((view.kind, view.tag, view.title, view.compact, view.pinned),
                         (live.TASK_MEMORY, "MEMORY", "Memory (this PC)", True, True))
        self.assertEqual([step.kind for step in view.steps], [live.MEMORY_SAVE])
        self.assertEqual(stream.running(), 0)


class OwnReadsTaskTests(unittest.TestCase):
    """The productivity bundle's own reads and offers: a rolling compact task of its own (R6)."""

    def test_own_reads_task_and_step_kinds_are_known(self) -> None:
        self.assertIn(live.TASK_OWN_READS, live.TASK_KINDS)
        for kind in (live.CALENDAR_DAY, live.OFFER_MADE):
            self.assertIn(kind, live.STEP_KINDS)
        self.assertEqual(live.TASK_TAGS[live.TASK_OWN_READS], "READS")
        self.assertNotIn(live.TASK_OWN_READS, live.ATTENTION_KINDS)   # never opens LIVE by itself
        self.assertIn(live.TASK_OWN_READS, live.COMPACT_KINDS)
        stream, _clock = make()
        reads = stream.task(live.TASK_OWN_READS, live.OWN_READS_TITLE, key=live.OWN_READS_KEY)
        again = stream.task(live.TASK_OWN_READS, live.OWN_READS_TITLE, key=live.OWN_READS_KEY)
        self.assertEqual(reads.id, again.id)
        day = reads.step(live.CALENDAR_DAY, "Today's events with guests, for prep offers (read only)", key="day")
        reads.step(live.OFFER_MADE, "Offered: plan my day - nothing read or run until you click", key="plan-offer",
                   status=STATUS_OK)
        day.done(STATUS_OK, summary="personal 4; work 3; read only")
        again.step(live.CALENDAR_DAY, "Today's events again", key="day", status=STATUS_OK)   # in place
        view = stream.snapshot()[0]
        self.assertEqual((view.kind, view.tag, view.title, view.compact, view.pinned),
                         (live.TASK_OWN_READS, "READS", "Jarvis's own reads and offers (this PC)", True, True))
        self.assertEqual([step.kind for step in view.steps], [live.CALENDAR_DAY, live.OFFER_MADE])
        self.assertEqual(stream.running(), 0)



class PlanTaskTests(unittest.TestCase):
    """The productivity bundle's day plan: its own attention task and its steps."""

    def test_plan_task_and_step_kinds_are_known(self) -> None:
        self.assertIn(live.TASK_PLAN, live.TASK_KINDS)
        self.assertEqual(live.TASK_TAGS[live.TASK_PLAN], "PLAN")
        self.assertIn(live.TASK_PLAN, live.ATTENTION_KINDS)          # it opens LIVE like an Ask
        self.assertNotIn(live.TASK_PLAN, live.COMPACT_KINDS)
        for kind in (live.MODE_REQUEST, live.PLAN_FREE, live.PLAN_VALIDATE):
            self.assertIn(kind, live.STEP_KINDS)
        stream, _clock = make()
        task = stream.task(live.TASK_PLAN, "plan my day")
        task.step(live.MODE_REQUEST, "Your request", status=STATUS_OK, fields=[("You typed", "plan my day")])
        task.step(live.PLAN_FREE, "Your free time", status=STATUS_OK)
        view = stream.snapshot()[0]
        self.assertEqual((view.kind, view.tag), (live.TASK_PLAN, "PLAN"))
        self.assertEqual([step.kind for step in view.steps], [live.MODE_REQUEST, live.PLAN_FREE])


class PrepTaskTests(unittest.TestCase):
    """The productivity bundle's meeting brief: its own attention task and its steps."""

    def test_prep_task_and_step_kinds_are_known(self) -> None:
        from briefing_reader import hud

        self.assertIn(live.TASK_PREP, live.TASK_KINDS)
        self.assertEqual(live.TASK_TAGS[live.TASK_PREP], "PREP")
        self.assertIn(live.TASK_PREP, live.ATTENTION_KINDS)          # it opens LIVE like an Ask
        self.assertNotIn(live.TASK_PREP, live.COMPACT_KINDS)
        self.assertIn(live.TASK_PREP, hud.LIVE_TAG_COLORS)
        for kind in (live.PREP_MEETING, live.PREP_PEOPLE, live.PREP_VALIDATE):
            self.assertIn(kind, live.STEP_KINDS)
        stream, _clock = make()
        task = stream.task(live.TASK_PREP, "prep me for my 2 PM")
        task.step(live.PREP_MEETING, "The meeting, as read now", status=STATUS_OK)
        task.step(live.PREP_PEOPLE, "Who Jarvis looks up", status=STATUS_OK)
        task.step(live.PREP_VALIDATE, "Checking the brief", status=STATUS_OK)
        view = stream.snapshot()[0]
        self.assertEqual((view.kind, view.tag), (live.TASK_PREP, "PREP"))
        self.assertEqual([step.kind for step in view.steps], [live.PREP_MEETING, live.PREP_PEOPLE, live.PREP_VALIDATE])


if __name__ == "__main__":
    unittest.main()
