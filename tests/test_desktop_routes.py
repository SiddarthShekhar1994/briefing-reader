"""Jarvis on your desktop, P1.7: which typed requests are desktop requests (ask.routes ROUTE_DESKTOP, only while
[desktop] is on: clear desktop verbs; everything else stays a normal Ask and the other shortcuts keep their words), the
desktop's local routes (desktop.routes, SPEC 2.2: answered on this PC with no planner run - Phase 1's open and find
ones; Phase 2's are recognised and answered "not yet"), Jarvis's own words for them (never a name of an app, a site
or a file), and the desktop's kinds kept out of the briefing's and Ask's card kinds (actions.py, ask/validate.py
ASK_KINDS): a briefing written from other people's email can never propose a desktop operation. Pure; every name is
invented.
"""

from __future__ import annotations

import ast
import inspect
import unittest

from briefing_reader import actions
from briefing_reader.ask import validate as ask_validate
from briefing_reader.ask.modes import MODE_DESKTOP, MODE_KINDS
from briefing_reader.ask.routes import (
    ROUTE_DESKTOP,
    ROUTE_FOLLOWUPS,
    ROUTE_MEMORY_SAVE,
    ROUTE_MEMORY_SHOW,
    ROUTE_NONE,
    ROUTE_PLAN,
    ROUTE_PREP,
    Route,
    RouteFlags,
    route,
)
from briefing_reader.desktop import guard
from briefing_reader.desktop import routes as dr
from briefing_reader.desktop import validate as dv
from briefing_reader.desktop.context import AppItem, DesktopContext, SiteItem
from tests.desktop_fakes import DOWNLOADS, LING, FakeEntry, FakeIndex, desktop_context

ON = RouteFlags(desktop=True)
ALL_ON = RouteFlags(memory=True, plan=True, prep=True, followups=True, desktop=True)
DESKTOP_REQUESTS = (
    "open my LING essay", "open Spotify", "launch Word", "start Spotify", "open Downloads", "open my documents folder",
    "open https://example.org/news", "open youtube.com", "find my budget spreadsheet",
    "where is the reading from last week?", "where did I put my essay.docx", "look for the PDF from Ana",
    "show me the photos from Sunday", "locate my CV", "where are my lecture notes?", "Jarvis, open my essay",
    "please open the budget.", "OPEN SPOTIFY", "open   my   LING   essay",
    "volume 30", "mute", "louder", "pause", "next song", "set a timer for 10 minutes",
    "remind me in 5 minutes to stretch", "take a screenshot", "focus mode for 45 minutes")
ASKS = (
    "open my email", "open the briefing", "open my calendar", "open my inbox", "open settings", "open the Approvals screen",
    "open Live", "start recording", "start over", "start my day", "start reading", "find a time for lunch",
    "where is my next meeting?", "find Ana's email", "show me the weather", "what's on today?",
    "move my Project sync to Friday and tell Ana", "open", "find", "where is", "open my follow-ups",
    "show me the meetings", "look for a free slot on Friday", "draft a reply to Ana", "")


class DesktopRouteTests(unittest.TestCase):
    """ask.routes: ROUTE_DESKTOP only with [desktop] on, for clear desktop verbs only."""

    def test_clear_desktop_verbs(self) -> None:
        for text in DESKTOP_REQUESTS:
            with self.subTest(text=text):
                self.assertTrue(dr.desktop_request(text))
                self.assertEqual(route(text, ON), Route(ROUTE_DESKTOP, " ".join(text.split())))   # the words
                self.assertEqual(route(text, RouteFlags()).kind, ROUTE_NONE)      # [desktop] off: a normal Ask
                self.assertEqual(route(text, RouteFlags(memory=True, plan=True, prep=True, followups=True)).kind,
                                 ROUTE_NONE)

    def test_everything_else_is_a_normal_ask(self) -> None:
        for text in ASKS:
            with self.subTest(text=text):
                self.assertFalse(dr.desktop_request(text))
                self.assertEqual(route(text, ON).kind, ROUTE_NONE)

    def test_the_other_shortcuts_keep_their_words(self) -> None:
        cases = (("remember that the essay is due Friday", ROUTE_MEMORY_SAVE),
                 ("what do you remember?", ROUTE_MEMORY_SHOW), ("plan my day", ROUTE_PLAN),
                 ("prep me for my 2 PM", ROUTE_PREP), ("show me my follow-ups", ROUTE_FOLLOWUPS),
                 ("any follow-ups?", ROUTE_FOLLOWUPS), ("open my LING essay", ROUTE_DESKTOP))
        for text, kind in cases:
            with self.subTest(text=text):
                self.assertEqual(route(text, ALL_ON).kind, kind)

    def test_the_route_names_no_words(self) -> None:
        found = route("open my LING essay", ON)
        self.assertNotIn("LING", repr(found))
        self.assertEqual(found.payload, "open my LING essay")


class LocalRouteTests(unittest.TestCase):
    """desktop.routes.route: SPEC 2.2's full-match local routes (no planner run)."""

    def setUp(self) -> None:
        self.context = desktop_context()

    def local(self, text: str, **kwargs) -> dr.LocalRoute | None:  # type: ignore[no-untyped-def]
        return dr.route(text, kwargs.pop("context", self.context), **kwargs)

    def test_phase_two_shapes_are_recognised_and_not_yet(self) -> None:
        cases = {
            dv.VOLUME: ("volume 30", "volume 30%", "volume up", "volume down", "volume mute", "volume unmute", "louder",
                        "quieter", "mute", "unmute", "turn the volume down", "turn it up"),
            dv.MEDIA: ("pause", "play", "resume", "next", "skip", "previous", "play the music", "resume the music",
                       "pause the video", "next song", "previous track", "skip the track"),
            dv.TIMER: ("timer 10 minutes", "set a timer for 5 mins", "start a timer for 25 minutes",
                       "remind me in 20 minutes", "remind me in 20 minutes to stretch"),
            dv.SCREENSHOT: ("screenshot", "screen shot", "take a screenshot", "screen grab", "take a screen grab"),
            dv.FOCUS_MODE: ("focus for 45 minutes", "focus mode for 30 mins"),
        }
        for kind, texts in cases.items():
            for text in texts:
                with self.subTest(text=text):
                    found = self.local(text)
                    self.assertEqual((found.kind, found.code, found.tier), (kind, dv.NOT_YET, ""))
                    self.assertTrue(dr.desktop_request(text))
                    self.assertEqual(self.local(text, context=None), found)       # no candidates needed
        self.assertEqual(dr.NOT_YET_KINDS, frozenset(cases))

    def test_known_folders(self) -> None:
        downloads = self.context.places[3]
        cases = (("open Downloads", "downloads", downloads), ("open my downloads folder", "downloads", downloads),
                 ("open the Documents", "documents", self.context.places[0]),
                 ("show me my pictures", "pictures", None),               # not listed: the place comes later (P1.8)
                 ("show the desktop", "desktop", None), ("Jarvis, open music.", "music", None),
                 ("please open videos!", "videos", None))
        for text, folder, place in cases:
            with self.subTest(text=text):
                found = self.local(text)
                self.assertEqual((found.kind, found.tier, found.code, found.folder), (dv.OPEN_FOLDER, dv.INSTANT, "",
                                                                                    folder))
                self.assertIs(found.target, place)
        self.assertIsNone(self.local("opendownloads"))
        self.assertIsNone(self.local("open my downloads from yesterday"))     # not the folder itself: the planner

    def test_web_addresses(self) -> None:
        found = self.local("open https://example.org/news")                    # typed word for word: at once
        self.assertEqual((found.kind, found.tier, found.target, found.flags),
                         (dv.OPEN_URL, dv.INSTANT, "https://example.org/news", frozenset()))
        self.assertEqual(self.local("open https://example.org/news", context=None), found)
        cases = (("open youtube.com", "https://youtube.com"), ("open bbc.co.uk/news", "https://bbc.co.uk/news"),
                 ("open www.example.org", "https://www.example.org"))
        for text, url in cases:
            with self.subTest(text=text):                                      # a bare address: on a click only
                found = self.local(text)
                self.assertEqual((found.kind, found.tier, found.target, found.flags),
                                 (dv.OPEN_URL, dv.ONE_CLICK, url, frozenset({dv.FLAG_BARE_HOST})))
                self.assertEqual(self.local(text, context=None), found)
        for text in ("open http://example.org/", "open https://localhost/admin", "open https://192.168.1.4/",
                     "open notes.md", "open setup.exe", "open ftp://example.org", "open javascript:alert(1)",
                     "open https://owner:secret@example.org/"):
            with self.subTest(text=text):
                self.assertIsNone(self.local(text))                    # not on this PC: the planner, then the checks

    def test_a_files_name_never_goes_to_the_web_without_a_click(self) -> None:
        """P1.R (p1r-2): "open logo.ai" (an Illustrator file), "open song.ly" (LilyPond), "open track.it" (Impulse
        Tracker) never open https://logo.ai at once: the file of that very name is found when the folders hold it (its
        card shows it - never the web), else the web page waits on a card for the owner's click, its host shown. A
        document's name (notes.md, report.pdf) is never a web address at all; only an https address typed word for
        word, or a saved site, opens at once."""
        disk = FakeIndex([FakeEntry(DOWNLOADS + "\\logo.ai", "logo.ai"), FakeEntry(DOWNLOADS + "\\Logo.AI old.png",
                                                                                    "Logo.AI old.png"),
                          FakeEntry(LING + "\\Song.LY", "Song.LY"), FakeEntry(LING + "\\notes.md", "notes.md")])
        for text, name in (("open logo.ai", "logo.ai"), ("open song.ly", "Song.LY")):
            with self.subTest(text=text):                                      # the file is in the folders
                found = self.local(text, search=disk.search)
                self.assertEqual((found.kind, found.tier), (dv.FIND_FILES, dv.INSTANT))
                self.assertEqual([item.name for item in found.hits], [name])   # that very name only
                self.assertIsNone(found.target)
        for text, url in (("open logo.ai", "https://logo.ai"), ("open poster.ai", "https://poster.ai"),
                          ("open track.it", "https://track.it"), ("open song.ly", "https://song.ly"),
                          ("open bbc.co.uk", "https://bbc.co.uk"), ("open example.org", "https://example.org")):
            for search in (FakeIndex([]).search, None):
                with self.subTest(text=text, search=search is not None):      # no such file: a click first
                    found = self.local(text, search=search)
                    self.assertEqual((found.kind, found.tier, found.target, found.flags),
                                     (dv.OPEN_URL, dv.ONE_CLICK, url, frozenset({dv.FLAG_BARE_HOST})))
        for text in ("open notes.md", "open report.pdf", "open essay.docx", "open budget.xlsx", "open photo.jpg"):
            with self.subTest(text=text):                                      # a document's name: never the web
                self.assertIsNone(self.local(text, search=disk.search))
        self.assertFalse(dr.WEB_ENDINGS & (guard.OPENABLE_TYPES | guard.SCRIPT_CAPABLE_TYPES))
        for text in ("open https://logo.ai", "open https://www.bbc.co.uk/news"):
            with self.subTest(text=text):                                      # typed word for word: at once
                found = self.local(text, search=disk.search)
                self.assertEqual((found.kind, found.tier, found.flags), (dv.OPEN_URL, dv.INSTANT, frozenset()))
        portal = self.local("open the student portal", search=disk.search)    # a saved site: at once
        self.assertEqual((portal.kind, portal.tier), (dv.OPEN_URL, dv.INSTANT))
        self.assertEqual(dr.route_words(self.local("open logo.ai"), "sir"),
                         "Understood: open a web page. Nothing was opened yet, sir.")

    def test_apps_and_saved_sites_by_their_whole_name(self) -> None:
        word, spotify, shell = self.context.apps
        portal = self.context.sites[0]
        cases = (("open Spotify", spotify), ("launch the Spotify app", spotify), ("start microsoft   word", word),
                 ("OPEN SPOTIFY", spotify), ("open Student portal", portal), ("open the student portal", portal),
                 ("open my Student Portal", portal))
        for text, target in cases:
            with self.subTest(text=text):
                found = self.local(text)
                self.assertIs(found.target, target)
                self.assertEqual((found.kind, found.tier), (dv.OPEN_APP if isinstance(target, AppItem)
                                                            else dv.OPEN_URL, dv.INSTANT))
        for text in ("open Word", "open Spoti", "open portal", "open Windows PowerShell", "launch powershell"):
            with self.subTest(text=text):
                self.assertIsNone(self.local(text))                    # not exact, or never opened: the planner
        self.assertIsNone(self.local("open Spotify", context=None))      # no candidates: nothing to match
        bad = DesktopContext(sites=(SiteItem("http://plain.example.org/", "Plain site"),))
        self.assertIsNone(dr.route("open Plain site", bad))
        self.assertIsNotNone(shell)

    def test_find_with_the_local_search(self) -> None:
        index = FakeIndex([FakeEntry(LING + "\\LING essay draft.docx", "LING essay draft.docx"),
                           FakeEntry(DOWNLOADS + "\\Budget 2026.xlsx", "Budget 2026.xlsx"),
                           FakeEntry(DOWNLOADS + "\\Old budget notes.txt", "Old budget notes.txt")])
        found = self.local("find my budget", search=index.search)
        self.assertEqual((found.kind, found.tier, found.words, len(found.hits)), (dv.FIND_FILES, dv.INSTANT, "budget",
                                                                                  2))
        found = self.local("where is the LING essay draft?", search=index.search)
        self.assertEqual([hit.name for hit in found.hits], ["LING essay draft.docx"])
        self.assertEqual(index.queries[-1], ("ling", "essay", "draft"))
        self.assertIsNone(self.local("find unicorns", search=index.search))   # no hit: the planner
        self.assertIsNone(self.local("find my budget"))                       # no local search yet (P1.8)

        def broken(_words: object) -> list:
            raise OSError(5, "denied")

        self.assertIsNone(self.local("find my budget", search=broken))        # a search that fails: the planner
        loose = FakeIndex([FakeEntry(DOWNLOADS + "\\Budget.xlsx", "Budget.xlsx")])
        self.assertIsNone(self.local("find the budget essay", search=lambda words: loose.entries))   # every word

    def test_anything_else_goes_to_the_planner(self) -> None:
        for text in ("open my LING essay", "open the essay I wrote yesterday", "find the reading from last week",
                     "open Spotify and my essay", "show me the photos from Sunday", "", "   "):
            with self.subTest(text=text):
                self.assertIsNone(self.local(text))

    def test_repr_names_the_kind_only(self) -> None:
        found = self.local("open Spotify")
        self.assertEqual(repr(found), "LocalRoute(open_app)")
        self.assertEqual(repr(self.local("volume 30")), "LocalRoute(volume, not_yet)")


class RouteWordsTests(unittest.TestCase):
    """Jarvis's own words for a local route (the HUD's caption): what was understood and that nothing was opened yet,
    or that it can't be done yet. Never an app's, a site's or a file's name."""

    def test_the_words(self) -> None:
        context = desktop_context()
        index = FakeIndex([FakeEntry(DOWNLOADS + "\\Budget.xlsx", "Budget.xlsx"),
                           FakeEntry(DOWNLOADS + "\\Budget notes.txt", "Budget notes.txt")])
        one = FakeIndex([FakeEntry(DOWNLOADS + "\\Budget.xlsx", "Budget.xlsx")])
        cases = (
            (dr.route("open Downloads", context), "Understood: open your Downloads folder. Nothing was opened yet, sir."),
            (dr.route("show me my pictures", context),
             "Understood: open your Pictures folder. Nothing was opened yet, sir."),
            (dr.route("open Spotify", context), "Understood: open an app. Nothing was opened yet, sir."),
            (dr.route("open Student portal", context), "Understood: open a web page. Nothing was opened yet, sir."),
            (dr.route("open youtube.com", context), "Understood: open a web page. Nothing was opened yet, sir."),
            (dr.route("find my budget", context, search=index.search),
             "Understood: look for files - 2 matches. Nothing was opened yet, sir."),
            (dr.route("find my budget", context, search=one.search),
             "Understood: look for files - 1 match. Nothing was opened yet, sir."),
            (dr.route("volume 30", context), "I can't change the volume yet, sir - nothing was changed."),
            (dr.route("pause", context), "I can't control music yet, sir - nothing was changed."),
            (dr.route("set a timer for 10 minutes", context), "I can't set timers yet, sir - nothing was changed."),
            (dr.route("take a screenshot", context), "I can't take screenshots yet, sir - nothing was changed."),
            (dr.route("focus for 45 minutes", context), "I can't start focus mode yet, sir - nothing was changed."),
        )
        for found, words in cases:
            with self.subTest(words=words):
                said = dr.route_words(found)
                self.assertEqual(said, words)
                for name in ("Spotify", "Student", "portal", "youtube", "Budget", "budget", "LING"):
                    self.assertNotIn(name, said)
        self.assertEqual(dr.route_words(dr.route("open Spotify", context), ""),
                         "Understood: open an app. Nothing was opened yet.")
        self.assertEqual(dr.route_words(dr.route("volume 30", context), "boss"),
                         "I can't change the volume yet, boss - nothing was changed.")


class DesktopKindsStayOutTests(unittest.TestCase):
    """SPEC 12: desktop kinds are never card kinds. A briefing written from other people's email (actions.py) and an
    Ask's answer (ask.validate.ASK_KINDS) can never propose a desktop operation, and a desktop run proposes no card
    lines at all (MODE_KINDS empty)."""

    SKILLS = tuple(dv.SKILL_PHASES)

    def test_no_desktop_kind_is_a_card_kind(self) -> None:
        self.assertEqual(set(self.SKILLS) & set(actions.STRUCTURED_KINDS), set())
        self.assertEqual(set(self.SKILLS) & set(ask_validate.ASK_KINDS), set())
        self.assertEqual(MODE_KINDS[MODE_DESKTOP], frozenset())
        for module in (actions, ask_validate):                # no string in either module is a desktop kind
            tree = ast.parse(inspect.getsource(module))
            strings = {node.value for node in ast.walk(tree)
                       if isinstance(node, ast.Constant) and isinstance(node.value, str)}
            with self.subTest(module=module.__name__):
                self.assertEqual(strings & {*self.SKILLS, "desktop"}, set())
                for skill in self.SKILLS:
                    if "_" in skill:
                        self.assertNotIn(skill, inspect.getsource(module))   # not even named there

    def test_a_desktop_line_is_never_a_card_to_approve(self) -> None:
        """A line naming a desktop skill - in a briefing or an Ask's answer - is at most information: its label is
        read as any other made-up kind word ("timer"), or as the existing kind its first word names ("open_file:" an
        Open card, "move_files:" a Move), never as a key=value card, so it has no Approve and nothing carries it out."""
        for skill in self.SKILLS:
            for line in (f"{skill}: f1", f"{skill.replace('_', ' ').title()}: f1 | d3",
                         f"{skill}: files=f1 | to=d3 | place=d3 | app=a1",
                         f"**{skill}**: C:\\Users\\Owner\\Downloads\\SpotifySetup.exe"):
                with self.subTest(line=line):
                    card = actions.parse_action_line(line)
                    self.assertFalse(card.structured or card.decidable or card.actionable)
                    asked = actions.restrict(card, ask_validate.ASK_KINDS, "ask")
                    self.assertFalse(asked.decidable or asked.actionable)
                    if card.kind not in ask_validate.ASK_KINDS:
                        self.assertTrue(asked.error)          # "Ask can't propose this kind"
                    self.assertFalse(asked.link.casefold().startswith(("c:", "file:")))

    def test_a_local_target_never_rides_on_an_open_card(self) -> None:
        card = actions.parse_action_line("Open: The installer | C:\\Users\\Owner\\Downloads\\SpotifySetup.exe")
        self.assertFalse(card.link.casefold().startswith(("c:", "file:")))
        self.assertFalse(actions.is_web_open(card))


if __name__ == "__main__":
    unittest.main()
