"""The Live screen and its pop-out in the hologram look (round 2, HOLOGRAM_SPEC 10 / 16.3): live_view's
stage (the strip's readout, the 300 px timeline, the focal picture as a viewfinder with its exact text),
the pop-out's header and body, and what stays as it was - nothing written to disk or logged, other
people's text always marked. Every name, address and text is invented."""

from __future__ import annotations

import builtins
import os
import pathlib
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from PySide6.QtCore import QPoint, QSize
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QWidget

from briefing_reader import holo, hud, live, live_view, ui
from briefing_reader import pictures as live_pictures
from tests.ui_fakes import fonts, settle, tiny_jpeg

_app: QApplication | None = None
AT = datetime(2026, 10, 12, 13, 12, 5)
QUERY = "Example Museum opening hours Saturday"
HITS = (("Visit | Example Museum", "https://example.org/visit", "Saturday 10:00 - 18:00."),
        ("Example Museum - Example Guide", "https://example.net/museums/example-museum", "Opens 10 am."))
MARKER = "LIVESCREEN-zq81"


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_live_screen", "-platform", "offscreen"])
    fonts()


class Clock:
    def __init__(self) -> None:
        self.t = 5000.0

    def __call__(self) -> float:
        return self.t


class StageCase(unittest.TestCase):
    """A LiveStage around its own LiveLog (thumbnails as on the Live screen), fed from a stream."""

    def setUp(self) -> None:
        self.clock = Clock()
        self.stream = live.LiveStream(clock=self.clock, wall=lambda: AT)
        self.version = 0
        self.log = hud.LiveLog(clock=self.clock, thumb=live_view.THUMB)
        self.stage = live_view.LiveStage(self.log)
        self.host = QWidget()
        self.host.resize(1040, 640)
        self.stage.setParent(self.host)
        self.stage.resize(1040, 640)
        self.addCleanup(self.host.deleteLater)
        self.host.show()
        settle()

    def apply(self) -> None:
        changes = self.stream.changes(self.version)
        self.version = changes.version
        self.log.apply(changes)
        settle(4)

    def ask(self, *, search: bool = True) -> tuple[live.LiveTask, live.LiveStep, live.LiveStep]:
        task = self.stream.task(live.TASK_ASK, "Example Museum, Saturday hours")
        request = task.step(live.ASK_REQUEST, "Your request", status=live.STATUS_OK,
                            fields=[("You typed", f"When does the Example Museum open Saturday? {MARKER}")])
        step = task.step(live.WEB_SEARCH, "Web search 1", fields=[("Query (exact)", QUERY, True)])
        if search:
            step.picture(live_pictures.search_picture(QUERY, HITS))
            step.block("Results as the search gave them", f"Visit | Example Museum {MARKER}", untrusted=True)
        return task, request, step

    def action(self, *, seconds: float = 10.0) -> tuple[live.LiveTask, live.LiveStep, live.LiveStep]:
        task = self.stream.task(live.TASK_ACTION, "Email work: Budget review")
        task.step(live.ACTION_APPROVED, "You approved", status=live.STATUS_OK, fields=[("Account", "work")])
        payload = task.step(live.ACTION_PAYLOAD, "Will send exactly this", status_text="PREVIEW",
                            fields=[("To", "ana@example.edu"), ("Subject", "Re: Budget review")])
        payload.block("Message", "Hi Ana,\nFriday works.", start_open=True)
        countdown = task.step(live.ACTION_COUNTDOWN, "Undo countdown", status_text="SENDING",
                              deadline=self.clock.t + seconds)
        return task, payload, countdown


class LayoutTests(StageCase):
    def test_the_strip_the_timeline_and_the_focal_column(self) -> None:
        stage = self.stage
        self.assertEqual(stage.timeline.width(), live_view.TIMELINE_WIDTH)
        self.assertEqual(self.log.width(), live_view.TIMELINE_WIDTH)
        self.assertEqual(stage.strip.height(), live_view.STRIP_HEIGHT + 10)
        for widget in (stage.readout, stage.pop_button, stage.clear_button):
            self.assertTrue(widget.isVisible())
            self.assertTrue(stage.strip.isAncestorOf(widget))
        self.assertFalse(stage.follow_button.isVisible())
        self.assertEqual(stage.pop_button.variant(), holo.ICON)          # Pop out: an icon
        self.assertEqual(stage.clear_button.variant(), holo.GHOST)       # Clear: ghost sm
        self.assertEqual(stage.clear_button.text(), "Clear")
        # Under the strip: the timeline at the left, the focal column beside it.
        strip_bottom = stage.strip.mapTo(stage, QPoint(0, stage.strip.height())).y()
        self.assertGreaterEqual(self.log.mapTo(stage, QPoint(0, 0)).y(), strip_bottom)
        self.assertGreater(stage.focal.mapTo(stage, QPoint(0, 0)).x(),
                           self.log.mapTo(stage, QPoint(self.log.width(), 0)).x())
        # Nothing yet: IDLE and the quiet empty state.
        self.assertEqual(stage.readout.text(), "IDLE")
        self.assertEqual(stage.focal.frame.state(), live_view.EMPTY)
        self.assertEqual(stage.focal.frame.accessibleName(), live_view.EMPTY_TEXT)
        self.assertTrue(stage.focal.exact.toggle.isHidden())

    def test_the_focal_frame_is_at_most_640_by_400(self) -> None:
        self.ask()
        self.apply()
        frame = self.stage.focal.frame
        self.assertEqual(frame.state(), live_view.READY)
        self.assertLessEqual(frame.frame_rect().width(), live_view.FOCAL_MAX_W)
        self.assertLessEqual(frame.height(), live_view.FOCAL_MAX_H)
        self.stage.resize(1500, 900)
        self.host.resize(1500, 900)
        settle(4)
        self.assertEqual(frame.frame_rect().width(), live_view.FOCAL_MAX_W)
        self.assertLessEqual(frame.height(), live_view.FOCAL_MAX_H)
        self.assertFalse(self.stage.grab().toImage().isNull())

    def test_the_timeline_draws_pictures_as_thumbnails(self) -> None:
        _task, _request, step = self.ask()
        self.apply()
        widget = self.log.step_widget(step.id)
        self.assertTrue(widget.is_open())                       # running: its details open
        picture = widget.picture_widget()
        self.assertIsNotNone(picture)
        w, h = picture._thumb(picture.width())
        self.assertLessEqual(w, live_view.THUMB[0])
        self.assertLessEqual(h, live_view.THUMB[1])
        self.assertFalse(picture.grab().toImage().isNull())


class ReadoutTests(unittest.TestCase):
    def test_idle_working_and_counting_down(self) -> None:
        clock = Clock()
        readout = live_view.LiveReadout(clock=clock)
        self.addCleanup(readout.deleteLater)
        readout.show()
        settle()
        self.assertEqual((readout.text(), readout.color(), readout.parts()), ("IDLE", holo.INK_4, ("IDLE", "")))
        readout.set_state(hud.LIVE_WORKING, since=clock.t - 12.4)
        self.assertEqual(readout.text(), "WORKING 12 s")              # hud.LiveStatus's texts, unchanged
        self.assertEqual(readout.parts(), ("WORKING", "12 s"))
        self.assertEqual(readout.color(), holo.HOLO)
        readout.set_state(hud.LIVE_COUNTDOWN, "SENDING", deadline=clock.t + 6.2)
        self.assertEqual(readout.text(), "SENDING IN 7 S")
        self.assertEqual(readout.color(), holo.AMBER)
        self.assertIn("sending in 7 s", readout.accessibleName())
        hint = readout.sizeHint().width()
        clock.t += 6
        self.assertEqual(readout.text(), "SENDING IN 1 S")
        self.assertEqual(readout.sizeHint().width(), hint)            # the width never jumps per second
        self.assertLess(readout.minimumSizeHint().width(), hint)
        readout.set_state(hud.LIVE_IDLE)
        self.assertEqual(readout.text(), "IDLE")
        self.assertIsInstance(readout, hud.LiveStatus)


class FocalTests(StageCase):
    def test_the_newest_step_is_focal_and_a_click_picks_another(self) -> None:
        task, request, search = self.ask()
        self.apply()
        focal, frame, stage = self.stage.focal, self.stage.focal.frame, self.stage
        self.assertEqual(frame.step_id(), search.id)                   # follows the newest step
        self.assertEqual(frame.header_texts(), ("WEB SEARCH", "RUNNING"))
        self.assertTrue(self.log.step_widget(search.id).row.is_selected())
        self.assertFalse(focal.pinned())
        search.done(live.STATUS_OK)
        self.apply()
        self.log.step_widget(request.id).row.click()                   # the owner picks the request
        settle()
        self.assertEqual(frame.step_id(), request.id)
        self.assertTrue(focal.pinned())
        self.assertTrue(stage.follow_button.isVisible())
        self.assertTrue(self.log.step_widget(request.id).row.is_selected())
        self.assertFalse(self.log.step_widget(search.id).row.is_selected())
        self.assertEqual(frame.state(), live_view.TEXT)                # no picture: drawn as text
        self.assertEqual(frame.header_texts()[0], f"ASK {hud.MIDDLE_DOT} REQUEST")
        newer = task.step(live.PLANNER_RUN, "Planner run 1")
        self.apply()
        self.assertEqual(frame.step_id(), request.id)                  # the owner's pick holds
        stage.follow_button.click()
        settle()
        self.assertEqual(frame.step_id(), newer.id)                    # Follow live: the newest again
        self.assertFalse(stage.follow_button.isVisible())
        self.log.step_widget(newer.id).row.click()                     # a running step: still following
        settle()
        self.assertFalse(focal.pinned())
        self.log.step_widget(request.id).row.click()
        settle()
        task.finish(live.STATUS_OK)
        self.stream.clear()                                            # the pick goes with its task
        self.apply()
        self.assertFalse(focal.pinned())
        self.assertEqual(frame.state(), live_view.EMPTY)
        self.assertIsNone(frame.step())                                # nothing of it kept
        self.assertTrue(focal.exact.toggle.isHidden())

    def test_reveal_shows_that_tasks_newest_step(self) -> None:
        first, _request, first_search = self.ask()
        first_search.done(live.STATUS_OK)
        first.finish(live.STATUS_OK)
        self.ask(search=False)
        self.apply()
        self.assertTrue(self.log.reveal(first.id))
        settle()
        self.assertEqual(self.stage.focal.frame.step_id(), first_search.id)

    def test_a_countdown_shows_what_will_be_sent_with_amber_brackets(self) -> None:
        self.ask()
        self.apply()
        _request = self.log.task_widget(self.log.task_ids()[-1]).step_widgets()[0]
        _request.row.click()                                          # a pick before the card is approved
        settle()
        self.assertTrue(self.stage.focal.pinned())
        task, payload, countdown = self.action(seconds=9.5)
        self.apply()
        frame = self.stage.focal.frame
        self.assertFalse(self.stage.focal.pinned())                    # the countdown comes into view
        self.assertEqual(frame.step_id(), payload.id)
        self.assertTrue(frame.will_send())
        self.assertEqual(frame.header_texts()[1], "10 S")
        self.assertEqual(self.log.ticking(), hud.LiveLog.TICK_COUNTDOWN_MS)
        self.clock.t += 3.0
        self.log.tick()                                                # the log's timer moves the frame on
        self.assertEqual(frame.header_texts()[1], "7 S")
        self.host.hide()                                               # hidden: nothing ticks
        self.assertEqual(self.log.ticking(), 0)
        self.host.show()
        settle()
        countdown.done(live.STATUS_CANCELLED, status_text="UNDONE")
        task.finish(live.STATUS_CANCELLED, status_text="UNDONE")
        self.apply()
        self.assertFalse(frame.will_send())

    def test_header_labels_by_picture_kind(self) -> None:
        def view(kind: str, picture: object = None) -> SimpleNamespace:
            return SimpleNamespace(kind=kind, picture=picture, blocks=())

        dot = hud.MIDDLE_DOT
        cases = [
            (view(live.MAIL_THREAD, SimpleNamespace(kind=live_pictures.KIND_EMAIL)), False, f"EMAIL {dot} READ"),
            (view(live.ACTION_PAYLOAD, SimpleNamespace(kind=live_pictures.KIND_OUTGOING)), True,
             f"EMAIL {dot} WILL BE SENT"),
            (view(live.ACTION_PAYLOAD, SimpleNamespace(kind=live_pictures.KIND_DAY,
                                                       day=SimpleNamespace(change=live_pictures.CHANGE_MOVE))),
             False, f"CALENDAR {dot} MOVE"),
            (view(live.CALENDAR_READ, SimpleNamespace(kind=live_pictures.KIND_WEEK)), False, f"CALENDAR {dot} READ"),
            (view(live.WEB_SEARCH, SimpleNamespace(kind=live_pictures.KIND_SEARCH)), False, "WEB SEARCH"),
            (view(live.WEB_FETCH, SimpleNamespace(kind=live_pictures.KIND_PAGE,
                                                  page=SimpleNamespace(host="example.org"))),
             False, f"PAGE {dot} EXAMPLE.ORG"),
            (view(live.PLANNER_RUN), False, f"PLANNER {dot} RUN"),
        ]
        for step, will, label in cases:
            with self.subTest(label=label):
                self.assertEqual(live_view.picture_label(step, will), label)


class OthersTextTests(StageCase):
    def test_written_by_other_people_is_never_left_out(self) -> None:
        task, request, search = self.ask()
        thread = task.step(live.MAIL_THREAD, "Read email thread")
        thread.block("Text given to the planner", "From: Ana\nHi", untrusted=True)
        url = "https://example.org/visit"
        fetch = task.step(live.WEB_FETCH, "Page read", status=live.STATUS_OK, fields=[("Address", url, True)])
        fetch.picture(live_pictures.page_ready(url, image=tiny_jpeg(96, 60), width=96, height=60))
        source = task.step(live.WEB_SOURCE, "Source [2]", status=live.STATUS_OK)
        source.picture(live_pictures.page_unavailable("https://example.net/x", "the page did not load"))
        search.done(live.STATUS_OK)
        thread.done(live.STATUS_OK)
        self.apply()
        frame = self.stage.focal.frame
        expected = {search.id: (live_view.OTHERS_MARK, ""), thread.id: (live_view.OTHERS_MARK, ""),
                    fetch.id: (live_view.OTHERS_MARK, live_view.READ_IN_FULL),
                    source.id: (live_view.OTHERS_MARK, live_view.CITED_NOT_READ), request.id: ("", "")}
        for step_id, footer in expected.items():
            with self.subTest(step=step_id):
                self.stage.focal.select(step_id)
                settle()
                self.assertEqual(frame.step_id(), step_id)
                self.assertEqual(frame.footer_texts(), footer)
                self.assertFalse(frame.grab().toImage().isNull())
        self.stage.focal.select(fetch.id)
        self.assertEqual(frame.state(), live_view.READY)
        self.assertEqual(frame.header_texts()[0], f"PAGE {hud.MIDDLE_DOT} EXAMPLE.ORG")
        self.stage.focal.select(source.id)
        self.assertEqual(frame.state(), live_view.UNAVAILABLE)
        self.assertIn("did not load", frame.accessibleName())


class ExactTextTests(StageCase):
    def test_collapsed_then_the_exact_text_marked_when_it_is_other_peoples(self) -> None:
        _task, request, search = self.ask()
        self.apply()
        exact = self.stage.focal.exact
        self.assertTrue(exact.toggle.isVisible())
        self.assertFalse(exact.is_open())                              # collapsed at first
        self.assertFalse(exact.box.isVisible())
        self.assertEqual(exact.toggle.text(), live_view.SHOW_EXACT)
        self.assertEqual(exact.text(), "")                              # nothing built while closed
        exact.toggle.click()
        settle()
        self.assertTrue(exact.box.isVisible())
        self.assertEqual(exact.toggle.text(), live_view.HIDE_EXACT)
        self.assertTrue(exact.mark.isVisible())
        self.assertEqual(exact.mark.text(), "WRITTEN BY OTHER PEOPLE")
        text = exact.text_edit.toPlainText()
        self.assertIn(f"Query (exact): {QUERY}", text)
        self.assertIn(f"Results as the search gave them:\nVisit | Example Museum {MARKER}", text)
        self.assertTrue(exact.text_edit.isReadOnly())
        self.assertTrue(exact.text_edit.tabChangesFocus())
        self.stage.focal.select(request.id)                            # Jarvis's own text: no mark
        settle()
        self.assertTrue(exact.is_open())
        self.assertFalse(exact.mark.isVisible())
        self.assertIn("You typed: When does the Example Museum open Saturday?", exact.text_edit.toPlainText())
        exact.toggle.click()
        settle()
        self.assertFalse(exact.box.isVisible())
        self.assertEqual(exact.toggle.text(), live_view.SHOW_EXACT)
        self.assertEqual(exact.text_edit.toPlainText(), "")             # closed: nothing of it kept
        bare = self.stream.task(live.TASK_ASK, "Bare").step(live.ASK_CHECKS, "Checks")   # nothing exact to show
        self.apply()
        self.stage.focal.select(bare.id)
        self.assertTrue(exact.toggle.isHidden())
        self.assertIn(search.id, [step.view.id for step in self.log.task_widget(_task.id).step_widgets()])

    def test_cut_and_not_kept_blocks_say_so(self) -> None:
        view = SimpleNamespace(fields=(live.Field("Model", "sonnet"),),
                               items=(live.Item("Card - Email", live.STATUS_OK),), items_more=2,
                               blocks=(live.Block("Stdin", "y" * 40, 100, cut=60),
                                       live.Block("Reply", "", 130, kept=False)))
        text = live_view.exact_text(view)
        self.assertIn("Model: sonnet", text)
        self.assertIn("- Card - Email (DONE)", text)
        self.assertIn("+2 more", text)
        self.assertIn("[60 more characters not shown]", text)
        self.assertIn("130 characters - not kept ([live] text = false)", text)


class PopOutTests(unittest.TestCase):
    def test_the_520_pop_out_its_header_picture_and_compact_timeline(self) -> None:
        clock = Clock()
        stream = live.LiveStream(clock=clock, wall=lambda: AT)
        window = ui.LiveWindow()
        self.addCleanup(window.deleteLater)
        window.show()
        settle(4)
        self.assertEqual(window.size(), QSize(520, 680))
        self.assertEqual(window.header.height(), live_view.POP_HEADER_HEIGHT)
        self.assertIs(window.dock_button, window.header.back_button)
        self.assertEqual(window.dock_button.text(), "Bring it back")
        self.assertFalse(window.log.hints())                           # details collapsed
        self.assertEqual(window.log.thumb_size(), live_view.THUMB)
        task = stream.task(live.TASK_ASK, "Pop-out")
        step = task.step(live.WEB_SEARCH, "Web search 1", fields=[("Query (exact)", QUERY, True)])
        step.picture(live_pictures.search_picture(QUERY, HITS))
        window.log.apply(stream.changes(0))
        settle(6)
        frame = window.focal.frame
        self.assertEqual(frame.step_id(), step.id)
        self.assertAlmostEqual(frame.frame_rect().width(), 488, delta=1)   # full width (520 - 2 x 16)
        self.assertLessEqual(frame.height(), int(window.content.height() * live_view.POP_FOCAL_SHARE) + 1)
        self.assertFalse(window.log.step_widget(step.id).is_open())    # the timeline as compact rows
        header = window.focal.mapTo(window, QPoint(0, 0)).y()
        self.assertGreaterEqual(header, window.header.mapTo(window, QPoint(0, window.header.height())).y())
        self.assertGreater(window.log.mapTo(window, QPoint(0, 0)).y(), header)
        seen: list[str] = []
        window.dockRequested.connect(lambda: seen.append("dock"))
        window.clearRequested.connect(lambda: seen.append("clear"))
        window.dock_button.click()
        window.clear_button.click()
        window.header.close_button.click()                              # closing docks too
        self.assertEqual(seen, ["dock", "clear", "dock"])
        window.hide()


class MemoryOnlyTests(StageCase):
    def test_nothing_is_written_to_disk_or_logged(self) -> None:
        """LIVE content lives in memory only: the stage, its frame, its exact text and the pop-out open no
        file, write nothing and log nothing (not a title, not a text, not a picture)."""
        calls: list[str] = []

        def refuse(name: str):
            def call(*args, **kwargs):
                calls.append(name)
                raise AssertionError(f"{name} called")
            return call

        with mock.patch.object(builtins, "open", refuse("open")), \
                mock.patch.object(os, "open", refuse("os.open")), \
                mock.patch.object(pathlib.Path, "write_text", refuse("write_text")), \
                mock.patch.object(pathlib.Path, "write_bytes", refuse("write_bytes")), \
                self.assertNoLogs("briefing_reader", level="DEBUG"):
            task, request, search = self.ask()
            search.done(live.STATUS_OK)
            self.action()
            self.apply()
            self.stage.focal.select(search.id)
            self.stage.focal.exact.set_open(True)
            settle()
            self.stage.grab()
            self.stage.focal.frame.click()                              # shown larger: the log's viewer
            settle()
            viewer = self.log.picture_viewer()
            self.assertIsNotNone(viewer)
            viewer.close()
            self.stage.focal.select(request.id)
            self.stage.grab()
            self.log.clear()
            settle()
        self.assertEqual(calls, [])
        self.assertIsNone(self.stage.focal.frame.step())
        self.assertNotIn(MARKER, repr(self.stage.focal.frame))


class TimelineLookTests(StageCase):
    def test_steps_are_one_line_until_opened_and_marks_have_their_words(self) -> None:
        task = self.stream.task(live.TASK_ASK, "Marks")
        rows = {}
        for status in (live.STATUS_OK, live.STATUS_WARN, live.STATUS_BLOCKED, live.STATUS_FAILED,
                       live.STATUS_CANCELLED):
            rows[status] = task.step(live.ASK_CHECKS, f"A {status} step with a long title that would wrap twice",
                                     status=status, summary="what happened")
        self.apply()
        for status, step in rows.items():
            with self.subTest(status=status):
                row = self.log.step_widget(step.id).row
                closed = row.heightForWidth(row.width())
                self.assertEqual(closed, 36)                            # one line
                self.assertEqual(row.right_text(), row.elapsed() if row.word() == "DONE" else row.word())
                self.assertIn(row.word(), row.accessibleName())         # never colour alone
                self.assertEqual(hud.live_mark_color(status), {
                    live.STATUS_OK: holo.OK, live.STATUS_WARN: holo.AMBER, live.STATUS_BLOCKED: holo.ALERT,
                    live.STATUS_FAILED: holo.ALERT, live.STATUS_CANCELLED: holo.INK_4}[status])
        header = self.log.task_widget(task.id).header
        self.assertEqual(header.kicker(), f"ASK {hud.MIDDLE_DOT} 1:12 PM")
        self.assertTrue(header.meta().startswith("1:12:05 PM - 5 steps"))
        compact = self.stream.task(live.TASK_BRIEFING, "AM briefing from Notion")
        compact.step(live.BRIEFING_FETCH, "Read the briefing page", status=live.STATUS_OK)
        compact.finish(live.STATUS_OK)
        self.apply()
        compact_header = self.log.task_widget(compact.id).header
        self.assertTrue(compact_header.one_line())
        self.assertEqual(compact_header.heightForWidth(compact_header.width()), 36)
        image = self.log.grab().toImage()
        self.assertFalse(image.isNull())
        self.assertNotEqual(QColor(image.pixel(5, 5)).alpha(), -1)


if __name__ == "__main__":
    unittest.main()
