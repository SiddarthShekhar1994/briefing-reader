"""Tests for the approved motion prototype in the app (briefing_reader.alive, find_view, dock, orb, ui): the
labelled Dock and its rail, the Find list (Ctrl+K), the screen change (wipe and assembly), the start-up and
``[ui] motion``.

Qt runs offscreen in this process with the bundled fonts and shows nothing; every text is invented.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace

from PySide6.QtCore import QEventLoop, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QEnterEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLineEdit, QWidget

from briefing_reader import alive, dock, find_view, holo, offer_view, orb, settings_view, ui
from tests.ui_fakes import fonts, settle

_app: QApplication | None = None
PLAN_OFFER = offer_view.OfferInfo(1, "plan", "offer:1", offer_view.PLAN_KICKER, offer_view.PLAN_TITLE, "Plan my day",
                                   (offer_view.PLAN_META,), "Shall I plan your day, sir?")
PREP_OFFER = offer_view.OfferInfo(2, "prep", "offer:2", "2:00 PM . IN 15 MIN", "Project sync", "Prepare brief",
                                   ("Work . 2 guests",), "Shall I prepare a brief?")


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_alive", "-platform", "offscreen"])
    fonts()


def wait(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def motion(test: unittest.TestCase, level: str = holo.MOTION_LIVELY) -> None:
    holo.set_reduced_motion(False)
    holo.set_motion_level(level)
    test.addCleanup(holo.set_reduced_motion, None)
    test.addCleanup(holo.set_motion_level, holo.MOTION_LIVELY)


class LabelledDockTests(unittest.TestCase):
    def setUp(self) -> None:
        motion(self)
        self.window = QWidget()
        self.window.resize(600, 640)
        row = QHBoxLayout(self.window)
        row.setContentsMargins(0, 0, 0, 0)
        self.dock = dock.Dock(self.window, labelled=True)
        row.addWidget(self.dock)
        self.sink = QLineEdit(self.window)
        row.addWidget(self.sink)
        row.addStretch(1)
        self.activated: list[str] = []
        self.dock.slotActivated.connect(self.activated.append)
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.window.hide)
        self.window.show()
        settle()

    def test_every_slot_shows_its_glyph_and_its_name_about_200_px_wide(self) -> None:
        self.assertTrue(self.dock.is_labelled())
        self.assertEqual(self.dock.width(), holo.DOCK_WIDTH_LABELLED)
        self.dock.set_hud_key("Ctrl+Alt+H")
        self.dock.set_slot_visible(dock.DESKTOP, True)               # [desktop] on: the Switch prototype's Dock
        settle()
        for key in dock.SLOTS:
            button = self.dock.button(key)
            self.assertEqual(button.label_text(), dock.LABELS[key])
            rect = self.dock.slot_rect(key)
            self.assertEqual((rect.x(), rect.width(), rect.height()), (12, 176, 44))
        self.assertEqual([dock.LABELS[k] for k in dock.SLOTS],
                         ["Home", "Briefing", "Approvals", "Live", "Memory", "Follow-ups", "Meetings", "Desktop",
                          "Settings"])
        self.assertEqual(self.dock.slot_rect(dock.SETTINGS).bottom(), 640 - 16)          # Settings at the bottom
        # Desktop: the HUD tag at the row's end, "Ctrl+Alt+H switches" right under it.
        tag = self.dock.button(dock.DESKTOP).tag_rect()
        self.assertIsNotNone(tag)
        self.assertEqual(tag.right(), 176 - 12)
        hint = self.dock.desktop_hint()
        self.assertTrue(hint.isVisible())
        self.assertEqual((hint.keys(), hint.text()), (["Ctrl", "Alt", "H"], "Ctrl+Alt+H switches"))
        self.assertEqual(hint.geometry().top() - self.dock.slot_rect(dock.DESKTOP).bottom(), 1 + 6 - 1)
        self.assertFalse(self.window.grab().isNull())
        self.dock.set_labelled(False)                                # folded: glyphs only, no hint
        self.assertFalse(hint.isVisible())
        self.assertIsNone(self.dock.button(dock.DESKTOP).tag_rect())
        self.dock.set_labelled(True)
        self.dock.set_hud_key("")                                    # no HUD key: no hint
        self.assertFalse(hint.isVisible())

    def test_badges_the_new_dot_and_names_stay(self) -> None:
        self.dock.set_approvals(2)
        self.dock.set_briefing_new(True)
        self.dock.set_followups(1)
        self.assertEqual(self.dock.button(dock.APPROVALS).count(), 2)
        self.assertTrue(self.dock.button(dock.BRIEFING).has_dot())
        self.assertEqual(self.dock.accessible_name(dock.APPROVALS), "Approvals, 2 waiting")
        self.assertEqual(self.dock.button(dock.APPROVALS).accessibleName(), "Approvals, 2 waiting")
        self.assertEqual(self.dock.accessible_name(dock.BRIEFING), "Briefing, new")
        self.assertEqual(self.dock.signal_words(dock.FOLLOWUPS), "1 waiting on you")
        self.assertIn("Shortcut Ctrl+3", self.dock.button(dock.APPROVALS).accessibleDescription())
        self.assertFalse(self.window.grab().isNull())

    def test_no_tooltip_while_the_names_show(self) -> None:
        self.dock.button(dock.APPROVALS).setFocus(Qt.FocusReason.TabFocusReason)
        settle()
        self.assertTrue(self.dock.button(dock.APPROVALS).has_focus_ring())
        self.assertIsNone(self.dock.tooltip_for())
        button = self.dock.button(dock.FOLLOWUPS)
        QApplication.sendEvent(button, QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
        wait(520)
        self.assertIsNone(self.dock.tooltip_for())

    def test_the_rail_slides_to_the_current_slot(self) -> None:
        start = self.dock.rail_rect()
        self.assertEqual((start.x(), start.width(), start.height()), (0.0, 3.0, 32.0))
        self.assertEqual(start.center().y(), self.dock.slot_rect(dock.HOME).center().y())
        QTest.mouseClick(self.dock.button(dock.APPROVALS), Qt.MouseButton.LeftButton)
        self.assertEqual(self.activated, [dock.APPROVALS])
        self.assertTrue(self.dock.is_sliding())
        wait(200)
        middle = self.dock.rail_rect().center().y()
        self.assertLess(self.dock.slot_rect(dock.HOME).center().y(), middle)
        self.assertLess(middle, self.dock.slot_rect(dock.APPROVALS).center().y())
        wait(320)
        self.assertFalse(self.dock.is_sliding())
        self.assertEqual(self.dock.rail_rect().center().y(), self.dock.slot_rect(dock.APPROVALS).center().y())

    def test_motion_off_and_calm_change_the_slide(self) -> None:
        holo.set_motion_level(holo.MOTION_OFF)
        self.dock.set_current(dock.LIVE)
        self.assertFalse(self.dock.is_sliding())
        self.assertEqual(self.dock.rail_rect().center().y(), self.dock.slot_rect(dock.LIVE).center().y())
        holo.set_motion_level(holo.MOTION_CALM)
        self.dock.set_current(dock.MEMORY)
        self.assertTrue(self.dock.is_sliding())
        self.assertEqual(self.dock._slide_anim.duration(), 600)

    def test_folding_to_glyphs_brings_the_tooltips_back(self) -> None:
        self.dock.set_labelled(False)
        settle()
        self.assertEqual(self.dock.width(), holo.DOCK_WIDTH)
        self.assertEqual(self.dock.button(dock.APPROVALS).label_text(), "")
        self.assertEqual(self.dock.slot_rect(dock.APPROVALS).width(), 44)
        self.dock.button(dock.APPROVALS).setFocus(Qt.FocusReason.TabFocusReason)
        settle()
        self.assertEqual(self.dock.tooltip_for(), dock.APPROVALS)
        self.assertEqual(self.dock.tooltip().text(), ("Approvals", "CTRL+3"))
        self.dock.set_labelled(True)
        self.assertIsNone(self.dock.tooltip_for())                 # the names are on the rail again


class AppWindowCase(unittest.TestCase):
    """The app's main window (ui.BriefingWindow) on its reading view at the default size."""

    SIZE = (1120, 700)

    def setUp(self) -> None:
        motion(self)
        orb.set_session_locked(False)
        self.window = ui.BriefingWindow(10, 30)
        self.addCleanup(self.window.deleteLater)
        self.window.set_clock(lambda: datetime(2026, 10, 12, 21, 51))
        self.reading = self.window.reading
        self.reading.set_ask_available(True)
        self.window.show_reading_view()
        self.window.resize(*self.SIZE)
        self.window.show()
        self.window.activateWindow()
        settle()
        self.addCleanup(self.window.hide)
        self.reading.transition.finish()

    def keys(self, key: Qt.Key, modifier: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier) -> None:
        widget = QApplication.focusWidget() or self.window
        QTest.keyClick(widget, key, modifier)
        settle()


class ReadingDockTests(AppWindowCase):
    def test_labelled_at_the_default_size_and_folded_in_narrow_windows(self) -> None:
        rail = self.reading.dock
        self.assertTrue(rail.is_labelled())
        self.assertEqual(rail.width(), 200)
        for width, labelled in ((1000, False), (900, False), (1040, True), (1120, True)):
            self.window.resize(width, 700)
            settle()
            self.assertEqual(rail.is_labelled(), labelled, width)
            self.assertEqual(rail.width(), 200 if labelled else 64)
            for key in rail.visible_slots():
                corner = rail.button(key).mapTo(self.window, QPoint(rail.button(key).width(), 0))
                self.assertLessEqual(corner.x(), self.window.width())
        self.assertEqual(ui.DOCK_LABELS_FROM, 1024)

    def test_shortcuts_and_names_work_in_both_widths(self) -> None:
        self.reading.set_pending(2, 3)
        for width in (1120, 900):
            self.window.resize(width, 700)
            settle()
            self.reading.set_tab(ui.SCREEN_HOME)
            self.keys(Qt.Key.Key_3, Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(self.reading.current_tab(), ui.SCREEN_APPROVALS)
            self.assertEqual(self.reading.dock.current(), dock.APPROVALS)
            self.keys(Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(self.reading.current_tab(), ui.SCREEN_SETTINGS)
            self.assertEqual(self.reading.dock.button(dock.APPROVALS).accessibleName(), "Approvals, 2 waiting")


class FindListTests(AppWindowCase):
    def labels(self, query: str = "") -> list[str]:
        return [item.label for item in self.reading.find_items(query)]

    def test_every_screen_with_its_shortcut_and_counts_in_words(self) -> None:
        self.reading.set_pending(2, 3)
        self.reading.set_new_briefing(True, "Your AM briefing is ready to view")
        items = self.reading.find_items("")
        self.assertEqual([item.label for item in items],
                         ["Home", "Briefing - new", "Approvals - 2 waiting", "Live", "Settings"])   # bundle off
        self.assertEqual([item.hint for item in items], ["CTRL+1", "CTRL+2", "CTRL+3", "CTRL+4", "CTRL+,"])
        self.assertTrue(all(item.kind == find_view.KIND_SCREEN for item in items))
        self.assertEqual(self.labels("appr"), ["Approvals - 2 waiting"])
        self.assertEqual(self.labels("  SETT "), ["Settings"])
        self.reading.set_memory_available(True)
        self.assertIn("Memory", self.labels(""))

    def test_ctrl_k_opens_it_and_the_keyboard_works(self) -> None:
        self.reading.command_bar.focus_input()
        settle()
        opener = QApplication.focusWidget()
        self.keys(Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
        overlay = self.reading.find_overlay
        self.assertTrue(overlay.is_open())
        self.assertIs(QApplication.focusWidget(), overlay.field)
        self.assertEqual(overlay.current(), 0)
        self.keys(Qt.Key.Key_Down)
        self.keys(Qt.Key.Key_Down)
        self.assertEqual(overlay.current_item().key, dock.APPROVALS)
        self.keys(Qt.Key.Key_Up)
        self.assertEqual(overlay.current_item().key, dock.BRIEFING)
        self.keys(Qt.Key.Key_Tab)                                     # focus is trapped in the list
        self.assertIs(QApplication.focusWidget(), overlay.field)
        self.keys(Qt.Key.Key_Escape)
        wait(250)
        self.assertFalse(overlay.isVisible())
        self.assertIs(QApplication.focusWidget(), opener)             # back where it was
        self.assertEqual(self.reading.current_tab(), ui.SCREEN_HOME)

    def test_typing_filters_and_enter_opens_the_screen(self) -> None:
        self.reading.header.find_button.click()
        overlay = self.reading.find_overlay
        self.assertTrue(overlay.is_open())
        QTest.keyClicks(overlay.field, "live")
        settle()
        self.assertEqual([item.label for item in overlay.items()], ["Live"])
        self.keys(Qt.Key.Key_Return)
        wait(250)
        self.assertFalse(overlay.isVisible())
        self.assertEqual(self.reading.current_tab(), ui.SCREEN_LIVE)
        self.assertEqual(self.reading.dock.current(), dock.LIVE)
        self.assertTrue(QApplication.focusWidget() is not None and QApplication.focusWidget().isVisible())

    def test_a_scrim_click_closes_it(self) -> None:
        self.reading.open_find()
        overlay = self.reading.find_overlay
        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(4, overlay.height() - 4))
        wait(250)
        self.assertFalse(overlay.isVisible())

    def test_actions_only_with_the_bundle_and_an_open_offer_and_only_do_what_their_link_does(self) -> None:
        reading = self.reading
        links: list[str] = []
        approvals: list[str] = []
        reading.homeLink.connect(links.append)
        for signal in (reading.approveClicked, reading.denyClicked, reading.undoAction, reading.editAction):
            signal.connect(approvals.append)
        reading.set_offers([PLAN_OFFER, PREP_OFFER])
        self.assertNotIn("Plan my day", self.labels(""))                # the bundle is off
        reading.set_find_actions(True)
        self.assertEqual(self.labels("")[-2:], ["Plan my day", "Prepare a brief for Project sync"])
        reading.set_offers([])
        self.assertNotIn("Plan my day", self.labels(""))                # no link right now: no action
        reading.set_offers([PLAN_OFFER])
        reading.open_find()
        overlay = reading.find_overlay
        QTest.keyClicks(overlay.field, "plan")
        settle()
        self.assertEqual([(i.kind, i.key) for i in overlay.items()], [(find_view.KIND_ACTION, "offer:1")])
        self.keys(Qt.Key.Key_Return)
        self.assertEqual(links, ["offer:1"])                            # what the tile's commit does
        reading.offer_tile.commit_button.click()
        self.assertEqual(links, ["offer:1", "offer:1"])
        self.assertEqual(approvals, [])
        self.assertEqual(reading.current_tab(), ui.SCREEN_HOME)

    def test_ask_jarvis_submits_like_enter(self) -> None:
        bar = self.reading.command_bar
        submitted: list[str] = []
        bar.submitted.connect(submitted.append)
        self.reading.set_tab(ui.SCREEN_SETTINGS)
        self.reading.open_find()
        overlay = self.reading.find_overlay
        QTest.keyClicks(overlay.field, "what's   on tomorrow")
        settle()
        self.assertEqual([(i.kind, i.label) for i in overlay.items()],
                         [(find_view.KIND_ASK, "Ask Jarvis: what's on tomorrow")])
        self.keys(Qt.Key.Key_Return)
        self.assertEqual(submitted, ["what's on tomorrow"])
        self.assertEqual(self.reading.current_tab(), ui.SCREEN_HOME)
        self.assertEqual(bar.text(), "what's on tomorrow")
        bar.clear_input()
        QTest.keyClicks(bar.input, "what's on tomorrow")
        QTest.keyClick(bar.input, Qt.Key.Key_Return)                   # Enter there: the same submission
        self.assertEqual(submitted, ["what's on tomorrow", "what's on tomorrow"])
        bar.set_running(True)
        self.assertEqual([i.kind for i in self.reading.find_items("xyzzy")], [find_view.KIND_NONE])
        bar.set_running(False)
        self.reading.set_ask_available(False)
        self.assertEqual(self.labels("xyzzy"), [find_view.NOTHING_TEXT])

    def test_nothing_in_the_list_approves_sends_or_changes_anything(self) -> None:
        reading = self.reading
        reading.set_pending(2, 3)
        reading.set_find_actions(True)
        reading.set_offers([PLAN_OFFER])
        seen: list[str] = []
        for signal in (reading.approveClicked, reading.denyClicked, reading.lockedClicked, reading.undoAction,
                       reading.holdUndo, reading.editAction, reading.recipientTicked, reading.signInAccount,
                       reading.openSource, reading.copyText):
            signal.connect(lambda *args, s=signal: seen.append(str(args)))
        kinds = {item.kind for item in reading.find_items("")} | {i.kind for i in reading.find_items("send it")}
        self.assertLessEqual(kinds, {find_view.KIND_SCREEN, find_view.KIND_ACTION, find_view.KIND_ASK})
        for item in reading.find_items(""):
            reading._on_find_pick(item)
            settle()
        self.assertEqual(seen, [])


class ScreenTransitionTests(AppWindowCase):
    def test_a_screen_change_wipes_and_assembles(self) -> None:
        transition = self.reading.transition
        self.reading.set_tab(ui.SCREEN_SETTINGS)
        self.assertTrue(transition.is_running())
        self.assertEqual(transition.level(), holo.MOTION_LIVELY)
        settle()
        self.assertTrue(transition.has_snapshot())
        self.assertGreaterEqual(len(transition.panel_rects()), 1)
        transition._on_tick(100.0)
        self.assertIsNotNone(transition.wipe_y())
        opacity, rise, shown = transition.panel_state(0)
        self.assertTrue(0.0 < opacity < 1.0 and 0.0 < rise < holo.ASSEMBLE_RISE and 0.0 < shown < 1.0)
        self.assertFalse(self.window.grab().isNull())
        self.assertLessEqual(transition.total_ms(), 700)
        wait(transition.total_ms() + 200)
        self.assertFalse(transition.isVisible())
        self.assertFalse(transition.has_snapshot())

    def test_a_click_during_it_works_and_focus_is_untouched(self) -> None:
        reading = self.reading
        opened: list[bool] = []
        reading.openConfig.connect(lambda: opened.append(True))
        reading.set_tab(ui.SCREEN_SETTINGS)
        settle()
        self.assertTrue(reading.transition.is_running())
        button = reading.settings_page.config_button
        center = button.mapTo(self.window, button.rect().center())
        hit = self.window.childAt(center)
        self.assertTrue(hit is button or button.isAncestorOf(hit), hit)          # the overlay is not hit
        self.assertEqual(reading.transition.focusPolicy(), Qt.FocusPolicy.NoFocus)
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        settle()
        self.assertEqual(opened, [True])
        self.assertFalse(reading.transition.isVisible())                       # the click ended it at once

    def test_a_key_ends_it_at_once(self) -> None:
        self.reading.set_tab(ui.SCREEN_LIVE)
        self.assertTrue(self.reading.transition.is_running())
        self.keys(Qt.Key.Key_Tab)
        self.assertFalse(self.reading.transition.isVisible())

    def test_calm_fades_without_the_wipe_and_off_skips_it(self) -> None:
        transition = self.reading.transition
        holo.set_motion_level(holo.MOTION_CALM)
        self.reading.set_tab(ui.SCREEN_SETTINGS)
        self.assertTrue(transition.is_running())
        transition._on_tick(100.0)
        self.assertIsNone(transition.wipe_y())
        opacity, rise, shown = transition.panel_state(0)
        self.assertTrue(0.0 < opacity < 1.0 and rise == 0.0 and shown == 1.0)
        transition.finish()
        holo.set_motion_level(holo.MOTION_OFF)
        self.reading.set_tab(ui.SCREEN_LIVE)
        self.assertFalse(transition.isVisible())
        holo.set_motion_level(holo.MOTION_LIVELY)
        holo.set_reduced_motion(True)                                          # Windows' animations off
        self.assertEqual(holo.motion_level(), holo.MOTION_OFF)
        self.reading.set_tab(ui.SCREEN_HOME)
        self.assertFalse(transition.isVisible())

    def test_main_panels(self) -> None:
        page = QWidget()
        self.addCleanup(page.deleteLater)
        page.resize(400, 300)
        inner = QWidget(page)
        inner.setGeometry(0, 0, 400, 300)
        low, high = QWidget(inner), QWidget(inner)
        low.setGeometry(0, 150, 400, 100)
        high.setGeometry(0, 0, 400, 100)
        page.show()
        settle()
        self.assertEqual(alive.main_panels(page), [high, low])
        page.hide()


class StartupTests(AppWindowCase):
    def test_presence_runs_once_and_again_after_30_minutes_hidden(self) -> None:
        now = [0.0]
        plays: list[float] = []
        presence = alive.StartupPresence(lambda: plays.append(now[0]), clock=lambda: now[0])
        presence.note_shown(True)
        self.assertEqual(plays, [])                                        # not armed: not the app's start
        presence.note_shown(False)
        presence.arm()
        presence.note_shown(True)
        self.assertEqual(plays, [0.0])
        for hidden_for, expected in ((10 * 60, 1), (29 * 60, 1), (31 * 60, 2)):
            presence.note_shown(False)
            now[0] += hidden_for
            presence.note_shown(True)
            self.assertEqual(len(plays), expected, hidden_for)
        self.assertEqual(presence.plays(), 2)

    def show_armed(self) -> ui.ReadingView:
        self.window.hide()
        settle()
        self.reading.startup = alive.StartupPresence(self.reading._play_startup)
        self.reading.arm_startup()
        self.window.show()
        settle()
        return self.reading

    def test_the_first_showing_plays_it_and_nothing_waits(self) -> None:
        reading = self.show_armed()
        self.assertEqual(reading.startup.plays(), 1)
        self.assertTrue(reading.hero.intro_running())
        self.assertTrue(reading.header.wordmark_typing())
        self.assertTrue(reading.scan_sweep.is_running())
        self.assertEqual(reading.header.wordmark.accessibleName(), "Jarvis")
        self.assertEqual(reading.scan_sweep.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertTrue(reading.scan_sweep.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        reading.command_bar.focus_input()                                    # input works meanwhile
        QTest.keyClicks(reading.command_bar.input, "hello")
        self.assertEqual(reading.command_bar.text(), "hello")
        self.assertLessEqual(orb.OrbWidget.intro_seconds(), 1.25)
        wait(1900)
        self.assertFalse(reading.hero.intro_running())
        self.assertFalse(reading.header.wordmark_typing())
        self.assertEqual(reading.header.wordmark_shown(), "J.A.R.V.I.S.")
        self.assertFalse(reading.scan_sweep.isVisible())
        self.window.hide()
        settle()
        self.window.show()
        settle()
        self.assertEqual(reading.startup.plays(), 1)                          # once

    def test_motion_off_skips_it_and_calm_only_assembles_the_orb(self) -> None:
        holo.set_motion_level(holo.MOTION_OFF)
        reading = self.show_armed()
        self.assertFalse(reading.hero.intro_running())
        self.assertFalse(reading.header.wordmark_typing())
        self.assertFalse(reading.scan_sweep.isVisible())
        self.assertEqual(reading.header.wordmark_shown(), "J.A.R.V.I.S.")
        holo.set_motion_level(holo.MOTION_CALM)
        reading._play_startup()
        self.assertTrue(reading.hero.intro_running())
        self.assertFalse(reading.header.wordmark_typing())
        self.assertFalse(reading.scan_sweep.isVisible())

    def test_the_orb_assembles_from_the_centre_out(self) -> None:
        hero = orb.OrbWidget(orb.HERO)
        self.addCleanup(hero.deleteLater)
        hero.set_phase(2.35)
        rest = hero.sample()
        hero.play_intro(start=2.35 - 0.3)
        frame = hero.sample()
        self.assertGreater(frame.core_op, 0.0)                             # the core first
        self.assertGreater(frame.opacities[5], 0.0)                        # R6 (innermost) is on its way
        self.assertEqual(frame.opacities[0], 0.0)                          # R1 (outermost) has not started
        self.assertEqual(frame.frame, 0.0)
        self.assertLess(frame.scales[5], rest.scales[5])
        self.assertTrue(hero.intro_running())
        self.assertEqual(hero.frame_interval(), orb.FRAME_ACTIVE_MS)
        hero.play_intro(start=2.35 - 2.0)
        done = hero.sample()
        self.assertEqual((done.opacities, done.scales), (rest.opacities, rest.scales))
        holo.set_reduced_motion(True)
        hero.play_intro()
        self.assertFalse(hero.intro_running())


class MotionSettingTests(unittest.TestCase):
    def test_levels_and_lengths(self) -> None:
        motion(self)
        self.assertEqual(holo.motion_level(), holo.MOTION_LIVELY)
        self.assertEqual(holo.motion_ms(400), 400)
        holo.set_motion_level(" Calm ")
        self.assertEqual((holo.motion_level(), holo.motion_ms(400)), (holo.MOTION_CALM, 600))
        holo.set_motion_level("off")
        self.assertEqual((holo.motion_level(), holo.motion_ms(400)), (holo.MOTION_OFF, 0))
        holo.set_motion_level("sparkly")
        self.assertEqual(holo.configured_motion(), holo.MOTION_LIVELY)
        holo.set_reduced_motion(True)
        self.assertEqual(holo.motion_level(), holo.MOTION_OFF)

    def test_the_settings_screen_shows_it(self) -> None:
        motion(self)
        for level, words in (("lively", "Lively"), ("calm", "Calm"), ("off", "Off - no entrance")):
            config = SimpleNamespace(ui=SimpleNamespace(motion=level))
            rows = dict(settings_view.settings_rows(config, "times"))
            self.assertTrue(rows["Screen motion"].startswith(words), rows["Screen motion"])
        holo.set_reduced_motion(True)
        rows = dict(settings_view.settings_rows(SimpleNamespace(ui=SimpleNamespace(motion="lively")), "times"))
        self.assertEqual(rows["Screen motion"], settings_view.MOTION_WINDOWS_OFF)
        self.assertIn("[ui]", settings_view.HOW_TEXT.format(tables=settings_view._tables("times")))

    def test_the_app_applies_it_and_arms_the_start_up(self) -> None:
        motion(self)
        from tests.ui_fakes import AppHarness

        harness = AppHarness(self, config_extra='\n[ui]\nmotion = "calm"\n')
        self.assertEqual(harness.config.ui.motion, holo.MOTION_CALM)
        self.assertEqual(holo.configured_motion(), holo.MOTION_CALM)
        self.assertTrue(harness.c.window.reading.startup.armed())
        self.assertFalse(harness.c.window.reading._find_actions)        # the bundle is off: no offer links

    def test_the_bundle_lets_offer_links_into_the_find_list(self) -> None:
        motion(self)
        from tests.ui_fakes import AppHarness

        harness = AppHarness(self, productivity=True)
        self.assertTrue(harness.c.window.reading._find_actions)


if __name__ == "__main__":
    unittest.main()
