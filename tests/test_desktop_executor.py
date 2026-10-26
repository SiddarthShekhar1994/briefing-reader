"""Carrying a desktop request's steps out (briefing_reader.desktop.executor; SPEC 2.1, 3 step 6, 5.2 and 5.3; hard
rules 3, 6, 8 and 14; P1.8). Each skill and tier: INSTANT steps make their one OS call on the target's FINAL path,
ONE-CLICK and CARD steps make none (they wait for the owner's click); every target is re-checked right before its OS
call - a target that changed between the plan and the open (replaced, edited, deleted, swapped for a junction, now
outside the folders, a type that does not open, an app gone from the Start menu, an address that is not https) is
refused and left as it is; the rate caps hold again; a cancel stops before the next step; a failure says so; D5's
other matches are kept; Jarvis's own words and Live's lines never name anything.

Everything runs on tests.desktop_fakes' fake over a temporary folder: its opens open nothing (they are recorded).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime

from briefing_reader.ask import desktop_mode
from briefing_reader.ask.modes import MODE_DESKTOP, ORIGIN_TYPED, ModeRequest
from briefing_reader.desktop import apps as desktop_apps
from briefing_reader.desktop import backend as dbk
from briefing_reader.desktop import executor as dx
from briefing_reader.desktop import paths, routes
from briefing_reader.desktop import validate as dv
from briefing_reader.desktop.context import FileItem, Place, SiteItem
from briefing_reader.desktop.refs import FILE, Ref
from tests.desktop_fakes import DOCS, DOWNLOADS, LING, OWNER, desktop_context, desktop_disk

NOW = datetime(2026, 10, 26, 10, 21)
ESSAY = LING + "\\LING essay draft.docx"
CANARY = "CANARY_7e1"


class ExecutorCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.fake = desktop_disk(tmp.name)
        self.folders = dbk.ROOT_FOLDERS
        self.context = desktop_context(backend=self.fake)
        self.executor = dx.Executor(self.fake, scope=self.scope, apps=lambda: desktop_apps.load(self.fake).apps,
                                    find=self.find)
        self.finds: list[dx.FindQuery] = []

    def scope(self) -> paths.Scope:
        return paths.load_scope(self.folders, (), self.fake)

    def find(self, query: dx.FindQuery) -> tuple:
        self.finds.append(query)
        return tuple(item for item in self.context.files if any(word in item.name.casefold() for word in query.words))

    def plan(self, words: str, *steps: dict, context=None) -> dv.DesktopPlan:  # type: ignore[no-untyped-def]
        request = ModeRequest(MODE_DESKTOP, ORIGIN_TYPED, typed=words, typed_route=True)
        prompt = desktop_mode.build_prompt(NOW, request, context or self.context, run_id="t1")
        answer = desktop_mode.plan_from({"say": "", "steps": list(steps)})
        self.assertIsNotNone(answer)
        return dv.check_plan(answer, prompt.refs, words, run_id="t1")

    def run_plan(self, words: str, *steps: dict, cancel=None) -> dx.DesktopRun:  # type: ignore[no-untyped-def]
        plan = self.plan(words, *steps)
        return self.executor.run_plan(plan, words=words, others_from=self.context.files, cancel=cancel)

    def statuses(self, run: dx.DesktopRun) -> list[tuple[str, str, str]]:
        return [(result.skill, result.status, result.code) for result in run.results]


def _step(number: int, skill: str, target: object, tier: str = dv.INSTANT, **planned: object) -> dv.CheckedStep:
    """A step as the validator would have accepted it (to show the executor refuses on its own: hard rule 6)."""
    kind = {dv.OPEN_FILE: FILE, dv.SHOW_IN_FOLDER: FILE}.get(skill, "d" if skill == dv.OPEN_FOLDER else "a")
    refs = (Ref(f"{kind}{number}", kind, target, "x"),) if target is not None else ()
    return dv.CheckedStep(number, skill, "", tier, frozenset(), refs, dv.PlannedStep(skill, **planned))


class InstantTests(ExecutorCase):
    def test_a_file_opens_on_its_final_path_and_the_other_matches_are_kept(self) -> None:
        run = self.run_plan("open my LING essay", {"skill": "open_file", "why": "x", "files": ["f1"]})
        self.assertEqual(self.statuses(run), [(dv.OPEN_FILE, dx.DONE, "")])
        self.assertEqual(self.fake.opened, [("open_document", ESSAY)])
        result = run.results[0]
        self.assertEqual(result.target.path, ESSAY)
        self.assertEqual([item.name for item in result.others], ["LING essay draft (old).docx",
                                                                  "LING reading list.pdf"])
        self.assertEqual(dx.run_words(run), "Opened the file, sir.")
        self.assertFalse(dx.run_soft(run))
        # Only checks before the open: nothing is written, moved or read.
        self.assertLessEqual({name for name, _argument in self.fake.calls},
                             {"known_folder", "drive_type", "final_path", "file_info", "exists", "file_id",
                              "open_document"})

    def test_show_in_folder_and_open_folder(self) -> None:
        run = self.run_plan("show me the LING essay and open the LING folder",
                            {"skill": "show_in_folder", "why": "x", "files": ["f1"]},
                            {"skill": "open_folder", "why": "x", "place": "d3"})
        self.assertEqual(self.statuses(run), [(dv.SHOW_IN_FOLDER, dx.DONE, ""), (dv.OPEN_FOLDER, dx.DONE, "")])
        self.assertEqual(self.fake.opened, [("show_in_folder", ESSAY), ("open_folder", LING)])
        self.assertEqual(dx.run_words(run), "Opened the folder and showed the file in its folder, sir.")

    def test_an_app_named_exactly_launches_with_the_start_menu_read_again(self) -> None:
        context = desktop_context(backend=self.fake)
        self.context = context
        run = self.run_plan("open Spotify", {"skill": "open_app", "why": "x", "app": "a2"})
        self.assertEqual(self.statuses(run), [(dv.OPEN_APP, dx.DONE, "")])
        self.assertEqual(self.fake.opened, [("launch_app", "SpotifyAB.SpotifyMusic")])
        self.assertEqual(self.fake.asked("start_menu_apps"), [str(desktop_apps.MAX_LISTED)])   # read at the open
        self.assertEqual(dx.run_words(run), "Opened the app, sir.")

    def test_a_typed_address_and_a_saved_site_open(self) -> None:
        run = self.run_plan("open https://news.example.org/today and the student portal",
                            {"skill": "open_url", "why": "x", "url": "https://news.example.org/today"},
                            {"skill": "open_url", "why": "x", "site": "s1"})
        self.assertEqual(self.statuses(run), [(dv.OPEN_URL, dx.DONE, ""), (dv.OPEN_URL, dx.DONE, "")])
        self.assertEqual(self.fake.opened, [("open_url", "https://news.example.org/today"),
                                            ("open_url", "https://portal.example.edu/home")])
        self.assertEqual(dx.run_words(run), "Opened 2 web pages, sir.")

    def test_a_find_lists_what_it_finds_and_opens_nothing(self) -> None:
        run = self.run_plan("find my LING files", {"skill": "find_files", "why": "x", "query": "LING",
                                                   "place": "d3", "kind": "document"})
        self.assertEqual(self.statuses(run), [(dv.FIND_FILES, dx.FOUND, "")])
        self.assertEqual(self.finds, [dx.FindQuery(("ling",), (LING,), "document", None)])
        self.assertEqual(len(run.results[0].found), 3)
        self.assertEqual(self.fake.opened, [])
        self.assertEqual(dx.run_words(run), "Found 3 matching files, sir.")

    def test_each_target_is_checked_right_before_its_own_os_call(self) -> None:
        budget = DOCS + "\\Budget 2026.xlsx"
        self.run_plan("open my LING essay and the budget", {"skill": "open_file", "why": "x", "files": ["f1"]},
                      {"skill": "open_file", "why": "x", "files": ["f3"]})
        calls = self.fake.calls
        first_open = calls.index(("open_document", ESSAY))
        second_open = calls.index(("open_document", budget))
        between = calls[first_open + 1:second_open]
        self.assertIn(("final_path", budget), between)                        # the budget re-checked after the
        self.assertIn(("file_id", budget), between)                           # essay opened, right before its own
        self.assertIn(("known_folder", "documents"), between)                 # the scope read afresh
        self.assertIn(("file_id", ESSAY), calls[:first_open])


class WaitingTests(ExecutorCase):
    def test_one_click_and_card_steps_wait_and_open_nothing(self) -> None:
        run = self.run_plan("open the portal, my grades, Spotify and the music app",
                            {"skill": "open_url", "why": "x", "url": "https://portal.example.edu/modules"},
                            {"skill": "open_file", "why": "x", "files": ["f4"]},
                            {"skill": "open_file", "why": "x", "files": ["f5"]},
                            {"skill": "open_app", "why": "x", "app": "a1"})
        self.assertEqual([(result.skill, result.tier, result.status) for result in run.results],
                         [(dv.OPEN_URL, dv.ONE_CLICK, dx.WAITING), (dv.OPEN_FILE, dv.CARD, dx.WAITING),
                          (dv.OPEN_FILE, dv.CARD, dx.WAITING), (dv.OPEN_APP, dv.ONE_CLICK, dx.WAITING)])
        self.assertEqual(self.fake.opened, [])
        self.assertEqual(self.fake.asked("open_document"), [])
        self.assertEqual(dx.run_words(run), "4 steps wait for your click or OK - nothing was opened, sir.")
        self.assertTrue(dx.run_soft(run))                                    # a card waits: amber

    def test_the_words_for_what_waits(self) -> None:
        def words(flags: frozenset[str], tier: str) -> str:
            return dx.run_words(dx.DesktopRun("t", (dx.StepResult(1, dv.OPEN_FILE, tier, dx.WAITING, "", flags),)))

        self.assertEqual(words(frozenset({dv.FLAG_SCRIPT}), dv.CARD),
                         "A file that can run macros or scripts needs your OK first - nothing was opened, sir.")
        self.assertEqual(words(frozenset({dv.FLAG_SHORTCUT}), dv.CARD),
                         "A shortcut needs your OK first - nothing was opened, sir.")
        self.assertEqual(words(frozenset({dv.FLAG_HIDDEN_CHARS}), dv.ONE_CLICK),
                         "A file with hidden characters in its name waits for your click - nothing was opened, sir.")
        self.assertEqual(words(frozenset({dv.FLAG_NOT_NAMED}), dv.ONE_CLICK),
                         "An app you didn't name exactly waits for your click - nothing was opened, sir.")
        self.assertEqual(words(frozenset({dv.FLAG_SUGGESTED}), dv.ONE_CLICK),
                         "A web page you didn't type waits for your click - nothing was opened, sir.")

    def test_a_step_held_for_a_click_is_checked_too(self) -> None:
        """Before it is held for its card it is checked as an open would be: a file gone since is refused, not
        held."""
        plan = self.plan("open my grades", {"skill": "open_file", "why": "x", "files": ["f4"]})
        os.remove(self.fake.real(DOCS + "\\Grades macro.xlsm"))
        run = self.executor.run_plan(plan, words="open my grades")
        self.assertEqual([(result.tier, result.status, result.code) for result in run.results],
                         [(dv.CARD, dx.REFUSED, dx.CHANGED)])
        self.assertEqual(self.fake.opened, [])

    def test_a_name_with_hidden_characters_is_promoted_to_a_click(self) -> None:
        name = "LING essay" + chr(0x202E) + "xcod.docx"
        path = LING + "\\" + name
        self.fake.make_file(path)
        item = FileItem(path, name, LING, identity=self.fake.identity(path))
        run = self.executor.run_plan(dv.DesktopPlan("t", (_step(1, dv.OPEN_FILE, item),)))
        self.assertEqual([(result.tier, result.status) for result in run.results], [(dv.ONE_CLICK, dx.WAITING)])
        self.assertIn(dv.FLAG_HIDDEN_CHARS, run.results[0].flags)
        self.assertEqual(self.fake.opened, [])


class ChangedTests(ExecutorCase):
    """A target that changed between the plan and the open is refused (SPEC 5.3) - and nothing is opened."""

    def open_essay(self) -> dx.DesktopRun:
        return self.run_plan("open my LING essay", {"skill": "open_file", "why": "x", "files": ["f1"]})

    def test_replaced_by_another_file_of_the_same_name(self) -> None:
        os.remove(self.fake.real(ESSAY))
        self.fake.make_file(ESSAY, b"x" * 4800)                               # same name, same size: a new file id
        run = self.open_essay()
        self.assertEqual(self.statuses(run), [(dv.OPEN_FILE, dx.REFUSED, dx.CHANGED)])
        self.assertEqual(self.fake.opened, [])
        self.assertEqual(dx.run_words(run), "The file changed since I looked - left as it is, sir.")

    def test_edited_since(self) -> None:
        with open(self.fake.real(ESSAY), "ab") as handle:
            handle.write(b"more")
        self.assertEqual(self.statuses(self.open_essay()), [(dv.OPEN_FILE, dx.REFUSED, dx.CHANGED)])
        self.assertEqual(self.fake.opened, [])

    def test_gone_since(self) -> None:
        os.remove(self.fake.real(ESSAY))
        self.assertEqual(self.statuses(self.open_essay()), [(dv.OPEN_FILE, dx.REFUSED, dx.CHANGED)])
        self.assertEqual(self.fake.opened, [])

    def test_its_folder_swapped_for_a_junction(self) -> None:
        secret = OWNER + "\\AppData\\Local\\Elsewhere"
        self.fake.make_file(secret + "\\LING essay draft.docx", b"x" * 4800)
        shutil.move(self.fake.real(LING), self.fake.real(DOCS + "\\Uni\\LING moved"))
        self.fake.junction(LING, secret)
        run = self.open_essay()
        self.assertEqual(run.results[0].status, dx.REFUSED)
        self.assertIn(run.results[0].code, (dv.EXCLUDED, dv.PATH_OUTSIDE, dx.CHANGED))
        self.assertEqual(self.fake.opened, [])

    def test_now_outside_the_folders_you_allow(self) -> None:
        plan = self.plan("open my LING essay", {"skill": "open_file", "why": "x", "files": ["f1"]})
        self.folders = ("downloads",)                                         # Documents is no longer a root
        run = self.executor.run_plan(plan, words="open my LING essay")
        self.assertEqual(self.statuses(run), [(dv.OPEN_FILE, dx.REFUSED, dv.PATH_OUTSIDE)])
        self.assertEqual(self.fake.opened, [])
        self.assertEqual(dx.run_words(run), "I didn't open the file - it's outside the folders you allow, sir.")

    def test_its_drive_is_now_a_network_drive(self) -> None:
        plan = self.plan("open my LING essay", {"skill": "open_file", "why": "x", "files": ["f1"]})
        self.fake.set_drive("C:\\", dbk.DRIVE_REMOTE)
        run = self.executor.run_plan(plan, words="open my LING essay")
        self.assertEqual(run.results[0].status, dx.REFUSED)
        self.assertEqual(self.fake.opened, [])
        self.assertNotIn(("final_path", ESSAY), self.fake.calls[-20:])        # never resolved on that drive

    def test_a_folder_that_changed(self) -> None:
        plan = self.plan("open the LING folder", {"skill": "open_folder", "why": "x", "place": "d3"})
        shutil.move(self.fake.real(LING), self.fake.real(DOCS + "\\Uni\\LING old"))
        self.fake.make_dir(LING)                                              # a new folder of the same name
        run = self.executor.run_plan(plan, words="open the LING folder")
        self.assertEqual(self.statuses(run), [(dv.OPEN_FOLDER, dx.REFUSED, dx.CHANGED)])
        self.assertEqual(self.fake.opened, [])

    def test_an_item_never_listed_with_its_identity_is_never_opened(self) -> None:
        item = FileItem(ESSAY, "LING essay draft.docx", LING)                 # no identity recorded
        run = self.executor.run_plan(dv.DesktopPlan("t", (_step(1, dv.OPEN_FILE, item),)))
        self.assertEqual(self.statuses(run), [(dv.OPEN_FILE, dx.REFUSED, dx.CHANGED)])
        self.assertEqual(self.fake.opened, [])


class NeverAgainTests(ExecutorCase):
    """The NEVER list again, in the executor itself (hard rule 6): what the validator would have refused is refused
    here too, whatever a step says."""

    def test_a_secret_file_a_blocked_type_and_a_shortcut(self) -> None:
        for name in (".env", "token.json", "setup.exe", "notes.ps1", "readme.chm"):
            self.fake.make_file(DOWNLOADS + "\\" + name)
        steps = [_step(n, dv.OPEN_FILE, FileItem(DOWNLOADS + "\\" + name, name, DOWNLOADS,
                                                 identity=self.fake.identity(DOWNLOADS + "\\" + name)))
                 for n, name in enumerate((".env", "token.json", "setup.exe", "notes.ps1", "readme.chm"), 1)]
        lnk = DOWNLOADS + "\\Spotify.lnk"
        steps.append(_step(6, dv.OPEN_FILE, FileItem(lnk, "Spotify.lnk", DOWNLOADS, identity=self.fake.identity(lnk))))
        run = self.executor.run_plan(dv.DesktopPlan("t", tuple(steps)))
        self.assertEqual([(result.status, result.code) for result in run.results],
                         [(dx.REFUSED, dv.EXCLUDED)] * 2 + [(dx.REFUSED, dv.BLOCKED_TYPE)] * 3
                         + [(dx.WAITING, "")])                                # a shortcut: a card, never at once
        self.assertEqual(run.results[-1].tier, dv.CARD)
        self.assertEqual(self.fake.opened, [])

    def test_a_folder_named_for_a_shell_class_never_opens(self) -> None:
        god = DOWNLOADS + "\\Tools.{ED7BA470-8E54-465E-825C-99712043E01C}"
        self.fake.make_dir(god)
        place = Place(god, ("Downloads", "Tools"), self.fake.identity(god))
        run = self.executor.run_plan(dv.DesktopPlan("t", (_step(1, dv.OPEN_FOLDER, place),)))
        self.assertEqual([(result.status, result.code) for result in run.results], [(dx.REFUSED, dv.EXCLUDED)])
        self.assertEqual(self.fake.opened, [])

    def test_an_app_no_longer_on_the_start_menu_or_never_open(self) -> None:
        from briefing_reader.desktop.context import AppItem

        gone = AppItem("Contoso.Gone!App", "Contoso")
        terminal = AppItem("Microsoft.WindowsTerminal_8wekyb3d8bbwe!App", "Terminal")
        renamed = AppItem("SpotifyAB.SpotifyMusic", "Spotify Premium")
        run = self.executor.run_plan(dv.DesktopPlan("t", tuple(_step(n, dv.OPEN_APP, app) for n, app in
                                                               enumerate((gone, terminal, renamed), 1))))
        self.assertEqual([(result.status, result.code) for result in run.results], [(dx.REFUSED, dx.CHANGED)] * 3)
        self.assertEqual(self.fake.opened, [])

    def test_an_address_that_is_not_https_on_a_public_host(self) -> None:
        steps = tuple(_step(n, dv.OPEN_URL, None, url=url) for n, url in enumerate(
            ("http://example.org/", "https://localhost/", "file:///C:/x.html", "ms-settings:privacy",
             "https://192.168.1.1/", "javascript:alert(1)"), 1))
        run = self.executor.run_plan(dv.DesktopPlan("t", steps))
        self.assertEqual([result.code for result in run.results],
                         [dv.SHAPE, dv.SITE_NOT_APPROVED, dv.SHAPE, dv.SHAPE, dv.SITE_NOT_APPROVED, dv.SHAPE])
        self.assertEqual(self.fake.opened, [])

    def test_the_backends_last_guard(self) -> None:
        self.fake.open_errors["open_document"] = ValueError("refused by the last guard")
        run = self.run_plan("open my LING essay", {"skill": "open_file", "why": "x", "files": ["f1"]})
        self.assertEqual(self.statuses(run), [(dv.OPEN_FILE, dx.REFUSED, dx.GUARD)])


class LimitTests(ExecutorCase):
    def test_at_most_5_opens_and_3_web_pages_a_request(self) -> None:
        names = ("LING essay draft.docx", "LING reading list.pdf", "Budget 2026.xlsx")
        items = [item for item in self.context.files if item.name in names] * 2
        steps = [_step(n, dv.OPEN_FILE, item) for n, item in enumerate(items, 1)]
        steps += [_step(7 + n, dv.OPEN_URL, None, url=f"https://site{n}.example.org/") for n in range(4)]
        run = self.executor.run_plan(dv.DesktopPlan("t", tuple(steps)))
        self.assertEqual([result.status for result in run.results], [dx.DONE] * 5 + [dx.REFUSED] * 5)
        self.assertTrue(all(result.code == dv.CAP for result in run.results[5:]))
        self.assertEqual(len(self.fake.opened), 5)

    def test_three_urls_then_the_cap(self) -> None:
        steps = [_step(n, dv.OPEN_URL, None, url=f"https://site{n}.example.org/") for n in range(1, 5)]
        run = self.executor.run_plan(dv.DesktopPlan("t", tuple(steps)))
        self.assertEqual([result.status for result in run.results], [dx.DONE] * 3 + [dx.REFUSED])
        self.assertEqual(run.results[-1].code, dv.CAP)

    def test_a_cancel_stops_before_the_next_step(self) -> None:
        cancel = threading.Event()
        self.fake.on_call = lambda method, _argument: cancel.set() if method == "open_document" else None
        run = self.run_plan("open my LING essay and the budget", {"skill": "open_file", "why": "x", "files": ["f1"]},
                            {"skill": "open_file", "why": "x", "files": ["f3"]}, cancel=cancel)
        self.assertEqual([result.status for result in run.results], [dx.DONE, dx.CANCELLED])
        self.assertEqual(len(self.fake.opened), 1)                            # the one in flight completed
        self.assertEqual(dx.run_words(run), "Opened the file. Stopped - nothing more was opened, sir.")
        cancel.set()
        self.fake.opened.clear()
        run = self.run_plan("open my LING essay", {"skill": "open_file", "why": "x", "files": ["f1"]}, cancel=cancel)
        self.assertEqual([result.status for result in run.results], [dx.CANCELLED])
        self.assertEqual(self.fake.opened, [])

    def test_a_failure_says_so_with_its_winerror(self) -> None:
        self.fake.open_errors["open_document"] = OSError(0, "no", None, 1155)
        with self.assertLogs("briefing_reader.desktop", level="INFO") as logs:
            run = self.run_plan("open my LING essay", {"skill": "open_file", "why": "x", "files": ["f1"]})
        self.assertEqual([(result.status, result.code, result.winerror) for result in run.results],
                         [(dx.FAILED, dx.OS_FAILED, 1155)])
        self.assertEqual(dx.run_words(run), "Windows couldn't open the file, sir.")
        self.assertIn("OSError 1155", "\n".join(logs.output))
        self.assertEqual(dx.describe(run.results[0]),
                         "Step 1 - open_file - instant - failed: os_error (Windows error 1155)")


class RouteTests(ExecutorCase):
    def test_a_known_folder_is_found_in_the_scope_as_it_is_now(self) -> None:
        run = self.executor.run_route(routes.route("open Downloads", self.context), words="open Downloads")
        self.assertEqual(self.statuses(run), [(dv.OPEN_FOLDER, dx.DONE, "")])
        self.assertEqual(self.fake.opened, [("open_folder", DOWNLOADS)])
        self.assertEqual(dx.run_words(run), "Opened your Downloads folder, sir.")
        self.assertEqual(dx.describe(run.results[0]), "On this PC - open_folder - instant - opened")
        self.folders = ("documents",)
        run = self.executor.run_route(routes.route("open my Downloads folder", self.context))
        self.assertEqual(self.statuses(run), [(dv.OPEN_FOLDER, dx.REFUSED, dv.PATH_OUTSIDE)])
        self.assertEqual(dx.run_words(run),
                         "I didn't open your Downloads folder - it's outside the folders you allow, sir.")

    def test_an_app_an_address_a_site_a_find_and_a_later_phase(self) -> None:
        context = desktop_context(backend=self.fake)
        found = routes.route("open Spotify", context)
        self.assertEqual(self.statuses(self.executor.run_route(found)), [(dv.OPEN_APP, dx.DONE, "")])
        found = routes.route("open youtube.com", context)
        self.assertEqual(self.statuses(self.executor.run_route(found)), [(dv.OPEN_URL, dx.DONE, "")])
        found = routes.route("open the student portal", context)
        self.assertEqual(self.statuses(self.executor.run_route(found)), [(dv.OPEN_URL, dx.DONE, "")])
        self.assertEqual(self.fake.opened, [("launch_app", "SpotifyAB.SpotifyMusic"),
                                            ("open_url", "https://youtube.com"),
                                            ("open_url", "https://portal.example.edu/home")])
        found = routes.route("find LING essay", context, search=lambda words: list(context.files))
        run = self.executor.run_route(found)
        self.assertEqual(self.statuses(run), [(dv.FIND_FILES, dx.FOUND, "")])
        self.assertEqual([item.name for item in run.results[0].found], ["LING essay draft.docx",
                                                                        "LING essay draft (old).docx"])
        run = self.executor.run_route(routes.route("volume 30", context))
        self.assertEqual(self.statuses(run), [(dv.VOLUME, dx.REFUSED, dv.NOT_YET)])
        self.assertEqual(len(self.fake.opened), 3)


class WordsTests(unittest.TestCase):
    """Jarvis's own words for a run: kinds and counts, a known folder's label at most - never a name, a path, a
    title or an address (the canary is everywhere a result can carry it)."""

    def result(self, skill: str, status: str, code: str = "", **kwargs: object) -> dx.StepResult:
        return dx.StepResult(1, skill, dv.INSTANT, status, code, frozenset(), **kwargs)

    def test_never_a_name(self) -> None:
        item = FileItem(f"C:\\Users\\Owner\\{CANARY}\\{CANARY}.docx", f"{CANARY}.docx", identity=None)
        place = Place(f"C:\\Users\\Owner\\{CANARY}", ("Documents", CANARY))
        results = (self.result(dv.OPEN_FILE, dx.DONE, target=item, others=(item,)),
                   self.result(dv.OPEN_FOLDER, dx.DONE, target=place),
                   self.result(dv.OPEN_URL, dx.DONE, target=SiteItem(f"https://{CANARY}.example.org/", CANARY)),
                   self.result(dv.FIND_FILES, dx.FOUND, found=(item, place)),
                   self.result(dv.OPEN_APP, dx.REFUSED, dx.CHANGED),
                   self.result(dv.OPEN_FILE, dx.FAILED, dx.OS_FAILED, winerror=5))
        run = dx.DesktopRun("t", results)
        texts = [dx.run_words(run), *(dx.describe(result) for result in results), repr(run),
                 *(repr(result) for result in results)]
        for text in texts:
            self.assertNotIn(CANARY, text)
            self.assertNotIn("Owner", text)
        self.assertEqual(dx.run_words(run), "Opened the file, the folder and the web page. Found 1 matching file and "
                                            "1 matching folder. 2 steps couldn't be done - Live says why, sir.")

    def test_the_plans_refusals_and_an_empty_run(self) -> None:
        run = dx.DesktopRun("t", (self.result(dv.OPEN_APP, dx.DONE),))
        plan = dv.DesktopPlan("t", (dv.CheckedStep(1, dv.OPEN_APP, "", dv.INSTANT),
                                    dv.CheckedStep(2, dv.RECYCLE_FILES, dv.INTENT_NOT_LICENSED)))
        self.assertEqual(dx.run_words(run, plan), "Opened the app. 1 step was refused - Live says why, sir.")
        self.assertTrue(dx.run_soft(run, plan))
        self.assertEqual(dx.run_words(dx.DesktopRun("t")), "")
        self.assertEqual(dx.run_words(run, address=""), "Opened the app.")
        self.assertEqual(dx.run_words(dx.DesktopRun("t", (self.result(dv.FIND_FILES, dx.FOUND),))),
                         "Found nothing that matches, sir.")


if __name__ == "__main__":
    unittest.main()
