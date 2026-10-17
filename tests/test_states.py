"""Tests for the hologram's states (round 2, part 3b; HOLOGRAM_SPEC 16.7): waiting for the briefing, the toast,
the offline voice, muted, quiet hours and the error tiles - their words, their looks and that every action only
opens a screen the app already has (the toast and the tiles never act by themselves; a refused mail's Retry never
sends: only the card's own button starts its countdown).

Qt runs offscreen in this process with the bundled fonts and shows nothing; every text is invented.
"""

from __future__ import annotations

import re
import types
import unittest
from datetime import datetime, time
from unittest import mock

from PySide6.QtCore import QEvent, QEventLoop, QPointF, Qt, QTimer
from PySide6.QtGui import QEnterEvent, QGuiApplication
from PySide6.QtWidgets import QAbstractButton, QApplication, QLineEdit

from briefing_reader import holo, home, hud, offer_view, orb, states_view, ui
from briefing_reader.ask import offers
from tests.ui_fakes import PDT, fonts, settle

_app: QApplication | None = None
PLAN_OFFER = offer_view.OfferInfo(1, "plan", "offer:1", offer_view.PLAN_KICKER, offer_view.PLAN_TITLE, "Plan my day",
                                   (offer_view.PLAN_META,), "Shall I plan your day, sir?")
VENDORS = re.compile(r"edge|microsoft|azure|sapi|google|notion", re.IGNORECASE)


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_states", "-platform", "offscreen"])
    fonts()


def wait(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class WordsTests(unittest.TestCase):
    def test_the_waiting_words(self) -> None:
        self.assertEqual(states_view.waiting_line("AM"), "Checking for your AM briefing, sir.")
        self.assertEqual(states_view.waiting_line("PM", "boss"), "Checking for your PM briefing, boss.")
        self.assertEqual(states_view.waiting_line("new", ""), "Checking for your briefing.")
        self.assertEqual(states_view.waiting_word("PM"), "WAITING FOR THE PM BRIEFING")
        self.assertEqual(states_view.waiting_word(""), "WAITING FOR THE AM BRIEFING")   # the orb's own word
        self.assertEqual(states_view.waiting_readout("every minute", "10:27 AM"),
                         "EVERY MINUTE \u00b7 UNTIL 10:27 AM")
        self.assertEqual(states_view.waiting_readout("", ""), "")

    def test_the_offline_and_quiet_words_name_no_vendor(self) -> None:
        line = states_view.offline_line()
        self.assertEqual(line, "My usual voice is out of reach, sir. I'll keep trying.")
        self.assertIsNone(VENDORS.search(line))
        self.assertEqual(states_view.offline_badge(), "WINDOWS VOICE")
        self.assertEqual(states_view.offline_badge("10:14"), "WINDOWS VOICE \u00b7 RETRY 10:14")
        self.assertEqual(states_view.quiet_word("8:00 AM"), "QUIET HOURS UNTIL 8:00 AM")
        self.assertEqual(states_view.clock_text(time(8, 0)), "8:00 AM")
        self.assertEqual(states_view.clock_text(time(8, 0), hour24=True), "08:00")
        self.assertEqual(states_view.clock_text(time(22, 30)), "10:30 PM")

    def test_toast_lines_hold_at_most_8_words_and_sir_closes_them(self) -> None:
        self.assertEqual(states_view.toast_briefing_line("AM"), "Your AM briefing is ready, sir.")
        self.assertEqual(states_view.toast_briefing_line("PM", ""), "Your PM briefing is ready.")
        self.assertEqual(states_view.toast_prep_line("Project Development check-in", 15),
                         "Project Development check-in in 15 minutes, sir.")
        self.assertEqual(states_view.toast_prep_line("Sync", 1), "Sync in 1 minute, sir.")
        long = states_view.toast_prep_line("Quarterly planning review with the whole design team", 15)
        self.assertEqual(long, "Quarterly planning review with\u2026 in 15 minutes, sir.")
        for line in (long, states_view.toast_briefing_line("AM"), states_view.toast_prep_line("", 5, "boss")):
            self.assertLessEqual(len(line.split()), states_view.TOAST_WORDS, line)
            self.assertFalse(line.lower().startswith(("sir", "boss")), line)
        self.assertTrue(states_view.toast_prep_line("", 5, "boss").endswith(", boss."))


class ToastTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toast = states_view.Toast()
        self.addCleanup(self.toast.deleteLater)
        self.addCleanup(self.toast.dismiss)

    def test_it_is_jarvis_own_window_and_never_takes_the_focus(self) -> None:
        toast = self.toast
        flags = toast.windowFlags()
        for flag in (Qt.WindowType.Tool, Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint,
                     Qt.WindowType.WindowDoesNotAcceptFocus):
            self.assertTrue(flags & flag, flag)
        self.assertTrue(toast.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertEqual(toast.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertEqual(toast.action_button.focusPolicy(), Qt.FocusPolicy.NoFocus)
        other = QLineEdit()
        self.addCleanup(other.deleteLater)
        other.show()
        other.activateWindow()
        other.setFocus()
        settle()
        toast.post("Your AM briefing is ready, sir.", "View", "view")
        toast.post("Your AM briefing is ready, sir.", "View", "view")       # a second show: the same rules
        settle()
        self.assertTrue(toast.isVisible())
        handle = toast.windowHandle()
        self.assertTrue(handle.flags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertIs(handle.property("_q_showWithoutActivating"), True)
        # (The offscreen platform activates every window it shows; Windows honours the flags above.) Nothing in
        # the toast ever holds the keyboard focus.
        focus = QApplication.focusWidget()
        self.assertFalse(focus is toast or (focus is not None and toast.isAncestorOf(focus)))
        toast.action_button.click()
        focus = QApplication.focusWidget()
        self.assertFalse(focus is toast or (focus is not None and toast.isAncestorOf(focus)))
        other.hide()

    def test_one_line_one_action_bottom_right_of_the_work_area(self) -> None:
        toast = self.toast
        toast.post("Project Development check-in in 15 minutes, sir.", "Prepare brief", "prep",
                   look=(orb.SPEAKING, False, False))
        settle()
        self.assertEqual(toast.size(), states_view.TOAST_SIZE)
        self.assertEqual(toast.line(), "Project Development check-in in 15 minutes, sir.")
        self.assertEqual([button.text() for button in toast.findChildren(QAbstractButton)], ["Prepare brief"])
        self.assertEqual(toast.mini.state(), orb.SPEAKING)
        area = QGuiApplication.primaryScreen().availableGeometry()
        frame = toast.geometry()
        self.assertEqual(area.right() - frame.right(), states_view.TOAST_MARGIN)
        self.assertEqual(area.bottom() - frame.bottom(), states_view.TOAST_MARGIN)

    def test_it_hides_after_8_seconds_and_hovering_pauses(self) -> None:
        toast = self.toast
        self.assertEqual(states_view.TOAST_MS, 8000)
        toast.post("Your PM briefing is ready, sir.", "View", "view")
        self.assertGreater(toast.remaining_ms(), 7000)
        with mock.patch.object(states_view, "TOAST_MS", 120):
            toast.post("Your PM briefing is ready, sir.", "View", "view")
        wait(300)
        self.assertFalse(toast.isVisible())                                 # gone on its own
        with mock.patch.object(states_view, "TOAST_MS", 150):
            toast.post("Your PM briefing is ready, sir.", "View", "view")
        QApplication.sendEvent(toast, QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
        self.assertTrue(toast.is_paused())
        wait(350)
        self.assertTrue(toast.isVisible())                                  # paused while the pointer is over it
        QApplication.sendEvent(toast, QEvent(QEvent.Type.Leave))
        self.assertFalse(toast.is_paused())
        wait(400)
        self.assertFalse(toast.isVisible())                                 # the rest of the time ran out

    def test_its_action_only_hands_its_key_back(self) -> None:
        toast = self.toast
        seen: list[str] = []
        toast.actionClicked.connect(seen.append)
        toast.post("Your AM briefing is ready, sir.", "View", "view")
        settle()
        toast.action_button.click()
        self.assertEqual(seen, ["view"])
        self.assertFalse(toast.isVisible())


class ReadingStatesTests(unittest.TestCase):
    def setUp(self) -> None:
        holo.set_reduced_motion(False)
        orb.set_session_locked(False)
        self.addCleanup(holo.set_reduced_motion, None)
        self.window = ui.BriefingWindow(10, 30)
        self.addCleanup(self.window.deleteLater)
        self.window.set_clock(lambda: datetime(2026, 10, 12, 21, 51))
        self.window.set_speaker_available(True)
        self.reading = self.window.reading
        self.reading.set_ask_available(True)
        self.reading.set_activity_state(hud.ORB_STANDBY, False)
        self.reading.set_greeting("Good evening, sir.")
        self.window.show_reading_view()
        self.window.resize(ui.READING_SIZE)
        self.window.show()
        settle()
        self.addCleanup(self.window.hide)

    def word(self) -> tuple[str, str]:
        return self.reading.state_word(), self.reading.header.readout.tone()

    def test_waiting_for_the_briefing(self) -> None:
        reading = self.reading
        reading.set_waiting(True, "PM", "EVERY MINUTE \u00b7 UNTIL 10:27 AM")
        settle()
        self.assertEqual(reading.hero.state(), orb.WAITING)
        self.assertEqual(self.word(), ("WAITING FOR THE PM BRIEFING", home.TONE_HOLO))
        self.assertEqual(reading.captions.resting(), "Checking for your PM briefing, sir.")
        self.assertTrue(reading.waiting_readout.isVisible())
        self.assertEqual(reading.waiting_readout.text(), "EVERY MINUTE \u00b7 UNTIL 10:27 AM")
        reading.set_waiting(False)
        settle()
        self.assertEqual(reading.hero.state(), orb.IDLE)
        self.assertEqual(reading.captions.resting(), "Good evening, sir.")
        self.assertFalse(reading.waiting_readout.isVisible())

    def test_offline_voice(self) -> None:
        reading, window = self.reading, self.window
        window.set_service("voice", hud.STATUS_WARN, "Voice: offline (Windows voice)")
        reading.set_offline_voice(True)
        settle()
        self.assertEqual(self.word(), ("OFFLINE VOICE", home.TONE_STEEL))
        self.assertTrue(reading.hero.is_offline())
        self.assertEqual(reading.captions.resting(), "My usual voice is out of reach, sir. I'll keep trying.")
        self.assertTrue(reading.voice_badge.isVisible())
        self.assertEqual(reading.voice_badge.text(), "WINDOWS VOICE")
        self.assertEqual(reading.voice_badge.tone(), holo.TONE_STEEL)
        self.assertEqual(reading.header.service_notes(), [])                # the readout says it, not a note
        reading.set_offline_voice(True, "10:14")
        self.assertEqual(reading.voice_badge.text(), "WINDOWS VOICE \u00b7 RETRY 10:14")
        reading.set_offline_voice(False)
        settle()
        self.assertEqual(self.word(), ("STANDBY", home.TONE_HOLO))
        self.assertFalse(reading.voice_badge.isVisible())
        self.assertEqual(reading.captions.resting(), "Good evening, sir.")

    def test_muted(self) -> None:
        reading, window = self.reading, self.window
        window.set_muted(True)
        settle()
        self.assertEqual(self.word(), ("MUTED", home.TONE_STEEL))
        self.assertTrue(reading.hero.is_muted())
        self.assertTrue(reading.header.muted())                             # the mute button is pressed
        reading.say_caption("Your check-in starts at ten, sir.", spoken=False)
        self.assertEqual(reading.captions.state(), home.SPEAKING)            # text only, at the reading pace
        reading.set_offers([PLAN_OFFER])                                     # an offer still shows its tile
        settle()
        self.assertTrue(reading.offer_tile.isVisible())
        facts = offers.QuietFacts(speak_ok=False)
        reason = offers.may_speak(offers.KIND_PLAN, facts, now=datetime(2026, 10, 12, 9, 0), settings=None,
                                  meeting=False, last_spoken=None)
        self.assertEqual(offers.speech_disposition(offers.KIND_PLAN, reason), offers.TEXT_ONLY)
        window.set_muted(False)
        self.assertFalse(reading.header.muted())

    def test_quiet_hours(self) -> None:
        reading = self.reading
        until = ["8:00 AM"]
        reading.set_quiet_probe(lambda: until[0])
        settle()
        self.assertEqual(self.word(), ("QUIET HOURS UNTIL 8:00 AM", home.TONE_STEEL))
        self.assertEqual(reading.hero.state(), orb.IDLE)
        self.assertEqual(reading.hero.dim(), states_view.QUIET_DIM)
        reading.set_activity_state(hud.ORB_SPEAKING, False)                  # Jarvis answers: the orb at full
        self.assertEqual(self.word(), ("SPEAKING", home.TONE_HOLO))
        self.assertEqual(reading.hero.dim(), 1.0)
        reading.set_activity_state(hud.ORB_STANDBY, False)
        reading.set_offers([PLAN_OFFER])                                     # an offer is its tile, said or not
        settle()
        self.assertTrue(reading.offer_tile.isVisible())
        self.assertEqual(self.word(), ("NEEDS YOU", home.TONE_AMBER))
        reading.set_offers([])
        reason = offers.may_speak(offers.KIND_PLAN, offers.QuietFacts(), now=datetime(2026, 10, 12, 23, 0),
                                  settings=types.SimpleNamespace(quiet_from="22:00", quiet_until="08:00"),
                                  meeting=False, last_spoken=None)
        self.assertEqual(offers.speech_disposition(offers.KIND_PLAN, reason), offers.TEXT_ONLY)
        until[0] = ""
        reading.refresh_quiet()
        self.assertEqual(self.word(), ("STANDBY", home.TONE_HOLO))
        self.assertEqual(reading.hero.dim(), 1.0)
        reading.set_quiet_probe(None)
        self.assertEqual(reading.quiet_until(), "")

    def test_the_quiet_hours_probe_follows_the_bundles_rule(self) -> None:
        def controller(at: datetime, *, bundle: bool = True, hour24: bool = False) -> types.SimpleNamespace:
            config = types.SimpleNamespace(productivity=types.SimpleNamespace(quiet_from="22:00", quiet_until="08:00"),
                                           display=types.SimpleNamespace(hour24=hour24))
            return types.SimpleNamespace(productivity=object() if bundle else None, config=config,
                                         _offer_wall=lambda _moment: at)

        probe = ui.AppController._quiet_until_text
        self.assertEqual(probe(controller(datetime(2026, 10, 12, 23, 30))), "8:00 AM")
        self.assertEqual(probe(controller(datetime(2026, 10, 13, 6, 0), hour24=True)), "08:00")
        self.assertEqual(probe(controller(datetime(2026, 10, 12, 14, 0))), "")
        self.assertEqual(probe(controller(datetime(2026, 10, 12, 23, 30), bundle=False)), "")   # no bundle

    def test_a_briefing_that_could_not_load_is_an_error_tile_with_retry(self) -> None:
        reading = self.reading
        pressed: list[str] = []
        reading.playPause.connect(lambda: pressed.append("retry"))
        reading.set_controls("Retry", True, False)
        settle()
        tile = reading.error_tile
        self.assertTrue(tile.isVisible())
        self.assertIs(reading.stage.side_tile(), tile)
        info = tile.info()
        self.assertEqual((info.sentence, info.action), ("Couldn't load your briefing.", "Retry"))
        self.assertEqual(len(info.sentence.split(". ")), 1)                  # one sentence
        self.assertEqual([button.text() for button in tile.findChildren(QAbstractButton)], ["Retry"])
        self.assertEqual(self.word(), ("NEEDS YOU", home.TONE_AMBER))         # never a red orb
        tile.action_button.click()
        self.assertEqual(pressed, ["retry"])                                 # the Briefing screen's own Retry
        reading.set_controls("Play", False, False)                           # loading again
        settle()
        self.assertFalse(tile.isVisible())
        reading.set_controls("Retry", False, False)                          # cannot retry: no tile
        settle()
        self.assertFalse(tile.isVisible())

    def test_claude_code_not_signed_in_shows_how_to_fix(self) -> None:
        reading, window = self.reading, self.window
        window.set_service("claude", hud.STATUS_WARN, "Ask Jarvis: SIGN IN - Run: claude auth login")
        reading.set_ask_signed_out(True, "Run: claude auth login --claudeai (Claude Code is not signed in)")
        settle()
        tile = reading.error_tile
        self.assertTrue(tile.isVisible())
        self.assertEqual((tile.info().sentence, tile.info().action), ("Claude Code isn't signed in.", "How to fix"))
        self.assertEqual(reading.header.service_notes(), [])                # the tile says it
        seen: list[str] = []
        tile.actionClicked.connect(seen.append)
        tile.action_button.click()
        settle()
        self.assertTrue(tile.help_shown())
        self.assertIn("claude auth login", " ".join(tile.texts()[2]))
        self.assertEqual(seen, [ui.ERROR_SIGN_IN])
        self.assertEqual(reading.current_tab(), ui.SCREEN_HOME)              # nothing opened or run
        reading.set_ask_signed_out(False)
        settle()
        self.assertFalse(tile.isVisible())
        self.assertEqual([name for name, _s, _t in reading.header.service_notes()], ["claude"])

    def test_gmail_refused_retry_only_reopens_the_same_card(self) -> None:
        reading = self.reading
        decided: list[tuple[str, str]] = []
        reading.approveClicked.connect(lambda action_id: decided.append(("approve", action_id)))
        reading.denyClicked.connect(lambda action_id: decided.append(("deny", action_id)))
        for action_id in ("r1", "r2"):
            card = reading.add_action_card(action_id, "Reply", f"Re: invented subject {action_id}", "", True,
                                           approve_text="Retry")
            card.set_status(hud.CARD_PENDING)
        card = reading.action_card("r2")
        reading.set_mail_refused("r2", "reply")
        self.assertEqual(reading.errors(), [])                              # only a failed card counts
        card.set_status(hud.CARD_FAILED, "Nothing was sent")
        reading.set_mail_refused("r2", "reply")
        settle()
        tile = reading.error_tile
        self.assertTrue(tile.isVisible())
        self.assertEqual((tile.info().sentence, tile.info().action), ("Gmail refused the reply.", "Retry"))
        tile.action_button.click()
        settle()
        self.assertEqual(decided, [])                                        # nothing approved, nothing sent
        self.assertEqual(reading.current_tab(), ui.SCREEN_APPROVALS)
        self.assertEqual(reading.approvals.focal_id(), "r2")                 # the SAME card, its own Retry
        self.assertEqual(card.status(), hud.CARD_FAILED)
        self.assertFalse(tile.isVisible())
        reading.set_tab(ui.SCREEN_HOME)
        reading.set_mail_refused("r2", "reply")
        self.assertEqual(len(reading.errors()), 1)
        card.set_status(hud.CARD_COUNTDOWN)                                  # the owner's Retry on the card
        settle()
        self.assertEqual(reading.errors(), [])                               # the tile goes with the failure
        self.assertEqual(decided, [])

    def test_one_tile_beside_the_orb_an_offer_first(self) -> None:
        reading = self.reading
        reading.set_controls("Retry", True, False)
        reading.set_offers([PLAN_OFFER])
        settle()
        self.assertIs(reading.stage.side_tile(), reading.offer_tile)
        self.assertTrue(reading.offer_tile.isVisible())
        self.assertFalse(reading.error_tile.isVisible())
        reading.set_offers([])
        settle()
        self.assertIs(reading.stage.side_tile(), reading.error_tile)
        self.assertTrue(reading.error_tile.isVisible())


class ControllerStatesTests(unittest.TestCase):
    """The app's own signals drive the states; the toast is posted only where the app already surfaces
    something (the announcement, a prep offer that flashes the taskbar) and its action only opens a screen."""

    def flow(self, **kwargs: object) -> object:
        from tests.test_launch_flow import Flow

        flow = Flow(self, **kwargs)
        self.addCleanup(lambda: flow.c._toast_window is not None and flow.c._toast_window.dismiss())
        return flow

    def test_the_announcement_posts_the_toast_only_while_the_owner_is_elsewhere(self) -> None:
        from tests.test_launch_flow import variant

        flow = self.flow(expected_run="AM")
        c = flow.c
        c.start()
        flow.attempt(variant(flow.page, datetime(2026, 10, 3, 23, 50, tzinfo=PDT), "PM"))
        self.assertEqual(c.state, ui.STATE_WAITING)
        flow.active = False                                                  # the owner is in another app
        with mock.patch.object(ui, "force_foreground") as forced:
            flow.attempt(flow.page)                                          # the scheduled arrival
            settle()
            forced.assert_not_called()                                       # never the focus by itself
            toast = c._toast_window
            self.assertIsNotNone(toast)
            self.assertTrue(toast.isVisible())
            self.assertEqual((toast.line(), toast.action_text()), ("Your AM briefing is ready, sir.", "View"))
            self.assertEqual(flow.reading.current_tab(), ui.SCREEN_HOME)
            toast.action_button.click()                                      # the owner's click
            settle()
            forced.assert_called_once_with(c.window)
        self.assertEqual(flow.reading.current_tab(), ui.SCREEN_BRIEFING)
        self.assertEqual(c.player.starts, [])                                # viewing, not reading

    def test_an_open_window_gets_no_toast(self) -> None:
        flow = self.flow(expected_run="AM")
        c = flow.c
        c.start()
        flow.active = True
        flow.attempt(flow.page)
        settle()
        self.assertTrue(c._toast_window is None or not c._toast_window.isVisible())

    def test_a_flashing_prep_offer_posts_prepare_brief_which_only_opens_home(self) -> None:
        flow = self.flow(expected_run=None)
        c = flow.c
        c.enter_reading(autoplay=False)
        settle()
        flow.reading.set_tab(ui.SCREEN_LIVE if flow.reading.live_available() else ui.SCREEN_APPROVALS)
        links: list[str] = []
        flow.reading.homeLink.connect(links.append)
        start = datetime(2026, 10, 4, 11, 0)
        offer = types.SimpleNamespace(id=7, kind="prep", flash=True, start=start,
                                      posted_at=datetime(2026, 10, 4, 10, 45),
                                      event_title="Project Development check-in", alias="", guests=3,
                                      sub="", group=())
        with mock.patch.object(QApplication, "alert") as alert, mock.patch.object(ui, "force_foreground") as forced:
            c._on_offer_posted(offer)
            settle()
            alert.assert_called_once()                                        # the taskbar flash, as before
            toast = c._toast_window
            self.assertEqual((toast.line(), toast.action_text()),
                             ("Project Development check-in in 15 minutes, sir.", "Prepare brief"))
            forced.assert_not_called()
            toast.action_button.click()
            settle()
            forced.assert_called_once_with(c.window)
        self.assertEqual(flow.reading.current_tab(), ui.SCREEN_HOME)
        self.assertEqual(links, [])                                           # the offer's own click prepares

    def test_waiting_for_the_run_the_owner_opened_into(self) -> None:
        from tests.test_launch_flow import variant

        flow = self.flow(expected_run="AM")
        c = flow.c
        c.start()
        flow.attempt(variant(flow.page, datetime(2026, 10, 3, 23, 50, tzinfo=PDT), "PM"))
        c.handle_activation({"cmd": "activate", "run": None, "now": False, "open": True})
        settle()
        waiting, run, readout = flow.reading.waiting()
        self.assertEqual((waiting, run), (True, "AM"))
        self.assertTrue(readout.startswith("EVERY MINUTE \u00b7 UNTIL "), readout)
        self.assertEqual(flow.reading.captions.resting(), "Checking for your AM briefing, sir.")
        flow.attempt(flow.page)                                               # it landed
        settle()
        self.assertEqual(flow.reading.waiting(), (False, "", ""))


if __name__ == "__main__":
    unittest.main()
