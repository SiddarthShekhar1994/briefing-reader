"""Tests for the pictures in briefing_reader.live's stream (Qt-free): LiveStep.picture, the payload's
picture, [live] pictures / text, finished and removed tasks, the picture memory caps (one picture,
one task, all: the oldest finished tasks' pictures go first; RUNNING and pinned tasks keep theirs),
the bytes after Clear / close / trims, the change feed, and a burst from many threads. Every value
is invented.
"""

from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta, timezone

from briefing_reader import live, pictures
from briefing_reader.gmail import MailView
from briefing_reader.live import LiveLimits, LiveStream, Payload

PDT = timezone(timedelta(hours=-7))
WALL = datetime(2026, 10, 9, 13, 30, tzinfo=PDT)
URL = "https://www.example.org/visit"


def make(**kwargs) -> LiveStream:
    kwargs.setdefault("wall", lambda: WALL)
    return LiveStream(**kwargs)


def page(size: int = 1000, url: str = URL) -> pictures.Picture:
    return pictures.page_ready(url, image=b"x" * size, width=960, height=600)


def outgoing(body: str = "Hi Ana,\nThursday works.\n") -> pictures.Picture:
    view = MailView("me@example.edu", "ana@example.edu", "", "Re: Lunch", "", "", "", body)
    return pictures.outgoing_picture(view, account="work")


def week() -> pictures.Picture:
    return pictures.week_picture({"work": []}, WALL - timedelta(days=1), WALL + timedelta(days=7), now=WALL)


def step_view(stream: LiveStream, step: live.LiveStep) -> live.StepView:
    view = step.view()
    assert view is not None
    return view


class PictureTests(unittest.TestCase):
    def test_a_picture_is_kept_and_replaced(self) -> None:
        stream = make()
        step = stream.task(live.TASK_ASK, "Ask").step(live.CALENDAR_READ, "Calendar")
        first = week()
        step.picture(first)
        self.assertIs(step_view(stream, step).picture, first)
        self.assertEqual(stream.picture_bytes(), first.cost)
        second = outgoing()
        step.picture(second)
        self.assertIs(step_view(stream, step).picture, second)
        self.assertEqual(stream.picture_bytes(), second.cost)
        self.assertTrue(stream.task(live.TASK_ASK, "x").pictures)

    def test_only_pictures_are_taken(self) -> None:
        stream = make()
        step = stream.task(live.TASK_ASK, "Ask").step(live.CALENDAR_READ, "Calendar")
        for value in ("a picture", 3, object(), None):
            step.picture(value)
        self.assertIsNone(step_view(stream, step).picture)
        live.NO_STEP.picture(week())
        self.assertEqual(stream.picture_bytes(), 0)

    def test_pictures_off_refuses_the_drawn_ones_only(self) -> None:
        stream = make(pictures=False)
        self.assertFalse(stream.pictures)
        step = stream.task(live.TASK_WEB, "Web").step(live.WEB_FETCH, "Read a page")
        step.picture(outgoing())
        self.assertIsNone(step_view(stream, step).picture)
        search = stream.task(live.TASK_WEB, "Web").step(live.WEB_SEARCH, "Web search 1")
        search.picture(pictures.search_picture("museum hours", [("Visit", URL)]))   # drawn by Jarvis too
        self.assertIsNone(step_view(stream, search).picture)
        step.picture(page())
        self.assertEqual(step_view(stream, step).picture.kind, pictures.KIND_PAGE)

    def test_a_search_picture_is_kept_and_text_off_drops_only_its_snippets(self) -> None:
        for keep_text, snippet in ((True, "Open daily."), (False, "")):
            with self.subTest(keep_text=keep_text):
                stream = make(keep_text=keep_text)
                step = stream.task(live.TASK_WEB, "Web").step(live.WEB_SEARCH, "Web search 1")
                step.picture(pictures.search_picture("museum hours", [("Visit", URL, "Open daily.")]))
                shown = step_view(stream, step).picture
                hit = shown.search.hits[0]
                self.assertEqual((shown.kind, shown.search.query, hit.title, hit.snippet),
                                 (pictures.KIND_SEARCH, "museum hours", "Visit", snippet))
                self.assertTrue(step_view(stream, step).open_hint)
                self.assertEqual(stream.picture_bytes(), shown.cost)

    def test_text_off_strips_mail_bodies_and_refuses_page_pictures(self) -> None:
        stream = make(keep_text=False)
        task = stream.task(live.TASK_ACTION, "Email")
        mail_step = task.step(live.ACTION_PAYLOAD, "Will send exactly this")
        mail_step.picture(outgoing(body="secret plan"))
        shown = step_view(stream, mail_step).picture
        self.assertEqual((shown.mail.body, shown.mail.body_kept, shown.mail.body_chars), ("", False, 11))
        page_step = task.step(live.WEB_FETCH, "Read a page")
        page_step.picture(page())
        self.assertIsNone(step_view(stream, page_step).picture)

    def test_a_picture_opens_the_step(self) -> None:
        self.assertFalse(live.open_hint(live.CALENDAR_READ, live.STATUS_OK))
        self.assertTrue(live.open_hint(live.CALENDAR_READ, live.STATUS_OK, picture=week()))
        self.assertFalse(live.open_hint(live.CALENDAR_READ, live.STATUS_OK, picture=week().dropped("x")))
        stream = make()
        step = stream.task(live.TASK_ASK, "Ask").step(live.CALENDAR_READ, "Calendar", status=live.STATUS_OK)
        self.assertFalse(step_view(stream, step).open_hint)
        step.picture(week())
        self.assertTrue(step_view(stream, step).open_hint)

    def test_a_payloads_picture_comes_with_show(self) -> None:
        stream = make()
        step = stream.task(live.TASK_ACTION, "Email").step(live.ACTION_PAYLOAD, "Will send")
        picture = outgoing()
        self.assertEqual(step.show(Payload("Will send exactly this", (live.Field("To", "ana@example.edu"),),
                                           "Message", "Hi", picture=picture)), ())
        self.assertIs(step_view(stream, step).picture, picture)
        again = outgoing()
        changed = step.show(Payload("Will send exactly this", (live.Field("To", "ana@example.edu"),), "Message", "Hi",
                                    picture=again))
        self.assertEqual(changed, ())   # the picture is not a "changed" label
        self.assertIs(step_view(stream, step).picture, again)
        step.show(Payload("Will send exactly this", (live.Field("To", "ana@example.edu"),), "Message", "Hi"))
        self.assertIs(step_view(stream, step).picture, again)   # no picture: the old one stays

    def test_a_finished_task_takes_only_a_late_picture_of_the_same_kind(self) -> None:
        stream = make()
        task = stream.task(live.TASK_WEB, "Web")
        waiting = task.step(live.WEB_FETCH, "Read a page")
        waiting.picture(pictures.page_pending(URL))
        other = task.step(live.WEB_FETCH, "Read another page")
        task.finish(live.STATUS_OK)
        other.picture(page())
        self.assertIsNone(step_view(stream, other).picture)
        waiting.picture(week())
        self.assertEqual(step_view(stream, waiting).picture.state, live.PICTURE_PENDING)
        ready = page()
        waiting.picture(ready)
        self.assertIs(step_view(stream, waiting).picture, ready)
        waiting.picture(page())   # no longer waiting
        self.assertIs(step_view(stream, waiting).picture, ready)

    def test_removed_or_closed_is_a_no_op(self) -> None:
        stream = make()
        task = stream.task(live.TASK_ASK, "Ask")
        step = task.step(live.CALENDAR_READ, "Calendar")
        task.finish(live.STATUS_OK)
        stream.clear()
        step.picture(week())
        self.assertEqual(stream.picture_bytes(), 0)
        closed = make()
        step = closed.task(live.TASK_ASK, "Ask").step(live.CALENDAR_READ, "Calendar")
        closed.close()
        step.picture(week())
        self.assertEqual(closed.picture_bytes(), 0)

    def test_the_change_feed_delivers_the_picture(self) -> None:
        stream = make()
        step = stream.task(live.TASK_ASK, "Ask").step(live.CALENDAR_READ, "Calendar")
        version = stream.changes(0).version
        heard = []
        stream.set_listener(lambda: heard.append(1))
        picture = week()
        step.picture(picture)
        self.assertEqual(heard, [1])
        changes = stream.changes(version)
        (task,) = changes.tasks
        self.assertIs(task.steps[0].picture, picture)
        self.assertNotIn("picture", repr(task.steps[0]).casefold())


class CapTests(unittest.TestCase):
    def test_one_picture_too_big(self) -> None:
        stream = make(limits=LiveLimits(max_picture_bytes=5_000))
        step = stream.task(live.TASK_WEB, "Web").step(live.WEB_FETCH, "Read a page")
        step.picture(page(10_000))
        shown = step_view(stream, step).picture
        self.assertEqual((shown.state, shown.note), (live.PICTURE_DROPPED, "Picture not kept: " + live.PICTURE_TOO_BIG))
        self.assertEqual(stream.picture_bytes(), shown.cost)

    def test_one_task_over_its_share(self) -> None:
        stream = make(limits=LiveLimits(max_picture_bytes=10_000, max_task_picture_bytes=15_000))
        task = stream.task(live.TASK_WEB, "Web")
        first, second = task.step(live.WEB_FETCH, "One"), task.step(live.WEB_FETCH, "Two")
        first.picture(page(8_000))
        second.picture(page(8_000))
        self.assertEqual(step_view(stream, first).picture.state, live.PICTURE_READY)
        self.assertEqual(step_view(stream, second).picture.note, "Picture not kept: " + live.PICTURE_TASK_CAP)
        first.picture(page(9_000))   # replacing its own picture is counted without the old one
        self.assertEqual(step_view(stream, first).picture.state, live.PICTURE_READY)

    def test_the_total_drops_the_oldest_finished_tasks_pictures_first(self) -> None:
        stream = make(limits=LiveLimits(max_picture_bytes=10_000, max_task_picture_bytes=40_000,
                                        max_total_picture_bytes=25_000))
        old = stream.task(live.TASK_WEB, "Old")
        old_step = old.step(live.WEB_FETCH, "One")
        old_step.picture(page(9_000))
        old.finish(live.STATUS_OK)
        newer = stream.task(live.TASK_WEB, "Newer")
        newer_step = newer.step(live.WEB_FETCH, "One")
        newer_step.picture(page(9_000))
        newer.finish(live.STATUS_OK)
        running = stream.task(live.TASK_WEB, "Running")
        first = running.step(live.WEB_FETCH, "One")
        first.picture(page(9_000))   # 27,000+ > 25,000: the oldest finished task's picture goes
        self.assertEqual(step_view(stream, old_step).picture.note, "Picture not kept: " + live.PICTURE_MEMORY)
        self.assertEqual(step_view(stream, newer_step).picture.state, live.PICTURE_READY)
        self.assertEqual(step_view(stream, first).picture.state, live.PICTURE_READY)
        self.assertLessEqual(stream.picture_bytes(), 25_000)
        second = running.step(live.WEB_FETCH, "Two")
        second.picture(page(9_000))
        self.assertEqual(step_view(stream, newer_step).picture.state, live.PICTURE_DROPPED)
        self.assertEqual(step_view(stream, second).picture.state, live.PICTURE_READY)
        third = running.step(live.WEB_FETCH, "Three")
        third.picture(page(9_000))   # nothing finished left to drop: the new one is not kept
        self.assertEqual(step_view(stream, third).picture.note, "Picture not kept: " + live.PICTURE_MEMORY)
        self.assertEqual(step_view(stream, first).picture.state, live.PICTURE_READY)   # RUNNING keeps its own

    def test_running_and_pinned_tasks_keep_theirs(self) -> None:
        stream = make(limits=LiveLimits(max_picture_bytes=10_000, max_total_picture_bytes=15_000))
        pinned = stream.task(live.TASK_BACKGROUND, "Background reads", key="background")
        pinned_step = pinned.step(live.AGENDA_READ, "Agenda", status=live.STATUS_OK)
        pinned_step.picture(page(6_000))
        other = stream.task(live.TASK_WEB, "Running")
        other_step = other.step(live.WEB_FETCH, "One")
        other_step.picture(page(6_000))
        late = stream.task(live.TASK_WEB, "Late").step(live.WEB_FETCH, "One")
        late.picture(page(6_000))
        self.assertEqual(step_view(stream, pinned_step).picture.state, live.PICTURE_READY)
        self.assertEqual(step_view(stream, other_step).picture.state, live.PICTURE_READY)
        self.assertEqual(step_view(stream, late).picture.state, live.PICTURE_DROPPED)

    def test_bytes_after_clear_close_and_trims(self) -> None:
        stream = make(limits=LiveLimits(max_tasks=2))
        tasks = []
        for number in range(3):
            task = stream.task(live.TASK_WEB, f"Web {number}")
            task.step(live.WEB_FETCH, "One").picture(page(1_000))
            task.finish(live.STATUS_OK)
            tasks.append(task)
        expected = sum(page(1_000).cost for _ in range(2))
        self.assertEqual(stream.picture_bytes(), expected)   # the oldest task went with its picture
        running = stream.task(live.TASK_WEB, "Running")
        running.step(live.WEB_FETCH, "One").picture(page(2_000))
        stream.clear()
        self.assertEqual(stream.picture_bytes(), page(2_000).cost)
        background = stream.task(live.TASK_BACKGROUND, "Background reads", key="background")
        background.step(live.AGENDA_READ, "Agenda", status=live.STATUS_OK).picture(page(500))
        background.step(live.AGENDA_READ, "Again", key="agenda").picture(page(500))
        background.step(live.AGENDA_READ, "Again", key="agenda").picture(page(700))   # replaced in place
        self.assertEqual(stream.picture_bytes(), page(2_000).cost + page(500).cost + page(700).cost)
        stream.clear()   # the rolling task's finished steps go with their pictures
        self.assertEqual(stream.picture_bytes(), page(2_000).cost + page(700).cost)
        stream.close()
        self.assertEqual(stream.picture_bytes(), 0)

    def test_a_burst_from_many_threads(self) -> None:
        stream = make(limits=LiveLimits(max_picture_bytes=5_000, max_task_picture_bytes=40_000,
                                        max_total_picture_bytes=60_000))
        errors: list[BaseException] = []

        def work(number: int) -> None:
            try:
                for round_ in range(20):
                    task = stream.task(live.TASK_WEB, f"Web {number}.{round_}")
                    step = task.step(live.WEB_FETCH, "One")
                    step.picture(page(1_000 + 200 * (round_ % 7)))
                    step.picture(page(9_000))   # too big: not kept
                    step.picture(week())
                    if round_ % 3:
                        task.finish(live.STATUS_OK)
                    if round_ % 5 == 0:
                        stream.clear()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(number,)) for number in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        counted = sum(step.picture.cost for task in stream.snapshot() for step in task.steps
                      if step.picture is not None)
        self.assertEqual(stream.picture_bytes(), counted)
        self.assertLessEqual(stream.picture_bytes(), 60_000)


if __name__ == "__main__":
    unittest.main()
