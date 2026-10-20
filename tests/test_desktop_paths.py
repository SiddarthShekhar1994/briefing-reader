"""Tests for the desktop feature's roots and path check (briefing_reader.desktop.paths): the path fuzz - junctions,
8.3 names, alternate data streams, UNC, \\\\?\\ and other device paths, reserved names, bidi and invisible
characters, traversal, excluded subtrees (code folders, the Jarvis project, AppData, the Recycle Bin) and secret
files - and the roots.

Everything runs on tests/desktop_fakes.FakeDesktopBackend: a pretend C: drive inside a temporary folder (real
junctions are made in it); the real desktop is never reached.
"""

from __future__ import annotations

import tempfile
import unicodedata
import unittest

from briefing_reader.desktop import backend as dbk
from briefing_reader.desktop import guard, paths
from tests.desktop_fakes import KNOWN, OWNER, FakeDesktopBackend

DOCS = KNOWN["documents"]
DOWNLOADS = KNOWN["downloads"]
PROJECT = KNOWN["desktop"] + "\\Projects\\Jarvis 1.0\\briefing-reader-dev"
DATA = KNOWN["local_appdata"] + "\\briefing-reader"
PATHS_LOGGER = "briefing_reader.desktop.paths"


class PathCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.backend = FakeDesktopBackend(tmp.name)
        b = self.backend
        b.make_file(DOCS + "\\Uni\\LING\\Essay final.docx")
        b.make_file(DOWNLOADS + "\\Reading list.pdf")
        b.make_file(PROJECT + "\\.git", b"gitdir: elsewhere")      # a worktree's .git is a file
        b.make_file(PROJECT + "\\README.md")
        b.make_file(DATA + "\\actions.json")
        b.make_file("C:\\Windows\\System32\\drivers\\etc\\hosts")
        self.scope = paths.load_scope(dbk.ROOT_FOLDERS, (), b, excluded=(PROJECT, DATA))

    def check(self, path: str) -> paths.PathVerdict:
        return paths.check_path(path, self.scope, self.backend)

    def assert_refused(self, path: str, code: str, reason: str) -> None:
        verdict = self.check(path)
        self.assertFalse(verdict.ok, path)
        self.assertEqual((verdict.code, verdict.reason), (code, reason), ascii(path))
        self.assertEqual((verdict.path, verdict.root), ("", None))


class RootTests(PathCase):
    def test_the_six_known_folders_are_the_roots(self) -> None:
        roots = self.scope.roots
        self.assertEqual([root.key for root in roots], list(dbk.ROOT_FOLDERS))
        self.assertEqual([root.label for root in roots],
                         ["Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"])
        self.assertEqual({root.key for root in roots if root.onedrive}, {"desktop", "documents", "pictures"})
        self.assertEqual(roots[1].path, DOCS)
        self.assertEqual(self.scope.onedrive, KNOWN["onedrive"])
        self.assertIn((PROJECT, paths.R_JARVIS), self.scope.places)
        for name in guard.EXCLUDED_KNOWN_FOLDERS:
            self.assertIn((KNOWN[name], paths.R_SYSTEM_PLACE), self.scope.places)

    def test_a_folder_that_is_missing_networked_or_linked_is_left_out(self) -> None:
        b = self.backend
        b.known["music"] = None
        b.known["videos"] = "\\\\server\\share\\Videos"                     # a redirected network folder
        b.make_dir("C:\\Elsewhere\\Pictures")
        b.junction(OWNER + "\\Linked Pictures", "C:\\Elsewhere\\Pictures")
        b.known["pictures"] = OWNER + "\\Linked Pictures"                   # a folder moved behind a junction
        b.calls.clear()
        with self.assertLogs(PATHS_LOGGER, level="WARNING") as captured:
            scope = paths.load_scope(dbk.ROOT_FOLDERS, (), b)
        self.assertEqual([root.key for root in scope.roots], ["desktop", "documents", "downloads"])
        self.assertNotIn("\\\\server\\share\\Videos", b.asked("final_path"))   # a network path is never opened
        text = "\n".join(captured.output)
        for key in ("music", "videos", "pictures"):
            self.assertIn(key, text)
        self.assertNotIn("Owner", text)
        self.assertNotIn("server", text)

    def test_a_root_in_a_code_folder_or_appdata_is_left_out(self) -> None:
        b = self.backend
        b.make_dir(DOCS + "\\.git")
        b.known["downloads"] = KNOWN["local_appdata"] + "\\Downloads"
        b.make_dir(KNOWN["downloads"])
        with self.assertLogs(PATHS_LOGGER, level="WARNING"):
            scope = paths.load_scope(dbk.ROOT_FOLDERS, (), b)
        self.assertNotIn("documents", [root.key for root in scope.roots])
        self.assertNotIn("downloads", [root.key for root in scope.roots])

    def test_extra_folders_only_under_the_user_folder(self) -> None:
        b = self.backend
        for folder in (OWNER + "\\Uni", "C:\\Data", KNOWN["local_appdata"] + "\\Stuff", OWNER + "\\Code"):
            b.make_dir(folder)
        b.make_dir(OWNER + "\\Code\\node_modules")
        b.make_dir("D:\\Stick")
        b.set_drive("D:\\", dbk.DRIVE_REMOVABLE)
        extra = (OWNER + "\\Uni", "C:\\Data", OWNER, KNOWN["local_appdata"] + "\\Stuff", OWNER + "\\Code", "D:\\Stick",
                 "Uni", OWNER + "\\Missing", "\\\\server\\share")
        with self.assertLogs(PATHS_LOGGER, level="WARNING") as captured:
            scope = paths.load_scope(("documents",), extra, b)
        self.assertEqual([(root.key, root.label) for root in scope.roots], [("documents", "Documents"),
                                                                             ("extra1", "Uni")])
        self.assertTrue(self.check_with(scope, OWNER + "\\Uni").ok)
        text = "\n".join(captured.output)
        for index in range(2, 10):
            self.assertIn(f"extra{index}" if index in (4, 5) else f"extra folder {index}", text)
        self.assertNotIn("Owner", text)

    def check_with(self, scope: paths.Scope, path: str) -> paths.PathVerdict:
        return paths.check_path(path, scope, self.backend)

    def test_reprs_name_no_folder(self) -> None:
        verdict = self.check(DOCS + "\\Uni\\LING\\Essay final.docx")
        self.assertTrue(verdict.ok)
        for text in (repr(verdict), repr(verdict.root), repr(self.scope)):
            self.assertNotIn("Owner", text)
            self.assertNotIn("Essay", text)


class AcceptedTests(PathCase):
    def test_a_file_and_a_folder_in_the_roots(self) -> None:
        verdict = self.check(DOCS + "\\Uni\\LING\\Essay final.docx")
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.path, DOCS + "\\Uni\\LING\\Essay final.docx")
        self.assertEqual((verdict.root.key, verdict.flags), ("documents", frozenset({paths.FLAG_ONEDRIVE})))
        folder = self.check(DOCS + "\\Uni")
        self.assertTrue(folder.ok)
        local = self.check(DOWNLOADS + "/Reading list.pdf")                   # forward slashes too
        self.assertTrue(local.ok)
        self.assertEqual((local.root.key, local.flags), ("downloads", frozenset()))
        self.assertTrue(self.check(DOCS).ok)                                   # a root itself

    def test_onedrive_cloud_files_are_fine_and_flagged_online_only(self) -> None:
        name = DOCS + "\\Uni\\Lecture.mp4"
        self.backend.make_file(name)
        self.backend.set_tag(name, 0x9000301A)                                 # IO_REPARSE_TAG_CLOUD_3
        self.backend.set_attributes(name, dbk.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
        verdict = self.check(name)
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.flags, frozenset({paths.FLAG_ONEDRIVE, paths.FLAG_ONLINE_ONLY}))
        for tag in range(16):
            self.assertTrue(dbk.cloud_tag(0x9000001A | tag << 12))
            self.assertFalse(dbk.link_tag(0x9000001A | tag << 12))

    def test_8_3_names_resolve_to_their_long_names(self) -> None:
        short = self.backend.alias(OWNER + "\\DOWNLO~1", DOWNLOADS)
        verdict = self.check(short + "\\Reading list.pdf")
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.path, DOWNLOADS + "\\Reading list.pdf")
        self.backend.alias("C:\\PROGRA~1", KNOWN["program_files"])            # an 8.3 way into a system place
        self.backend.make_file(KNOWN["program_files"] + "\\App\\setup.txt")
        self.assert_refused("C:\\PROGRA~1\\App\\setup.txt", paths.REFUSE_OUTSIDE, paths.R_OUTSIDE)
        self.backend.alias(OWNER + "\\APPDAT~1", OWNER + "\\AppData")
        self.assert_refused(OWNER + "\\APPDAT~1\\Local\\briefing-reader\\actions.json", paths.REFUSE_OUTSIDE,
                            paths.R_OUTSIDE)
        self.backend.alias(DOCS + "\\PROJEC~1", DOCS + "\\Projects")
        self.backend.make_file(DOCS + "\\Projects\\repo\\.git\\HEAD")
        self.backend.make_file(DOCS + "\\Projects\\repo\\notes.txt")
        self.assert_refused(DOCS + "\\PROJEC~1\\repo\\notes.txt", paths.REFUSE_EXCLUDED, paths.R_CODE_FOLDER)

    def test_bidi_names_are_flagged_and_their_real_type_decides(self) -> None:
        sneaky = DOWNLOADS + "\\Invoice\u202efdp.exe"                            # shows as "Invoiceexe.pdf"
        plain = DOWNLOADS + "\\Notes\u200b.txt"
        self.backend.make_file(sneaky)
        self.backend.make_file(plain)
        verdict = self.check(sneaky)
        self.assertTrue(verdict.ok)
        self.assertIn(paths.FLAG_HIDDEN_CHARS, verdict.flags)
        self.assertEqual(guard.open_kind(verdict.path), guard.OPEN_BLOCKED)    # never opened: it is an exe
        self.assertEqual(paths.display_name("Invoice\u202efdp.exe"), "Invoice<U+202E>fdp.exe")
        other = self.check(plain)
        self.assertIn(paths.FLAG_HIDDEN_CHARS, other.flags)
        self.assertEqual(guard.open_kind(other.path), guard.OPEN_DOCUMENT)
        self.assertEqual(paths.display_name("Notes\u200b.txt"), "Notes<U+200B>.txt")


class LexicalTests(PathCase):
    def test_bad_shapes_are_refused_before_the_disk_is_touched(self) -> None:
        d = DOWNLOADS
        cases = [
            ("\\\\server\\share\\Essay.docx", paths.REFUSE_EXCLUDED, paths.R_UNC),
            ("//server/share/Essay.docx", paths.REFUSE_EXCLUDED, paths.R_UNC),
            ("\\\\192.168.1.5\\c$\\Users", paths.REFUSE_EXCLUDED, paths.R_UNC),
            ("\\\\?\\" + d + "\\Reading list.pdf", paths.REFUSE_OUTSIDE, paths.R_DEVICE_PREFIX),
            ("\\\\?\\UNC\\server\\share\\x.txt", paths.REFUSE_OUTSIDE, paths.R_DEVICE_PREFIX),
            ("\\\\.\\" + d + "\\Reading list.pdf", paths.REFUSE_OUTSIDE, paths.R_DEVICE_PREFIX),
            ("\\\\.\\PhysicalDrive0", paths.REFUSE_OUTSIDE, paths.R_DEVICE_PREFIX),
            ("\\??\\" + d + "\\Reading list.pdf", paths.REFUSE_OUTSIDE, paths.R_DEVICE_PREFIX),
            ("C:Users\\Owner\\Downloads\\Reading list.pdf", paths.REFUSE_OUTSIDE, paths.R_DRIVE_RELATIVE),
            ("C:", paths.REFUSE_OUTSIDE, paths.R_DRIVE_RELATIVE),
            ("Downloads\\Reading list.pdf", paths.REFUSE_OUTSIDE, paths.R_RELATIVE),
            ("\\Users\\Owner\\Downloads\\Reading list.pdf", paths.REFUSE_OUTSIDE, paths.R_RELATIVE),
            ("1:\\x", paths.REFUSE_OUTSIDE, paths.R_RELATIVE),
            (d + "\\Reading list.pdf:Zone.Identifier", paths.REFUSE_OUTSIDE, paths.R_ADS),
            (d + "\\Reading list.pdf::$DATA", paths.REFUSE_OUTSIDE, paths.R_ADS),
            (d + ":hidden\\x.txt", paths.REFUSE_OUTSIDE, paths.R_ADS),
            (d + "\\..\\AppData\\Local\\briefing-reader\\actions.json", paths.REFUSE_OUTSIDE, paths.R_DOT),
            ("C:/Users/Owner/Downloads/../../Owner/AppData", paths.REFUSE_OUTSIDE, paths.R_DOT),
            (d + "\\.\\Reading list.pdf", paths.REFUSE_OUTSIDE, paths.R_DOT),
            (d + "\\...\\x.txt", paths.REFUSE_OUTSIDE, paths.R_TRAILING),
            (d + "\\Reading list.pdf.", paths.REFUSE_OUTSIDE, paths.R_TRAILING),
            (d + "\\Reading list.pdf ", paths.REFUSE_OUTSIDE, paths.R_TRAILING),
            (d + "\\Old \\x.txt", paths.REFUSE_OUTSIDE, paths.R_TRAILING),
            (d + "\\CON", paths.REFUSE_OUTSIDE, paths.R_RESERVED),
            (d + "\\nul.txt", paths.REFUSE_OUTSIDE, paths.R_RESERVED),
            (d + "\\Com1.docx", paths.REFUSE_OUTSIDE, paths.R_RESERVED),
            (d + "\\LPT9", paths.REFUSE_OUTSIDE, paths.R_RESERVED),
            (d + "\\COM0.txt", paths.REFUSE_OUTSIDE, paths.R_RESERVED),
            (d + "\\com\u00b9.txt", paths.REFUSE_OUTSIDE, paths.R_RESERVED),
            (d + "\\aux .txt", paths.REFUSE_OUTSIDE, paths.R_RESERVED),
            (d + "\\CONIN$", paths.REFUSE_OUTSIDE, paths.R_RESERVED),
            (d + "\\PRN\\x.txt", paths.REFUSE_OUTSIDE, paths.R_RESERVED),
            (d + "\\a?.txt", paths.REFUSE_OUTSIDE, paths.R_BAD_CHAR),
            (d + "\\a*.txt", paths.REFUSE_OUTSIDE, paths.R_BAD_CHAR),
            (d + "\\a|b.txt", paths.REFUSE_OUTSIDE, paths.R_BAD_CHAR),
            (d + "\\<b>.txt", paths.REFUSE_OUTSIDE, paths.R_BAD_CHAR),
            (d + '\\"q".txt', paths.REFUSE_OUTSIDE, paths.R_BAD_CHAR),
            (d + "\\a\x01.txt", paths.REFUSE_OUTSIDE, paths.R_BAD_CHAR),
            (d + "\\a\x00.txt", paths.REFUSE_OUTSIDE, paths.R_BAD_CHAR),
            (d + "\\a\x85.txt", paths.REFUSE_OUTSIDE, paths.R_BAD_CHAR),
            ("C:\\Users\\\\Owner\\Downloads", paths.REFUSE_OUTSIDE, paths.R_BAD_CHAR),
            (d + "\\" + "x" * 33_000, paths.REFUSE_OUTSIDE, paths.R_TOO_LONG),
            (d + "\\a" * 70, paths.REFUSE_OUTSIDE, paths.R_TOO_LONG),
            ("", paths.REFUSE_OUTSIDE, paths.R_NOT_TEXT),
        ]
        for path, code, reason in cases:
            with self.subTest(path=ascii(path)[:80]):
                self.backend.calls.clear()
                self.assert_refused(path, code, reason)
                self.assertEqual(self.backend.calls, [])                       # nothing was opened or looked at
        for value in (None, 12, b"C:\\x", ["C:\\x"]):
            self.assertEqual(paths.check_path(value, self.scope, self.backend).reason, paths.R_NOT_TEXT)

    def test_a_network_result_from_the_final_path_is_refused(self) -> None:
        self.assertEqual(paths._strip_final("\\\\?\\UNC\\server\\share\\x.txt"), "\\\\server\\share\\x.txt")
        self.assertEqual(paths.lexical_problem(paths._strip_final("\\\\?\\UNC\\server\\share\\x.txt")), paths.R_UNC)
        self.assertEqual(paths._strip_final("\\\\?\\C:\\Users\\Owner"), "C:\\Users\\Owner")
        self.assertEqual(paths.lexical_problem(paths._strip_final("\\\\?\\Volume{0}\\x")), paths.R_DEVICE_PREFIX)

    def test_a_missing_path_is_refused(self) -> None:
        self.assert_refused(DOCS + "\\Uni\\Nope.docx", paths.REFUSE_OUTSIDE, paths.R_MISSING)


class LinkTests(PathCase):
    def test_a_junction_out_of_the_roots_is_refused(self) -> None:
        link = self.backend.junction(DOCS + "\\Shortcut", "C:\\Windows\\System32")
        self.assert_refused(link + "\\drivers\\etc\\hosts", paths.REFUSE_OUTSIDE, paths.R_OUTSIDE)
        self.backend.junction(DOCS + "\\Data", KNOWN["local_appdata"])
        self.assert_refused(DOCS + "\\Data\\briefing-reader\\actions.json", paths.REFUSE_OUTSIDE, paths.R_OUTSIDE)

    def test_a_junction_inside_the_roots_is_refused_too(self) -> None:
        link = self.backend.junction(DOCS + "\\To Downloads", DOWNLOADS)
        self.assert_refused(link + "\\Reading list.pdf", paths.REFUSE_EXCLUDED, paths.R_LINK)
        self.assert_refused(link, paths.REFUSE_EXCLUDED, paths.R_LINK)
        self.assertTrue(self.check(DOWNLOADS + "\\Reading list.pdf").ok)       # the real place is fine

    def test_symbolic_links_and_app_execution_aliases_are_refused(self) -> None:
        self.backend.make_dir(DOCS + "\\Linked")
        self.backend.set_tag(DOCS + "\\Linked", dbk.IO_REPARSE_TAG_SYMLINK)
        self.backend.make_file(DOCS + "\\Linked\\a.txt")
        self.assert_refused(DOCS + "\\Linked\\a.txt", paths.REFUSE_EXCLUDED, paths.R_LINK)
        self.backend.make_file(DOWNLOADS + "\\python.exe")
        self.backend.set_tag(DOWNLOADS + "\\python.exe", dbk.IO_REPARSE_TAG_APPEXECLINK)
        self.assert_refused(DOWNLOADS + "\\python.exe", paths.REFUSE_EXCLUDED, paths.R_LINK)
        for tag in (dbk.IO_REPARSE_TAG_MOUNT_POINT, dbk.IO_REPARSE_TAG_SYMLINK, dbk.IO_REPARSE_TAG_APPEXECLINK):
            self.assertTrue(dbk.link_tag(tag))
            self.assertFalse(dbk.cloud_tag(tag))

    def test_hidden_system_components_are_refused(self) -> None:
        self.backend.make_file(DOCS + "\\Secret\\a.txt")
        self.backend.set_attributes(DOCS + "\\Secret", dbk.FILE_ATTRIBUTE_HIDDEN | dbk.FILE_ATTRIBUTE_SYSTEM)
        self.assert_refused(DOCS + "\\Secret\\a.txt", paths.REFUSE_EXCLUDED, paths.R_HIDDEN_SYSTEM)
        self.backend.make_file(DOCS + "\\Hidden\\a.txt")
        self.backend.set_attributes(DOCS + "\\Hidden", dbk.FILE_ATTRIBUTE_HIDDEN)   # hidden only: fine
        self.assertTrue(self.check(DOCS + "\\Hidden\\a.txt").ok)

    def test_removable_and_network_drives_are_refused(self) -> None:
        self.backend.make_file("D:\\Coursework\\a.txt")
        for kind in (dbk.DRIVE_REMOVABLE, dbk.DRIVE_REMOTE, dbk.DRIVE_CDROM, dbk.DRIVE_UNKNOWN):
            self.backend.set_drive("D:\\", kind)
            self.assert_refused("D:\\Coursework\\a.txt", paths.REFUSE_EXCLUDED, paths.R_NOT_FIXED)


class ExcludedTests(PathCase):
    def test_code_folders_are_never_a_place(self) -> None:
        b = self.backend
        for index, marker in enumerate(guard.CODE_MARKERS):
            folder = f"{DOCS}\\Code {index}"
            if marker == ".env":
                b.make_file(f"{folder}\\{marker}")                              # a folder holding a .env file
            else:
                b.make_dir(f"{folder}\\{marker}")
            b.make_file(f"{folder}\\src\\main.txt")
            with self.subTest(marker=marker):
                self.assert_refused(f"{folder}\\src\\main.txt", paths.REFUSE_EXCLUDED, paths.R_CODE_FOLDER)
                self.assert_refused(folder, paths.REFUSE_EXCLUDED, paths.R_CODE_FOLDER)   # the folder itself
        b.make_file(DOCS + "\\web\\node_modules\\pkg\\index.txt")
        self.assert_refused(DOCS + "\\web\\node_modules\\pkg\\index.txt", paths.REFUSE_EXCLUDED, paths.R_CODE_FOLDER)
        b.make_file(DOCS + "\\Py\\.venv\\pyvenv.txt")
        self.assert_refused(DOCS + "\\Py\\.venv\\pyvenv.txt", paths.REFUSE_EXCLUDED, paths.R_CODE_FOLDER)
        self.assertTrue(self.check(DOCS + "\\Uni\\LING\\Essay final.docx").ok)   # a sibling is fine

    def test_the_jarvis_project_is_never_a_place(self) -> None:
        self.assert_refused(PROJECT + "\\README.md", paths.REFUSE_EXCLUDED, paths.R_JARVIS)
        self.assert_refused(PROJECT, paths.REFUSE_EXCLUDED, paths.R_JARVIS)
        bare = paths.load_scope(dbk.ROOT_FOLDERS, (), self.backend)            # without the app's own places ...
        verdict = paths.check_path(PROJECT + "\\README.md", bare, self.backend)
        self.assertEqual((verdict.code, verdict.reason), (paths.REFUSE_EXCLUDED, paths.R_CODE_FOLDER))   # ... .git
        verdict = paths.check_path(DATA + "\\actions.json", bare, self.backend)
        self.assertEqual((verdict.code, verdict.reason), (paths.REFUSE_OUTSIDE, paths.R_OUTSIDE))

    def test_system_places_inside_a_root(self) -> None:
        b = self.backend
        for name in ("AppData", "appdata", "$Recycle.Bin", "System Volume Information", "WindowsApps"):
            b.make_file(f"{DOCS}\\Old PC\\{name}\\x.txt")
            with self.subTest(name=name):
                self.assert_refused(f"{DOCS}\\Old PC\\{name}\\x.txt", paths.REFUSE_EXCLUDED, paths.R_SYSTEM_PLACE)
        b.make_file(DOCS + "\\\uff21ppData\\x.txt")                              # a fullwidth lookalike counts too
        self.assert_refused(DOCS + "\\\uff21ppData\\x.txt", paths.REFUSE_EXCLUDED, paths.R_SYSTEM_PLACE)
        b.make_file(DOCS + "\\Windows tips\\x.txt")                             # an ordinary folder name is fine
        self.assertTrue(self.check(DOCS + "\\Windows tips\\x.txt").ok)

    def test_places_outside_the_roots(self) -> None:
        for path in ("C:\\Windows\\System32\\drivers\\etc\\hosts", DATA + "\\actions.json", OWNER):
            with self.subTest(path=path):
                self.assert_refused(path, paths.REFUSE_OUTSIDE, paths.R_OUTSIDE)

    def test_secret_files_are_never_touched(self) -> None:
        names = (".env", ".env.local", "server.pem", "server.KEY", "cert.pfx", "cert.p12", "putty.ppk", "id_rsa",
                 "id_rsa.pub", "id_ed25519", "Passwords.kdbx", "old.kdb", "credentials.json",
                 "credentials (1).json", "token.json", "token_cache.json", "client_secret_123.json",
                 ".git-credentials", ".netrc", "_netrc", ".npmrc", ".pypirc", "wallet.dat", "Wallet.DAT")
        for name in names:
            self.backend.make_file(f"{DOWNLOADS}\\Keys\\{name}")
            with self.subTest(name=name):
                self.assertTrue(guard.secret_file(name))
                self.assert_refused(f"{DOWNLOADS}\\Keys\\{name}", paths.REFUSE_EXCLUDED, paths.R_SECRET)
        self.backend.make_file(DOCS + "\\.ssh\\config")
        self.assert_refused(DOCS + "\\.ssh\\config", paths.REFUSE_EXCLUDED, paths.R_SECRET)
        for name in ("Envelope.docx", "Keynote notes.txt", "Tokens of thanks.docx", "credentials.txt",
                     "Key dates.pdf", "wallet.txt"):
            self.assertFalse(guard.secret_file(name), name)

    def test_logs_never_hold_a_path(self) -> None:
        with self.assertNoLogs("briefing_reader.desktop", level="INFO"):
            for path in (DOCS + "\\Uni\\LING\\Essay final.docx", "\\\\server\\x", PROJECT + "\\README.md"):
                self.check(path)


class NameTests(unittest.TestCase):
    """New names (a rename, a new folder, a zip): SPEC 4.4 rule 4."""

    def test_good_names(self) -> None:
        for name in ("Notes 2026.docx", "LING essay (final).docx", "Caf\u00e9 menu.pdf", "a", "x" * 115 + ".docx"):
            self.assertEqual(paths.name_problem(name), "", name)

    def test_bad_names(self) -> None:
        bad = ["", "x" * 121, "CON", "nul.txt", "COM1.docx", "lpt9", "a.txt.", "a.txt ", "a/b.txt", "a\\b.txt",
               "a:b.txt", "a*b", "a?b", 'a"b', "a<b", "a>b", "a|b", "a\x01b", "Invoice\u202efdp.exe", "a\u200bb.txt",
               "a\u2066b.txt", "e\u0301.txt", ".", "..", None, 7]
        for name in bad:
            with self.subTest(name=ascii(name)):
                self.assertEqual(paths.name_problem(name), paths.REFUSE_NAME)
        self.assertEqual(paths.name_problem(unicodedata.normalize("NFC", "e\u0301.txt")), "")

    def test_blocked_and_shortcut_types(self) -> None:
        for name in ("setup.exe", "run.BAT", "x.ps1", "page.url", "Essay.lnk", "evil.scr", "a.msi"):
            self.assertEqual(paths.name_problem(name), paths.REFUSE_TYPE, name)

    def test_reserved_names(self) -> None:
        for name in ("CON", "con", "Prn", "AUX", "NUL", "CONIN$", "conout$", "COM1", "com9", "LPT1", "lpt0",
                     "COM\u00b2", "con.txt", "NUL.tar.gz", "aux "):
            self.assertTrue(paths.reserved_name(name), name)
        for name in ("CONSOLE", "Contract", "COM10", "LPT", "nullable.txt", "auxiliary"):
            self.assertFalse(paths.reserved_name(name), name)


if __name__ == "__main__":
    unittest.main()
