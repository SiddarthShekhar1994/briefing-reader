"""Jarvis on your desktop, P2.3 in the app: the NEEDS YOUR OK card of file steps, its run and its Undo (SPEC 5.4, 6.3,
6.4, 8.1, 8.3, 8.4, 9, 10.1-10.3 and 16 P2.3; hard rules 1, 4, 7, 12, 13, 14 and 17).

A tidy request typed on the HUD goes to the planner (tests.ask_fakes: no real run) and comes back as ONE amber card
beside the File Explorer window showing Downloads (a pretend window: tests.desktop_fakes.FakeWinApi) with a cyan
bracket, its tag and a tether; it counts in "N need your OK". Nothing moves until the owner's click on "Move 5 files";
then exactly the ticked rows move on the desktop thread (the fake backend over a temporary folder - nothing real is
touched), "MOVING 2 OF 5" as each ends, and the stamp; Stop on the card, Esc (tests.desktop_fakes.FakeStopKeys: no real
hot key, held only while the batch acts) or a Turn off stop it after the row on its way; its Undo puts the files back,
newest first, and says plainly what it left ("Moved since - left where it is."). An expired plan, a click too soon
after another decision or while the cards are locked runs nothing. The card never takes the focus, leaving the HUD
leaves no window or hook (Approvals reminds instead), and no name reaches a log. Every name is invented.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from briefing_reader import hud_overlay, ui
from briefing_reader.desktop import journal as jn
from briefing_reader.desktop import tidy as dt
from briefing_reader.desktop import winwatch as ww
from briefing_reader.desktop.context import FileItem
from tests.ask_fakes import plan_stream
from tests.desktop_fakes import DOCS, DOWNLOADS, LING, UNI, FakeStopKeys, FakeWinApi, desktop_context
from tests.test_desktop_requests import DesktopRequestApp
from tests.ui_fakes import fonts, settle, wait_for

DOT = "\u00b7"
CRUMB = "\u203a"
NAMES = ("LING_reading_wk7.pdf", "ECON_problem_set_4.pdf", "IMG_2041.jpg", "Syllabus_HIST210.docx",
         "budget_oct.xlsx", "setup_zoom_v6.exe")
WORDS = "tidy my Downloads into LING, Uni and Documents"
TIDY_PLAN = {"say": "I'd move five files.",
             "steps": [{"skill": "move_files", "why": "x", "files": ["f1"], "to": "d3"},
                       {"skill": "move_files", "why": "x", "files": ["f2", "f4"], "to": "d2"},
                       {"skill": "move_files", "why": "x", "files": ["f3", "f5", "f6"], "to": "d1"}]}
# The card's rows, in order: (name, where it goes) - the installer (row 6) unticked.
ROWS = [("LING_reading_wk7.pdf", LING), ("ECON_problem_set_4.pdf", UNI), ("Syllabus_HIST210.docx", UNI),
        ("IMG_2041.jpg", DOCS), ("budget_oct.xlsx", DOCS), ("setup_zoom_v6.exe", DOCS)]
EXPLORER = 0x3A0
EXPLORER_RECT = (16, 150, 280, 380)          # the pretend File Explorer window on Downloads (one 1x screen)
EXPLORER_EXE = "C:\\Windows\\" + "explorer.exe"
LOGGED_NAMES = ("LING_reading", "ECON", "IMG_2041", "Syllabus", "budget_oct", "setup_zoom", "Downloads", "Uni",
                "Owner", "C:\\")

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_desktop_tidy_app", "-platform", "offscreen"])
    fonts()


class TidyFlowCase(DesktopRequestApp):
    """DesktopRequestApp with the plan's six files in the pretend Downloads, the journal in a temporary folder, the
    fake Esc seam and the pretend File Explorer window on Downloads; the click guard's clock a fake one."""

    def setUp(self) -> None:
        super().setUp()
        screen = QGuiApplication.primaryScreen()
        geometry = screen.geometry()
        self.winapi = FakeWinApi(monitors=[ww.Monitor(screen.name(), ww.Rect(geometry.x(), geometry.y(),
                                                                             geometry.width(), geometry.height()))])
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.journal_dir = os.path.join(tmp.name, "journal")
        self.keys = FakeStopKeys()
        self.clock = [1000.0]

    def make_tidy(self, *, explorer: bool = True) -> None:
        self.make_ask(plan_stream(TIDY_PLAN), desktop_window_api=self.winapi)
        files = []
        for name in NAMES:
            path = DOWNLOADS + "\\" + name
            self.fake.make_file(path, b"x" * 100)
            files.append(FileItem(path, name, DOWNLOADS, size=100))
        context = desktop_context(files=files, backend=self.fake)
        self.desktop.context_source = lambda _words: context
        self.desktop.journal_folder = lambda: self.journal_dir
        self.desktop.stop_keys = self.keys
        self.c._click_clock = lambda: self.clock[0]
        if explorer:
            self.winapi.add(EXPLORER, pid=61, exe=EXPLORER_EXE, rect=EXPLORER_RECT, folder=DOWNLOADS)
        self.enter()
        self.type_on_hud(WORDS)
        self.finish()
        settle()

    def later(self, seconds: float = 2.0) -> None:
        self.clock[0] += seconds

    def entry(self):  # type: ignore[no-untyped-def]
        return self.desktop.results.tidy()

    def card(self) -> hud_overlay.DesktopCard:
        entry = self.entry()
        self.assertIsNotNone(entry, "the tidy card shows")
        return entry.window

    def press(self, key: str, *, wait: bool = True) -> None:
        QTest.mouseClick(self.card().buttons()[key], Qt.MouseButton.LeftButton)
        settle()
        if wait:
            self.assertTrue(wait_for(lambda: not self.desktop.busy()), "the card's job ended")
            settle()

    def tick(self, number: int) -> None:
        QTest.mouseClick(self.card().ticks()[number], Qt.MouseButton.LeftButton)
        settle()

    def where(self, number: int) -> tuple[str, str]:
        """Row ``number``'s file: (where it was, where the plan puts it) - pretend paths."""
        name, folder = ROWS[number - 1]
        return DOWNLOADS + "\\" + name, folder + "\\" + name

    def there(self, path: str) -> bool:
        return os.path.lexists(self.fake.real(path))

    def moves(self) -> list[str]:
        return [argument for method, argument in self.fake.calls if method == "move_item"]

    def hold_at_move(self, number: int) -> tuple[threading.Event, threading.Event]:
        """The desktop thread holds at its ``number``th move (before it is made) until the test lets it go."""
        holding, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        seen = [0]

        def slow(method: str, _argument: str) -> None:
            if method == "move_item":
                seen[0] += 1
                if seen[0] == number:
                    holding.set()
                    release.wait(10)
        self.fake.on_call = slow
        return holding, release

    def kicker(self) -> str:
        return self.card().desk().kicker

    def rows(self) -> list[hud_overlay.DeskRow]:
        return list(self.card().desk().rows)

    def tasks(self) -> list:
        return [task for task in self.desktop_tasks() if "NEEDS YOUR OK" in task.title or task.title.startswith("Undo")]


class WaitingCardTests(TidyFlowCase):
    def test_the_card_waits_beside_downloads_amber_and_nothing_moves_without_the_click(self) -> None:
        self.make_tidy()
        card = self.card()
        content = card.desk()
        self.assertEqual(content.kicker, f"MOVE FILES {DOT} NEEDS YOUR OK")
        self.assertEqual(content.title, "Tidy Downloads into your folders")
        self.assertEqual(content.tone, hud_overlay.TONE_AMBER)                  # a decision waits
        self.assertEqual([(row.name, row.to) for row in content.rows],
                         [(name, f"Documents {CRUMB} Uni {CRUMB} LING" if folder == LING else
                           f"Documents {CRUMB} Uni" if folder == UNI else "Documents") for name, folder in ROWS])
        self.assertEqual([(row.mark, row.ticked, row.struck) for row in content.rows],
                         [(hud_overlay.MARK_TICK, True, False)] * 5 + [(hud_overlay.MARK_TICK, False, True)])
        self.assertEqual(content.rows[0].flags, "UPLOADS TO ONEDRIVE")             # D3: flagged, in words
        self.assertEqual(content.rows[5].flags, f"UPLOADS TO ONEDRIVE {DOT} INSTALLER - STAYS UNLESS YOU TICK IT")
        self.assertEqual(content.note, "Nothing is deleted. Installers stay where they are. 5 go to OneDrive and "
                                       "will upload.")
        self.assertEqual([(button.key, button.text, button.enabled) for button in content.buttons],
                         [("dismiss", "Not now", True), ("go", "Move 5 files", True)])
        self.assertEqual(card.cut_texts(), [])
        self.assertEqual(self.moves(), [])                                         # nothing until the click
        self.assertEqual(self.keys.arms, 0)                                        # Esc never held while it waits
        # Beside the File Explorer window showing Downloads: the cyan bracket, its tag and a tether.
        explorer = QRect(*EXPLORER_RECT)
        entry = self.entry()
        self.assertTrue(entry.found() and entry.following())
        layer = self.desktop.overlay.layer_for(QGuiApplication.primaryScreen())
        self.assertEqual(layer.brackets(), (hud_overlay.Bracket(explorer, hud_overlay.TONE_CYAN, "DOWNLOADS"),))
        self.assertEqual(len(layer.tethers()), 1)
        self.assertFalse(card.geometry().intersects(explorer))
        self.assertFalse(card.geometry().intersects(self.hud().occupied_rect()))
        self.assertEqual({hook[2] for hook in self.winapi.hooks.values()}, {61})   # Explorer's process only
        # It counts in "N need your OK" while it waits.
        pending = self.reading.pending_count()
        self.assertIn(hud_overlay.needs_ok_text(pending + 1), self.hud().chip_texts())
        self.assertEqual(self.desktop.waiting_count(), 1)
        # A tool window on top that never takes the focus; its ticks and buttons NoFocus.
        flags = card.windowFlags()
        for flag in (Qt.WindowType.Tool, Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint,
                     Qt.WindowType.WindowDoesNotAcceptFocus):
            self.assertTrue(flags & flag)
        controls = [*card.buttons().values(), *card.ticks().values()]
        self.assertTrue(controls and all(control.focusPolicy() == Qt.FocusPolicy.NoFocus for control in controls))
        self.assertIn(int(card.winId()), self.native.excluded)
        self.native.keys.clear()
        focus = QApplication.focusWidget()
        self.tick(3)
        self.tick(3)
        self.assertEqual(self.native.keys, [])                                    # a tick activates nothing ...
        self.assertIs(QApplication.focusWidget(), focus)                          # ... and moves no focus

    def test_not_now_takes_the_card_away_and_carries_nothing_out(self) -> None:
        self.make_tidy()
        self.press("dismiss", wait=False)
        self.assertIsNone(self.entry())
        self.assertIsNone(self.desktop.tidy())
        self.assertEqual(self.desktop.overlay.cards(), [])
        self.assertEqual(self.moves(), [])
        self.assertEqual(self.desktop.waiting_count(), 0)
        self.assertEqual(self.winapi.live_hooks(), 0)

    def test_untick_all_says_nothing_ticked_and_the_button_does_nothing(self) -> None:
        self.make_tidy()
        for number in range(1, 6):
            self.tick(number)
        go = self.card().buttons()["go"]
        self.assertEqual((go.text(), go.isEnabled()), ("Nothing ticked", False))
        QTest.mouseClick(go, Qt.MouseButton.LeftButton)
        settle()
        self.assertEqual(self.desktop.tidy().stage, dt.WAITING)
        self.assertEqual(self.moves(), [])
        self.tick(6)                                                               # the installer, ticked by hand
        self.assertEqual(self.card().buttons()["go"].text(), "Move 1 file")
        self.assertNotIn("Installers stay", self.card().desk().note)

    def test_leaving_the_hud_keeps_the_waiting_card_as_data_and_approvals_reminds(self) -> None:
        self.make_tidy()
        self.back()
        self.assertEqual(self.desktop.overlay.cards(), [])                       # no card window ...
        self.assertEqual(self.winapi.live_hooks(), 0)                            # ... and no hook in the window
        self.assertTrue(self.desktop.tidy().waiting())
        self.reading.set_tab(ui.SCREEN_APPROVALS)
        settle()
        row = self.c._desktop_waiting
        self.assertTrue(row.isVisible())
        self.assertEqual(row.label.text(), "Waiting on your desktop")
        QTest.mouseClick(row.show_me, Qt.MouseButton.LeftButton)                 # "Show me": onto the HUD
        settle()
        self.assert_on_hud()
        self.assertEqual(self.kicker(), f"MOVE FILES {DOT} NEEDS YOUR OK")
        self.assertEqual(self.moves(), [])


class RunTests(TidyFlowCase):
    def test_approve_moves_every_ticked_row_on_the_desktop_thread_then_the_stamp(self) -> None:
        self.make_tidy()
        self.native.keys.clear()
        focus = QApplication.focusWidget()
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            self.press("go")
        for number in range(1, 6):
            old, new = self.where(number)
            self.assertFalse(self.there(old))
            self.assertTrue(self.there(new))
        self.assertTrue(self.there(self.where(6)[0]))                              # the installer stayed
        self.assertEqual(len(self.moves()), 5)
        self.assert_on_the_desktop_thread()                                        # never the calendar's worker
        self.assertEqual((self.keys.arms, self.keys.disarms, self.keys.armed()), (1, 1, False))
        self.assertEqual(set(self.keys.threads), {"desktop"})
        content = self.card().desk()
        self.assertTrue(content.kicker.startswith(f"MOVED 5 FILES {DOT} "), content.kicker)
        self.assertEqual(content.tone, hud_overlay.TONE_DONE)
        self.assertEqual([row.mark for row in content.rows], [hud_overlay.MARK_DONE] * 5 + [hud_overlay.MARK_IDLE])
        self.assertEqual(content.link, ("undo", "Undo"))
        self.assertEqual(content.link_note, "Puts every file back where it was.")
        self.assertEqual(content.buttons, ())
        self.assertEqual(self.card().cut_texts(), [])
        self.assertEqual(self.hud().caption_text(), "Moved 5 files, sir.")
        self.assertEqual(self.desktop.waiting_count(), 0)
        self.assertEqual(self.native.keys, [])                                     # the click activated nothing
        self.assertIs(QApplication.focusWidget(), focus)
        journal = jn.Journal(self.journal_dir, self.desktop.tidy().card.run_id)
        self.assertEqual(sorted(item.state for item in journal.items().values()), [jn.DONE] * 5)
        task = self.tasks()[-1]
        self.assertEqual(task.title, "Move 5 files - your click on a NEEDS YOUR OK card")
        fields = {field.label: field.value for field in task.steps[0].fields}
        self.assertTrue(fields["Approved"].startswith("Approved by your click "))
        self.assertEqual([item.text for item in task.steps[1].items],
                         [f"Row {number} - move - done" for number in range(1, 6)])
        text = "\n".join(logs.output)
        self.assertRegex(text, r"Desktop \w+: done 5/5, skipped 0, failed 0, \d+\.\d s")
        for name in LOGGED_NAMES:
            self.assertNotIn(name, text)

    def test_partial_untick_moves_only_the_rows_left_ticked(self) -> None:
        self.make_tidy()
        self.tick(2)
        self.tick(4)
        self.assertEqual(self.card().buttons()["go"].text(), "Move 3 files")
        self.assertEqual([row.struck for row in self.rows()], [False, True, False, True, False, True])
        self.press("go")
        self.assertEqual([self.there(self.where(number)[1]) for number in range(1, 7)],
                         [True, False, True, False, True, False])
        self.assertEqual([self.there(self.where(number)[0]) for number in (2, 4, 6)], [True, True, True])
        self.assertTrue(self.kicker().startswith(f"MOVED 3 FILES {DOT} "))
        self.assertEqual([row.mark for row in self.rows()],
                         [hud_overlay.MARK_DONE, hud_overlay.MARK_IDLE, hud_overlay.MARK_DONE, hud_overlay.MARK_IDLE,
                          hud_overlay.MARK_DONE, hud_overlay.MARK_IDLE])

    def test_the_cards_stop_mid_batch_stops_after_the_row_on_its_way(self) -> None:
        self.make_tidy()
        holding, release = self.hold_at_move(2)
        self.press("go", wait=False)
        self.assertTrue(wait_for(holding.is_set))
        self.assertTrue(wait_for(lambda: self.kicker() == f"MOVING 2 OF 5{dt.ELLIPSIS}"))
        self.assertEqual(self.card().desk().tone, hud_overlay.TONE_CYAN)          # not amber: no decision waits
        self.assertTrue(self.keys.armed())                                         # Esc held while it acts
        self.assertEqual(self.rows()[0].mark, hud_overlay.MARK_DONE)
        self.press("stop", wait=False)
        self.assertEqual(self.kicker(), f"STOPPING AFTER THIS ONE{dt.ELLIPSIS}")
        release.set()
        self.assertTrue(wait_for(lambda: not self.desktop.busy()))
        settle()
        self.assertEqual(len(self.moves()), 2)                                     # the one on its way finished
        self.assertTrue(self.kicker().startswith(f"STOPPED BY YOU {DOT} MOVED 2 OF 5 FILES {DOT} "), self.kicker())
        self.assertEqual([row.words for row in self.rows()[2:5]], ["Not done - you stopped it."] * 3)
        self.assertEqual(self.hud().caption_text(), "Stopped, sir - moved 2 of 5.")
        self.assertFalse(self.keys.armed())
        task = self.tasks()[-1]
        stop = next(step for step in task.steps if step.title == "Stopped")
        self.assertEqual({field.label: field.value for field in stop.fields}["Why"], "Stop on the card")

    def test_esc_mid_batch_stops_it_and_is_held_only_while_it_acts(self) -> None:
        self.make_tidy()
        self.assertFalse(self.keys.press())                                        # at idle Esc is not Jarvis's
        holding, release = self.hold_at_move(3)
        self.press("go", wait=False)
        self.assertTrue(wait_for(holding.is_set))
        self.assertTrue(self.keys.press())                                         # the owner's Esc, anywhere
        self.assertTrue(wait_for(lambda: self.kicker() == f"STOPPING AFTER THIS ONE{dt.ELLIPSIS}"))
        release.set()
        self.assertTrue(wait_for(lambda: not self.desktop.busy()))
        settle()
        self.assertEqual(len(self.moves()), 3)
        self.assertTrue(self.kicker().startswith(f"STOPPED BY YOU {DOT} MOVED 3 OF 5 FILES"))
        self.assertEqual((self.keys.arms, self.keys.disarms, self.keys.armed()), (1, 1, False))
        self.assertFalse(self.keys.press())                                        # let go after the batch
        task = self.tasks()[-1]
        stop = next(step for step in task.steps if step.title == "Stopped")
        fields = {field.label: field.value for field in stop.fields}
        self.assertEqual(fields["Why"], "Esc")
        self.assertRegex(fields["Took"], r"^\d+ ms to stop$")

    def test_turn_off_mid_run_stops_at_the_next_row_and_the_card_goes(self) -> None:
        self.make_tidy()
        holding, release = self.hold_at_move(2)
        self.press("go", wait=False)
        self.assertTrue(wait_for(holding.is_set))
        self.assertTrue(self.desktop.turn_off())
        release.set()
        self.assertTrue(wait_for(lambda: not self.desktop.busy()))
        settle()
        self.assertEqual(len(self.moves()), 2)                                     # nothing after the Turn off
        self.assertTrue(self.there(self.where(3)[0]))
        self.assertIsNone(self.desktop.tidy())
        self.assertIsNone(self.entry())
        self.assertFalse(self.keys.armed())

    def test_an_expired_plan_is_refused_in_plain_words_and_nothing_moves(self) -> None:
        self.make_tidy()
        self.desktop._service._now = lambda: datetime.now(timezone.utc) + timedelta(minutes=16)
        self.press("go")
        self.assertEqual(self.moves(), [])
        self.assertEqual(self.keys.arms, 0)                                        # nothing acted: Esc never held
        content = self.card().desk()
        self.assertTrue(content.kicker.startswith(f"NOTHING MOVED {DOT} "))
        self.assertEqual(content.tone, hud_overlay.TONE_CORAL)                     # a refusal: coral
        self.assertEqual(content.note, "This card is more than 15 minutes old, so nothing was moved. Ask again for "
                                       "a fresh one.")
        self.assertEqual(self.hud().caption_text(),
                         "Nothing was moved, sir - the card was more than 15 minutes old.")

    def test_a_click_too_soon_or_while_locked_runs_nothing(self) -> None:
        self.make_tidy()
        self.c._decision_clicked_at = self.clock[0] - 0.5                          # another decision 0.5 s ago
        self.press("go", wait=False)
        self.assertEqual(self.desktop.tidy().stage, dt.WAITING)
        self.assertEqual(self.moves(), [])
        self.later()
        self.c._connect_alias = "personal"                                         # a sign-in locks the cards
        self.press("go", wait=False)
        self.assertEqual(self.desktop.tidy().stage, dt.WAITING)
        self.c._connect_alias = None
        self.later()
        self.press("go")
        self.assertEqual(len(self.moves()), 5)


class UndoTests(TidyFlowCase):
    def test_undo_after_a_change_puts_back_the_rest_with_the_partial_stamp(self) -> None:
        self.make_tidy()
        self.press("go")
        self.later()
        moved_away = DOCS + "\\elsewhere.pdf"                                      # row 1 moved since
        os.rename(self.fake.real(self.where(1)[1]), self.fake.real(moved_away))
        self.fake.make_file(self.where(2)[0], b"new")                              # something new in row 2's place
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            self.press("undo")
        for number in (3, 4, 5):
            old, new = self.where(number)
            self.assertTrue(self.there(old))
            self.assertFalse(self.there(new))
        self.assertTrue(self.there(moved_away) and self.there(self.where(2)[1]))   # left as they are
        content = self.card().desk()
        self.assertTrue(content.kicker.startswith(
            f"PUT BACK 3 OF 5 {DOT} 1 MOVED SINCE {DOT} 1 SOMETHING NEW IN ITS PLACE"), content.kicker)
        self.assertEqual(content.tone, hud_overlay.TONE_CORAL)
        self.assertEqual([row.words for row in content.rows[:5]],
                         ["Moved since - left where it is.", "Something new is in its old place - left where it is.",
                          "Put back.", "Put back.", "Put back."])
        self.assertEqual(content.note, "2 were left as they are - see why above.")
        self.assertEqual(self.card().cut_texts(), [])
        self.assertIsNone(content.link)
        self.assertEqual(self.hud().caption_text(), "Put back 3 of 5, sir - see the card for the rest.")
        self.assertEqual((self.keys.arms, self.keys.disarms), (2, 2))               # Esc held for the Undo too
        undo = self.tasks()[-1]
        self.assertEqual(undo.title, "Undo - your click on the card's stamp")
        self.assertEqual(sorted(item.text for item in undo.steps[1].items),
                         ["Row 1 - move - left: moved_since", "Row 2 - move - left: occupied",
                          "Row 3 - move - put back", "Row 4 - move - put back", "Row 5 - move - put back"])
        text = "\n".join(logs.output)
        self.assertRegex(text, r"Desktop \w+: put back 3/5, left 2, \d+\.\d s")
        for name in LOGGED_NAMES:
            self.assertNotIn(name, text)

    def test_undo_puts_everything_back_newest_first(self) -> None:
        self.make_tidy()
        self.press("go")
        self.later()
        self.fake.calls.clear()
        self.press("undo")
        self.assertEqual([self.there(self.where(number)[0]) for number in range(1, 7)], [True] * 6)
        backs = [argument for method, argument in self.fake.calls if method == "move_item"]
        self.assertEqual(backs, [self.where(number)[1] for number in (5, 4, 3, 2, 1)])    # reverse order
        content = self.card().desk()
        self.assertTrue(content.kicker.startswith(f"PUT BACK 5 OF 5 {DOT} "), content.kicker)
        self.assertEqual(content.tone, hud_overlay.TONE_DONE)
        self.assertEqual(content.note, "Everything is back as it was.")
        self.assertEqual(self.hud().caption_text(), "Put back, sir. Everything is where it was.")

    def test_undo_is_offered_for_undo_days_only(self) -> None:
        self.make_tidy()
        self.press("go")
        self.later()
        self.desktop.tidy().at = datetime.now() - timedelta(days=8)              # [desktop] undo_days: 7
        self.fake.calls.clear()
        self.press("undo")
        self.assertEqual(self.moves(), [])
        self.assertIsNone(self.card().desk().link)
        self.assertEqual(self.hud().caption_text(), "That was more than 7 days ago, sir - Undo isn't offered any more.")

    def test_undo_is_the_owners_click_only_and_passes_the_guards(self) -> None:
        self.make_tidy()
        self.press("go")
        self.press("undo", wait=False)                                             # within 1 s of the approval
        self.assertEqual(self.desktop.tidy().stage, dt.DONE)
        self.assertTrue(all(self.there(self.where(number)[1]) for number in range(1, 6)))
        self.later()
        self.press("undo")
        self.assertEqual(self.desktop.tidy().stage, dt.UNDONE)


if __name__ == "__main__":
    unittest.main()
