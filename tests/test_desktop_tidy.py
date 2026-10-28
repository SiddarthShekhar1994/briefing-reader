"""Jarvis on your desktop, P2.3: the parts of the NEEDS YOUR OK card of file steps (SPEC 5.4, 6.3, 8.1, 8.4, 9, 10.3
and 16 P2.3; hard rules 4, 7, 12, 13, 14 and 17) - Esc held only while a batch acts (desktop.stopkeys: the real seam is
never made or reached here; tests.desktop_fakes.FakeStopKeys stands for it), the card's words through its life
(desktop.tidy), the click's batch and the Undo on the desktop thread's service (desktop.service over the fake backend
and a temporary journal - nothing real is touched), each row's progress (desktop.fileops), the File Explorer window of
the card's folder (desktop.winwatch.find_folder over pretend windows), the DesktopCard window (hud_overlay) and the
organising verbs that now reach the desktop (desktop.routes). Every name is invented.
"""

from __future__ import annotations

import ctypes
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from briefing_reader import holo, hud_overlay
from briefing_reader.ask import desktop_mode as dm
from briefing_reader.ask.modes import MODE_DESKTOP, ModeRequest
from briefing_reader.desktop import backend as dbk
from briefing_reader.desktop import cardplan as cp
from briefing_reader.desktop import fileops as fo
from briefing_reader.desktop import journal as jn
from briefing_reader.desktop import paths
from briefing_reader.desktop import routes as dr
from briefing_reader.desktop import stopkeys as sk
from briefing_reader.desktop import tidy as dt
from briefing_reader.desktop import validate as dv
from briefing_reader.desktop import winwatch as ww
from briefing_reader.desktop.context import FileItem
from briefing_reader.desktop.service import DesktopService
from tests.desktop_fakes import DOCS, DOWNLOADS, LING, UNI, FakeStopKeys, FakeWinApi, desktop_context, desktop_disk
from tests.ui_fakes import fonts, settle

NOW = datetime(2026, 10, 28, 10, 22, tzinfo=timezone.utc)
MONO = 500.0
DOT = "\u00b7"
CRUMB = "\u203a"
NAMES = ("LING_reading_wk7.pdf", "ECON_problem_set_4.pdf", "IMG_2041.jpg", "Syllabus_HIST210.docx",
         "budget_oct.xlsx", "setup_zoom_v6.exe")
WORDS = "tidy my Downloads into LING, Uni and Documents"
STEPS = [{"skill": "move_files", "why": "x", "files": ["f1"], "to": "d3"},
         {"skill": "move_files", "why": "x", "files": ["f2", "f4"], "to": "d2"},
         {"skill": "move_files", "why": "x", "files": ["f3", "f5", "f6"], "to": "d1"}]
DESTS = (LING, UNI, UNI, DOCS, DOCS, DOCS)
ORDER = (0, 1, 3, 2, 4, 5)        # the card's rows in NAMES' order (the steps group f2+f4 and f3+f5+f6)
_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_desktop_tidy", "-platform", "offscreen"])
    fonts()


# --------------------------------------------------------------------------
# Esc, held only while a batch acts
# --------------------------------------------------------------------------

class StopKeysTests(unittest.TestCase):
    def test_the_real_seam_is_never_made_or_reached_while_the_tests_run(self) -> None:
        with mock.patch.object(ctypes, "WinDLL", side_effect=AssertionError("reached ctypes.WinDLL")):
            with self.assertRaises(dbk.RealBackendInTests):
                sk.Win32StopKeys()
            seam = object.__new__(sk.Win32StopKeys)                       # its constructor never ran
            for name, arguments in (("arm", (lambda: None,)), ("disarm", ())):
                with self.subTest(method=name):
                    self.assertTrue(getattr(getattr(sk.Win32StopKeys, name), "reaches_os", False))
                    with self.assertRaises(dbk.RealBackendInTests):
                        getattr(seam, name)(*arguments)
        self.assertIsInstance(sk.default_stopkeys(), sk.NoStopKeys)
        self.assertFalse(sk.NoStopKeys().arm(lambda: None))

    def test_held_for_the_block_only_and_let_go_in_any_case(self) -> None:
        keys, pressed = FakeStopKeys(), []
        self.assertFalse(keys.press())
        with sk.held(keys, lambda: pressed.append("esc")) as held:
            self.assertTrue(held and keys.armed())
            self.assertTrue(keys.press())
        self.assertEqual(pressed, ["esc"])
        self.assertFalse(keys.armed() or keys.press())
        with self.assertRaises(ValueError):
            with sk.held(keys, lambda: None):
                raise ValueError("the batch failed")
        self.assertFalse(keys.armed())
        self.assertEqual((keys.arms, keys.disarms), (2, 2))
        keys.refuse = True                                                   # another program holds Esc
        with sk.held(keys, lambda: None) as held:
            self.assertFalse(held)

        class Refusing:
            def arm(self, _on_stop: object) -> bool:
                raise dbk.RealBackendInTests("never in a test")

            def disarm(self) -> None:
                raise OSError(5, "refused")

        with sk.held(Refusing(), lambda: None) as held:                      # nothing escapes; no Esc
            self.assertFalse(held)
        with sk.held(None, lambda: None) as held:
            self.assertFalse(held)


# --------------------------------------------------------------------------
# A card of the tidy plan on the pretend disk
# --------------------------------------------------------------------------

class TidyCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.fake = desktop_disk(os.path.join(self.tmp, "disk"))
        self.files = []
        for name in NAMES:
            path = DOWNLOADS + "\\" + name
            self.fake.make_file(path, b"x" * 100)
            self.files.append(FileItem(path, name, DOWNLOADS, size=100))
        self.scope = paths.load_scope(dbk.ROOT_FOLDERS, (), self.fake)
        self.journal_dir = os.path.join(self.tmp, "journal")

    def plan(self, steps: list | None = None, words: str = WORDS, between: object = None) -> cp.CardPlan:
        context = desktop_context(files=self.files, backend=self.fake)
        if callable(between):
            between()                                                        # a change after the files were listed
        request = ModeRequest(MODE_DESKTOP, "typed", typed=words, typed_route=True)
        prompt = dm.build_prompt(NOW, request, context, run_id="t1")
        answer = dm.plan_from({"say": "", "steps": steps or STEPS})
        checked = dv.check_plan(answer, prompt.refs, words, run_id="t1", names=dm.context_names(context))
        card = cp.build_card(checked, self.fake, self.scope, now=NOW, mono=MONO)
        assert card is not None
        return card

    def card(self, steps: list | None = None, words: str = WORDS, between: object = None) -> dt.TidyCard:
        card = dt.make_card(self.plan(steps, words, between), self.scope, at=NOW)
        assert card is not None
        return card

    def service(self, now: datetime = NOW) -> DesktopService:
        return DesktopService(folders=dbk.ROOT_FOLDERS, backend_factory=lambda: self.fake, now=lambda: now,
                              clock=lambda: MONO + 1, journal_folder=lambda: self.journal_dir)

    def there(self, path: str) -> bool:
        return os.path.lexists(self.fake.real(path))

    def moves(self) -> list[str]:
        return [argument for method, argument in self.fake.calls if method == "move_item"]

    def row_path(self, number: int) -> tuple[str, str]:
        index = ORDER[number - 1]
        return DOWNLOADS + "\\" + NAMES[index], DESTS[number - 1] + "\\" + NAMES[index]


class CardWordsTests(TidyCase):
    def test_the_waiting_card_shows_each_row_its_flags_in_words_and_the_installer_unticked(self) -> None:
        card = self.card()
        self.assertEqual(card.title, "Tidy Downloads into your folders")
        self.assertEqual(card.folder, DOWNLOADS)
        self.assertEqual(card.folder_name, "Downloads")
        self.assertEqual(card.sha, card.plan.sha256)
        self.assertEqual([(row.name, row.to) for row in card.rows],
                         [("LING_reading_wk7.pdf", f"Documents {CRUMB} Uni {CRUMB} LING"),
                          ("ECON_problem_set_4.pdf", f"Documents {CRUMB} Uni"),
                          ("Syllabus_HIST210.docx", f"Documents {CRUMB} Uni"), ("IMG_2041.jpg", "Documents"),
                          ("budget_oct.xlsx", "Documents"), ("setup_zoom_v6.exe", "Documents")])
        self.assertEqual([row.ticked for row in card.rows], [True] * 5 + [False])
        self.assertEqual(card.rows[5].flag_words(), f"UPLOADS TO ONEDRIVE {DOT} INSTALLER - STAYS UNLESS YOU TICK IT")
        state = dt.TidyState(card)
        text = dt.tidy_text(state)
        self.assertEqual((text.kicker, text.tone), (f"MOVE FILES {DOT} NEEDS YOUR OK", dt.AMBER))
        self.assertEqual(text.note, "Nothing is deleted. Installers stay where they are. 5 go to OneDrive and will "
                                    "upload.")
        self.assertEqual(text.buttons, (("dismiss", "Not now", "quiet", False, True),
                                        ("go", "Move 5 files", "commit", True, True)))
        self.assertTrue(state.toggle(6, True))
        self.assertEqual(dt.tidy_text(state).buttons[1][1], "Move 6 files")
        self.assertNotIn("Installers stay", dt.tidy_text(state).note)
        for number in range(1, 7):
            state.toggle(number, False)
        self.assertEqual(dt.tidy_text(state).buttons[1], ("go", "Nothing ticked", "commit", True, False))
        self.assertTrue(state.toggle(1, True))
        self.assertEqual(dt.tidy_text(state).buttons[1][1], "Move 1 file")
        self.assertEqual(dt.tidy_text(state).note, "Nothing is deleted. Installers stay where they are. 1 goes to "
                                                   "OneDrive and will upload.")
        self.assertIn("TidyCard(t1", repr(card))
        for name in NAMES:
            self.assertNotIn(name, repr(card) + repr(state) + repr(card.rows))

    def test_a_taken_name_shows_its_free_name_and_a_row_that_cant_go_says_why(self) -> None:
        self.fake.make_file(LING + "\\LING_reading_wk7.pdf", b"theirs")                  # the name is taken there

        def change() -> None:
            os.remove(self.fake.real(DOWNLOADS + "\\ECON_problem_set_4.pdf"))
            self.fake.make_file(DOWNLOADS + "\\ECON_problem_set_4.pdf", b"changed since it was listed")

        card = self.card(between=change)
        self.assertEqual(card.rows[0].name, "LING_reading_wk7 (2).pdf")
        self.assertIn("RENAMED - THAT NAME WAS TAKEN", card.rows[0].flag_words())
        self.assertEqual(card.rows[1].code, fo.R_CHANGED)
        text = dt.tidy_text(dt.TidyState(card))
        self.assertEqual((text.rows[1].mark, text.rows[1].words), (dt.MARK_INFO, "It changed since I looked - left as "
                                                                                 "it is."))
        self.assertEqual(text.buttons[1][1], "Move 4 files")

    def test_the_card_through_its_life_in_words(self) -> None:
        state = dt.TidyState(self.card())
        state.start()
        self.assertEqual(state.chosen, (1, 2, 3, 4, 5))
        self.assertEqual((dt.tidy_text(state).kicker, dt.tidy_text(state).tone), (f"MOVING 1 OF 5{dt.ELLIPSIS}",
                                                                                   dt.CYAN))
        state.started(True)
        self.assertEqual(dt.tidy_text(state).note, "Stop or Esc stops it after this one. Nothing is deleted.")
        state.row_ended(dt.RowEnd(1, jn.DONE, "", 7), 1)
        text = dt.tidy_text(state)
        self.assertEqual(text.kicker, f"MOVING 2 OF 5{dt.ELLIPSIS}")
        self.assertEqual([row.mark for row in text.rows],
                         [dt.MARK_DONE, dt.MARK_NOW, dt.MARK_NONE, dt.MARK_NONE, dt.MARK_NONE, dt.MARK_IDLE])
        self.assertEqual(text.buttons, (("stop", "Stop", "quiet", True, True),))
        self.assertTrue(state.stop_asked("esc"))
        self.assertFalse(state.stop_asked("stop"))
        self.assertEqual(dt.tidy_text(state).kicker, f"STOPPING AFTER THIS ONE{dt.ELLIPSIS}")
        ends = (dt.RowEnd(1, jn.DONE, "", 7), dt.RowEnd(2, jn.DONE, "", 8), dt.RowEnd(3, jn.SKIPPED, fo.R_STOPPED, 9),
                dt.RowEnd(4, jn.SKIPPED, fo.R_STOPPED, 10), dt.RowEnd(5, jn.SKIPPED, fo.R_STOPPED, 11))
        state.finish(dt.CardOutcome("t1", "", ends, 1.0, True), datetime(2026, 10, 28, 10, 22))
        text = dt.tidy_text(state)
        self.assertEqual(text.kicker, f"STOPPED BY YOU {DOT} MOVED 2 OF 5 FILES {DOT} 10:22 AM")
        self.assertEqual(text.tone, dt.CORAL)
        self.assertEqual([row.words for row in text.rows[2:5]], ["Not done - you stopped it."] * 3)
        self.assertEqual((text.link, text.link_note), (("undo", "Undo"), "Puts every file back where it was."))
        self.assertEqual(dt.said(state), "Stopped, sir - moved 2 of 5.")
        self.assertEqual(state.undo_seqs(), (7, 8))
        state.start_undo()
        self.assertEqual(dt.tidy_text(state).kicker, f"PUTTING BACK{dt.ELLIPSIS}")
        state.finish_undo(dt.UndoOutcome("t1", (dt.RowEnd(0, jn.UNDONE, "", 8), dt.RowEnd(0, jn.UNDO_REFUSED,
                                                                                          fo.R_MOVED_SINCE, 7)),
                                         f"PUT BACK 1 OF 2 {DOT} 1 MOVED SINCE"), datetime(2026, 10, 28, 10, 23))
        text = dt.tidy_text(state)
        self.assertEqual((text.kicker, text.tone), (f"PUT BACK 1 OF 2 {DOT} 1 MOVED SINCE", dt.CORAL))
        self.assertEqual([row.words for row in text.rows[:2]], ["Moved since - left where it is.", "Put back."])
        self.assertEqual(text.note, "1 was left as it is - see why above.")
        self.assertEqual(dt.said(state), "Put back 1 of 2, sir - see the card for the rest.")

    def test_done_refused_and_stopped_first_in_plain_words(self) -> None:
        card = self.card()
        at = datetime(2026, 10, 28, 22, 5)
        done = dt.TidyState(card)
        done.start()
        done.finish(dt.CardOutcome("t1", "", tuple(dt.RowEnd(n, jn.DONE, "", n) for n in range(1, 6))), at)
        self.assertEqual((dt.tidy_text(done).kicker, dt.tidy_text(done).tone), (f"MOVED 5 FILES {DOT} 10:05 PM",
                                                                                 dt.MINT))
        self.assertEqual(dt.tidy_text(done, hour24=True).kicker, f"MOVED 5 FILES {DOT} 22:05")
        self.assertEqual(dt.said(done, "ma'am"), "Moved 5 files, ma'am.")
        done.start_undo()
        done.finish_undo(dt.UndoOutcome("t1", tuple(dt.RowEnd(0, jn.UNDONE, "", n) for n in range(5, 0, -1)),
                                        "PUT BACK 5 OF 5"), at + timedelta(minutes=1))
        self.assertEqual((dt.tidy_text(done).kicker, dt.tidy_text(done).note),
                         (f"PUT BACK 5 OF 5 {DOT} 10:06 PM", "Everything is back as it was."))
        notes = {cp.CARD_EXPIRED: "This card is more than 15 minutes old, so nothing was moved. Ask again for a fresh "
                                  "one.",
                 cp.CARD_CHANGED: "This card's plan isn't the one you were shown, so nothing was moved. Ask again.",
                 dt.UNAVAILABLE: "Jarvis can't change files on this PC right now, so nothing was moved.",
                 dt.TURNED_OFF: "Jarvis on your desktop was turned off, so nothing was moved."}
        for code, note in notes.items():
            with self.subTest(code=code):
                state = dt.TidyState(card)
                state.start()
                state.finish(dt.CardOutcome("t1", code), at)
                text = dt.tidy_text(state)
                self.assertEqual((text.kicker, text.tone, text.note, text.buttons, text.link),
                                 (f"NOTHING MOVED {DOT} 10:05 PM", dt.CORAL, note, (), None))
        first = dt.TidyState(card)
        first.start()
        first.finish(dt.CardOutcome("t1", dt.STOPPED_FIRST), at)                 # nothing ran: it waits again
        self.assertTrue(first.waiting())
        self.assertEqual(dt.said(first), "Stopped, sir - nothing was moved.")
        done.stage = dt.DONE
        done.finish_undo(dt.UndoOutcome("t1", code=dt.UNAVAILABLE), at)          # Undo kept on the stamp
        self.assertEqual(done.stage, dt.DONE)
        self.assertEqual(dt.said(done), "Nothing was put back, sir - I can't change files on this PC right now.")

    def test_copies_and_zips_say_plainly_that_undo_cant_bin_them_yet(self) -> None:
        steps = [{"skill": "copy_files", "why": "x", "files": ["f1", "f2"], "to": "d3"}]
        state = dt.TidyState(self.card(steps, "copy my Downloads files into LING"))
        self.assertEqual(dt.tidy_text(state).kicker, f"COPY FILES {DOT} NEEDS YOUR OK")
        self.assertEqual(dt.tidy_text(state).buttons[1][1], "Copy 2 files")
        state.start()
        state.finish(dt.CardOutcome("t1", "", (dt.RowEnd(1, jn.DONE, "", 1), dt.RowEnd(2, jn.DONE, "", 2))), NOW)
        text = dt.tidy_text(state)
        self.assertFalse(state.undoable())
        self.assertIsNone(text.link)
        self.assertEqual(text.note, "Undo can't take back copies or zips yet - that needs the Recycle Bin, which comes "
                                    "in a later update.")


# --------------------------------------------------------------------------
# The click's batch and the Undo (desktop.service, on the desktop thread in the app)
# --------------------------------------------------------------------------

class ServiceTests(TidyCase):
    def test_the_click_runs_exactly_the_ticked_rows_with_esc_held_only_meanwhile(self) -> None:
        card, keys = self.card(), FakeStopKeys()
        heard: list[tuple[int, str, int, bool]] = []
        started: list[bool] = []

        def on_row(end: dt.RowEnd, ended: int, _total: int) -> None:
            heard.append((end.number, end.state, ended, keys.armed()))

        outcome = self.service().carry_out_card(card, sha=card.sha, ticked={1, 3, 5}, on_row=on_row,
                                                on_start=started.append, stop_keys=keys)
        self.assertEqual(outcome.code, "")
        self.assertEqual([(end.number, end.state) for end in outcome.rows], [(1, jn.DONE), (3, jn.DONE), (5, jn.DONE)])
        self.assertEqual(heard, [(1, jn.DONE, 1, True), (3, jn.DONE, 2, True), (5, jn.DONE, 3, True)])
        self.assertEqual((started, outcome.esc, keys.arms, keys.disarms, keys.armed()), ([True], True, 1, 1, False))
        for number in (1, 3, 5):
            self.assertTrue(self.there(self.row_path(number)[1]))
        for number in (2, 4, 6):
            self.assertTrue(self.there(self.row_path(number)[0]))
        journal = jn.Journal(self.journal_dir, "t1")
        self.assertEqual({seq: item.state for seq, item in journal.items().items()},
                         {end.seq: jn.DONE for end in outcome.rows})

    def test_an_expired_or_changed_plan_a_stop_first_or_no_journal_runs_nothing(self) -> None:
        card = self.card()
        stopped = threading.Event()
        stopped.set()
        cases = {cp.CARD_EXPIRED: (self.service(NOW + timedelta(minutes=15)), card.sha, None),
                 cp.CARD_CHANGED: (self.service(), "0" * 64, None),
                 dt.STOPPED_FIRST: (self.service(), card.sha, stopped)}
        for code, (service, sha, cancel) in cases.items():
            with self.subTest(code=code):
                keys = FakeStopKeys()
                outcome = service.carry_out_card(card, sha=sha, ticked={1, 2}, cancel=cancel, stop_keys=keys)
                self.assertEqual((outcome.code, outcome.rows, keys.arms), (code, (), 0))
        unjournalled = DesktopService(folders=dbk.ROOT_FOLDERS, backend_factory=lambda: self.fake, now=lambda: NOW,
                                      clock=lambda: MONO + 1)                     # the app's folder: refused in tests
        self.assertEqual(unjournalled.carry_out_card(card, sha=card.sha, ticked={1}).code, dt.UNAVAILABLE)
        self.assertEqual(self.moves(), [])
        self.assertFalse(os.path.exists(self.journal_dir))

    def test_a_stop_during_the_batch_leaves_the_rest_and_undo_puts_back_newest_first(self) -> None:
        card, stop = self.card(), threading.Event()

        def on_row(end: dt.RowEnd, ended: int, _total: int) -> None:
            if ended == 2:
                stop.set()                                                       # Stop after the second row

        service = self.service()
        outcome = service.carry_out_card(card, sha=card.sha, ticked={1, 2, 3, 4, 5}, cancel=stop, on_row=on_row)
        self.assertEqual([(end.state, end.reason) for end in outcome.rows],
                         [(jn.DONE, ""), (jn.DONE, ""), (jn.SKIPPED, fo.R_STOPPED), (jn.SKIPPED, fo.R_STOPPED),
                          (jn.SKIPPED, fo.R_STOPPED)])
        self.assertEqual(len(self.moves()), 2)
        state = dt.TidyState(card)
        state.start()
        state.finish(outcome, NOW)
        self.fake.calls.clear()
        os.rename(self.fake.real(self.row_path(1)[1]), self.fake.real(DOCS + "\\elsewhere.pdf"))   # moved since
        keys = FakeStopKeys()
        undo = service.undo_card("t1", state.undo_seqs(), stop_keys=keys)
        self.assertEqual(undo.stamp, f"PUT BACK 1 OF 2 {DOT} 1 MOVED SINCE")
        self.assertEqual(self.moves(), [self.row_path(2)[1]])                    # newest first, one at a time
        self.assertTrue(self.there(self.row_path(2)[0]))
        self.assertEqual((keys.arms, keys.disarms), (1, 1))
        state.finish_undo(undo, NOW)
        self.assertEqual({number: end.reason for number, end in state.undo_ends.items()},
                         {1: fo.R_MOVED_SINCE, 2: ""})


class ProgressTests(TidyCase):
    def test_each_row_is_heard_as_it_ends_and_a_failing_listener_never_stops_the_batch(self) -> None:
        card = self.plan()
        heard: list[tuple[int, str, int, int]] = []
        stop = threading.Event()

        def progress(result: fo.OpResult, ended: int, total: int) -> None:
            heard.append((result.seq, result.state, ended, total))
            if ended == 1:
                stop.set()
            raise RuntimeError("only a view")

        journal = jn.Journal(self.journal_dir, "t1")
        report = fo.FileOps(self.fake, journal, scope=lambda: self.scope, cancel=stop.is_set,
                            progress=progress).run(list(card.ops({1, 2, 3})))
        self.assertEqual([(state, ended, total) for _seq, state, ended, total in heard],
                         [(jn.DONE, 1, 3), (jn.SKIPPED, 2, 3), (jn.SKIPPED, 3, 3)])
        self.assertEqual([result.state for result in report.results], [jn.DONE, jn.SKIPPED, jn.SKIPPED])


# --------------------------------------------------------------------------
# The File Explorer window of the card's folder
# --------------------------------------------------------------------------

class FindFolderTests(unittest.TestCase):
    def test_the_window_showing_the_folder_whole_path_never_by_title(self) -> None:
        api = FakeWinApi()
        watch = ww.WindowWatch(api)
        self.assertEqual(watch.find_folder(DOWNLOADS), 0)                          # none open: no bracket
        api.add(0x10, pid=70, exe="x.exe", folder=DOCS)
        api.add(0x11, pid=70, exe="x.exe", folder=DOWNLOADS + "\\Sub")
        self.assertEqual(watch.find_folder(DOWNLOADS), 0)                          # never a near match
        api.add(0x12, pid=70, exe="x.exe", folder=DOWNLOADS.upper() + "\\")
        self.assertEqual(watch.find_folder(DOWNLOADS), 0x12)                       # case and a slash aside
        api.add(0x13, pid=70, exe="x.exe", folder=DOWNLOADS)
        api.front = 0x12
        self.assertEqual(watch.find_folder(DOWNLOADS), 0x12)                       # the one in front, of two
        api.front = 0
        self.assertEqual(watch.find_folder(DOWNLOADS), 0x13)                       # else the top one
        for state in ({"minimised": True}, {"cloaked": True}, {"visible": False}, {"owned": True}):
            with self.subTest(state=state):
                for hwnd in (0x12, 0x13):
                    api.set(hwnd, **state)
                self.assertEqual(watch.find_folder(DOWNLOADS), 0)
                for hwnd in (0x12, 0x13):
                    api.set(hwnd, **{key: not value for key, value in state.items()})
        api.add(0x14, pid=api.own, exe="x.exe", folder=DOCS)
        api.remove(0x10)
        self.assertEqual(watch.find_folder(DOCS), 0)                               # never Jarvis's own
        self.assertEqual(watch.find_folder(""), 0)
        api.broken.add("folder_windows")
        self.assertEqual(watch.find_folder(DOWNLOADS), 0)                          # unknown: no bracket
        self.assertEqual(ww.NoWinApi().folder_windows(), [])


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------

class DesktopCardTests(unittest.TestCase):
    def content(self, *, ticked: bool = True, enabled: bool = True) -> hud_overlay.DeskContent:
        rows = (hud_overlay.DeskRow(1, "LING_reading_wk7.pdf", f"Documents {CRUMB} Uni {CRUMB} LING",
                                    "UPLOADS TO ONEDRIVE", hud_overlay.MARK_TICK, ticked, not ticked),
                hud_overlay.DeskRow(2, "setup_zoom_v6.exe", "Documents",
                                    f"UPLOADS TO ONEDRIVE {DOT} INSTALLER - STAYS UNLESS YOU TICK IT",
                                    hud_overlay.MARK_TICK, False, True))
        buttons = (hud_overlay.DeskButton("dismiss", "Not now", holo.AMBER_GHOST),
                   hud_overlay.DeskButton("go", "Move 1 file" if enabled else "Nothing ticked", holo.AMBER_COMMIT,
                                          True, enabled))
        return hud_overlay.DeskContent(f"MOVE FILES {DOT} NEEDS YOUR OK", "Tidy Downloads into your folders",
                                       hud_overlay.TONE_AMBER, rows, "Nothing is deleted. Installers stay where they "
                                       "are. 1 goes to OneDrive and will upload.", hud_overlay.TONE_AMBER, buttons)

    def test_never_the_focus_its_ticks_and_buttons_nofocus_acting_on_click(self) -> None:
        card = hud_overlay.DesktopCard(native=hud_overlay.NoNative())
        self.addCleanup(card.deleteLater)
        card.set_desk(self.content())
        flags = card.windowFlags()
        for flag in (Qt.WindowType.Tool, Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint,
                     Qt.WindowType.WindowDoesNotAcceptFocus):
            self.assertTrue(flags & flag)
        self.assertTrue(card.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        ticks, buttons = card.ticks(), card.buttons()
        self.assertEqual((sorted(ticks), sorted(buttons)), ([1, 2], ["dismiss", "go"]))
        self.assertTrue(all(widget.focusPolicy() == Qt.FocusPolicy.NoFocus
                            for widget in [*ticks.values(), *buttons.values()]))
        self.assertEqual(card.width(), hud_overlay.DESK_WIDTH)
        self.assertEqual(card.cut_texts(), [])
        self.assertEqual(ticks[1].accessibleName(), "Leave out LING_reading_wk7.pdf")
        heard: list[tuple[int, bool]] = []
        clicked: list[str] = []
        card.tickToggled.connect(lambda number, ticked: heard.append((number, ticked)))
        card.buttonClicked.connect(clicked.append)
        card.show()
        settle()
        QTest.mouseClick(ticks[2], Qt.MouseButton.LeftButton)
        QTest.mouseClick(buttons["go"], Qt.MouseButton.LeftButton)
        self.assertEqual((heard, clicked), ([(2, True)], ["go"]))
        card.set_desk(self.content(ticked=False, enabled=False))
        self.assertIs(card.ticks()[2], ticks[2])                                    # the same boxes, set anew
        self.assertFalse(card.ticks()[1].isChecked())
        self.assertFalse(card.buttons()["go"].isEnabled())
        QTest.mouseClick(card.buttons()["go"], Qt.MouseButton.LeftButton)
        self.assertEqual(clicked, ["go"])                                           # disabled: nothing
        card.hide()


# --------------------------------------------------------------------------
# The organising verbs reach the desktop
# --------------------------------------------------------------------------

class RoutingTests(unittest.TestCase):
    def test_organising_verbs_with_a_file_folder_or_place_word_are_desktop_requests(self) -> None:
        for text in (WORDS, "move my essay draft.docx to Uni", "rename my CV", "zip my photos from Sunday",
                     "copy the budget spreadsheet to Documents", "organise my Downloads", "sort out my downloads",
                     "file my receipts", "put the lecture notes into LING", "compress my coursework folder"):
            with self.subTest(text=text):
                self.assertTrue(dr.desktop_request(text))
        for text in ("move my Project sync to Friday and tell Ana", "copy Ana's email to Ben", "sort out my day",
                     "put lunch in my calendar", "move the meeting to 3pm", "rename the meeting", "tidy up",
                     "file a complaint", "move", "copy that"):
            with self.subTest(text=text):
                self.assertFalse(dr.desktop_request(text))


if __name__ == "__main__":
    unittest.main()
