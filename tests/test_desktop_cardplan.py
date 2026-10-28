"""Jarvis on your desktop, P2.2: the frozen card plan (desktop.cardplan; SPEC 3 step 7, 4.4, 5.2-5.4, 6.2, 10.3 and
16 P2.2; hard rules 3, 4, 5, 7, 8 and 12). A checked plan's file steps become a card's rows on the desktop thread,
each checked as things are now by fileops' own re-check (a fake backend over a temporary folder: nothing real is
touched); a taken name gets its free "(2)" name ON THE PLAN; each row carries its flags (OneDrive in or out,
online-only, pinned, an installer - unticked -, hidden characters, big, many, renamed, a new type); the plan is frozen
as canonical JSON with its sha256 and a 15-minute expiry; an expired, tampered or changed plan is refused with its
code, and a target changed since is left as it is when the click runs it. Nothing is moved, copied, made or zipped
until a test runs the card's own rows through fileops on the pretend disk. Every name is invented.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest import mock

from briefing_reader.ask import desktop_mode as dm
from briefing_reader.ask.modes import MODE_DESKTOP, ModeRequest
from briefing_reader.desktop import backend as dbk
from briefing_reader.desktop import cardplan as cp
from briefing_reader.desktop import executor as dx
from briefing_reader.desktop import fileops as fo
from briefing_reader.desktop import journal as jn
from briefing_reader.desktop import paths
from briefing_reader.desktop import validate as dv
from briefing_reader.desktop.context import FileItem, Place
from tests.desktop_fakes import DOCS, DOWNLOADS, LING, desktop_context, desktop_disk

NOW = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)
MONO = 500.0
RLO = chr(0x202E)
CANARY = "CANARY_p22"
TIDY = "tidy my Downloads into LING, copy my budget to Downloads, zip my essay into a new Essays folder and rename " \
       "my reading list"
# desktop_context()'s refs: d1 Documents, d2 Uni, d3 LING, d4 Downloads; f1 the LING essay, f2 the reading list
# (LING), f3 the budget and f4 a macro workbook (Documents), f5 a shortcut, f6 an installer, f7 a name that reads like
# an order and f8 an old essay draft (Downloads).
FILES_STEPS = (
    {"skill": "move_files", "why": "x", "files": ["f8"], "to": "d3"},
    {"skill": "copy_files", "why": "x", "files": ["f3"], "to": "d4"},
    {"skill": "make_folder", "why": "x", "id": "n1", "place": "d4", "name": "Essays"},
    {"skill": "zip_files", "why": "x", "files": ["f1"], "name": "LING essay.zip", "to": "n1"},
    {"skill": "rename_file", "why": "x", "files": ["f2"], "name": "Reading list.pdf"},
)
_CHANGING = ("move_item", "copy_file", "make_folder", "remove_folder", "write_zip")


def move(files: list[str], to: str) -> dict:
    return {"skill": "move_files", "why": "x", "files": files, "to": to}


def copy(files: list[str], to: str) -> dict:
    return {"skill": "copy_files", "why": "x", "files": files, "to": to}


class CardCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.fake = desktop_disk(os.path.join(self.tmp, "disk"))
        self.scope = paths.load_scope(dbk.ROOT_FOLDERS, (), self.fake)

    def context(self, extra: tuple[FileItem, ...] = ()) -> object:
        """desktop_context() on the pretend disk (each item with its identity there now), plus ``extra`` files."""
        files = list(desktop_context().files) + list(extra)
        return desktop_context(files=files, backend=self.fake)

    def plan(self, words: str, *steps: dict, context: object = None) -> dv.DesktopPlan:
        context = context if context is not None else self.context()
        request = ModeRequest(MODE_DESKTOP, "typed", typed=words, typed_route=True)
        prompt = dm.build_prompt(NOW, request, context, run_id="c1")
        answer = dm.plan_from({"say": "", "steps": list(steps)})
        assert answer is not None
        return dv.check_plan(answer, prompt.refs, words, run_id="c1", names=dm.context_names(context))

    def card(self, words: str, *steps: dict, context: object = None, now: datetime = NOW) -> cp.CardPlan:
        plan = self.plan(words, *steps, context=context)
        self.assertEqual([step.code for step in plan.steps], [""] * len(steps), plan.steps)
        card = cp.build_card(plan, self.fake, self.scope, now=now, mono=MONO)
        assert card is not None
        return card

    def changes(self) -> list[tuple[str, str]]:
        return [call for call in self.fake.calls if call[0] in _CHANGING]

    def run_card(self, card: cp.CardPlan, ticked: object = None) -> fo.BatchReport:
        """The click's own run (P2.3's): the card's chosen rows through fileops, re-checked before each OS call."""
        journal = jn.Journal(os.path.join(self.tmp, "journal"), card.run_id)
        return fo.FileOps(self.fake, journal, scope=lambda: self.scope).run(list(card.ops(ticked)))

    def there(self, path: str) -> bool:
        return os.path.lexists(self.fake.real(path))


class BuildTests(CardCase):
    """Each file step becomes its rows, checked as things are now; nothing is changed."""

    def test_each_step_becomes_its_rows_and_nothing_changes(self) -> None:
        plan = self.plan(TIDY, *FILES_STEPS)
        card = cp.build_card(plan, self.fake, self.scope, now=NOW, mono=MONO)
        self.assertEqual([(row.number, row.step, row.skill, row.kind, row.code, row.ticked) for row in card.rows],
                         [(1, 1, "move_files", fo.MOVE, "", True), (2, 2, "copy_files", fo.COPY, "", True),
                          (3, 3, "make_folder", fo.MKDIR, "", True), (4, 4, "zip_files", fo.ZIP, "", True),
                          (5, 5, "rename_file", fo.RENAME, "", True)])
        self.assertIs(card.rows[0].sources[0], plan.steps[0].targets[0].target)    # the run's own entries
        self.assertIs(card.rows[0].folder, plan.steps[0].targets[1].target)
        self.assertEqual((card.rows[2].key, card.rows[2].name, card.rows[3].folder), ("n1", "Essays", "n1"))
        self.assertEqual((card.rows[4].folder, card.rows[4].name), (None, "Reading list.pdf"))
        self.assertIs(card.refs, plan.refs)                                    # its own RefIndex, never a newer one
        self.assertEqual(card.rows_of(4), (card.rows[3],))
        self.assertEqual(card.counts(), {"rows": 5, "ok": 5, "ticked": 5, "refused": 0})
        self.assertEqual(self.changes(), [])                                   # nothing until the owner's click
        self.assertEqual(self.fake.opened, [])

    def test_a_step_of_many_files_is_a_row_each_and_a_zip_is_one(self) -> None:
        extra = tuple(FileItem(DOWNLOADS + f"\\Scan {n}.pdf", f"Scan {n}.pdf", DOWNLOADS) for n in range(3))
        for item in extra:
            self.fake.make_file(item.path, b"scan")
        context = self.context(extra)
        card = self.card("zip my scans, then tidy them into LING",
                         {"skill": "zip_files", "why": "x", "files": ["f9", "f10"], "name": "Scans.zip", "to": "d1"},
                         move(["f9", "f10", "f11"], "d3"), context=context)
        self.assertEqual([(row.step, row.kind, len(row.sources)) for row in card.rows],
                         [(1, fo.ZIP, 2), (2, fo.MOVE, 1), (2, fo.MOVE, 1), (2, fo.MOVE, 1)])
        moved_first = self.plan("tidy my scans into LING and zip them", move(["f9", "f10", "f11"], "d3"),
                                {"skill": "zip_files", "why": "x", "files": ["f9", "f10"], "name": "Scans.zip"},
                                context=context)
        self.assertEqual([step.code for step in moved_first.steps], ["", dv.SHAPE])   # they won't be there

    def test_a_plan_with_no_file_step_has_no_card(self) -> None:
        plan = self.plan("open my LING essay", {"skill": "open_file", "why": "x", "files": ["f1"]})
        self.assertIsNone(cp.build_card(plan, self.fake, self.scope, now=NOW))


class FreeNameTests(CardCase):
    """Hard rule 5: nothing is ever overwritten - a taken name gets its free "(2)" name ON THE PLAN, never one chosen
    silently when the click runs."""

    def test_a_taken_name_is_given_its_free_name_on_the_plan(self) -> None:
        self.fake.make_file(LING + "\\LING essay draft (old).docx", b"theirs")
        card = self.card("tidy my Downloads into LING", move(["f8"], "d3"))
        row = card.rows[0]
        self.assertEqual((row.name, row.code), ("LING essay draft (old) (2).docx", ""))
        self.assertIn(cp.FLAG_RENAMED, row.flags)
        self.assertEqual(json.loads(card.canonical())["rows"][0]["name"], "LING essay draft (old) (2).docx")
        report = self.run_card(card)
        self.assertEqual([(result.state, result.reason) for result in report.results], [(jn.DONE, "")])
        self.assertTrue(self.there(LING + "\\LING essay draft (old) (2).docx"))
        with open(self.fake.real(LING + "\\LING essay draft (old).docx"), "rb") as handle:
            self.assertEqual(handle.read(), b"theirs")                         # never overwritten

    def test_a_copy_beside_itself_and_names_an_earlier_row_takes(self) -> None:
        twin = FileItem(DOWNLOADS + "\\Budget 2026 (2).xlsx", "Budget 2026 (2).xlsx", DOWNLOADS)
        self.fake.make_file(twin.path, b"twin")
        context = self.context((twin,))
        card = self.card("copy my budgets into Documents", copy(["f3"], "d1"), copy(["f9"], "d1"), context=context)
        self.assertEqual([row.name for row in card.rows], ["Budget 2026 (2).xlsx", "Budget 2026 (2) (2).xlsx"])
        self.assertTrue(all(cp.FLAG_RENAMED in row.flags for row in card.rows))
        report = self.run_card(card)
        self.assertEqual([result.state for result in report.results], [jn.DONE, jn.DONE])
        self.assertTrue(self.there(DOCS + "\\Budget 2026 (2).xlsx") and self.there(DOCS + "\\Budget 2026 (2) (2).xlsx"))

    def test_a_new_folder_and_a_rename_whose_names_are_taken(self) -> None:
        self.fake.make_dir(DOWNLOADS + "\\Old")
        self.fake.make_file(LING + "\\Notes.docx")
        card = self.card("tidy my Downloads into a new folder and rename my essay",
                         {"skill": "make_folder", "why": "x", "id": "n1", "place": "d4", "name": "Old"},
                         move(["f8"], "n1"),
                         {"skill": "rename_file", "why": "x", "files": ["f1"], "name": "Notes.docx"})
        self.assertEqual([(row.kind, row.name) for row in card.rows],
                         [(fo.MKDIR, "Old (2)"), (fo.MOVE, ""), (fo.RENAME, "Notes (2).docx")])
        report = self.run_card(card)
        self.assertEqual([result.state for result in report.results], [jn.DONE, jn.DONE, jn.DONE])
        self.assertTrue(self.there(DOWNLOADS + "\\Old (2)\\LING essay draft (old).docx"))   # into the folder it made

    def test_a_new_spelling_is_not_a_clash(self) -> None:
        card = self.card("rename my essay", {"skill": "rename_file", "why": "x", "files": ["f1"],
                                             "name": "ling essay draft.docx"})
        self.assertEqual((card.rows[0].name, card.rows[0].code, card.rows[0].flags), ("ling essay draft.docx", "",
                                                                                      frozenset()))


class RowCheckTests(CardCase):
    """Every row through fileops' own check as things are now: a row that can't go keeps its reason, unticked."""

    def test_a_target_changed_since_it_was_listed(self) -> None:
        context = self.context()
        with open(self.fake.real(DOCS + "\\Budget 2026.xlsx"), "ab") as handle:
            handle.write(b"edited")
        card = self.card("copy my budget to Downloads", copy(["f3"], "d4"), context=context)
        self.assertEqual((card.rows[0].code, card.rows[0].ticked), (fo.R_CHANGED, False))
        self.assertEqual(card.chosen(), ())
        self.assertEqual(card.chosen({1}), ())                                  # a refused row never runs

    def test_online_only_moves_flagged_copies_and_zips_refused(self) -> None:
        self.fake.set_attributes(DOCS + "\\Budget 2026.xlsx", dbk.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
        moved = self.card("move my budget to Downloads", move(["f3"], "d4"))
        self.assertEqual(moved.rows[0].code, "")
        self.assertTrue({fo.FLAG_ONLINE_ONLY, fo.FLAG_LEAVES_ONEDRIVE} <= moved.rows[0].flags)
        copied = self.card("copy my budget to Downloads", copy(["f3"], "d4"))
        self.assertEqual(copied.rows[0].code, fo.R_ONLINE_ONLY)
        zipped = self.card("zip my budget", {"skill": "zip_files", "why": "x", "files": ["f3"], "name": "B.zip"})
        self.assertEqual(zipped.rows[0].code, fo.R_ONLINE_ONLY)

    def test_another_volume_is_refused_in_v1(self) -> None:
        self.fake.volumes[ntpath.normcase(DOWNLOADS)] = 4242                   # the same letter, another volume
        card = self.card("move my budget to Downloads", move(["f3"], "d4"))
        self.assertEqual(card.rows[0].code, fo.R_CROSS_VOLUME)
        self.assertEqual(cp.row_words(fo.R_CROSS_VOLUME), dv.reason_words(dv.CROSS_VOLUME))

    def test_a_folder_gone_since_and_rows_into_a_folder_that_cannot_be_made(self) -> None:
        self.fake.make_dir(DOWNLOADS + "\\Inbox")
        inbox = Place(DOWNLOADS + "\\Inbox", ("Downloads", "Inbox"), self.fake.identity(DOWNLOADS + "\\Inbox"))
        context = desktop_context(places=list(desktop_context().places) + [inbox], backend=self.fake)
        os.rmdir(self.fake.real(DOWNLOADS + "\\Inbox"))
        card = self.card("tidy my Downloads", move(["f8"], "d5"),
                         {"skill": "make_folder", "why": "x", "id": "n1", "place": "d5", "name": "Old"},
                         move(["f6"], "n1"), context=context)
        self.assertEqual([row.code for row in card.rows], [fo.R_CHANGED, fo.R_CHANGED, fo.R_NO_FOLDER])

    def test_a_zip_without_to_goes_next_to_its_first_file(self) -> None:
        card = self.card("zip my essay and budget", {"skill": "zip_files", "why": "x", "files": ["f1", "f3"],
                                                     "name": "Uni.zip"})
        row = card.rows[0]
        self.assertIsInstance(row.folder, Place)
        self.assertEqual((ntpath.normcase(row.folder.path), row.code), (ntpath.normcase(LING), ""))
        self.assertIsNotNone(row.folder.identity)
        report = self.run_card(card)
        self.assertEqual(report.results[0].state, jn.DONE)
        self.assertTrue(self.there(LING + "\\Uni.zip"))


class FlagTests(CardCase):
    """SPEC 5.4: each row's flags, as words the card shows (never colour alone)."""

    def flags(self, words: str, *steps: dict, context: object = None) -> list[frozenset[str]]:
        card = self.card(words, *steps, context=context)
        for row in card.rows:
            self.assertTrue(row.flags <= set(cp.ROW_FLAGS), row.flags)
        return [row.flags for row in card.rows]

    def test_onedrive_in_and_out(self) -> None:
        self.assertIn(fo.FLAG_TO_ONEDRIVE, self.flags("tidy my Downloads into LING", move(["f8"], "d3"))[0])
        self.assertIn(fo.FLAG_LEAVES_ONEDRIVE, self.flags("move my budget to Downloads", move(["f3"], "d4"))[0])
        moved_within = self.flags("move my budget into Uni", move(["f3"], "d2"))[0]
        self.assertFalse({fo.FLAG_TO_ONEDRIVE, fo.FLAG_LEAVES_ONEDRIVE} & moved_within)    # OneDrive to OneDrive
        self.assertIn(fo.FLAG_TO_ONEDRIVE, self.flags("copy the old draft into LING", copy(["f8"], "d3"))[0])

    def test_an_installer_stays_unticked(self) -> None:
        card = self.card("tidy my Downloads into Documents", move(["f6", "f8"], "d1"))
        self.assertEqual([(dv.FLAG_INSTALLER in row.flags, row.ticked) for row in card.rows],
                         [(True, False), (False, True)])
        self.assertEqual([row.number for row in card.chosen()], [2])         # it stays put unless he ticks it
        self.assertEqual([row.number for row in card.chosen({1, 2})], [1, 2])

    def test_pinned_hidden_characters_and_a_new_type(self) -> None:
        self.fake.set_attributes(DOCS + "\\Budget 2026.xlsx", fo.FILE_ATTRIBUTE_PINNED)
        self.assertIn(fo.FLAG_PINNED, self.flags("move my budget to Downloads", move(["f3"], "d4"))[0])
        hidden = FileItem(DOWNLOADS + "\\Invoice" + RLO + "xcod.docx", "Invoice" + RLO + "xcod.docx", DOWNLOADS)
        self.fake.make_file(hidden.path)
        self.assertIn(fo.FLAG_HIDDEN_CHARS, self.flags("move the invoice into LING", move(["f9"], "d3"),
                                                       context=self.context((hidden,)))[0])
        renamed = self.flags("rename my essay to a pdf", {"skill": "rename_file", "why": "x", "files": ["f1"],
                                                          "name": "LING essay draft.pdf"})[0]
        self.assertIn(dv.FLAG_TYPE_CHANGED, renamed)

    def test_big_and_many(self) -> None:
        with mock.patch.object(dv, "BIG_BYTES", 4000):
            self.assertIn(dv.FLAG_BIG, self.flags("move my essay to Downloads", move(["f1"], "d4"))[0])   # 4800 bytes
            self.assertNotIn(dv.FLAG_BIG, self.flags("move my reading list to Downloads", move(["f2"], "d4"))[0])
        with mock.patch.object(dv, "MANY_FILES", 1):
            zipped = self.flags("zip my essay and list", {"skill": "zip_files", "why": "x", "files": ["f1", "f2"],
                                                          "name": "LING.zip"})[0]
            self.assertIn(dv.FLAG_MANY, zipped)


class FrozenTests(CardCase):
    """The frozen plan: canonical JSON, its sha256, a 15-minute expiry, and nothing else runs."""

    def test_canonical_json_and_its_sha256(self) -> None:
        accented = FileItem(DOWNLOADS + "\\R" + chr(0xE9) + "sum" + chr(0xE9) + ".docx",
                            "R" + chr(0xE9) + "sum" + chr(0xE9) + ".docx", DOWNLOADS)
        self.fake.make_file(accented.path)
        context = self.context((accented,))
        card = self.card("tidy my Downloads into LING", move(["f8", "f9"], "d3"), context=context)
        text = card.canonical()
        self.assertTrue(text.isascii())                                        # every character past ASCII escaped
        body = json.loads(text)
        self.assertEqual(text, json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
        self.assertEqual(set(body), {"format", "run_id", "made_at", "expires_at", "rows"})
        self.assertEqual((body["format"], body["run_id"], body["made_at"], body["expires_at"]),
                         (1, "c1", "2026-10-28T10:00:00+00:00", "2026-10-28T10:15:00+00:00"))
        row = body["rows"][0]
        self.assertEqual(set(row), {"number", "step", "skill", "kind", "sources", "folder", "name", "key", "flags",
                                    "ticked", "code"})
        identity = self.fake.identity(DOWNLOADS + "\\LING essay draft (old).docx")
        self.assertEqual(row["sources"][0]["identity"], {"volume": identity.volume, "file_id": identity.file_id,
                                                         "size": identity.size, "mtime_ns": identity.mtime_ns,
                                                         "is_dir": False})
        self.assertEqual(row["folder"]["path"], LING)
        self.assertTrue(row["folder"]["identity"]["is_dir"])
        self.assertEqual(body["rows"][1]["sources"][0]["name"], accented.name)
        self.assertEqual(card.sha256, hashlib.sha256(text.encode("ascii")).hexdigest())
        self.assertEqual(len(card.sha256), 64)
        again = cp.build_card(self.plan("tidy my Downloads into LING", move(["f8", "f9"], "d3"), context=context),
                              self.fake, self.scope, now=NOW, mono=MONO)
        self.assertEqual(again.sha256, card.sha256)                            # the same plan, the same hash
        later = cp.build_card(self.plan("tidy my Downloads into LING", move(["f8", "f9"], "d3"), context=context),
                              self.fake, self.scope, now=NOW + timedelta(seconds=1), mono=MONO)
        self.assertNotEqual(later.sha256, card.sha256)
        self.assertEqual(cp.CARD_TTL, timedelta(minutes=15))
        self.assertEqual(card.expires_at - card.made_at, cp.CARD_TTL)

    def test_it_expires_after_15_minutes_by_either_clock(self) -> None:
        card = self.card("tidy my Downloads into LING", move(["f8"], "d3"))
        cases = [
            (NOW + timedelta(minutes=14, seconds=59), MONO + 899, ""),
            (NOW + timedelta(minutes=15), None, cp.CARD_EXPIRED),
            (NOW + timedelta(hours=3), MONO + 60, cp.CARD_EXPIRED),
            (NOW + timedelta(minutes=1), MONO + 900, cp.CARD_EXPIRED),          # the wall clock set back meanwhile
            (NOW - timedelta(hours=2), None, cp.CARD_EXPIRED),                  # set back before it was made
            (NOW - timedelta(seconds=30), None, ""),                            # within the slack
            (NOW.replace(tzinfo=None) + timedelta(minutes=1), None, ""),        # a naive time is UTC
            (NOW + timedelta(minutes=1), MONO - 1, cp.CARD_EXPIRED),            # a monotonic clock never goes back
        ]
        for now, mono, wanted in cases:
            with self.subTest(now=now, mono=mono):
                self.assertEqual(card.verify(now, mono=mono), wanted)

    def test_a_tampered_plan_is_refused(self) -> None:
        card = self.card("tidy my Downloads into LING", move(["f8"], "d3"))
        row = card.rows[0]
        elsewhere = Place(DOCS, ("Documents",), self.fake.identity(DOCS))
        other = replace(row.sources[0], identity=self.fake.identity(DOCS + "\\Budget 2026.xlsx"))
        tampered = [
            replace(card, rows=(replace(row, folder=elsewhere),)),             # another destination
            replace(card, rows=(replace(row, name="Other.docx"),)),            # another name
            replace(card, rows=(replace(row, sources=(other,)),)),             # another item's identity
            replace(card, rows=(replace(row, flags=frozenset()),)),            # its flags hidden
            replace(card, rows=(row, replace(row, number=2))),                 # a row added
            replace(card, rows=()),
            replace(card, run_id="x"),
            replace(card, sha256="0" * 64),
            replace(card, sha256=""),
            replace(card, expires_at=NOW + timedelta(days=1)),                 # a longer life
        ]
        stretched = replace(card, expires_at=NOW + timedelta(days=1))
        tampered.append(replace(stretched, sha256=stretched.digest()))        # even with its hash made again
        for number, plan in enumerate(tampered):
            with self.subTest(number=number):
                self.assertEqual(plan.verify(NOW + timedelta(minutes=1)), cp.CARD_CHANGED)
        self.assertEqual(card.verify(NOW + timedelta(minutes=1)), "")
        self.assertEqual(card.verify(NOW, sha=card.sha256), "")
        self.assertEqual(card.verify(NOW, sha="f" * 64), cp.CARD_CHANGED)     # not the plan the card showed
        object.__setattr__(row, "name", "Swapped.docx")                       # changed in memory after freezing
        self.assertEqual(card.verify(NOW), cp.CARD_CHANGED)

    def test_the_click_runs_exactly_the_plan_and_a_changed_target_is_left_as_it_is(self) -> None:
        card = self.card("tidy my Downloads into Documents and copy my budget", move(["f8", "f6"], "d1"),
                         copy(["f3"], "d4"))
        self.assertEqual([row.number for row in card.chosen()], [1, 3])       # the installer stays unticked
        self.assertEqual([row.number for row in card.chosen({2, 3, 99})], [2, 3])
        self.assertEqual(card.ops({3}), (card.rows[2].op(),))
        self.assertEqual(card.verify(NOW + timedelta(minutes=2), mono=MONO + 120), "")
        with open(self.fake.real(DOCS + "\\Budget 2026.xlsx"), "ab") as handle:
            handle.write(b"edited after the card was made")
        report = self.run_card(card)
        self.assertEqual([(result.state, result.reason) for result in report.results],
                         [(jn.DONE, ""), (jn.SKIPPED, fo.R_CHANGED)])           # "Changed since - left as it is."
        self.assertTrue(self.there(DOCS + "\\LING essay draft (old).docx"))
        self.assertTrue(self.there(DOWNLOADS + "\\SpotifySetup.exe"))           # unticked: never moved
        self.assertFalse(self.there(DOWNLOADS + "\\Budget 2026.xlsx"))

    def test_an_expired_or_changed_card_runs_nothing(self) -> None:
        card = self.card("tidy my Downloads into Documents and copy my budget", move(["f8"], "d1"),
                         copy(["f3"], "d4"))
        journal = jn.Journal(os.path.join(self.tmp, "journal"), card.run_id)
        ops = fo.FileOps(self.fake, journal, scope=lambda: self.scope)
        for plan, now, sha, wanted in (
                (card, NOW + timedelta(minutes=15), "", cp.CARD_EXPIRED),
                (replace(card, rows=card.rows[:1]), NOW, "", cp.CARD_CHANGED),
                (card, NOW, "e" * 64, cp.CARD_CHANGED)):
            with self.subTest(wanted=wanted):
                with self.assertLogs("briefing_reader.desktop.cardplan", level="INFO") as logs:
                    self.assertEqual(cp.carry_out(plan, ops, now=now, sha=sha), (wanted, None))
                self.assertIn(f"a card's click was refused ({wanted})", "\n".join(logs.output))
        self.assertEqual(self.changes(), [])                                   # nothing at all ran
        self.assertEqual(journal.records(), [])
        code, report = cp.carry_out(card, ops, now=NOW + timedelta(minutes=3), sha=card.sha256, ticked={2})
        self.assertEqual((code, [(result.kind, result.state) for result in report.results]), ("", [(fo.COPY, jn.DONE)]))
        self.assertEqual([call[0] for call in self.changes()], ["copy_file"])  # only the row he left ticked
        self.assertTrue(self.there(DOWNLOADS + "\\LING essay draft (old).docx"))

    def test_a_newer_runs_index_never_changes_the_card(self) -> None:
        card = self.card("tidy my Downloads into LING", move(["f8"], "d3"))
        newer = self.plan("open my budget", {"skill": "open_file", "why": "x", "files": ["f1"]},
                          context=desktop_context(files=[FileItem(DOCS + "\\Budget 2026.xlsx", "Budget 2026.xlsx",
                                                                  DOCS)], backend=self.fake))
        self.assertEqual(newer.refs.resolve("f1").target.name, "Budget 2026.xlsx")     # f1 is another file there
        self.assertEqual(card.rows[0].sources[0].name, "LING essay draft (old).docx")  # the card keeps its own
        self.assertEqual(card.verify(NOW), "")


class ExecutorTests(CardCase):
    """The desktop thread: the file steps' card is made and frozen, they wait for the owner's OK, nothing changes."""

    def executor(self) -> dx.Executor:
        return dx.Executor(self.fake, scope=lambda: self.scope, apps=lambda: (), clock=lambda: MONO, now=lambda: NOW)

    def test_file_steps_wait_on_their_card(self) -> None:
        plan = self.plan(TIDY + " and open my essay", *FILES_STEPS,
                         {"skill": "open_file", "why": "x", "files": ["f8"]})
        run = self.executor().run_plan(plan, words=TIDY)
        self.assertEqual([(result.skill, result.tier, result.status) for result in run.results],
                         [(step["skill"], dv.CARD, dx.WAITING) for step in FILES_STEPS]
                         + [("open_file", dv.INSTANT, dx.DONE)])
        self.assertIs(run.results[0].target, run.card)
        self.assertEqual(run.card.verify(NOW, mono=MONO), "")
        self.assertEqual(self.changes(), [])
        self.assertEqual(self.fake.opened, [("open_document", DOWNLOADS + "\\LING essay draft (old).docx")])
        self.assertEqual(dx.run_words(run, plan), "Opened the file. 5 steps need your OK first, sir.")

    def test_the_words_for_one_step_and_for_none_that_can_go(self) -> None:
        plan = self.plan("tidy my Downloads into LING", move(["f8"], "d3"))
        run = self.executor().run_plan(plan)
        self.assertEqual(dx.run_words(run, plan), "Moving the files needs your OK first - nothing has changed yet, "
                                                  "sir.")
        self.assertTrue(dx.run_waits(run))                                     # amber: a decision waits
        with open(self.fake.real(DOWNLOADS + "\\LING essay draft (old).docx"), "ab") as handle:
            handle.write(b"edited")
        run = self.executor().run_plan(plan)
        self.assertEqual([(result.status, result.code) for result in run.results], [(dx.REFUSED, fo.R_CHANGED)])
        self.assertEqual(dx.run_words(run, plan), "Moving the files can't go ahead - it changed since I looked - "
                                                  "left as it is, sir.")
        self.assertEqual(dx.describe(run.results[0]), "Step 1 - move_files - card - refused: changed")

    def test_no_card_when_cancelled_or_when_it_cannot_be_made(self) -> None:
        plan = self.plan("tidy my Downloads into LING", move(["f8"], "d3"))
        stop = mock.Mock(is_set=lambda: True)
        before = len(self.fake.calls)
        run = self.executor().run_plan(plan, cancel=stop)
        self.assertEqual([(result.status, result.code) for result in run.results], [(dx.CANCELLED, dx.STOPPED)])
        self.assertIsNone(run.card)
        self.assertEqual(len(self.fake.calls), before)                         # nothing more was looked at
        with mock.patch.object(cp, "build_card", side_effect=RuntimeError("boom")), \
                self.assertLogs("briefing_reader.desktop.executor", level="WARNING") as logs:
            run = self.executor().run_plan(plan)
        self.assertEqual([(result.status, result.code) for result in run.results], [(dx.REFUSED, dx.GUARD)])
        self.assertIn("the card's plan could not be made (RuntimeError)", "\n".join(logs.output))


class CanaryTests(CardCase):
    """Logs and reprs carry ids, kinds, counts and codes only - never a name, a path or the owner's words."""

    def test_no_name_or_path_in_logs_or_reprs(self) -> None:
        canary = FileItem(DOWNLOADS + f"\\{CANARY} notes.txt", f"{CANARY} notes.txt", DOWNLOADS)
        self.fake.make_file(canary.path)
        self.fake.make_file(LING + f"\\{CANARY} notes.txt")                   # a clash: a "(2)" name on the plan
        context = self.context((canary,))
        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            plan = self.plan(f"tidy my Downloads into LING and {CANARY}", move(["f9"], "d3"), context=context)
            run = dx.Executor(self.fake, scope=lambda: self.scope, apps=lambda: (), now=lambda: NOW).run_plan(plan)
        card = run.card
        self.assertIn("(2)", card.rows[0].name)
        text = "\n".join(logs.output) + repr(card) + repr(card.rows) + repr(run) + "".join(cp.describe_rows(card))
        for secret in (CANARY, "notes", "LING", "Downloads", "Owner", "C:\\", ".txt"):
            self.assertNotIn(secret, text)
        self.assertIn("card plan of 1 row(s) (1 ticked, 0 refused)", "\n".join(logs.output))
        self.assertEqual(cp.describe_rows(card), ["Row 1 - move - ticked (renamed, to_onedrive)"])


if __name__ == "__main__":
    unittest.main()
