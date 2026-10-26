"""The desktop thread and its work (briefing_reader.desktop.worker and .service; SPEC 3, 4.1 and 6.4; P1.8): one job
at a time in order on the thread named "desktop", nothing new after a stop; a typed request resolved there - each local
route carried out ("open Downloads", "open <app>", "open <https address>", "open <saved site>", "find <words>", and
a later phase's answered "not yet"), anything else the planner's context built from this PC within SPEC 4.1's caps
(names only reach the planner: build_prompt), and nothing at all without a backend. tests.desktop_fakes' fake backend
over a temporary folder stands in for this PC; nothing real is read or opened.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import CancelledError
from datetime import datetime

from briefing_reader.ask import desktop_mode
from briefing_reader.ask.modes import MODE_DESKTOP, ORIGIN_TYPED, ModeRequest
from briefing_reader.desktop import backend as dbk
from briefing_reader.desktop import executor as dx
from briefing_reader.desktop import service as ds
from briefing_reader.desktop import validate as dv
from briefing_reader.desktop.context import SiteItem
from briefing_reader.desktop.worker import WORKER_NAME, DesktopWorker
from tests.desktop_fakes import DOCS, DOWNLOADS, KNOWN, LING, OWNER, desktop_disk

NOW = datetime(2026, 10, 26, 10, 21)


class WorkerTests(unittest.TestCase):
    def test_one_job_at_a_time_in_order_on_the_desktop_thread(self) -> None:
        worker = DesktopWorker()
        self.addCleanup(worker.stop)
        self.assertFalse(worker.started())                                   # no thread before the first job
        seen: list[tuple[int, str]] = []
        running = threading.Event()
        overlap: list[bool] = []

        def job(number: int) -> int:
            overlap.append(running.is_set())
            running.set()
            time.sleep(0.01)
            seen.append((number, threading.current_thread().name))
            running.clear()
            return number * 2

        futures = [worker.submit(job, n) for n in range(5)]
        self.assertEqual([future.result(timeout=5) for future in futures], [0, 2, 4, 6, 8])
        self.assertEqual(seen, [(n, WORKER_NAME) for n in range(5)])
        self.assertEqual(overlap, [False] * 5)                               # never two at once
        self.assertEqual(WORKER_NAME, "desktop")

    def test_an_error_travels_in_the_future_and_the_thread_goes_on(self) -> None:
        worker = DesktopWorker()
        self.addCleanup(worker.stop)

        def boom() -> None:
            raise OSError(5, "no")

        with self.assertRaises(OSError):
            worker.submit(boom).result(timeout=5)
        self.assertEqual(worker.submit(lambda: "still here").result(timeout=5), "still here")

    def test_stop_lets_the_running_job_end_and_cancels_the_rest(self) -> None:
        worker = DesktopWorker()
        gate = threading.Event()
        first = worker.submit(gate.wait, 5)
        second = worker.submit(lambda: "never")
        time.sleep(0.05)
        worker.stop()
        gate.set()
        self.assertTrue(first.result(timeout=5))
        with self.assertRaises(CancelledError):
            second.result(timeout=5)
        late = worker.submit(lambda: "never")
        self.assertTrue(late.cancelled())
        worker.join(5)
        self.assertFalse(worker._thread.is_alive())                         # noqa: SLF001 - the thread ended

    def test_on_thread(self) -> None:
        worker = DesktopWorker()
        self.addCleanup(worker.stop)
        self.assertTrue(worker.submit(worker.on_thread).result(timeout=5))
        self.assertFalse(worker.on_thread())


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.fake = desktop_disk(tmp.name)
        self.made = 0

        def factory() -> object:
            self.made += 1
            return self.fake

        self.service = ds.DesktopService(folders=dbk.ROOT_FOLDERS, extra_folders=(),
                                         excluded=(OWNER + "\\OneDrive\\Desktop\\Projects\\Jarvis",),
                                         backend_factory=factory,
                                         sites=(SiteItem("https://portal.example.edu/home", "Student portal"),))

    def resolve(self, words: str, **kwargs: object) -> ds.Resolved:
        return self.service.resolve(words, run_id="r1", **kwargs)


class LocalRouteTests(ServiceCase):
    def test_open_a_known_folder(self) -> None:
        resolved = self.resolve("open Downloads")
        self.assertEqual(resolved.kind, ds.LOCAL)
        self.assertEqual(resolved.route.kind, dv.OPEN_FOLDER)
        self.assertEqual([result.status for result in resolved.run.results], [dx.DONE])
        self.assertEqual(self.fake.opened, [("open_folder", DOWNLOADS)])
        self.assertEqual(self.made, 1)                                       # the backend made once, here

    def test_open_an_app_by_its_start_menu_name_and_never_a_never_open_one(self) -> None:
        resolved = self.resolve("open calculator")
        self.assertEqual((resolved.kind, resolved.route.kind), (ds.LOCAL, dv.OPEN_APP))
        self.assertEqual(self.fake.opened, [("launch_app", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App")])
        for typed in ("open Terminal", "launch Windows PowerShell", "open Settings"):
            with self.subTest(typed=typed):
                resolved = self.resolve(typed)
                self.assertEqual(resolved.kind, ds.PLANNER)                    # not a local route: never opened
                self.assertNotIn("Terminal", [app.name for app in resolved.context.apps])
        self.assertEqual(len(self.fake.opened), 1)

    def test_open_an_address_and_a_saved_site(self) -> None:
        self.assertEqual(self.resolve("open https://news.example.org/today").kind, ds.LOCAL)
        self.assertEqual(self.resolve("open the student portal").kind, ds.LOCAL)
        self.assertEqual(self.fake.opened, [("open_url", "https://news.example.org/today"),
                                            ("open_url", "https://portal.example.edu/home")])
        resolved = self.resolve("open http://news.example.org/")               # not https: the planner's
        self.assertEqual(resolved.kind, ds.PLANNER)

    def test_a_bare_address_that_is_a_files_name_never_goes_to_the_web(self) -> None:
        """P1.R (p1r-2): the owner's Illustrator file Documents\\logo.ai - "open logo.ai" finds it (its card shows it)
        and nothing goes to the web; with no such file the page waits on its card for his click, its host shown."""
        self.fake.make_file(DOCS + "\\logo.ai", b"x" * 300)
        resolved = self.resolve("open logo.ai")
        self.assertEqual((resolved.kind, resolved.route.kind), (ds.LOCAL, dv.FIND_FILES))
        (result,) = resolved.run.results
        self.assertEqual((result.status, [item.path for item in result.found]), (dx.FOUND, [DOCS + "\\logo.ai"]))
        (card,) = resolved.run.shown
        self.assertEqual((card.kind, [row.name for row in card.rows], [row.action for row in card.rows]),
                         ("found", ["logo.ai"], ["show"]))                  # a type Jarvis never opens: shown
        self.assertEqual(self.fake.opened, [])
        self.assertEqual(dx.run_words(resolved.run), "Found 1 matching file, sir.")
        for typed, host in (("open poster.ai", "poster.ai"), ("open example.org", "example.org")):
            with self.subTest(typed=typed):
                resolved = self.resolve(typed)
                self.assertEqual((resolved.kind, resolved.route.kind), (ds.LOCAL, dv.OPEN_URL))
                self.assertEqual([(result.status, result.tier) for result in resolved.run.results],
                                 [(dx.WAITING, dv.ONE_CLICK)])
                (card,) = resolved.run.shown
                self.assertEqual((card.kind, card.host, card.address), ("waiting", host, "https://" + host))
                self.assertIn(dv.FLAG_BARE_HOST, card.flags)
                self.assertEqual(self.fake.opened, [])                       # nothing left the PC
        self.assertEqual(dx.run_words(resolved.run), "A web page waits for your click - nothing was opened, sir.")

    def test_find_words_lists_the_files_with_all_of_them(self) -> None:
        resolved = self.resolve("find LING essay")
        self.assertEqual((resolved.kind, resolved.route.kind), (ds.LOCAL, dv.FIND_FILES))
        found = resolved.run.results[0].found
        self.assertEqual({item.name for item in found}, {"LING essay draft.docx", "LING essay draft (old).docx"})
        self.assertTrue(all(item.identity is not None for item in found))
        self.assertEqual(self.fake.opened, [])
        resolved = self.resolve("find my zebra notes")                         # nothing with every word
        self.assertEqual(resolved.kind, ds.PLANNER)

    def test_a_later_phases_request_is_answered_not_yet(self) -> None:
        resolved = self.resolve("volume 30")
        self.assertEqual((resolved.kind, resolved.route.code), (ds.LOCAL, dv.NOT_YET))
        self.assertEqual(self.fake.opened, [])

    def test_with_the_planner_off_no_context_is_built(self) -> None:
        resolved = self.resolve("open my LING essay", planner=False)
        self.assertEqual((resolved.kind, resolved.context), (ds.PLANNER, None))
        self.assertEqual(self.fake.asked("search_index") + self.fake.asked("recent_items"), [])

    def test_without_a_backend_nothing_at_all(self) -> None:
        def refuse() -> object:
            raise dbk.RealBackendInTests("never in a test")

        service = ds.DesktopService(folders=dbk.ROOT_FOLDERS, backend_factory=refuse)
        with self.assertLogs("briefing_reader.desktop", level="WARNING") as logs:
            resolved = service.resolve("open Downloads", run_id="r2")
        self.assertEqual(resolved.kind, ds.UNAVAILABLE)
        self.assertIn("RealBackendInTests", "\n".join(logs.output))
        self.assertEqual(service.resolve("open Downloads").kind, ds.UNAVAILABLE)   # asked once, not again


class ContextTests(ServiceCase):
    def test_the_planners_context_from_this_pc(self) -> None:
        self.fake.recent = [DOCS + "\\Budget 2026.xlsx", OWNER + "\\AppData\\Roaming\\x.txt"]
        resolved = self.resolve("open my LING essay")
        self.assertEqual(resolved.kind, ds.PLANNER)
        context = resolved.context
        names = [item.name for item in context.files]
        self.assertEqual(set(names[:2]), {"LING essay draft.docx", "LING essay draft (old).docx"})
        self.assertIn("Budget 2026.xlsx", names)                              # a Recent item
        self.assertTrue(all(item.identity is not None for item in context.files))
        self.assertEqual([place.parts for place in context.places[:6]],
                         [("Desktop",), ("Documents",), ("Downloads",), ("Pictures",), ("Music",), ("Videos",)])
        self.assertIn(("Documents", "Uni", "LING"), [place.parts for place in context.places])
        lings = [item for item in context.files if item.name == "LING essay draft.docx"]
        self.assertEqual(lings[0].place, LING)                                # its deepest listed place
        self.assertGreaterEqual(context.looked_in, 6)
        # Names only reach the planner.
        prompt = desktop_mode.build_prompt(NOW, ModeRequest(MODE_DESKTOP, ORIGIN_TYPED, typed="open my LING essay",
                                                            typed_route=True), context)
        self.assertNotIn("C:\\", prompt.text)
        self.assertNotIn("Owner", prompt.text)
        self.assertIn('"LING essay draft.docx"', prompt.text)

    def test_the_place_the_owners_words_name_is_searched(self) -> None:
        self.fake.index_rows = None
        context = self.service.context_for("open the essay in my Downloads folder")
        self.assertEqual([item.name for item in context.files], ["LING essay draft (old).docx"])

    def test_the_days_the_words_say(self) -> None:
        self.assertEqual(ds.days_in("open what I downloaded yesterday"), 2)
        self.assertEqual(ds.days_in("find the photos from this week"), 7)
        self.assertEqual(ds.days_in("open my essay"), None)
        context = self.service.context_for("open the file I saved today")
        self.assertTrue(context.files)                                        # changed today: every invented file

    def test_the_apps_the_words_name_never_a_never_open_one(self) -> None:
        context = self.service.context_for("open my essay in Word or the terminal")
        self.assertEqual([app.name for app in context.apps], ["Microsoft Word"])
        self.assertEqual([site.title for site in context.sites], ["Student portal"])

    def test_the_caps(self) -> None:
        for number in range(260):
            self.fake.make_file(f"{DOWNLOADS}\\essay {number:03}.docx")
        context = self.service.context_for("open my essay")
        self.assertEqual(len(context.files), 200)
        self.assertLessEqual(len(context.places), 100)

    def test_kept_a_while_and_read_afresh_for_a_recheck(self) -> None:
        self.service.scope()
        self.service.scope()
        self.assertEqual(self.fake.asked("known_folder").count("downloads"), 1)
        self.service.scope(fresh=True)
        self.assertEqual(self.fake.asked("known_folder").count("downloads"), 2)
        self.service.apps()
        self.service.apps()
        self.assertEqual(len(self.fake.asked("start_menu_apps")), 1)
        self.service.apps(fresh=True)
        self.assertEqual(len(self.fake.asked("start_menu_apps")), 2)

    def test_the_jarvis_project_is_never_a_place(self) -> None:
        project = OWNER + "\\OneDrive\\Desktop\\Projects\\Jarvis"
        self.fake.make_file(project + "\\essay plan.md")
        context = self.service.context_for("open my essay plan")
        self.assertNotIn("essay plan.md", [item.name for item in context.files])
        self.assertFalse(any(place.path.startswith(project) for place in context.places))

    def test_a_plan_carried_out(self) -> None:
        context = self.service.context_for("open my LING essay")
        request = ModeRequest(MODE_DESKTOP, ORIGIN_TYPED, typed="open my LING essay", typed_route=True)
        prompt = desktop_mode.build_prompt(NOW, request, context, run_id="r1")
        number = next(n for n, item in enumerate(context.files, 1) if item.name == "LING essay draft.docx")
        answer = desktop_mode.plan_from({"say": "", "steps": [{"skill": "open_file", "why": "x",
                                                                "files": [f"f{number}"]}]})
        plan = dv.check_plan(answer, prompt.refs, "open my LING essay", run_id="r1")
        run = self.service.carry_out(plan, "open my LING essay", context)
        self.assertEqual([result.status for result in run.results], [dx.DONE])
        self.assertEqual(self.fake.opened, [("open_document", LING + "\\LING essay draft.docx")])
        self.assertIn("LING essay draft (old).docx", [item.name for item in run.results[0].others])

    def test_a_find_step_of_folders(self) -> None:
        found = self.service.find(dx.FindQuery(("ling",), None, "folder"))
        self.assertEqual([place.parts for place in found], [("Documents", "Uni", "LING")])
        found = self.service.find(dx.FindQuery(("budget",), (KNOWN["documents"],), "document"))
        self.assertEqual([item.name for item in found], ["Budget 2026.xlsx"])


if __name__ == "__main__":
    unittest.main()
