"""Tests for the hologram's round 1 wiring (briefing_reader.home and ui.ReadingView / AppController): caption
paging and reveal, the Dock's screens, Home's chips, the Transcript and Day view drawers, the header, the
hero orb's states from the app (thinking, speaking, listening, needs you, muted, offline voice, waiting),
when it stops, deep idle, Settings, and no technical readouts on Home.

Qt runs offscreen in this process with the bundled fonts and shows nothing; every text is invented.
"""

from __future__ import annotations

import dataclasses
import time
import unittest
from datetime import datetime
from unittest import mock

from PySide6.QtCore import QEvent, QEventLoop, Qt, QTimer, QUrl
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from briefing_reader import dock, holo, home, hud, offer_view, orb, ui
from tests.ui_fakes import AppHarness, fonts, settle

_app: QApplication | None = None
SPEC_ANSWER = ("Three things tomorrow, sir. Your AI-Trader check-in at 10, the Project Development check-in at "
               "12:30 on Google Meet, and your LING essay draft is due by evening. Shall I block an hour after "
               "lunch for the essay?")


PLAN_OFFER = offer_view.OfferInfo(1, "plan", "offer:1", offer_view.PLAN_KICKER, offer_view.PLAN_TITLE, "Plan my day",
                                   (offer_view.PLAN_META,), "Shall I plan your day, sir?")


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_home", "-platform", "offscreen"])
    fonts()


def wait(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class PagingTests(unittest.TestCase):
    def test_the_specs_example_pages(self) -> None:
        self.assertEqual(home.paginate(SPEC_ANSWER), [
            "Three things tomorrow, sir.",
            "Your AI-Trader check-in at 10, the Project Development check-in at 12:30 on Google Meet,",
            "and your LING essay draft is due by evening.",
            "Shall I block an hour after lunch for the essay?"])

    def test_at_most_14_words_and_never_inside_a_time_or_a_name(self) -> None:
        text = ("The meeting with Anna Maria Lopez Garcia at 10:30 AM on Thursday the fifteenth of October about "
                "the quarterly numbers and the hiring plan - bring the slides.")
        pages = home.paginate(text)
        self.assertTrue(all(len(page.split()) <= home.CAPTION_WORDS for page in pages), pages)
        self.assertEqual(" ".join(pages), " ".join(text.split()))           # nothing lost, nothing added
        for page in pages:
            self.assertFalse(page.endswith(("10:30", "Anna", "Maria", "Lopez", " at", " the")), page)
        words = " ".join(f"w{i}" for i in range(31))
        self.assertEqual([len(page.split()) for page in home.paginate(words)], [11, 11, 9])    # even, <= 14

    def test_empty_and_short_texts(self) -> None:
        self.assertEqual(home.paginate("   "), [])
        self.assertEqual(home.paginate("Good evening, sir."), ["Good evening, sir."])

    def test_the_thinking_line_has_no_seconds_and_at_most_8_words(self) -> None:
        self.assertEqual(home.thinking_line("Planning your day... 9 s"), "Planning your day...")
        self.assertEqual(home.thinking_line("Reading your calendar and today's briefing and the mail and more 12 s"),
                         "Reading your calendar and today's briefing and the...")

    def test_quiet_sub_leaves_the_readouts_for_live(self) -> None:
        dot = hud.MIDDLE_DOT
        self.assertEqual(home.quiet_sub(f"2 proposals under NEEDS YOUR OK {dot} 3.1 s"),
                         "2 proposals under NEEDS YOUR OK")
        self.assertEqual(home.quiet_sub(f"1 planner run {dot} 17 of 20 left today"), "")
        self.assertEqual(home.quiet_sub(f"1 planner run (none without recent mail) {dot} 12 Ask runs left today "
                                        f"{dot} as of 1:40 PM"), "as of 1:40 PM")
        self.assertEqual(home.quiet_sub(f"web research {dot} 2 sources {dot} 4.2 KB"), f"web research {dot} 2 sources")
        self.assertEqual(home.quiet_sub(""), "")


class CaptionViewTests(unittest.TestCase):
    def setUp(self) -> None:
        holo.set_reduced_motion(False)
        self.addCleanup(holo.set_reduced_motion, None)
        self.view = home.CaptionView()
        self.view.resize(760, 80)
        self.addCleanup(self.view.deleteLater)
        self.view.show()
        self.states: list[str] = []
        self.view.stateChanged.connect(self.states.append)

    def test_words_appear_one_at_a_time_and_pages_follow(self) -> None:
        view = self.view
        view.set_resting("Good evening, sir.")
        self.assertEqual((view.state(), view.shown_text()), (home.RESTING, "Good evening, sir."))
        with mock.patch.object(home, "SPOKEN_WPS", 40.0):
            view.say(SPEC_ANSWER)
            self.assertEqual(view.state(), home.SPEAKING)
            self.assertEqual(len(view.pages()), 4)
            self.assertEqual((view.page_index(), view.revealed_words()), (0, 1))   # the first word at once
            wait(60)
            self.assertGreater(view.revealed_words(), 1)
            deadline = time.monotonic() + 3
            while view.page_index() < 3 and time.monotonic() < deadline:
                wait(20)
            self.assertEqual(view.page_index(), 3)                  # the last page waits for the voice
            wait(400)
            self.assertEqual(view.state(), home.SPEAKING)
        self.assertEqual(view.revealed_words(), len(view.page_text().split()))

    def test_finish_shows_the_last_page_holds_and_rests(self) -> None:
        view = self.view
        view.set_resting("Good evening, sir.")
        view.say(SPEC_ANSWER)
        view.finish()                                              # the voice stopped early
        self.assertEqual((view.state(), view.page_index()), (home.HOLDING, 3))
        self.assertEqual(view.shown_text(), "Shall I block an hour after lunch for the essay?")
        with mock.patch.object(home, "FADE_OUT_MS", 30):
            view._hold_timer.setInterval(10)
            view._hold_timer.start()
            deadline = time.monotonic() + 2
            while view.state() != home.RESTING and time.monotonic() < deadline:
                wait(20)
        self.assertEqual((view.state(), view.shown_text()), (home.RESTING, "Good evening, sir."))
        self.assertEqual(self.states, [home.SPEAKING, home.HOLDING, home.RESTING])

    def test_a_silent_caption_reads_at_the_muted_pace_and_holds_5_s(self) -> None:
        view = self.view
        view.say("Muted words here.", spoken=False)
        self.assertEqual(view._interval_ms(), round(1000 / home.MUTED_WPS))
        deadline = time.monotonic() + 3
        while view.state() == home.SPEAKING and time.monotonic() < deadline:
            wait(20)
        self.assertEqual(view.state(), home.HOLDING)                # no voice to wait for
        self.assertEqual(view._hold_timer.interval(), home.HOLD_MUTED_MS)

    def test_the_thinking_line_and_highlighted_times(self) -> None:
        view = self.view
        view.set_thinking("Checking your calendar and deadlines... 4 s")
        self.assertEqual((view.state(), view.shown_text()), (home.THINKING, "Checking your calendar and deadlines..."))
        view.say("At 10:30 AM, 1 PM, 4 PM and 6 PM.")
        view.finish()
        self.assertEqual(view.highlighted(), ["10:30", "AM,", "1", "PM,", "4", "PM"])   # at most 3 times, never amber
        view.clear()
        self.assertEqual(view.state(), home.THINKING)              # still thinking under it
        view.set_thinking("")
        self.assertEqual(view.state(), home.RESTING)

    def test_a_known_audio_length_paces_the_words(self) -> None:
        view = self.view
        view.say("One two three four five six seven eight.", duration_ms=4000)     # 8 words, one page
        self.assertEqual(view._interval_ms(), 500)
        view.say("One two.", duration_ms=60_000)
        self.assertEqual(view._interval_ms(), home.WORD_MS_RANGE[1])           # never slower than this
        view.say("One two.", duration_ms=0)
        self.assertEqual(view._interval_ms(), round(1000 / home.SPOKEN_WPS))   # unknown: the estimate
        view.say("One two.", spoken=False, duration_ms=4000)
        self.assertEqual(view._interval_ms(), round(1000 / home.MUTED_WPS))    # muted: its own pace

    def test_a_bare_hour_after_at_is_a_time(self) -> None:
        view = self.view
        view.say("Your AI-Trader check-in at 10, the sync at 12:30 and lunch by 1.")
        view.finish()
        self.assertEqual(view.highlighted(), ["10,", "12:30", "1."])
        view.say("It runs for 3 days at 10 percent.")
        view.finish()
        self.assertEqual(view.highlighted(), [])

    def test_an_offers_question_rests_until_it_is_answered(self) -> None:
        view = self.view
        view.set_resting("Good evening, sir.")
        view.set_question("Shall I plan your day, sir?")
        self.assertEqual((view.state(), view.shown_text()), (home.RESTING, "Shall I plan your day, sir?"))
        view.say("Something else first.")
        view.finish()
        view.clear()
        self.assertEqual(view.shown_text(), "Shall I plan your day, sir?")    # it comes back after
        view.set_question("")
        self.assertEqual(view.shown_text(), "Good evening, sir.")

    def test_reduced_motion_shows_a_page_whole(self) -> None:
        holo.set_reduced_motion(True)
        self.view.say("One two three four.")
        self.assertEqual(self.view.revealed_words(), 4)
        self.assertFalse(self.view._frame.isActive())


class HomeWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        holo.set_reduced_motion(False)
        orb.set_session_locked(False)
        orb.frame_clock().set_deep(False)
        self.addCleanup(holo.set_reduced_motion, None)
        self.addCleanup(orb.frame_clock().set_deep, False)
        self.window = ui.BriefingWindow(10, 30)
        self.addCleanup(self.window.deleteLater)
        self.window.set_clock(lambda: datetime(2026, 10, 12, 21, 51))
        self.window.set_speaker_available(True)
        self.reading = self.window.reading
        self.reading.set_ask_available(True)
        self.reading.set_activity_state(hud.ORB_STANDBY, False)
        self.window.show_reading_view()
        self.window.resize(ui.READING_SIZE)
        self.window.show()
        settle()
        self.addCleanup(self.window.hide)

    def test_home_shows_the_hologram_and_no_old_panels(self) -> None:
        reading = self.reading
        self.assertEqual(reading.current_tab(), ui.SCREEN_HOME)
        for widget in (reading.dock, reading.header, reading.stage, reading.hero, reading.captions,
                       reading.command_bar):
            self.assertTrue(widget.isVisible())
        for widget in (reading.status_panel, reading.activity_panel, reading.approvals_stage, reading.agenda_panel,
                       reading.play_button, reading.skip_button, reading.everything_button, reading.notion_button,
                       reading.transcript_panel, reading.conversation):
            self.assertFalse(widget.isVisible())
        self.assertEqual(reading.state_word(), "STANDBY")
        self.assertEqual(reading.header.clock_texts(), ("9:51 PM", "MON 12 OCT"))
        self.assertIn("Open the day view", reading.header.clock.accessibleName())
        self.assertEqual(reading.hero.orb_size(), orb.HERO)        # 320 at 1120 x 700
        self.assertTrue(reading.hero.testAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent))   # its ground slice

    def test_one_quiet_chip_and_it_opens_its_screen(self) -> None:
        reading = self.reading
        self.assertEqual(reading.home_chip(), "")
        reading.set_pending(2, 3)
        self.assertEqual(reading.home_chip(), "2 need your OK")
        self.assertEqual(reading.approvals_chip.tone(), holo.CHIP_AMBER)
        reading.set_new_briefing(True, "Your AM briefing is ready to view")
        self.assertEqual(reading.home_chip(), ui.HOME_BRIEFING_CHIP)     # NEW first, and cyan
        self.assertEqual(reading.briefing_chip.tone(), holo.CHIP_CYAN)
        reading.briefing_chip.click()
        self.assertEqual(reading.current_tab(), hud.TAB_BRIEFING)
        reading.set_tab(ui.SCREEN_HOME)
        reading.set_new_briefing(False)
        reading.set_pending(1, 3)
        self.assertEqual(reading.home_chip(), "1 needs your OK")
        reading.approvals_chip.click()
        self.assertEqual(reading.current_tab(), ui.SCREEN_APPROVALS)
        self.assertTrue(reading.approvals_stage.isVisible())
        reading.set_tab(ui.SCREEN_HOME)
        reading.say_caption("Something to say.")
        self.assertEqual(reading.home_chip(), "")                   # only at rest

    def test_the_dock_signals(self) -> None:
        reading = self.reading
        reading.set_pending(3, 3)
        self.assertEqual(reading.dock.accessible_name(dock.APPROVALS), "Approvals, 3 waiting")
        reading.set_live_activity(hud.ACTIVITY_WORKING)
        self.assertEqual(reading.dock.live_activity(), dock.LIVE_WORKING)
        reading.set_live_state(hud.LIVE_COUNTDOWN, "SENDING", deadline=time.monotonic() + 5)
        self.assertEqual(reading.dock.live_activity(), dock.LIVE_COUNTDOWN)
        reading.set_live_activity(hud.ACTIVITY_UNREAD)
        self.assertEqual(reading.dock.live_activity(), dock.LIVE_IDLE)    # the Dock has no unread dot
        reading.set_memory_available(True)
        reading.mark_memory_unread()                                         # a note was saved
        self.assertEqual(reading.dock.accessible_name(dock.MEMORY), "Memory, new")
        self.assertTrue(reading.dock.button(dock.MEMORY).has_dot())
        reading.set_memory_suggestions(2)
        self.assertEqual(reading.dock.accessible_name(dock.MEMORY), "Memory, 2 suggestions")
        reading.set_tab(hud.TAB_MEMORY)
        self.assertFalse(reading.memory_unread())
        self.assertEqual(reading.dock.accessible_name(dock.MEMORY), "Memory, 2 suggestions")   # still pending
        reading.set_memory_suggestions(0)
        self.assertEqual(reading.dock.accessible_name(dock.MEMORY), "Memory")
        self.assertFalse(reading.dock.button(dock.MEMORY).has_dot())
        reading.set_followups_available(True)
        reading.set_followups_waiting(2)
        self.assertEqual(reading.dock.accessible_name(dock.FOLLOWUPS), "Follow-ups, 2 waiting on you")

    def test_the_hero_orb_follows_the_app(self) -> None:
        reading = self.reading
        cases = [
            (lambda: reading.set_activity_state(hud.ORB_WORKING, False, ui.ASK_PLANNING_LABEL), orb.THINKING,
             "THINKING", home.TONE_HOLO),
            (lambda: reading.set_activity_state(hud.ORB_SPEAKING, False), orb.SPEAKING, "SPEAKING", home.TONE_HOLO),
            (lambda: reading.set_activity_state(hud.ORB_STANDBY, False), orb.IDLE, "STANDBY", home.TONE_HOLO),
            (lambda: reading.set_pending(2, 2), orb.NEEDS, "NEEDS YOU", home.TONE_AMBER),
            (lambda: reading.set_pending(0, 2), orb.IDLE, "STANDBY", home.TONE_HOLO),
            (lambda: reading.set_offers([PLAN_OFFER]), orb.NEEDS, "NEEDS YOU", home.TONE_AMBER),
            (lambda: reading.set_offers([]), orb.IDLE, "STANDBY", home.TONE_HOLO),
            (lambda: reading.set_waiting(True), orb.WAITING, "WAITING FOR THE AM BRIEFING", home.TONE_HOLO),
            (lambda: reading.set_waiting(False), orb.IDLE, "STANDBY", home.TONE_HOLO),
            (lambda: reading.set_briefing_paused(True), orb.IDLE, ui.PAUSED_WORD, home.TONE_STEEL),
            (lambda: reading.set_briefing_paused(False), orb.IDLE, "STANDBY", home.TONE_HOLO),
            (lambda: reading.set_offline_voice(True), orb.IDLE, "OFFLINE VOICE", home.TONE_STEEL),
            (lambda: reading.set_muted_look(True), orb.IDLE, "MUTED", home.TONE_STEEL),
        ]
        for act, base, word, tone in cases:
            with self.subTest(word=word, base=base):
                act()
                self.assertEqual(reading.hero.state(), base)
                self.assertEqual(reading.dock.mini_orb().state(), base)
                self.assertEqual((reading.state_word(), reading.header.readout.tone()), (word, tone))
        self.assertEqual(reading.hero.look(), (orb.IDLE, True, True))
        self.assertTrue(reading.header.muted())

    def test_typing_in_the_bar_is_listening(self) -> None:
        reading = self.reading
        self.window.activateWindow()
        reading.command_bar.focus_input()
        settle()
        QTest.keyClicks(reading.command_bar.input, "what's on")
        settle()
        self.assertEqual(reading.hero.state(), orb.LISTENING)
        self.assertEqual(reading.state_word(), "LISTENING")
        self.assertTrue(reading.command_bar.field.is_listening())
        self.assertEqual(reading.command_bar.send_button.look(), "ready")
        reading.set_pending(1, 1)
        self.assertEqual(reading.hero.state(), orb.LISTENING)        # listening outranks needs you
        reading.command_bar.set_running(True)                         # the request runs: thinking, a stop glyph
        reading.set_activity_state(hud.ORB_WORKING, False, ui.ASK_PLANNING_LABEL)
        self.assertEqual(reading.hero.state(), orb.THINKING)
        self.assertEqual(reading.command_bar.send_button.look(), "stop")
        reading.command_bar.set_running(False)
        reading.command_bar.clear_input()
        reading.set_activity_state(hud.ORB_STANDBY, False)
        settle()
        self.assertEqual(reading.hero.state(), orb.NEEDS)

    def test_the_orb_moves_only_on_home_and_stops_when_minimized(self) -> None:
        reading = self.reading
        wait(50)
        self.assertTrue(reading.hero.is_animating())
        reading.set_tab(hud.TAB_LIVE)
        settle()
        self.assertFalse(reading.hero.is_animating())               # not on the current screen
        reading.set_tab(ui.SCREEN_HOME)
        settle()
        self.assertTrue(reading.hero.is_animating())
        self.window.showMinimized()
        wait(80)
        self.assertFalse(reading.hero.is_animating())
        self.window.showNormal()
        wait(80)
        self.assertTrue(reading.hero.is_animating())
        orb.set_session_locked(True)
        self.assertFalse(reading.hero.is_animating())
        orb.set_session_locked(False)

    def test_deep_idle_after_two_quiet_minutes(self) -> None:
        reading = self.reading
        reading.hero.settle()
        self.assertEqual(reading.hero.frame_interval(), orb.FRAME_IDLE_MS)
        orb.frame_clock()._activity_at = time.monotonic() - orb.DEEP_IDLE_S - 1
        wait(250)
        reading.hero.settle()
        self.assertTrue(orb.frame_clock().deep())
        self.assertEqual(reading.hero.frame_interval(), orb.FRAME_DEEP_MS)
        reading.set_activity_state(hud.ORB_SPEAKING, False)          # Jarvis speaks: awake at once
        self.assertFalse(orb.frame_clock().deep())
        self.assertEqual(reading.hero.frame_interval(), orb.FRAME_ACTIVE_MS)

    def test_the_transcript_drawer(self) -> None:
        reading = self.reading
        reading.conversation.add(hud.ROLE_JARVIS, "Good evening, sir.", when=datetime(2026, 10, 12, 21, 50),
                                 sub=f"1 proposal under NEEDS YOUR OK {hud.MIDDLE_DOT} 6.9 s")
        reading.mark_unread()
        self.assertTrue(reading.unread())
        self.window.activateWindow()
        reading.stage.transcript_button.setFocus(Qt.FocusReason.TabFocusReason)
        reading.stage.transcript_button.click()
        settle()
        wait(holo.DRAWER_IN_MS + 80)
        drawer = reading.transcript_drawer
        self.assertTrue(drawer.is_open() and reading.conversation.isVisible())
        self.assertFalse(reading.unread())
        entry = reading.conversation.entry_widgets()[0]
        self.assertEqual(entry.shown_sub(), "1 proposal under NEEDS YOUR OK")          # no seconds on Home
        self.assertEqual(entry.entry[3], f"1 proposal under NEEDS YOUR OK {hud.MIDDLE_DOT} 6.9 s")
        self.assertLessEqual(drawer.width(), home.DRAWER_WIDTH)
        self.assertEqual(drawer.geometry().right(), drawer.parentWidget().width() - 1)
        QTest.keyClick(drawer.close_button, Qt.Key.Key_Escape)
        wait(holo.DRAWER_OUT_MS + 80)
        self.assertFalse(drawer.isVisible())
        self.assertTrue(reading.stage.transcript_button.hasFocus())   # the focus goes back to the opener

    def test_the_clock_opens_the_day_view(self) -> None:
        reading = self.reading
        reading.set_tab(hud.TAB_BRIEFING)
        reading.header.clock.click()
        wait(holo.DRAWER_IN_MS + 80)
        self.assertTrue(reading.day_drawer.is_open())
        self.assertTrue(reading.agenda_panel.isVisible())
        self.assertEqual(reading.agenda_panel.mode(), hud.AGENDA_DAY)
        self.assertFalse(reading.agenda_panel.followups_section().isVisible())
        reading.set_tab(ui.SCREEN_HOME)                               # another screen closes it
        wait(holo.DRAWER_OUT_MS + 80)
        self.assertFalse(reading.day_drawer.isVisible())

    def test_the_header_buttons(self) -> None:
        reading = self.reading
        toggled: list[bool] = []
        self.window.header.muteToggled.connect(toggled.append)
        self.assertTrue(reading.header.mute_visible())
        reading.header.mute_button.click()
        self.assertEqual(toggled, [True])
        self.assertTrue(reading.header.muted())
        self.assertEqual(reading.state_word(), "MUTED")
        closed: list[int] = []
        self.window.closeRequested.connect(lambda: closed.append(1))
        reading.header.close_button.click()
        self.assertEqual(closed, [1])
        self.assertEqual(reading.header.close_button.accessibleName(), home.CLOSE_LABEL)
        reading.header.minimize_button.click()
        wait(50)
        self.assertTrue(self.window.isMinimized())
        self.window.showNormal()

    def test_settings_says_where_the_settings_live(self) -> None:
        reading = self.reading
        seen: list[str] = []
        reading.openConfig.connect(lambda: seen.append("config"))
        reading.openDataFolder.connect(lambda: seen.append("data"))
        reading.dock.button(dock.SETTINGS).click()
        settle()
        self.assertEqual(reading.current_tab(), ui.SCREEN_SETTINGS)
        reading.settings_page.config_button.click()
        reading.settings_page.data_button.click()
        self.assertEqual(seen, ["config", "data"])

    def test_services_that_are_not_ok_show_in_the_header(self) -> None:
        window, header = self.window, self.reading.header
        window.set_service("notion", hud.STATUS_OK, "Notion: connected")
        window.set_service("claude", hud.STATUS_OFF, "Ask: off")
        self.assertEqual(header.service_notes(), [])
        window.set_service("notion", hud.STATUS_WARN, "Notion: retrying - timed out")
        window.set_service("calendar", hud.STATUS_ERROR, "Google Calendar: sign in again")
        settle()
        self.assertEqual(header.service_notes(), [("notion", "warn", "Notion: retrying - timed out"),
                                                  ("calendar", "error", "Google Calendar: sign in again")])
        note = header._services["notion"]
        self.assertTrue(note.isVisible())
        self.assertEqual(note.accessibleName(), "Notion: retrying - timed out")
        self.assertEqual(window.header.service_chip("notion").status(), hud.STATUS_WARN)   # the old chip too
        window.set_service("notion", hud.STATUS_OK, "Notion: connected")
        self.assertEqual([name for name, _s, _t in header.service_notes()], ["calendar"])
        note = header._services["calendar"]
        for width in (ui.READING_MIN_SIZE.width(), ui.READING_SIZE.width()):
            window.resize(width, window.height())
            settle()
            self.assertLessEqual(header.clock.geometry().right(), header.width())        # nothing pushed out
            self.assertGreaterEqual(header.clock.geometry().left(), note.geometry().right())
        # The smallest window with a countdown too: the note keeps its dot (and its name for screen readers).
        window.resize(ui.READING_MIN_SIZE)
        self.reading.set_countdown("SENDING", time.monotonic() + 9.5)
        settle()
        self.assertTrue(note.is_compact() and note.isVisible())
        self.assertLessEqual(header.layout().minimumSize().width(), header.width())
        self.assertLessEqual(header.undo_button.geometry().right(), header.clock.geometry().left())
        self.assertEqual(note.accessibleName(), "Google Calendar: sign in again")
        self.reading.set_countdown()
        settle()
        self.assertFalse(note.is_compact())

    def test_an_open_offer_is_one_amber_tile_with_its_question(self) -> None:
        """Round 2, part 3a (spec 16.4 / 16.5 moment 1): the offer is ONE amber tile bottom-right of the orb
        (no chip beside it), its question rests under the orb, its commit only hands the offer's own link to
        the app and Not now puts it away on Home only."""
        reading = self.reading
        reading.set_greeting("Good evening, sir.")
        reading.set_new_briefing(True, "Your AM briefing is ready to view")
        reading.set_pending(2, 3)
        entry = reading.conversation.add(hud.ROLE_JARVIS, "Shall I plan your day, sir?", link_text="Plan my day",
                                         link_id="offer:1")
        links: list[str] = []
        put_away: list[int] = []
        reading.homeLink.connect(links.append)
        reading.offerNotNow.connect(put_away.append)
        reading.set_offers([dataclasses.replace(PLAN_OFFER, entry=entry)])
        settle()
        tile = reading.offer_tile
        self.assertEqual(reading.state_word(), "NEEDS YOU")
        self.assertTrue(tile.isVisible())
        self.assertIs(reading.stage.side_tile(), tile)
        self.assertEqual(reading.home_chip(), "")                               # one focal point: no chip
        kicker, title, meta = tile.texts()
        self.assertEqual((kicker, title), ("PLAN MY DAY", ["Blocks around your meetings"]))
        self.assertEqual(" ".join(meta), "Nothing goes on your calendar until you approve each block.")
        self.assertEqual((tile.commit_button.text(), tile.not_now_button.text()), ("Plan my day", "Not now"))
        self.assertEqual((tile.commit_button.variant(), tile.not_now_button.variant()),
                         (holo.AMBER_COMMIT, holo.AMBER_GHOST))
        self.assertEqual(reading.captions.shown_text(), "Shall I plan your day, sir?")
        hero = reading.hero.geometry()
        self.assertGreaterEqual(tile.x(), hero.center().x() + reading.hero.orb_size() / 2)   # right of the orb
        self.assertGreaterEqual(tile.y() + 12, hero.center().y())                          # its lower half
        self.assertLessEqual(tile.geometry().right(), reading.stage.width())
        tile.commit_button.click()
        settle()
        self.assertEqual(links, ["offer:1"])                                    # the offer's own link, once
        self.assertFalse(reading.transcript_drawer.is_open())
        self.assertEqual(reading.current_tab(), ui.SCREEN_HOME)
        tile.not_now_button.click()
        settle()
        self.assertEqual(put_away, [1])
        self.assertFalse(tile.isVisible())
        self.assertEqual(links, ["offer:1"])                                    # Not now runs nothing
        self.assertEqual(reading.captions.shown_text(), "Good evening, sir.")
        self.assertEqual(reading.home_chip(), ui.HOME_BRIEFING_CHIP)
        self.assertEqual(reading.conversation.entry(entry).link(), ("Plan my day", "offer:1"))   # kept there
        reading.set_offers([dataclasses.replace(PLAN_OFFER, entry=entry)])    # a later tick: still put away
        self.assertFalse(tile.isVisible())
        reading.set_pending(0, 3)
        self.assertEqual(reading.state_word(), "STANDBY")
        reading.set_offers([])
        reading.set_answer_chip(ui.HOME_BRIEF_CHIP, entry)
        self.assertEqual(reading.home_chip(), ui.HOME_BRIEF_CHIP)
        self.assertEqual(reading.answer_chip.tone(), holo.CHIP_CYAN)
        reading.show_request("prep my next meeting")                            # the next request: it goes
        self.assertNotEqual(reading.home_chip(), ui.HOME_BRIEF_CHIP)

    def test_a_cap_message_shows_without_its_readouts(self) -> None:
        bar = self.reading.command_bar
        message = ("Ask's daily limit reached; try again after 9:00 AM "
                   "(20 planner runs a day: [ask] max_per_day)")
        bar.set_status(message, hud.TONE_WARN)
        settle()
        self.assertTrue(bar.status_label.isVisible())
        self.assertEqual(bar.status_label.full_text(), "Ask's daily limit reached; try again after 9:00 AM")
        self.assertIn("[ask] max_per_day", bar.status_label.toolTip())             # the whole text stays
        bar.set_status("Prep: brief ready (1 thread read)", hud.TONE_DONE)
        self.assertEqual(bar.status_label.full_text(), "Prep: brief ready (1 thread read)")

    def test_no_technical_readouts_on_home(self) -> None:
        reading = self.reading
        bar = reading.command_bar
        bar.set_meta(f"uses your Claude plan {hud.MIDDLE_DOT} 4.2 s {hud.MIDDLE_DOT} 19 runs left this hour")
        bar.set_status("Planning... 7 s", hud.TONE_WORKING)
        settle()
        self.assertFalse(bar.meta_label.isVisible())
        self.assertFalse(bar.status_label.isVisible())
        self.assertEqual(reading.captions.thinking(), "Planning...")
        bar.set_status("Two blocks are waiting for your OK.", hud.TONE_DONE)
        settle()
        self.assertTrue(bar.status_label.isVisible())                # the answer, one line
        self.assertEqual(reading.captions.thinking(), "")


class ControllerHomeTests(unittest.TestCase):
    """The app's own states reach Home (AppController over the test fakes)."""

    def setUp(self) -> None:
        holo.set_reduced_motion(False)
        self.addCleanup(holo.set_reduced_motion, None)

    def test_needs_you_while_cards_wait_and_the_chip_opens_them(self) -> None:
        app = AppHarness(self, ask=False)
        app.reading(autoplay=False)
        reading = app.c.window.reading
        self.assertEqual(reading.current_tab(), ui.SCREEN_HOME)
        app.c.player.state = "idle"
        app.c._update_reading_controls()
        self.assertGreater(len(reading.approvals.cards()), 0)
        self.assertEqual(reading.hero.state(), orb.NEEDS)
        self.assertEqual(reading.state_word(), "NEEDS YOU")
        self.assertIn(reading.home_chip(), (ui.HOME_BRIEFING_CHIP, ui.needs_ok_chip(len(reading.approvals.cards()))))
        self.assertRegex(reading.captions.resting(), r"^(Good (morning|afternoon|evening)|Hello), sir\.$")

    def test_a_muted_voice_shows_its_words_as_a_caption(self) -> None:
        app = AppHarness(self, ask=False)
        app.reading(autoplay=False)
        reading = app.c.window.reading
        app.voice.set_muted(True)
        app.c._sync_speaker()
        self.assertEqual(reading.state_word(), "MUTED")
        self.assertFalse(app.c._say("notice", "Your meeting starts in 15 minutes, sir."))
        self.assertEqual(reading.captions.state(), home.SPEAKING)
        self.assertEqual(reading.captions.pages(), ["Your meeting starts in 15 minutes, sir."])
        self.assertFalse(reading.captions._spoken)

    def test_speech_is_a_caption_and_its_end_holds(self) -> None:
        app = AppHarness(self, ask=False)
        app.reading(autoplay=False)
        reading = app.c.window.reading
        app.c._on_voice_speaking("Good morning, sir. Your AM briefing is ready.")
        self.assertEqual(reading.hero.state(), orb.SPEAKING)
        self.assertEqual(reading.captions.pages(), ["Good morning, sir.", "Your AM briefing is ready."])
        app.c._on_voice_speaking("")
        self.assertEqual(reading.captions.state(), home.HOLDING)
        self.assertNotEqual(reading.hero.state(), orb.SPEAKING)

    def test_an_approved_card_keeps_the_owner_on_approvals(self) -> None:
        # [live] auto_open = "tab" brings LIVE up for an Ask, but a card the owner approves on the Approvals
        # screen keeps him there (its countdown, Undo and the next card); the Dock's Live slot gets its dot.
        app = AppHarness(self, ask=False)
        app.reading(autoplay=False)
        c, reading = app.c, app.c.window.reading
        c.isActiveWindow = lambda: True
        reading.set_tab(ui.SCREEN_APPROVALS)
        settle()
        card = next(card for card in reading.approvals.cards() if card.actionable)
        card.approve_button.click()
        settle()
        self.assertIsNotNone(c._countdown)
        self.assertTrue(card.undo_button.isVisible())
        c._countdown.deadline = time.monotonic() - 0.001
        c._on_countdown_tick()
        settle()
        self.assertEqual(reading.current_tab(), ui.SCREEN_APPROVALS)

    def test_a_countdown_left_on_approvals_keeps_its_undo_in_the_header(self) -> None:
        app = AppHarness(self, ask=False)
        app.reading(autoplay=False)
        c, reading = app.c, app.c.window.reading
        c.isActiveWindow = lambda: True
        reading.set_tab(ui.SCREEN_APPROVALS)
        settle()
        card = next(card for card in reading.approvals.cards() if card.actionable)
        card.approve_button.click()
        settle()
        self.assertEqual(reading.header.countdown_text(), "")         # the card's own strip has it
        reading.set_tab(ui.SCREEN_HOME)                               # e.g. to ask something meanwhile
        settle()
        self.assertIsNotNone(c._countdown)
        self.assertRegex(reading.header.countdown_text(), r"^(SENDING|ADDING) IN \d+ S$")
        self.assertTrue(reading.header.undo_button.isVisible())
        c._countdown.started -= 5
        reading.header.undo_button.click()
        settle()
        self.assertIsNone(c._countdown)
        self.assertEqual(reading.header.countdown_text(), "")
        self.assertTrue(card.actionable)                              # it waits for a decision again

    def test_settings_open_the_files_on_this_pc(self) -> None:
        app = AppHarness(self, ask=False)
        app.reading(autoplay=False)
        reading = app.c.window.reading
        self.assertTrue(reading.settings_page.config_path.text().endswith("config.toml"))
        with mock.patch.object(ui.QDesktopServices, "openUrl") as open_url:
            reading.settings_page.config_button.click()
            reading.settings_page.data_button.click()
        urls = [call.args[0] for call in open_url.call_args_list]
        self.assertEqual(len(urls), 2)
        self.assertTrue(all(isinstance(url, QUrl) and url.isLocalFile() for url in urls))
        self.assertTrue(urls[0].toLocalFile().endswith("config.toml"))
        self.assertEqual(urls[1].toLocalFile().rstrip("/").lower(),
                         str(app.config.data_dir).replace("\\", "/").rstrip("/").lower())

    def test_the_lock_probe_and_the_input_watcher_are_wired(self) -> None:
        app = AppHarness(self, ask=False)
        self.assertIs(orb.frame_clock()._probe, app.c._locked)
        app.reading(autoplay=False)                                   # shown: its QWindow is watched
        handle = app.c.window.windowHandle()
        self.assertTrue(orb.watching(handle))
        orb.frame_clock().set_deep(True)
        key = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Shift, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(handle, key)
        self.assertFalse(orb.frame_clock().deep())                    # a key ends deep idle
        orb.frame_clock().set_deep(True)
        app.c.window.activateWindow()
        QApplication.sendEvent(app.c.window, QEvent(QEvent.Type.ActivationChange))
        self.assertEqual(orb.frame_clock().deep(), not app.c.window.isActiveWindow())


if __name__ == "__main__":
    unittest.main()
