"""Jarvis on your desktop, P2.1: the journal (desktop.journal; SPEC 6.1 and 15; hard rules 12 and 14) - records and
states, the flush before each OS call, a line a crash tore, start-up recovery of every state a crash can leave (an
item found at its source, at its destination, at neither; never redone) and the purge after 30 days.

Crashes are made for real in the middle of desktop.fileops' rows: the fake backend's call is wrapped to raise an
exception nothing catches, before or after the call itself, on tests.desktop_fakes' temporary disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

from briefing_reader.desktop import backend as dbk
from briefing_reader.desktop import fileops as fo
from briefing_reader.desktop import journal as jn
from briefing_reader.desktop import paths
from briefing_reader.desktop.context import FileItem, Place
from tests.desktop_fakes import DOCS, DOWNLOADS, LING, FakeRecycleBin, desktop_disk

NOW = 1_800_000_000.0
DAY = 86_400
BUDGET = DOCS + "\\Budget 2026.xlsx"
ESSAY = LING + "\\LING essay draft.docx"
CANARY = "CANARY_j21"
_FILE_OPS = ("move_item", "copy_file", "make_folder", "remove_folder", "write_zip")


class _Crash(BaseException):
    """The app dying in the middle of a row (nothing catches it)."""


class RecordTests(unittest.TestCase):
    def test_every_kind_and_state_round_trips_as_one_ascii_line(self) -> None:
        for kind in jn.KINDS:
            for state in jn.STATES:
                record = jn.Record(3, kind, state, src="C:\\Users\\Owner\\Documents\\R\u00e9sum\u00e9.docx",
                                   dst="C:\\Users\\Owner\\Downloads\\R\u00e9sum\u00e9.docx", volume_serial=0xBEEF,
                                   file_id=2 ** 127 + 5, size=12, mtime_ns=1_700_000_000_123_456_789, attrs=0x20,
                                   sha256="ab" * 32, reason_code="changed", is_dir=False, undo=state == jn.UNDONE,
                                   at=NOW, tmp="C:\\t.part", inputs=("C:\\a", "C:\\b"), flags=("online_only",))
                line = record.to_line()
                self.assertTrue(line.isascii() and line.endswith(b"\n") and line.count(b"\n") == 1)
                self.assertEqual(jn.Record.from_json(json.loads(line)), record)
        self.assertNotIn("Owner", repr(record))                            # a record's repr never shows a path
        self.assertEqual(record.identity, dbk.FileIdentity(0xBEEF, 2 ** 127 + 5, 12, 1_700_000_000_123_456_789))

    def test_what_is_not_a_record_is_not_read(self) -> None:
        good = json.loads(jn.Record(1, jn.MOVE, jn.DONE).to_line())
        for change in ({"seq": 0}, {"seq": "1"}, {"seq": True}, {"kind": "delete"}, {"state": "gone"},
                       {"size": -1}, {"is_dir": "yes"}, {"sha256": "xyz"}, {"flags": "online_only"},
                       {"inputs": [1]}, {"src": 5}, {"at": "today"}):
            with self.subTest(change=change):
                self.assertIsNone(jn.Record.from_json({**good, **change}))
        self.assertIsNone(jn.Record.from_json([good]))
        self.assertIsNotNone(jn.Record.from_json({**good, "later_field": 1}))   # newer fields are ignored

    def test_run_ids_name_a_file_and_nothing_else(self) -> None:
        for run_id in ("d7f3", "run_1-A", "a" * 64):
            self.assertTrue(jn.run_id_ok(run_id), run_id)
        for run_id in ("", "..\\x", "a/b", "C:x", "a.b", " a", "a" * 65, None, "-a"):
            self.assertFalse(jn.run_id_ok(run_id), run_id)
            with self.assertRaises(ValueError):
                jn.Journal(tempfile.gettempdir(), run_id)                   # type: ignore[arg-type]


class JournalFileTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = os.path.join(tmp.name, "journal")
        self.journal = jn.Journal(self.folder, "r1", clock=lambda: NOW)

    def test_each_save_is_flushed_to_the_disk(self) -> None:
        real_fsync = os.fsync
        flushed: list[int] = []

        def fsync(descriptor: int) -> None:
            flushed.append(descriptor)
            real_fsync(descriptor)

        with mock.patch.object(os, "fsync", fsync):
            self.journal.append(jn.Record(1, jn.MOVE, jn.DOING))
            self.journal.extend([jn.Record(2, jn.MOVE, jn.PLANNED), jn.Record(3, jn.MOVE, jn.PLANNED)])
        self.assertEqual(len(flushed), 2)                                   # one flush per save
        self.assertEqual([record.at for record in self.journal.records()], [NOW] * 3)
        self.assertEqual(self.journal.next_seq(), 4)

    def test_a_torn_line_is_skipped_and_the_next_record_starts_afresh(self) -> None:
        self.journal.append(jn.Record(1, jn.MOVE, jn.DOING))
        with open(self.journal.path, "ab") as handle:
            handle.write(b'{"seq":1,"kind":"move","sta')                    # the app died mid-write
        self.journal.append(jn.Record(2, jn.COPY, jn.PLANNED))
        records, bad = self.journal.read()
        self.assertEqual([(r.seq, r.state) for r in records], [(1, jn.DOING), (2, jn.PLANNED)])
        self.assertEqual(bad, 1)

    def test_items_take_their_latest_state(self) -> None:
        self.journal.extend([jn.Record(1, jn.MOVE, jn.PLANNED), jn.Record(2, jn.ZIP, jn.PLANNED),
                             jn.Record(1, jn.MOVE, jn.DOING), jn.Record(1, jn.MOVE, jn.DONE, file_id=7,
                                                                        volume_serial=1),
                             jn.Record(1, jn.MOVE, jn.DOING, undo=True), jn.Record(2, jn.COPY, jn.DONE),
                             jn.Record(2, jn.ZIP, jn.SKIPPED, reason_code="changed")])
        items = self.journal.items()
        self.assertEqual((items[1].state, items[1].undoing, items[1].done.file_id), (jn.DOING, True, 7))
        self.assertEqual(items[1].doing.undo, False)
        self.assertEqual((items[2].kind, items[2].state, items[2].reason_code), (jn.ZIP, jn.SKIPPED, "changed"))
        self.assertEqual(len(items[2].records), 2)                          # a line of another kind is ignored

    def test_a_journal_that_cannot_be_saved_raises(self) -> None:
        os.makedirs(self.journal.path)                                      # its name is taken by a folder
        with self.assertRaises(jn.JournalError), self.assertLogs("briefing_reader.desktop", "WARNING") as logs:
            self.journal.append(jn.Record(1, jn.MOVE, jn.DOING))
        self.assertNotIn(self.folder, "\n".join(logs.output))

    def test_the_real_journal_folder_is_never_used_while_the_tests_run(self) -> None:
        with self.assertRaises(dbk.RealBackendInTests):
            jn.default_folder()

    def test_purge_removes_runs_older_than_30_days_and_nothing_else(self) -> None:
        def run(run_id: str, *ages: float) -> str:
            for number, age in enumerate(ages, 1):
                jn.Journal(self.folder, run_id, clock=lambda age=age: NOW - age * DAY).append(
                    jn.Record(number, jn.MOVE, jn.DONE))
            return run_id + ".jsonl"

        old, recent, mixed = run("old", 31), run("recent", 29), run("mixed", 40, 2)
        blank = os.path.join(self.folder, "blank.jsonl")
        open(blank, "wb").close()
        os.utime(blank, (NOW - 45 * DAY, NOW - 45 * DAY))                   # no record: the file's own time counts
        others = ("notes.txt", "bad name.jsonl", "x.jsonl.bak")
        for name in others:
            open(os.path.join(self.folder, name), "wb").close()
            os.utime(os.path.join(self.folder, name), (NOW - 90 * DAY, NOW - 90 * DAY))
        with self.assertLogs("briefing_reader.desktop", "INFO") as logs:
            self.assertEqual(jn.purge(self.folder, now=NOW), 2)
        left = set(os.listdir(self.folder))
        self.assertEqual(left, {recent, mixed, *others})
        self.assertNotIn(old, left)
        self.assertIn("purged 2 run(s)", "\n".join(logs.output))
        self.assertEqual(jn.purge(os.path.join(self.folder, "missing"), now=NOW), 0)


class RecoveryTests(unittest.TestCase):
    """An item a crash left "doing" is corrected at the next start by looking - never redone."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.fake = desktop_disk(os.path.join(tmp.name, "disk"))
        self.folder = os.path.join(tmp.name, "journal")
        self.journal = jn.Journal(self.folder, "crash1")
        self.ops = fo.FileOps(self.fake, self.journal, scope=self.scope)

    def scope(self) -> paths.Scope:
        return paths.load_scope(dbk.ROOT_FOLDERS, (), self.fake)

    def item(self, path: str) -> FileItem:
        return FileItem(path, path.rsplit("\\", 1)[-1], identity=self.fake.identity(path))

    def place(self, path: str) -> Place:
        return Place(path, (path.rsplit("\\", 1)[-1],), identity=self.fake.identity(path))

    def there(self, path: str) -> bool:
        return os.path.lexists(self.fake.real(path))

    def crash(self, method: str, *, after: bool, nth: int = 1) -> None:
        """The app dies at the ``nth`` call of ``method``: just before it, or just after it is done."""
        original = getattr(self.fake, method)
        seen: list[int] = []

        def crashing(*args, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(1)
            if len(seen) == nth and not after:
                raise _Crash()
            result = original(*args, **kwargs)
            if len(seen) == nth:
                raise _Crash()
            return result

        setattr(self.fake, method, crashing)
        self.addCleanup(lambda: self.fake.__dict__.pop(method, None))

    def heal(self) -> None:
        for method in _FILE_OPS + ("content_hash",):
            self.fake.__dict__.pop(method, None)

    def crashed(self, *ops: fo.FileOp) -> None:
        with self.assertRaises(_Crash):
            self.ops.run(list(ops))
        self.heal()
        self.assertEqual(self.journal.items()[max(self.journal.items())].state, jn.DOING)

    def recover(self) -> jn.RecoveryReport:
        before = len(self.fake.calls)
        report = jn.recover(self.folder, self.fake)
        asked = {method for method, _argument in self.fake.calls[before:]}
        self.assertEqual(asked & set(_FILE_OPS), set())                     # looked only: nothing redone
        return report

    def last(self, seq: int | None = None) -> jn.Record:
        items = self.journal.items()
        return items[seq if seq is not None else max(items)].last

    def test_a_move_found_at_its_destination_its_source_or_neither(self) -> None:
        self.crash("move_item", after=True)
        self.crashed(fo.FileOp(fo.MOVE, (self.item(BUDGET),), self.place(DOWNLOADS)))
        self.crash("move_item", after=False)
        self.crashed(fo.FileOp(fo.RENAME, (self.item(ESSAY),), None, "Essay v2.docx"))
        self.crash("move_item", after=True)
        self.crashed(fo.FileOp(fo.MOVE, (self.item(LING + "\\LING reading list.pdf"),), self.place(DOCS)))
        os.remove(self.fake.real(DOCS + "\\LING reading list.pdf"))         # then the owner removed it
        report = self.recover()
        self.assertEqual([(self.last(n).state, self.last(n).reason_code) for n in (1, 2, 3)],
                         [(jn.DONE, jn.R_RECOVERED), (jn.FAILED, jn.R_INTERRUPTED), (jn.FAILED, jn.R_LOST)])
        self.assertEqual(report.corrected, {jn.DONE: 1, jn.FAILED: 2})
        self.assertEqual(self.last(1).file_id, self.fake.identity(DOWNLOADS + "\\Budget 2026.xlsx").file_id)
        self.assertTrue(self.there(ESSAY))                                  # never redone
        self.assertEqual(self.recover().items, 0)                           # corrected once
        self.assertEqual(fo.Undoer(self.fake, self.journal, scope=self.scope).undo().put_back, 1)
        self.assertTrue(self.there(BUDGET))

    def test_a_file_copy_whole_part_made_or_not_made(self) -> None:
        self.crash("copy_file", after=True)
        self.crashed(fo.FileOp(fo.COPY, (self.item(BUDGET),), self.place(DOWNLOADS)))
        self.crash("copy_file", after=True)
        self.crashed(fo.FileOp(fo.COPY, (self.item(ESSAY),), self.place(DOWNLOADS)))
        with open(self.fake.real(DOWNLOADS + "\\LING essay draft.docx"), "r+b") as handle:
            handle.truncate(100)                                            # what a crash mid-copy leaves
        self.crash("copy_file", after=False)
        self.crashed(fo.FileOp(fo.COPY, (self.item(LING + "\\LING reading list.pdf"),), self.place(DOWNLOADS)))
        self.recover()
        self.assertEqual([(self.last(n).state, self.last(n).reason_code) for n in (1, 2, 3)],
                         [(jn.DONE, jn.R_RECOVERED), (jn.FAILED, jn.R_PARTIAL), (jn.FAILED, jn.R_INTERRUPTED)])
        with open(self.fake.real(BUDGET), "rb") as handle:
            self.assertEqual(self.last(1).sha256, hashlib.sha256(handle.read()).hexdigest())
        self.assertTrue(self.there(DOWNLOADS + "\\LING essay draft.docx"))  # part-made: left for the owner

    def test_a_zip_renamed_into_place_left_under_its_temporary_name_or_not_begun(self) -> None:
        self.crash("move_item", after=True)
        self.crashed(fo.FileOp(fo.ZIP, (self.item(BUDGET),), self.place(DOWNLOADS), "One.zip"))
        self.crash("move_item", after=False)
        self.crashed(fo.FileOp(fo.ZIP, (self.item(ESSAY),), self.place(DOWNLOADS), "Two.zip"))
        self.crash("write_zip", after=False)
        self.crashed(fo.FileOp(fo.ZIP, (self.item(ESSAY),), self.place(DOWNLOADS), "Three.zip"))
        self.fake.make_file(DOWNLOADS + "\\Three.zip", b"the owner's own zip, made after the crash")
        self.recover()
        self.assertEqual([(self.last(n).state, self.last(n).reason_code) for n in (1, 2, 3)],
                         [(jn.DONE, jn.R_RECOVERED), (jn.FAILED, jn.R_PARTIAL), (jn.FAILED, jn.R_INTERRUPTED)])
        with open(self.fake.real(DOWNLOADS + "\\One.zip"), "rb") as handle:
            self.assertEqual(self.last(1).sha256, hashlib.sha256(handle.read()).hexdigest())
        self.assertTrue(self.there(self.last(2).tmp))                       # left, never removed here
        self.assertFalse(self.there(DOWNLOADS + "\\Two.zip"))
        self.assertEqual(self.last(3).file_id, 0)                           # the owner's zip is never taken for it

    def test_a_folder_copy_and_a_new_folder(self) -> None:
        self.crash("move_item", after=True)
        self.crashed(fo.FileOp(fo.COPY, (self.place(LING),), self.place(DOWNLOADS)))
        self.crash("copy_file", after=True)
        self.crashed(fo.FileOp(fo.COPY, (self.place(LING),), self.place(DOCS)))
        self.crash("make_folder", after=True)
        self.crashed(fo.FileOp(fo.MKDIR, (), self.place(DOCS), "Made"))
        self.crash("make_folder", after=False)
        self.crashed(fo.FileOp(fo.MKDIR, (), self.place(DOCS), "Not made"))
        self.recover()
        self.assertEqual([(self.last(n).state, self.last(n).reason_code) for n in (1, 2, 3, 4)],
                         [(jn.DONE, jn.R_RECOVERED), (jn.FAILED, jn.R_PARTIAL), (jn.DONE, jn.R_RECOVERED),
                          (jn.FAILED, jn.R_INTERRUPTED)])
        self.assertEqual(self.last(1).sha256, fo.tree_digest(DOWNLOADS + "\\LING", self.fake))
        self.assertTrue(self.last(3).is_dir and self.last(3).file_id)

    def test_an_undo_left_doing(self) -> None:
        report = self.ops.run([fo.FileOp(fo.MOVE, (self.item(BUDGET),), self.place(DOWNLOADS)),
                               fo.FileOp(fo.MOVE, (self.item(ESSAY),), self.place(DOWNLOADS)),
                               fo.FileOp(fo.MOVE, (self.item(LING + "\\LING reading list.pdf"),), self.place(DOCS)),
                               fo.FileOp(fo.COPY, (self.item(DOCS + "\\Grades macro.xlsm"),), self.place(DOWNLOADS)),
                               fo.FileOp(fo.COPY, (self.item(DOWNLOADS + "\\SpotifySetup.exe"),), self.place(DOCS))])
        self.assertEqual({r.state for r in report.results}, {jn.DONE})
        bin_ = FakeRecycleBin(self.fake)
        undoer = fo.Undoer(self.fake, self.journal, scope=self.scope, recycler=bin_)
        for seq, method, after in ((1, "move_item", True), (2, "move_item", False), (3, "move_item", False)):
            self.crash(method, after=after)
            with self.assertRaises(_Crash):
                undoer.undo([seq])
            self.heal()
        os.remove(self.fake.real(DOCS + "\\LING reading list.pdf"))         # gone from where Jarvis put it
        for seq, after in ((4, True), (5, False)):
            original = bin_.recycle

            def crashing(path: str, after: bool = after, original=original) -> None:  # type: ignore[no-untyped-def]
                if after:
                    original(path)
                raise _Crash()

            bin_.recycle = crashing                                          # type: ignore[method-assign]
            with self.assertRaises(_Crash):
                undoer.undo([seq])
            bin_.recycle = original                                          # type: ignore[method-assign]
        self.assertEqual({self.journal.items()[n].state for n in range(1, 6)}, {jn.DOING})
        self.recover()
        self.assertEqual([(self.last(n).state, self.last(n).reason_code) for n in range(1, 6)],
                         [(jn.UNDONE, jn.R_RECOVERED), (jn.UNDO_REFUSED, jn.R_INTERRUPTED),
                          (jn.UNDO_REFUSED, jn.R_LOST), (jn.UNDONE, jn.R_RECOVERED),
                          (jn.UNDO_REFUSED, jn.R_INTERRUPTED)])
        self.assertTrue(all(self.last(n).undo for n in range(1, 6)))
        self.assertEqual(undoer.undoable(), (5, 3, 2))                     # a refused Undo stays offered
        self.assertEqual(undoer.undo([2]).put_back, 1)

    def test_a_path_that_is_no_plain_local_path_now_is_never_touched(self) -> None:
        self.fake.set_drive("Z:\\", dbk.DRIVE_REMOTE)
        self.journal.extend([jn.Record(1, jn.MOVE, jn.DOING, src="Z:\\Share\\a.txt", dst="Z:\\Share\\b.txt",
                                       volume_serial=1, file_id=2),
                             jn.Record(2, jn.MOVE, jn.DOING, src="\\\\server\\share\\a.txt", dst=BUDGET,
                                       volume_serial=1, file_id=2),
                             jn.Record(3, jn.COPY, jn.DOING, undo=True, src=BUDGET, dst="Z:\\b.txt"),
                             jn.Record(4, jn.RECYCLE, jn.DOING, src=BUDGET)])
        self.recover()
        self.assertEqual([(self.last(n).state, self.last(n).reason_code) for n in (1, 2, 3, 4)],
                         [(jn.FAILED, jn.R_UNCHECKED), (jn.FAILED, jn.R_UNCHECKED),
                          (jn.UNDO_REFUSED, jn.R_UNCHECKED), (jn.FAILED, jn.R_UNCHECKED)])
        touched = [argument for method, argument in self.fake.calls if method != "drive_type"]
        self.assertFalse(any(argument.startswith(("Z:", "\\\\")) for argument in touched))

    def test_recovery_logs_counts_only(self) -> None:
        folder = self.fake.make_dir(DOCS + "\\" + CANARY)
        self.crash("move_item", after=True)
        self.crashed(fo.FileOp(fo.MOVE, (self.item(BUDGET),), self.place(folder)))
        with open(self.journal.path, "ab") as handle:
            handle.write(b"{torn")
        with self.assertLogs("briefing_reader.desktop", "INFO") as logs:
            report = self.recover()
        self.assertEqual((report.items, report.unreadable, report.runs), (1, 1, 1))
        text = "\n".join(logs.output)
        self.assertIn("recovered 1 item(s) in 1 run(s) (done=1), 1 unreadable line(s)", text)
        self.assertNotIn(CANARY, text)
        self.assertNotIn("Owner", text)


if __name__ == "__main__":
    unittest.main()
