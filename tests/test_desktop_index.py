"""Jarvis's own search of this PC (briefing_reader.desktop.index; SPEC 4.1, 5 and 13; P1.8): the Windows Search query's
text (pure: every quote escaped, LIKE's wildcards taken literally, scoped to the roots), the bounded walk's caps (50,000
entries, 8 levels, 2 seconds) and rules, and above all that NOTHING outside the approved roots is ever listed - from the
index, the walk, the Recent items or the places - whatever tricks the paths play (junctions, symbolic links, \\\\?\\
paths, 8.3 names, mapped and network drives, OneDrive placeholders, case, trailing dots and spaces), with a fuzz over
all of them. Every listed item carries its final path and the identity it has on disk.

Everything runs on tests.desktop_fakes.FakeDesktopBackend over a temporary folder (its index answers what the test
sets, its listings may be made up); no real index, folder or shortcut is ever read.
"""

from __future__ import annotations

import ntpath
import random
import re
import tempfile
import unittest
from datetime import datetime, timezone

from briefing_reader.desktop import backend as dbk
from briefing_reader.desktop import guard, index, paths
from briefing_reader.desktop.context import FileItem, Place
from tests.desktop_fakes import DOCS, DOWNLOADS, KNOWN, LING, OWNER, UNI, FakeDesktopBackend, desktop_disk

ROOTS = [KNOWN[name] for name in dbk.ROOT_FOLDERS]
_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


def literals(sql: str) -> list[str]:
    """The SQL text's string literals, unescaped."""
    return [match.group(0)[1:-1].replace("''", "'") for match in _LITERAL_RE.finditer(sql)]


def skeleton(sql: str) -> str:
    """The SQL text with every literal replaced by ?: what the query DOES, whatever the words were."""
    return _LITERAL_RE.sub("?", sql)


def under_a_root(path: str, scope: paths.Scope) -> bool:
    key = ntpath.normcase(path).rstrip("\\")
    return any(key == ntpath.normcase(root.path).rstrip("\\")
               or key.startswith(ntpath.normcase(root.path).rstrip("\\") + "\\") for root in scope.roots)


class FakeDiskCase(unittest.TestCase):
    def disk(self) -> FakeDesktopBackend:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        return desktop_disk(tmp.name)

    def scope(self, fake: FakeDesktopBackend) -> paths.Scope:
        return paths.load_scope(dbk.ROOT_FOLDERS, (), fake)


# --------------------------------------------------------------------------
# The query's text
# --------------------------------------------------------------------------

class QueryTests(unittest.TestCase):
    def test_the_query_scoped_to_the_folders_with_the_words(self) -> None:
        sql = index.build_query(("essay", "ling"), ["C:\\Users\\Owner\\OneDrive\\Documents",
                                                     "C:\\Users\\Owner\\Downloads"])
        self.assertEqual(sql, "SELECT TOP 400 System.ItemPathDisplay, System.FileName, System.Size, "
                              "System.DateModified, System.FileAttributes FROM SystemIndex WHERE "
                              "(SCOPE='file:C:/Users/Owner/OneDrive/Documents' OR "
                              "SCOPE='file:C:/Users/Owner/Downloads') "
                              "AND (System.FileName LIKE '%essay%' OR System.FileName LIKE '%ling%') "
                              "ORDER BY System.DateModified DESC")

    def test_no_folder_no_query(self) -> None:
        self.assertEqual(index.build_query(("essay",), []), "")
        self.assertEqual(index.build_query(("essay",), ["", None]), "")                 # type: ignore[list-item]

    def test_every_quote_is_escaped_in_the_words_and_the_folders(self) -> None:
        sql = index.build_query(("o'brien", "x' OR 1=1 --", "a''b"), ["C:\\Users\\O'Brien\\Documents"])
        self.assertIn("SCOPE='file:C:/Users/O''Brien/Documents'", sql)
        self.assertIn("System.FileName LIKE '%o''brien%'", sql)
        self.assertEqual(literals(sql), ["file:C:/Users/O'Brien/Documents", "%o'brien%", "%x' OR 1=1 --%",
                                         "%a''b%"])
        # Whatever the words, the query does the same: a scope, three name matches, newest first.
        self.assertEqual(skeleton(sql), "SELECT TOP 400 System.ItemPathDisplay, System.FileName, System.Size, "
                                        "System.DateModified, System.FileAttributes FROM SystemIndex WHERE "
                                        "(SCOPE=?) AND (System.FileName LIKE ? OR System.FileName LIKE ? OR "
                                        "System.FileName LIKE ?) ORDER BY System.DateModified DESC")

    def test_likes_own_wildcards_are_taken_literally(self) -> None:
        self.assertEqual(index.like_pattern("100%_[x]"), "%100[%][_][[]x]%")
        sql = index.build_query(("50%",), ["C:\\Users\\Owner\\Downloads"])
        self.assertIn("LIKE '%50[%]%'", sql)

    def test_control_characters_never_reach_the_query(self) -> None:
        word = "essay" + chr(0) + chr(10) + "' ; DROP" + chr(0x85)
        sql = index.build_query((word,), ["C:\\Users\\Owner\\Downloads"])
        self.assertNotIn(chr(0), sql)
        self.assertNotIn(chr(10), sql)
        self.assertNotIn(chr(0x85), sql)
        self.assertEqual(skeleton(sql).count("?"), 2)

    def test_a_kind_and_the_days_back(self) -> None:
        now = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
        sql = index.build_query(("budget",), ["C:\\Users\\Owner\\Downloads"], kind="document", within_days=7,
                                now=now)
        self.assertIn("System.FileExtension = '.docx'", sql)
        self.assertIn("System.FileExtension = '.pdf'", sql)
        self.assertNotIn("System.FileExtension = '.exe'", sql)
        self.assertIn("System.DateModified >= '2026-10-19 12:00:00'", sql)
        self.assertEqual(index.build_query((), ["C:\\x"], kind="any"),
                         "SELECT TOP 400 " + ", ".join(dbk.INDEX_COLUMNS)
                         + " FROM SystemIndex WHERE (SCOPE='file:C:/x') ORDER BY System.DateModified DESC")

    def test_the_words_a_request_is_searched_by(self) -> None:
        self.assertEqual(index.query_words("Jarvis, please open my LING essay"), ("ling", "essay"))
        self.assertEqual(index.query_words("where is the budget spreadsheet?"), ("budget", "spreadsheet"))
        self.assertEqual(index.query_words("open the file"), ())
        self.assertEqual(len(index.query_words(" ".join(f"word{n}" for n in range(20)))), index.MAX_WORDS)

    def test_the_search_asks_the_index_for_the_roots_only(self) -> None:
        fake = desktop_disk(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        scope = paths.load_scope(dbk.ROOT_FOLDERS, (), fake)
        fake.index_rows = []
        index.LocalSearch(scope, fake).search(("essay",))
        scopes = [text for text in literals(fake.queries[0]) if text.startswith("file:")]
        self.assertEqual(scopes, ["file:" + root.path.replace("\\", "/") for root in scope.roots])


# --------------------------------------------------------------------------
# The walk
# --------------------------------------------------------------------------

def _dir(name: str, attributes: int = 0) -> dbk.DirEntry:
    return dbk.DirEntry(name, True, dbk.FILE_ATTRIBUTE_DIRECTORY | attributes)


def _file(name: str, attributes: int = 0, tag: int = 0) -> dbk.DirEntry:
    return dbk.DirEntry(name, False, attributes, tag, 10, 1)


class WalkCapTests(FakeDiskCase):
    def test_at_most_50000_entries(self) -> None:
        fake = self.disk()
        root = KNOWN["downloads"]
        fake.set_listing(root, [_dir(f"d{n}") for n in range(10)])
        for n in range(10):
            fake.set_listing(f"{root}\\d{n}", [_file(f"f{m}.txt") for m in range(9000)])
        found = index.walk([root], fake, lambda entry: False)
        self.assertEqual(found.entries, 50_000)
        self.assertEqual(found.stopped, "entries")
        self.assertLessEqual(len(fake.asked("list_folder")), 7)
        self.assertEqual(index.WALK, index.WalkCaps(50_000, 8, 2.0))

    def test_at_most_8_levels_below_where_it_began(self) -> None:
        fake = self.disk()
        folder = KNOWN["downloads"]
        for level in range(12):
            fake.set_listing(folder, [_dir("deeper"), _file(f"level{level + 1}.txt")])
            folder += "\\deeper"
        found = index.walk([KNOWN["downloads"]], fake, lambda entry: not entry.is_dir)
        self.assertEqual(found.deepest, 8)
        self.assertEqual(len(found.matches), 8)                              # level1.txt ... level8.txt
        listed = fake.asked("list_folder")
        self.assertEqual(max(path.count("\\deeper") for path in listed), 7)  # a level-8 folder is never listed

    def test_at_most_2_seconds(self) -> None:
        fake = self.disk()
        root = KNOWN["downloads"]
        fake.set_listing(root, [_dir(f"d{n}") for n in range(50)])
        for n in range(50):
            fake.set_listing(f"{root}\\d{n}", [_file("a.txt")])
        clock = [0.0]

        def slow(method: str, _argument: str) -> None:
            if method == "list_folder":
                clock[0] += 0.5                                              # every listing takes half a second
        fake.on_call = slow
        found = index.walk([root], fake, lambda entry: False, clock=lambda: clock[0])
        self.assertEqual(found.stopped, "time")
        self.assertEqual(found.folders, 4)                                   # 0.0, 0.5, 1.0, 1.5: then 2.0 s is up

    def test_a_listing_that_fails_is_passed_by(self) -> None:
        fake = self.disk()
        found = index.walk([OWNER + "\\Nowhere", KNOWN["downloads"]], fake, lambda entry: True)
        self.assertEqual(found.stopped, "")


class WalkRuleTests(FakeDiskCase):
    def test_what_the_walk_never_lists_or_goes_into(self) -> None:
        fake = self.disk()
        fake.make_file(DOWNLOADS + "\\essay ok.docx")
        fake.make_file(DOWNLOADS + "\\hidden essay.docx")
        fake.set_attributes(DOWNLOADS + "\\hidden essay.docx", dbk.FILE_ATTRIBUTE_HIDDEN)
        fake.make_file(OWNER + "\\AppData\\Local\\Secret\\essay in appdata.docx")
        fake.junction(DOWNLOADS + "\\junction", OWNER + "\\AppData\\Local\\Secret")
        fake.make_file(DOWNLOADS + "\\linked\\essay linked.docx")
        fake.set_tag(DOWNLOADS + "\\linked", dbk.IO_REPARSE_TAG_SYMLINK)
        fake.make_file(DOWNLOADS + "\\Projects\\app\\.git\\HEAD")
        fake.make_file(DOWNLOADS + "\\Projects\\app\\essay in repo.md")
        fake.make_file(DOWNLOADS + "\\node_modules\\essay module.md")
        fake.make_file(DOWNLOADS + "\\.ssh\\essay key.txt")
        fake.make_file(DOWNLOADS + "\\essay.env")
        fake.make_file(DOWNLOADS + "\\token.json")
        fake.make_file(DOWNLOADS + "\\AppData\\essay deep appdata.docx")
        fake.make_file(DOWNLOADS + "\\Cloud\\essay cloud.docx")
        fake.set_attributes(DOWNLOADS + "\\Cloud", dbk.FILE_ATTRIBUTE_RECALL_ON_OPEN)
        found = index.walk([DOWNLOADS], fake, lambda entry: not entry.is_dir)
        names = sorted(ntpath.basename(path) for path, _entry in found.matches)
        self.assertIn("essay ok.docx", names)
        for gone in ("hidden essay.docx", "essay in appdata.docx", "essay linked.docx", "essay in repo.md",
                     "essay module.md", "essay key.txt", "token.json", "essay deep appdata.docx", "essay cloud.docx",
                     "HEAD"):
            self.assertNotIn(gone, names)
        listed = [ntpath.normcase(path) for path in fake.asked("list_folder")]
        for never in ("\\junction", "\\linked", "\\node_modules", "\\.ssh", "\\appdata", "\\cloud"):
            self.assertFalse(any(path.endswith(never) for path in listed), never)   # never even listed

    def test_skipped(self) -> None:
        self.assertTrue(index.skipped(_file("a.txt", dbk.FILE_ATTRIBUTE_HIDDEN)))
        self.assertTrue(index.skipped(_dir("x", dbk.FILE_ATTRIBUTE_REPARSE_POINT)))   # an unknown reparse point
        self.assertTrue(index.skipped(dbk.DirEntry("x", True, dbk.FILE_ATTRIBUTE_REPARSE_POINT, 0xA0000003)))
        self.assertFalse(index.skipped(_file("a.pdf", dbk.FILE_ATTRIBUTE_REPARSE_POINT, 0x9000101A)))  # OneDrive's
        for name in ("AppData", "$Recycle.Bin", ".git", "venv", ".ssh", "node_modules", "WindowsApps"):
            self.assertTrue(index.skipped(_dir(name)), name)
        for name in (".env", "id_rsa", "google_token.json", "Passwords.kdbx"):
            self.assertTrue(index.skipped(_file(name)), name)
        self.assertTrue(index.skipped(_dir("Tools.{ED7BA470-8E54-465E-825C-99712043E01C}")))   # Control Panel's
        self.assertFalse(index.skipped(_file("Essay.docx")))


# --------------------------------------------------------------------------
# What is listed: only inside the roots, checked, with its identity
# --------------------------------------------------------------------------

class ListingTests(FakeDiskCase):
    def make(self):  # type: ignore[no-untyped-def]
        fake = self.disk()
        scope = self.scope(fake)
        return fake, scope, index.LocalSearch(scope, fake)

    def test_index_rows_outside_the_roots_are_never_listed(self) -> None:
        fake, scope, search = self.make()
        good = fake.make_file(DOWNLOADS + "\\Essay final.docx")
        fake.make_file(OWNER + "\\AppData\\Local\\Essay appdata.docx")
        fake.make_file("C:\\Windows\\Essay windows.docx")
        fake.make_file(OWNER + "\\Other\\Essay other.docx")
        fake.make_file("C:\\Users\\Someone\\Downloads\\Essay someone.docx")
        fake.make_file(DOWNLOADS + "\\Projects\\app\\.git\\HEAD")
        fake.make_file(DOWNLOADS + "\\Projects\\app\\Essay repo.docx")
        fake.make_file(DOWNLOADS + "\\token essay.json")                     # secret-shaped (token*.json)
        fake.make_file(DOWNLOADS + "\\.env")
        fake.junction(DOWNLOADS + "\\into appdata", OWNER + "\\AppData\\Local")
        fake.alias(OWNER + "\\APPDAT~1", OWNER + "\\AppData")
        fake.alias(OWNER + "\\DOWNLO~1", DOWNLOADS)
        fake.set_drive("Z:\\", dbk.DRIVE_REMOTE)
        fake.set_drive("E:\\", dbk.DRIVE_REMOVABLE)
        fake.make_file("E:\\Essay usb.docx")
        rows = [good, OWNER + "\\AppData\\Local\\Essay appdata.docx", "C:\\Windows\\Essay windows.docx",
                OWNER + "\\Other\\Essay other.docx", "C:\\Users\\Someone\\Downloads\\Essay someone.docx",
                DOWNLOADS + "\\Projects\\app\\Essay repo.docx", DOWNLOADS + "\\token essay.json",
                DOWNLOADS + "\\into appdata\\Essay appdata.docx", OWNER + "\\APPDAT~1\\Local\\Essay appdata.docx",
                "Z:\\Essay mapped.docx", "E:\\Essay usb.docx", "\\\\server\\share\\Essay unc.docx",
                "\\\\?\\" + good, "\\\\.\\" + good, DOWNLOADS + "\\..\\AppData\\Local\\Essay appdata.docx",
                good + ".", good + " ", good + ":hidden", "Essay final.docx", "C:Essay final.docx",
                OWNER + "\\DOWNLO~1\\Essay final.docx", good.upper()]
        fake.index_rows = [dbk.IndexRow(path, ntpath.basename(path)) for path in rows]
        found = search.search(("essay",))
        self.assertEqual(found.source, "index")
        self.assertEqual([item.path for item in found.files], [good])          # once, by its final path
        self.assertEqual(found.files[0].name, "Essay final.docx")
        self.assertEqual(found.files[0].identity, fake.identity(good))
        self.assertGreater(found.refused, 10)
        asked = [path for _name, path in fake.calls]
        for never in ("Z:\\Essay mapped.docx", "\\\\server\\share\\Essay unc.docx", "\\\\?\\" + good):
            self.assertNotIn(("final_path", never), fake.calls)               # never opened, not even resolved
        self.assertNotIn("Z:\\Essay mapped.docx", [path for path in asked if path.startswith("Z:\\E")])

    def test_onedrive_placeholders_are_listed_and_flagged(self) -> None:
        fake, scope, search = self.make()
        path = LING + "\\LING reading list.pdf"
        fake.set_tag(path, 0x9000101A)                                        # IO_REPARSE_TAG_CLOUD_1
        fake.set_attributes(path, dbk.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
        fake.index_rows = [dbk.IndexRow(path, "LING reading list.pdf")]
        found = search.search(("reading",))
        self.assertEqual([item.path for item in found.files], [path])
        self.assertTrue(found.files[0].online_only)

    def test_with_no_index_the_walk_finds_the_same_files(self) -> None:
        fake, scope, search = self.make()
        fake.index_rows = None                                                # no index: OSError
        found = search.search(("ling", "essay"))
        self.assertEqual(found.source, "walk")
        self.assertEqual({item.name for item in found.files[:2]},
                         {"LING essay draft.docx", "LING essay draft (old).docx"})     # both words first
        self.assertTrue(all(under_a_root(item.path, scope) for item in found.files))
        self.assertTrue(fake.asked("list_folder"))

    def test_an_index_that_finds_nothing_is_backed_by_the_walk(self) -> None:
        fake, scope, search = self.make()
        fake.index_rows = []
        found = search.search(("budget",))
        self.assertEqual(found.source, "walk")
        self.assertEqual([item.name for item in found.files], ["Budget 2026.xlsx"])

    def test_the_most_words_first_and_the_folders_given(self) -> None:
        fake, scope, search = self.make()
        fake.index_rows = None
        found = search.search(("ling", "essay"), folders=[DOWNLOADS])
        self.assertEqual([item.name for item in found.files], ["LING essay draft (old).docx"])
        self.assertEqual(found.looked_in, 1)
        found = search.search(("ling",), folders=[OWNER + "\\AppData"])     # a folder outside: nowhere to look
        self.assertEqual((found.files, found.looked_in), ((), 0))

    def test_a_kind_filter_and_no_words_no_search(self) -> None:
        fake, scope, search = self.make()
        fake.index_rows = None
        found = search.search(("ling",), kind="document")
        self.assertTrue(found.files)
        self.assertTrue(all(item.file_kind == "document" for item in found.files))
        self.assertEqual(search.search(()).files, ())

    def test_recent_items_only_inside_the_roots(self) -> None:
        fake, scope, _search = self.make()
        fake.make_file(OWNER + "\\AppData\\Roaming\\notes.txt")
        essay = LING + "\\LING essay draft.docx"
        fake.recent = [essay, OWNER + "\\AppData\\Roaming\\notes.txt", "\\\\server\\share\\a.docx", LING,
                       DOWNLOADS + "\\gone.docx", essay, "Z:\\x.docx", DOWNLOADS + "\\.env"]
        fake.set_drive("Z:\\", dbk.DRIVE_REMOTE)
        files = index.recent_files(scope, fake)
        self.assertEqual([item.path for item in files], [essay])
        self.assertEqual(files[0].identity, fake.identity(essay))

    def test_the_places_never_a_code_folder_a_link_or_a_hidden_one(self) -> None:
        fake, scope, _search = self.make()
        fake.make_file(DOCS + "\\Projects\\app\\.git\\HEAD")
        fake.make_dir(DOCS + "\\Projects\\notes")
        fake.junction(DOCS + "\\Linked", OWNER + "\\AppData\\Local")
        fake.make_dir(DOCS + "\\Private")
        fake.set_attributes(DOCS + "\\Private", dbk.FILE_ATTRIBUTE_HIDDEN | dbk.FILE_ATTRIBUTE_SYSTEM)
        tree = index.folder_tree(scope, fake)
        places = index.pick_places(scope, fake, tree, words=("ling",), named=[UNI])
        labels = [" > ".join(place.parts) for place in places]
        self.assertEqual(labels[:6], ["Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"])
        self.assertEqual(labels[6:8], ["Documents > Uni", "Documents > Uni > LING"])   # named, then the words
        self.assertIn("Documents > Projects > notes", labels)
        for never in ("Documents > Projects > app", "Documents > Linked", "Documents > Private"):
            self.assertNotIn(never, labels)
        for place in places:
            self.assertTrue(under_a_root(place.path, scope))
            self.assertEqual(place.identity, fake.identity(place.path))
            self.assertLessEqual(place.depth, 3)


# --------------------------------------------------------------------------
# The fuzz: nothing outside the roots, ever
# --------------------------------------------------------------------------

class FuzzTests(FakeDiskCase):
    """Random paths built from every trick at once - fed to the index, the Recent items and the walk's disk - and the
    one rule checked on everything listed: inside a root, passing the path check, its identity the one on disk, never
    in an excluded place, never a secret, never through a link."""

    BASES = (DOWNLOADS, DOCS, LING, KNOWN["pictures"], OWNER + "\\AppData\\Local", OWNER + "\\AppData\\Roaming",
             "C:\\Windows\\System32", "C:\\Program Files\\App", "C:\\ProgramData", OWNER, OWNER + "\\Other",
             "C:\\Users\\Someone\\Documents", "D:\\Data", "Z:\\Share", "C:\\$Recycle.Bin")
    NAMES = ("essay.docx", "Essay Final.DOCX", "essay.pdf", "essay.exe", ".env", "token.json", "essay.lnk",
             "essay notes.txt", "essay.docm", "id_rsa", "essay" + chr(0x202E) + "xcod.exe", "essay.md")

    def test_nothing_outside_the_roots_is_ever_listed(self) -> None:
        fake = self.disk()
        rng = random.Random(1808)
        fake.set_drive("D:\\", dbk.DRIVE_REMOVABLE)
        fake.set_drive("Z:\\", dbk.DRIVE_REMOTE)
        fake.make_file(DOWNLOADS + "\\Repo\\.git\\HEAD")
        fake.junction(DOWNLOADS + "\\J", OWNER + "\\AppData\\Local")
        fake.make_dir(DOCS + "\\S")
        fake.set_tag(DOCS + "\\S", dbk.IO_REPARSE_TAG_SYMLINK)
        fake.make_dir(DOCS + "\\H")
        fake.set_attributes(DOCS + "\\H", dbk.FILE_ATTRIBUTE_HIDDEN | dbk.FILE_ATTRIBUTE_SYSTEM)
        fake.alias(OWNER + "\\APPDAT~1", OWNER + "\\AppData")
        fake.alias(OWNER + "\\DOWNLO~1", DOWNLOADS)
        made: list[str] = []
        for _ in range(160):
            base = rng.choice(self.BASES + (DOWNLOADS + "\\Repo", DOWNLOADS + "\\J", DOCS + "\\S", DOCS + "\\H"))
            path = base + "\\" + rng.choice(self.NAMES)
            if not base.startswith(("Z:", "D:")) and not base.endswith(("\\J", "\\S")):
                try:
                    fake.make_file(path)
                except OSError:
                    continue
            made.append(path)
        tricks = []
        for path in made:
            variants = [path, path.upper(), path.lower(), path + ".", path + " ", path + ":ads", "\\\\?\\" + path,
                        "\\\\.\\" + path, "\\\\server\\share\\" + path[3:],
                        path.replace(DOWNLOADS, OWNER + "\\DOWNLO~1"),
                        path.replace(OWNER + "\\AppData", OWNER + "\\APPDAT~1"),
                        path.replace(OWNER, OWNER + "\\Downloads\\..")]
            tricks.append(rng.choice(variants))
            tricks.append(path)
        for path in rng.sample(made, 10):
            if not path.startswith(("Z:", "D:")):
                try:
                    fake.set_tag(path, 0x9000101A)
                    fake.set_attributes(path, dbk.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
                except OSError:
                    pass
        scope = self.scope(fake)
        fake.index_rows = [dbk.IndexRow(path, ntpath.basename(path)) for path in tricks]
        fake.recent = list(tricks)
        search = index.LocalSearch(scope, fake)
        from_index = search.search(("essay",)).files + search.search(("essay",), kind="document").files
        from_recent = index.recent_files(scope, fake, limit=200)
        fake.index_rows = None
        from_walk = search.search(("essay", "token", "env", "rsa")).files               # the walk
        places = index.pick_places(scope, fake, index.folder_tree(scope, fake), words=("j", "s", "h", "repo"))
        for found in (from_index, from_recent, from_walk, places):
            self.assertTrue(found)                                            # each way listed something ...
        listed: list[FileItem | Place] = [*from_index, *from_recent, *from_walk, *places]
        self.assertLess(len({item.path for item in listed}), len(set(tricks)))     # ... and refused much
        forbidden = ("\\appdata", "\\windows", "\\program files", "\\programdata", "\\other", "\\someone",
                     "\\repo", "\\$recycle.bin", "\\j\\", "\\s\\", "\\h\\")
        for item in listed:
            with self.subTest(path=ascii(item.path)):
                key = ntpath.normcase(item.path)
                self.assertTrue(under_a_root(item.path, scope))
                self.assertTrue(paths.check_path(item.path, scope, fake).ok)
                self.assertFalse(any(word in key + "\\" for word in forbidden))
                self.assertFalse(guard.secret_file(item.path))
                self.assertEqual(paths.lexical_problem(item.path), "")
                self.assertEqual(item.identity, fake.identity(item.path))
        names = [ntpath.basename(item.path) for item in listed if isinstance(item, FileItem)]
        self.assertFalse(any(name in (".env", "token.json", "id_rsa") for name in names))


if __name__ == "__main__":
    unittest.main()
