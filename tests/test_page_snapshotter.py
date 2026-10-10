"""Tests for briefing_reader.snapshot_engine.PageSnapshotter: the LIVE side of page snapshots
(requests from the research's thread, one shot at a time on the GUI thread, the run's budget,
end / idle / cancel / Clear / shutdown) with a fake camera - no QtWebEngine, no page, no network.

Qt runs offscreen. Every address is invented.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QTimer, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from briefing_reader import live, pictures, snapshot_engine, snapshots
from briefing_reader.snapshots import ShotResult
from tests.ui_fakes import settle, wait_for

_app: QApplication | None = None
PAGE = "https://www.example.org/report"


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_page_snapshotter", "-platform", "offscreen"])


def tiny_jpeg() -> bytes:
    image = QImage(8, 5, QImage.Format.Format_RGB32)
    image.fill(QColor("#2a9d8f"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "JPG", 80)
    buffer.close()
    return bytes(data.data())


class FakeCamera(QObject):
    """PageCamera's surface: records shots; the test decides when and how each one ends."""

    finished = Signal(object)

    def __init__(self, parent: QObject | None = None, *, started: bool = False) -> None:
        super().__init__(parent)
        self.shots: list[tuple[str, str | None, str, int, float]] = []
        self.running = False
        self.is_started = started
        self.aborted = 0
        self.closed = 0

    def shoot(self, url: str, html: str | None = None, *, final_url: str = "") -> None:
        assert not self.running, "one shot at a time"
        self.shots.append((url, html, final_url, threading.get_ident(), time.monotonic()))
        self.running = True
        self.is_started = True

    def busy(self) -> bool:
        return self.running

    def started(self) -> bool:
        return self.is_started

    def abort(self) -> None:
        self.aborted += 1
        if self.running:
            self.running = False
            QTimer.singleShot(0, lambda: self.finished.emit(ShotResult(False, snapshots.R_CANCELLED)))

    def close_view(self) -> None:
        self.abort()
        self.closed += 1

    def complete(self, ok: bool = True, reason: str = snapshots.R_TIMEOUT, final_url: str = "") -> None:
        assert self.running
        self.running = False
        if ok:
            self.finished.emit(ShotResult(True, image=tiny_jpeg(), width=8, height=5, final_url=final_url,
                                          took_ms=1200, blocked=2))
        else:
            self.finished.emit(ShotResult(False, reason, took_ms=900))


class SnapshotterCase(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = live.LiveStream()
        self.task = self.stream.task(live.TASK_WEB, "Web research")
        self.cameras: list[FakeCamera] = []
        self.started = False

    def make(self, **kwargs) -> snapshot_engine.PageSnapshotter:
        def factory(parent: QObject) -> FakeCamera:
            camera = FakeCamera(parent, started=self.started)
            self.cameras.append(camera)
            return camera

        kwargs.setdefault("camera_factory", factory)
        snapper = snapshot_engine.PageSnapshotter(_app, **kwargs)
        self.addCleanup(snapper.deleteLater)
        self.addCleanup(snapper.shutdown)
        return snapper

    @property
    def camera(self) -> FakeCamera:
        self.assertTrue(self.cameras, "no camera was made")
        return self.cameras[0]

    def step(self, title: str = "Read page") -> live.LiveStep:
        return self.task.step(live.WEB_FETCH, title)

    def picture(self, step: live.LiveStep) -> pictures.Picture | None:
        view = step.view()
        return view.picture if view is not None else None

    def state(self, step: live.LiveStep) -> str:
        picture = self.picture(step)
        return picture.state if picture is not None else ""

    def shots(self, count: int, timeout: float = 3.0) -> bool:
        return wait_for(lambda: bool(self.cameras) and len(self.camera.shots) >= count, timeout)


class RequestTests(SnapshotterCase):
    def test_a_worker_thread_request_shows_pending_at_once_and_shoots_on_the_gui_thread(self) -> None:
        snapper = self.make()
        step = self.step()
        worker = threading.Thread(target=lambda: snapper.request(step, PAGE, task_id=self.task.id))
        worker.start()
        worker.join(5)
        self.assertEqual(self.state(step), live.PICTURE_PENDING)
        self.assertEqual(self.picture(step).note, snapshots.NOTE_OPENING)
        self.assertTrue(self.shots(1))
        url, html, _final, thread, _at = self.camera.shots[0]
        self.assertEqual(url, PAGE)
        self.assertIsNone(html)
        self.assertEqual(thread, threading.main_thread().ident)
        self.camera.complete()
        self.assertTrue(wait_for(lambda: self.state(step) == live.PICTURE_READY))
        page = self.picture(step).page
        self.assertEqual(page.host, "www.example.org")
        self.assertEqual((page.width, page.height, page.blocked), (8, 5, 2))
        self.assertEqual(page.image[:2], b"\xff\xd8")
        self.assertEqual(snapper.counts(), {"requested": 1, "taken": 1, "unavailable": 0, "refused": 0})

    def test_one_shot_at_a_time_in_order(self) -> None:
        self.started = True
        snapper = self.make()
        steps = [self.step(f"Read {n}") for n in range(3)]
        for index, step in enumerate(steps):
            snapper.request(step, f"https://www.example.org/{index}", task_id=self.task.id)
        self.assertTrue(self.shots(1))
        settle(5)
        self.assertEqual(len(self.camera.shots), 1)
        for index in range(3):
            self.assertTrue(self.shots(index + 1))
            self.assertEqual(self.camera.shots[index][0], f"https://www.example.org/{index}")
            self.camera.complete(ok=index != 1, reason=snapshots.R_TIMEOUT)
        self.assertTrue(wait_for(lambda: all(self.state(step) != live.PICTURE_PENDING for step in steps)))
        self.assertEqual([self.state(step) for step in steps],
                         [live.PICTURE_READY, live.PICTURE_UNAVAILABLE, live.PICTURE_READY])
        self.assertEqual(self.picture(steps[1]).note, pictures.UNAVAILABLE_PREFIX + snapshots.R_TIMEOUT)

    def test_start_delay_only_before_the_engine_has_started(self) -> None:
        snapper = self.make()
        first, second = self.step(), self.step("Read 2")
        asked = time.monotonic()
        snapper.request(first, PAGE, task_id=self.task.id)
        settle(5)
        self.assertEqual(self.camera.shots, [])   # "Opening..." is drawn first
        self.assertTrue(self.shots(1))
        self.assertGreaterEqual(self.camera.shots[0][4] - asked, snapshots.START_DELAY_MS / 1000 * 0.8)
        self.camera.complete()
        snapper.request(second, "https://www.example.org/two", task_id=self.task.id)
        asked = time.monotonic()
        self.assertTrue(self.shots(2))
        self.assertLess(self.camera.shots[1][4] - asked, snapshots.START_DELAY_MS / 1000)

    def test_a_first_job_dropped_while_it_waits_leaves_the_delay_to_the_next_one(self) -> None:
        for how in ("cancel", "clear"):
            with self.subTest(how=how):
                self.cameras.clear()
                snapper = self.make()
                run = self.stream.task(live.TASK_WEB, f"Web research {how}")
                first = run.step(live.WEB_FETCH, "Read page")
                snapper.request(first, PAGE, task_id=run.id)
                settle(5)
                if how == "cancel":
                    snapper.cancel_task(run.id)
                else:
                    run.finish(live.STATUS_OK)
                    self.stream.clear()   # the finished task goes, with its step
                    snapper.drop_cleared()
                wait_for(lambda: False, snapshots.START_DELAY_MS / 1000 * 1.6)   # the delay's timer has run
                self.assertEqual(self.camera.shots, [])
                self.assertFalse(self.camera.started())
                task = self.stream.task(live.TASK_WEB, "Web research 2")
                step = task.step(live.WEB_FETCH, "Read page 2")
                asked = time.monotonic()
                snapper.request(step, "https://www.example.org/two", task_id=task.id)
                self.assertTrue(self.shots(1))
                # The engine has still not started: "Opening..." is drawn first for this run too.
                self.assertGreaterEqual(self.camera.shots[0][4] - asked, snapshots.START_DELAY_MS / 1000 * 0.8)

    def test_refused_pages_get_an_unavailable_picture_and_no_shot(self) -> None:
        self.started = True
        snapper = self.make()
        cases = {"http://www.example.org/": snapshots.R_NOT_PUBLIC_HTTPS,
                 "https://192.168.1.1/admin": snapshots.R_NOT_PUBLIC_HTTPS,
                 "https://nas.local/": snapshots.R_NOT_PUBLIC_HTTPS,
                 "https://www.google.com/search?q=budget": snapshots.R_SEARCH_PAGE}
        for url, reason in cases.items():
            with self.subTest(url=url):
                step = self.step()
                snapper.request(step, url, task_id=self.task.id)
                self.assertEqual(self.state(step), live.PICTURE_UNAVAILABLE)
                self.assertTrue(self.picture(step).note.startswith(pictures.UNAVAILABLE_PREFIX + reason))
        settle(5)
        self.assertEqual(self.cameras, [])   # no camera, no shot
        self.assertTrue(snapper.idle())
        self.assertEqual(snapper.counts()["refused"], 4)

    def test_at_most_four_per_run_and_never_the_same_page_twice(self) -> None:
        self.started = True
        snapper = self.make()
        steps = [self.step(f"Read {n}") for n in range(6)]
        urls = [f"https://www.example.org/{n}" for n in range(4)] + ["https://www.example.org/0#again",
                                                                      "https://www.example.org/9"]
        for step, url in zip(steps, urls):
            snapper.request(step, url, task_id=self.task.id)
        self.assertEqual([self.state(step) for step in steps[:4]], [live.PICTURE_PENDING] * 4)
        self.assertEqual(self.picture(steps[4]).note, pictures.UNAVAILABLE_PREFIX + snapshots.R_SAME_PAGE)
        self.assertEqual(self.picture(steps[5]).note, pictures.UNAVAILABLE_PREFIX + snapshots.R_OVER_CAP)
        other = self.stream.task(live.TASK_WEB, "Another research")
        later = other.step(live.WEB_FETCH, "Read")
        snapper.request(later, "https://www.example.org/0", task_id=other.id)
        self.assertEqual(self.state(later), live.PICTURE_PENDING)   # another run has its own budget

    def test_a_smaller_cap_from_max_fetches(self) -> None:
        self.started = True
        snapper = self.make(max_per_run=1)
        first, second = self.step(), self.step("Read 2")
        snapper.request(first, PAGE, task_id=self.task.id)
        snapper.request(second, "https://www.example.org/b", task_id=self.task.id)
        self.assertEqual(self.state(first), live.PICTURE_PENDING)
        self.assertEqual(self.picture(second).note,
                         pictures.UNAVAILABLE_PREFIX + "at most 1 page picture per research")   # its own cap
        self.assertEqual(self.make(max_per_run=99)._max_per_run, snapshots.MAX_PER_RUN)

    def test_text_off_refuses_every_page(self) -> None:
        stream = live.LiveStream(keep_text=True)   # the step can show it; the snapshotter has text off
        task = stream.task(live.TASK_WEB, "Web research")
        snapper = self.make(keep_text=False)
        step = task.step(live.WEB_FETCH, "Read")
        snapper.request(step, PAGE, task_id=task.id)
        self.assertEqual(self.picture(step).note, pictures.UNAVAILABLE_PREFIX + snapshots.R_TEXT_OFF)
        settle(5)
        self.assertEqual(self.cameras, [])

    def test_no_live_step_no_page(self) -> None:
        snapper = self.make()
        snapper.request(live.NO_STEP, PAGE, task_id=1)
        settle(5)
        self.assertEqual(self.cameras, [])
        self.assertEqual(snapper.counts()["requested"], 0)
        self.assertTrue(snapper.idle())

    def test_html_for_hands_local_html_and_a_reported_final_address(self) -> None:
        self.started = True
        pages = {PAGE: "<p>local</p>", "https://www.example.org/moved": ("<p>moved</p>", "https://www.example.net/b")}
        snapper = self.make(html_for=pages.get)
        first, second = self.step(), self.step("Read 2")
        snapper.request(first, PAGE, task_id=self.task.id)
        snapper.request(second, "https://www.example.org/moved", task_id=self.task.id)
        self.assertTrue(self.shots(1))
        self.assertEqual(self.camera.shots[0][1:3], ("<p>local</p>", ""))
        self.camera.complete(final_url=PAGE)
        self.assertTrue(self.shots(2))
        self.assertEqual(self.camera.shots[1][1:3], ("<p>moved</p>", "https://www.example.net/b"))
        self.camera.complete(final_url="https://www.example.net/b")
        self.assertTrue(wait_for(lambda: self.state(second) == live.PICTURE_READY))
        self.assertEqual(self.picture(first).page.final_host, "")
        self.assertEqual(self.picture(second).page.final_host, "www.example.net")
        self.assertIn("redirected to www.example.net", self.picture(second).note)

    def test_a_camera_that_cannot_be_made(self) -> None:
        def broken(_parent: QObject) -> FakeCamera:
            raise RuntimeError("no engine here")

        snapper = self.make(camera_factory=broken)
        step = self.step()
        snapper.request(step, PAGE, task_id=self.task.id)
        self.assertTrue(wait_for(lambda: self.state(step) == live.PICTURE_UNAVAILABLE))
        self.assertEqual(self.picture(step).note, pictures.UNAVAILABLE_PREFIX + snapshots.R_ENGINE_FAILED)

    def test_logs_name_no_address(self) -> None:
        self.started = True
        snapper = self.make()
        step, refused = self.step(), self.step("Read 2")
        with self.assertLogs("briefing_reader.snapshot_engine", level="DEBUG") as logs:
            snapper.request(refused, "https://nas.local/secret-path", task_id=self.task.id)
            snapper.request(step, PAGE, task_id=self.task.id)
            self.assertTrue(self.shots(1))
            self.camera.complete()
            snapper.end_run(self.task.id)
            self.assertTrue(wait_for(snapper.idle))
        text = "\n".join(logs.output)
        self.assertIn("Page snapshot: unavailable (refused)", text)
        self.assertIn("Page snapshot: taken in 1.2 s (1 of 1 for this research, 2 requests blocked)", text)
        self.assertIn("Page snapshots: engine stopped", text)
        for secret in ("example", "nas.local", "secret-path", "report", "https"):
            self.assertNotIn(secret, text)


class RunEndTests(SnapshotterCase):
    def test_end_run_closes_the_view_once_the_queue_is_done(self) -> None:
        self.started = True
        snapper = self.make()
        step = self.step()
        snapper.request(step, PAGE, task_id=self.task.id)
        self.assertTrue(self.shots(1))
        snapper.end_run(self.task.id)
        settle(5)
        self.assertEqual(self.camera.closed, 0)   # still shooting
        self.assertFalse(snapper.idle())
        self.camera.complete()
        self.assertTrue(wait_for(lambda: self.camera.closed == 1))
        self.assertTrue(wait_for(snapper.idle))

    def test_a_run_that_has_not_ended_keeps_the_view_until_the_idle_stop(self) -> None:
        self.started = True
        with mock.patch.object(snapshots, "IDLE_STOP_S", 0.2):
            snapper = self.make()
        step = self.step()
        snapper.request(step, PAGE, task_id=self.task.id)
        self.assertTrue(self.shots(1))
        self.camera.complete()
        self.assertTrue(wait_for(lambda: self.state(step) == live.PICTURE_READY))
        settle(5)
        self.assertEqual(self.camera.closed, 0)
        self.assertFalse(snapper.idle())   # the idle timer runs
        self.assertTrue(wait_for(lambda: self.camera.closed == 1, 3.0))
        self.assertTrue(snapper.idle())

    def test_a_result_for_a_finished_task_still_lands(self) -> None:
        self.started = True
        snapper = self.make()
        step = self.step()
        snapper.request(step, PAGE, task_id=self.task.id)
        self.assertTrue(self.shots(1))
        step.done(live.STATUS_OK)
        self.task.finish(live.STATUS_OK)
        snapper.end_run(self.task.id)
        self.camera.complete()
        self.assertTrue(wait_for(lambda: self.state(step) == live.PICTURE_READY))

    def test_clear_mid_run_skips_the_queued_page(self) -> None:
        self.started = True
        snapper = self.make()
        first, second = self.step(), self.step("Read 2")
        snapper.request(first, PAGE, task_id=self.task.id)
        snapper.request(second, "https://www.example.org/two", task_id=self.task.id)
        self.assertTrue(self.shots(1))
        self.task.finish(live.STATUS_OK)
        snapper.end_run(self.task.id)
        self.stream.clear()   # the finished task goes, with its steps
        self.assertIsNone(second.view())
        self.camera.complete()
        self.assertTrue(wait_for(lambda: self.camera.closed == 1))
        self.assertEqual(len(self.camera.shots), 1)   # the cleared step's page was never opened
        self.assertTrue(wait_for(snapper.idle))

    def test_drop_cleared_stops_the_shot_of_a_step_that_went(self) -> None:
        # LIVE's Clear while a finished research's late pictures are still being taken: the running
        # shot of a cleared step stops and its queued pages are never opened.
        self.started = True
        snapper = self.make()
        steps = [self.step(f"Read {n}") for n in range(3)]
        for index, step in enumerate(steps):
            snapper.request(step, f"https://www.example.org/{index}", task_id=self.task.id)
        self.assertTrue(self.shots(1))
        self.task.finish(live.STATUS_OK)
        snapper.end_run(self.task.id)
        self.stream.clear()
        snapper.drop_cleared()
        self.assertEqual(self.camera.aborted, 1)
        self.assertTrue(wait_for(lambda: self.camera.closed == 1))
        self.assertEqual(len(self.camera.shots), 1)
        self.assertTrue(wait_for(snapper.idle))
        self.assertEqual(snapper.counts()["unavailable"], 1)   # the stopped shot; the queued ones just went

    def test_drop_cleared_keeps_what_is_still_shown(self) -> None:
        self.started = True
        snapper = self.make()
        first, second = self.step(), self.step("Read 2")
        snapper.request(first, PAGE, task_id=self.task.id)
        snapper.request(second, "https://www.example.org/two", task_id=self.task.id)
        self.assertTrue(self.shots(1))
        self.stream.clear()          # the research still runs: its task and steps stay
        snapper.drop_cleared()
        self.assertEqual(self.camera.aborted, 0)
        self.camera.complete()
        self.assertTrue(self.shots(2))
        self.camera.complete()
        self.assertTrue(wait_for(lambda: self.state(second) == live.PICTURE_READY))
        snapper.drop_cleared()       # idle: nothing to do, nothing breaks
        snapper.end_run(self.task.id)
        self.assertTrue(wait_for(snapper.idle))

    def test_a_cancelled_run_is_let_go_once_the_view_closes(self) -> None:
        self.started = True
        snapper = self.make()
        step = self.step()
        snapper.request(step, PAGE, task_id=self.task.id)
        self.assertTrue(self.shots(1))
        snapper.cancel_task(self.task.id)
        snapper.end_run(self.task.id)
        self.assertTrue(wait_for(snapper.idle))
        self.assertEqual(snapper._asked, set())              # nothing kept for it but the refusal
        late = self.step("Read late")
        snapper.request(late, "https://www.example.org/late", task_id=self.task.id)
        self.assertEqual(self.picture(late).note, pictures.UNAVAILABLE_PREFIX + snapshots.R_CANCELLED)

    def test_cancel_drops_the_queue_and_aborts_the_running_shot(self) -> None:
        self.started = True
        snapper = self.make()
        steps = [self.step(f"Read {n}") for n in range(3)]
        for index, step in enumerate(steps):
            snapper.request(step, f"https://www.example.org/{index}", task_id=self.task.id)
        self.assertTrue(self.shots(1))
        snapper.cancel_task(self.task.id)
        self.assertEqual(self.camera.aborted, 1)
        self.assertTrue(wait_for(lambda: all(self.state(step) == live.PICTURE_UNAVAILABLE for step in steps)))
        for step in steps:
            self.assertEqual(self.picture(step).note, pictures.UNAVAILABLE_PREFIX + snapshots.R_CANCELLED)
        self.assertTrue(wait_for(lambda: self.camera.closed == 1))
        self.assertEqual(len(self.camera.shots), 1)
        later = self.step("Read late")
        snapper.request(later, "https://www.example.org/late", task_id=self.task.id)
        self.assertEqual(self.picture(later).note, pictures.UNAVAILABLE_PREFIX + snapshots.R_CANCELLED)

    def test_cancel_from_another_thread(self) -> None:
        self.started = True
        snapper = self.make()
        first, second = self.step(), self.step("Read 2")
        snapper.request(first, PAGE, task_id=self.task.id)
        snapper.request(second, "https://www.example.org/two", task_id=self.task.id)
        self.assertTrue(self.shots(1))
        worker = threading.Thread(target=lambda: snapper.cancel_task(self.task.id))
        worker.start()
        worker.join(5)
        self.assertTrue(wait_for(lambda: self.state(first) == live.PICTURE_UNAVAILABLE
                                 and self.state(second) == live.PICTURE_UNAVAILABLE))
        self.assertTrue(wait_for(snapper.idle))

    def test_shutdown_stops_everything_and_refuses_later_requests(self) -> None:
        self.started = True
        snapper = self.make()
        first, second = self.step(), self.step("Read 2")
        snapper.request(first, PAGE, task_id=self.task.id)
        snapper.request(second, "https://www.example.org/two", task_id=self.task.id)
        self.assertTrue(self.shots(1))
        with self.assertLogs("briefing_reader.snapshot_engine", level="INFO") as logs:
            snapper.shutdown()
        self.assertIn("Page snapshots: engine stopped", "\n".join(logs.output))
        self.assertEqual(self.camera.closed, 1)
        for step in (first, second):
            self.assertEqual(self.picture(step).note, pictures.UNAVAILABLE_PREFIX + snapshots.R_SHUTDOWN)
        later = self.step("Read late")
        snapper.request(later, "https://www.example.org/late", task_id=self.task.id)
        self.assertEqual(self.picture(later).note, pictures.UNAVAILABLE_PREFIX + snapshots.R_SHUTDOWN)
        settle(5)
        self.assertEqual(len(self.camera.shots), 1)
        self.assertTrue(snapper.idle())
        snapper.shutdown()   # twice is fine

    def test_a_closed_stream_is_harmless(self) -> None:
        self.started = True
        snapper = self.make()
        step = self.step()
        snapper.request(step, PAGE, task_id=self.task.id)
        self.assertTrue(self.shots(1))
        self.stream.close()
        self.camera.complete()
        snapper.end_run(self.task.id)
        self.assertTrue(wait_for(snapper.idle))

    def test_many_threads_never_raise_and_never_run_two_shots(self) -> None:
        self.started = True
        snapper = self.make()
        tasks = [self.stream.task(live.TASK_WEB, f"Research {n}") for n in range(5)]
        steps = [[task.step(live.WEB_FETCH, f"Read {k}") for k in range(6)] for task in tasks]

        def worker(index: int) -> None:
            for k, step in enumerate(steps[index]):
                snapper.request(step, f"https://www.example.org/{index}/{k % 5}", task_id=tasks[index].id)
            snapper.end_run(tasks[index].id)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        done = 0
        while wait_for(lambda: bool(self.cameras) and self.camera.running, 2.0):
            self.camera.complete()
            done += 1
        self.assertEqual(done, 20)   # 4 per run
        self.assertTrue(wait_for(snapper.idle))
        counts = snapper.counts()
        self.assertEqual(counts["requested"], 30)
        self.assertEqual(counts["taken"], 20)
        self.assertEqual(counts["refused"], 10)


class PageCameraGuardTests(unittest.TestCase):
    """The real PageCamera's refusals that come before the web engine starts (in this process, so
    nothing may start it: each case must end before QtWebEngine is imported)."""

    def shoot(self, url: str, html: str | None = None) -> tuple[ShotResult | None, bool]:
        camera = snapshot_engine.PageCamera(_app)
        self.addCleanup(camera.deleteLater)
        results: list[ShotResult] = []
        camera.finished.connect(results.append)
        camera.shoot(url, html)
        inside = bool(results)
        self.assertTrue(wait_for(lambda: bool(results)))
        self.assertFalse(camera.busy())
        self.assertFalse(camera.has_view())
        return results[0], inside

    def test_a_network_page_on_the_offscreen_platform(self) -> None:
        with mock.patch.object(snapshot_engine, "_start_engine") as start:
            result, inside = self.shoot(PAGE)
        start.assert_not_called()
        self.assertFalse(inside)   # delivered from the event loop, never inside shoot()
        self.assertEqual((result.ok, result.reason), (False, snapshots.R_TEST_RUN))

    def test_the_sandbox_turned_off(self) -> None:
        for env in ({"QTWEBENGINE_DISABLE_SANDBOX": "1"}, {"QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox"}):
            with self.subTest(env=env), mock.patch.dict("os.environ", env), \
                    mock.patch.object(snapshot_engine, "_start_engine") as start:
                result, _inside = self.shoot(PAGE, "<p>local</p>")
            start.assert_not_called()
            self.assertEqual(result.reason, snapshots.R_SANDBOX_OFF)

    def test_settings_that_undo_the_private_window_refuse_the_shot(self) -> None:
        for env in ({"QTWEBENGINE_REMOTE_DEBUGGING": "9222"},
                    {"QTWEBENGINE_CHROMIUM_FLAGS": "--remote-debugging-port=9222"},
                    {"QTWEBENGINE_CHROMIUM_FLAGS": "--ignore-certificate-errors"},
                    {"QTWEBENGINE_CHROMIUM_FLAGS": '"--no-proxy-server"'},
                    {"QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu --"},
                    {"SSLKEYLOGFILE": "C:/example/keys.log"}):
            with self.subTest(env=env), mock.patch.dict("os.environ", env), \
                    mock.patch.object(snapshot_engine, "_start_engine") as start:
                result, _inside = self.shoot(PAGE, "<p>local</p>")
            start.assert_not_called()   # Chromium is never started with them
            self.assertEqual(result.reason, snapshots.R_UNSAFE_FLAGS)

    def test_chromium_never_starts_without_the_network_gate(self) -> None:
        class DeadGate:
            def start(self) -> int:
                return 0

        engine = snapshot_engine._EngineState()
        with mock.patch.object(snapshot_engine, "_ENGINE", engine), \
                mock.patch.object(snapshot_engine, "_test_platform", return_value=False), \
                mock.patch.object(snapshot_engine, "webengine_available", return_value=True), \
                mock.patch.object(snapshot_engine, "_gate_factory", DeadGate), \
                mock.patch.object(snapshot_engine, "_classes") as classes, \
                mock.patch.dict("os.environ", {"QTWEBENGINE_CHROMIUM_FLAGS": "--foo"}):
            import os

            with self.assertLogs("briefing_reader.snapshot_engine", "WARNING") as logs:
                self.assertEqual(snapshot_engine._start_engine(), snapshots.R_ENGINE_FAILED)
            self.assertEqual(logs.output, ["WARNING:briefing_reader.snapshot_engine:Page snapshots: the network gate "
                                           "could not be started"])
            self.assertEqual(os.environ["QTWEBENGINE_CHROMIUM_FLAGS"], "--foo")   # no flags written
            classes.assert_not_called()
            self.assertFalse(engine.imported)

    def test_the_checks_judge_the_inherited_flags_never_jarvis_own(self) -> None:
        class Gate:
            def start(self) -> int:
                return 50123

        import os

        engine = snapshot_engine._EngineState()
        flags = "QTWEBENGINE_CHROMIUM_FLAGS"
        with mock.patch.object(snapshot_engine, "_ENGINE", engine), \
                mock.patch.object(snapshot_engine, "_test_platform", return_value=False), \
                mock.patch.object(snapshot_engine, "webengine_available", return_value=True), \
                mock.patch.object(snapshot_engine, "_gate_factory", Gate), \
                mock.patch.object(snapshot_engine, "_classes"), \
                mock.patch.dict("os.environ", {flags: "--lang=en-US"}), \
                mock.patch("PySide6.QtQuick.QQuickWindow.setGraphicsApi"):
            self.assertIs(snapshot_engine._judged_environ(), os.environ)   # nothing written yet: as it is
            self.assertEqual(snapshot_engine._start_engine(), "")
            merged = os.environ[flags]
            self.assertIn(snapshots.RESOLVER_RULES, snapshots.flag_tokens(merged))
            self.assertEqual(engine.written_flags, merged)
            self.assertEqual(snapshots.engine_flags_problem(os.environ), snapshots.R_UNSAFE_FLAGS)   # his own ...
            judged = snapshot_engine._judged_environ()
            self.assertEqual(judged[flags], "--lang=en-US")                                    # ... never judged
            self.assertEqual(snapshots.sandbox_problem(judged) or snapshots.engine_flags_problem(judged), "")
            self.assertEqual(os.environ[flags], merged)   # the process's own environment is untouched
            # Merged again (a start that failed after writing): from the inherited value, the same flags.
            engine.imported = False
            self.assertEqual(snapshot_engine._start_engine(), "")
            self.assertEqual(os.environ[flags], merged)
            self.assertEqual(snapshot_engine._judged_environ()[flags], "--lang=en-US")
            # A value changed since Jarvis wrote it is judged as it is now.
            os.environ[flags] = '"--no-proxy-server"'
            self.assertIs(snapshot_engine._judged_environ(), os.environ)
            self.assertEqual(snapshots.engine_flags_problem(snapshot_engine._judged_environ()),
                             snapshots.R_UNSAFE_FLAGS)

    def test_a_shot_after_jarvis_wrote_his_flags_is_not_refused_by_them(self) -> None:
        import os

        merged = snapshots.chromium_flags("", gate_port=50123)
        engine = snapshot_engine._EngineState()
        engine.inherited_flags, engine.written_flags = "", merged
        with mock.patch.object(snapshot_engine, "_ENGINE", engine), \
                mock.patch.dict("os.environ", {"QTWEBENGINE_CHROMIUM_FLAGS": merged}), \
                mock.patch.object(snapshot_engine, "_start_engine", return_value=snapshots.R_NO_ENGINE) as start:
            result, _inside = self.shoot(PAGE, "<p>local</p>")
            self.assertEqual(os.environ["QTWEBENGINE_CHROMIUM_FLAGS"], merged)
        start.assert_called_once()
        self.assertEqual(result.reason, snapshots.R_NO_ENGINE)   # past the checks (not "safety settings")

    def test_production_flags_point_chromium_at_the_gate(self) -> None:
        class Gate:
            started = 0

            def start(self) -> int:
                Gate.started += 1
                return 50123

        engine = snapshot_engine._EngineState()
        with mock.patch.object(snapshot_engine, "_ENGINE", engine), \
                mock.patch.object(snapshot_engine, "_test_platform", return_value=False), \
                mock.patch.object(snapshot_engine, "webengine_available", return_value=True), \
                mock.patch.object(snapshot_engine, "_gate_factory", Gate), \
                mock.patch.object(snapshot_engine, "_classes"), \
                mock.patch.dict("os.environ", {"QTWEBENGINE_CHROMIUM_FLAGS": "--proxy-server=127.0.0.1:9"}), \
                mock.patch("PySide6.QtQuick.QQuickWindow.setGraphicsApi"):
            import os

            self.assertEqual(snapshot_engine._start_engine(), "")
            flags = os.environ["QTWEBENGINE_CHROMIUM_FLAGS"].split()
        self.assertEqual(Gate.started, 1)
        self.assertIn("--proxy-server=127.0.0.1:50123", flags)
        self.assertIn("--proxy-bypass-list=<-loopback>", flags)
        self.assertNotIn("--proxy-server=127.0.0.1:9", flags)
        self.assertIn("--force-webrtc-ip-handling-policy=disable_non_proxied_udp", flags)

    def test_no_web_engine(self) -> None:
        with mock.patch.object(snapshot_engine, "_ENGINE", snapshot_engine._EngineState()), \
                mock.patch.object(snapshot_engine, "webengine_available", return_value=False), \
                mock.patch.object(snapshot_engine, "_classes") as classes:
            result, _inside = self.shoot(PAGE, "<p>local</p>")
            again, _inside = self.shoot(PAGE, "<p>local</p>")
        classes.assert_not_called()
        self.assertEqual(result.reason, snapshots.R_NO_ENGINE)
        self.assertEqual(again.reason, snapshots.R_NO_ENGINE)

    def test_a_shot_that_fails_to_start_ends_as_engine_failed(self) -> None:
        with mock.patch.object(snapshot_engine, "_start_engine", side_effect=RuntimeError("boom")):
            result, _inside = self.shoot(PAGE, "<p>local</p>")
        self.assertEqual(result.reason, snapshots.R_ENGINE_FAILED)

    def test_the_camera_never_started_the_engine_in_this_process(self) -> None:
        self.assertNotIn("PySide6.QtWebEngineCore", __import__("sys").modules)


if __name__ == "__main__":
    unittest.main()
