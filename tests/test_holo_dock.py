"""Tests for the Hologram Dock (briefing_reader.dock): slots, the sliding highlight, keyboard (one tab stop,
Up / Down / Home / End, Enter / Space), badges and their accessible names, hidden slots, tooltips, shortcuts.

Qt runs offscreen in this process and shows nothing.
"""

from __future__ import annotations

import unittest

from PySide6.QtCore import QEventLoop, QPointF, Qt, QTimer
from PySide6.QtGui import QEnterEvent, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLineEdit, QWidget

from briefing_reader import config, dock, holo, hud, orb

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_holo_dock", "-platform", "offscreen"])
    hud.load_fonts(config.PROJECT_ROOT / "fonts")


def settle(rounds: int = 4) -> None:
    for _ in range(rounds):
        QApplication.processEvents()


def wait(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class DockTests(unittest.TestCase):
    def setUp(self) -> None:
        holo.set_reduced_motion(False)
        self.addCleanup(holo.set_reduced_motion, None)
        self.window = QWidget()
        self.window.resize(500, 640)
        row = QHBoxLayout(self.window)
        row.setContentsMargins(0, 0, 0, 0)
        self.dock = dock.Dock(self.window)
        row.addWidget(self.dock)
        self.sink = QLineEdit(self.window)            # where focus rests outside the Dock
        row.addWidget(self.sink)
        row.addStretch(1)
        self.activated: list[str] = []
        self.dock.slotActivated.connect(self.activated.append)
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.window.hide)
        self.window.show()
        self.window.activateWindow()
        settle()
        self.sink.setFocus(Qt.FocusReason.OtherFocusReason)
        settle()

    def focus(self, key: str) -> None:
        self.dock.button(key).setFocus(Qt.FocusReason.TabFocusReason)
        settle()

    # ---- layout -------------------------------------------------------------

    def test_slots_and_layout(self) -> None:
        self.assertEqual(self.dock.visible_slots(), list(dock.SLOTS))
        self.assertEqual(self.dock.width(), 64)
        rects = {key: self.dock.slot_rect(key) for key in dock.SLOTS}
        for rect in rects.values():
            self.assertEqual((rect.width(), rect.height(), rect.x()), (44, 44, 10))
        self.assertEqual(rects[dock.HOME].y(), 16)
        self.assertEqual(rects[dock.BRIEFING].y() - rects[dock.HOME].bottom(), 1 + 6 + 9 + 6 - 1)   # hairline
        self.assertEqual(rects[dock.APPROVALS].y() - rects[dock.BRIEFING].y(), 50)
        self.assertEqual(rects[dock.SETTINGS].bottom(), 640 - 16)                                # pinned
        self.assertIsNotNone(self.dock.mini_orb())
        self.assertEqual(self.dock.mini_orb().size().width(), orb.MINI)

    # ---- activation and the highlight ------------------------------------------

    def test_click_emits_and_moves_the_highlight(self) -> None:
        QTest.mouseClick(self.dock.button(dock.APPROVALS), Qt.MouseButton.LeftButton)
        self.assertEqual(self.activated, [dock.APPROVALS])
        self.assertEqual(self.dock.current(), dock.APPROVALS)
        self.assertTrue(self.dock.is_sliding())
        middle = self.dock.highlight_rect()
        self.assertTrue(self.dock.slot_rect(dock.HOME).y() <= middle.y() <= self.dock.slot_rect(dock.APPROVALS).y())
        wait(520)                                   # the motion prototype's rail: 400 ms
        self.assertFalse(self.dock.is_sliding())
        self.assertEqual(self.dock.highlight_rect(), self.dock.slot_rect(dock.APPROVALS))
        self.assertIn("Current screen", self.dock.button(dock.APPROVALS).accessibleDescription())
        self.assertNotIn("Current screen", self.dock.button(dock.HOME).accessibleDescription())

    def test_settings_opens_without_becoming_current(self) -> None:
        QTest.mouseClick(self.dock.button(dock.SETTINGS), Qt.MouseButton.LeftButton)
        self.assertEqual(self.activated, [dock.SETTINGS])
        self.assertEqual(self.dock.current(), dock.HOME)
        self.dock.set_current(dock.SETTINGS, animate=False)
        self.assertEqual(self.dock.highlight_rect(), self.dock.slot_rect(dock.SETTINGS))

    def test_reduced_motion_jumps_the_highlight(self) -> None:
        holo.set_reduced_motion(True)
        self.dock.set_current(dock.LIVE)
        self.assertFalse(self.dock.is_sliding())
        self.assertEqual(self.dock.highlight_rect(), self.dock.slot_rect(dock.LIVE))

    # ---- keyboard ----------------------------------------------------------------

    def test_one_tab_stop_on_the_current_slot(self) -> None:
        tab = [key for key in dock.SLOTS if self.dock.button(key).focusPolicy() & Qt.FocusPolicy.TabFocus]
        self.assertEqual(tab, [dock.HOME])
        self.dock.set_current(dock.LIVE, animate=False)
        tab = [key for key in dock.SLOTS if self.dock.button(key).focusPolicy() & Qt.FocusPolicy.TabFocus]
        self.assertEqual(tab, [dock.LIVE])

    def test_arrows_home_end_move_focus(self) -> None:
        self.focus(dock.HOME)
        QTest.keyClick(self.dock.button(dock.HOME), Qt.Key.Key_Down)
        self.assertTrue(self.dock.button(dock.BRIEFING).hasFocus())
        self.assertEqual(self.dock.roving(), dock.BRIEFING)
        QTest.keyClick(self.dock.button(dock.BRIEFING), Qt.Key.Key_End)
        self.assertTrue(self.dock.button(dock.SETTINGS).hasFocus())
        QTest.keyClick(self.dock.button(dock.SETTINGS), Qt.Key.Key_Down)
        self.assertTrue(self.dock.button(dock.SETTINGS).hasFocus())          # no wrap
        QTest.keyClick(self.dock.button(dock.SETTINGS), Qt.Key.Key_Up)
        self.assertTrue(self.dock.button(dock.MEETINGS).hasFocus())
        QTest.keyClick(self.dock.button(dock.MEETINGS), Qt.Key.Key_Home)
        self.assertTrue(self.dock.button(dock.HOME).hasFocus())
        self.assertEqual(self.activated, [])
        tab = [key for key in dock.SLOTS if self.dock.button(key).focusPolicy() & Qt.FocusPolicy.TabFocus]
        self.assertEqual(tab, [dock.HOME])

    def test_enter_and_space_open(self) -> None:
        self.focus(dock.HOME)
        QTest.keyClick(self.dock.button(dock.HOME), Qt.Key.Key_Down)
        QTest.keyClick(self.dock.button(dock.BRIEFING), Qt.Key.Key_Return)
        QTest.keyClick(self.dock.button(dock.BRIEFING), Qt.Key.Key_Down)
        QTest.keyClick(self.dock.button(dock.APPROVALS), Qt.Key.Key_Space)
        self.assertEqual(self.activated, [dock.BRIEFING, dock.APPROVALS])
        self.assertEqual(self.dock.current(), dock.APPROVALS)

    def test_keyboard_focus_shows_the_ring_and_the_tooltip_at_once(self) -> None:
        self.focus(dock.APPROVALS)
        self.assertTrue(self.dock.button(dock.APPROVALS).has_focus_ring())
        self.assertEqual(self.dock.tooltip_for(), dock.APPROVALS)
        self.assertEqual(self.dock.tooltip().text(), ("Approvals", "CTRL+3"))
        self.assertIs(self.dock.tooltip().parent(), self.window)
        anchor = self.dock.button(dock.APPROVALS).mapTo(self.window, self.dock.button(dock.APPROVALS).rect().topRight())
        self.assertEqual(self.dock.tooltip().x(), anchor.x() + 1 + 10)
        self.dock.button(dock.APPROVALS).clearFocus()
        settle()
        self.assertIsNone(self.dock.tooltip_for())

    def test_hover_tooltip_waits_400_ms(self) -> None:
        button = self.dock.button(dock.FOLLOWUPS)
        QApplication.sendEvent(button, QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
        wait(200)
        self.assertIsNone(self.dock.tooltip_for())
        wait(320)
        self.assertEqual(self.dock.tooltip_for(), dock.FOLLOWUPS)
        self.assertEqual(self.dock.tooltip().text(), ("Follow-ups", "CTRL+6"))

    # ---- badges and names -----------------------------------------------------------

    def test_badges_carry_their_signal_in_the_name(self) -> None:
        names = self.dock.accessible_name
        self.assertEqual([names(k) for k in dock.SLOTS], [dock.LABELS[k] for k in dock.SLOTS])
        self.dock.set_briefing_new(True)
        self.dock.set_approvals(2)
        self.dock.set_followups(2)
        self.dock.set_memory_suggestions(1)
        self.dock.set_live_activity(dock.LIVE_WORKING)
        self.assertEqual(names(dock.BRIEFING), "Briefing, new")
        self.assertEqual(names(dock.APPROVALS), "Approvals, 2 waiting")
        self.assertEqual(names(dock.FOLLOWUPS), "Follow-ups, 2 waiting on you")
        self.assertEqual(names(dock.MEMORY), "Memory, 1 suggestion")
        self.dock.set_memory_new(True)
        self.assertEqual(names(dock.MEMORY), "Memory, 1 suggestion")          # suggestions first
        self.dock.set_memory_suggestions(0)
        self.assertEqual(names(dock.MEMORY), "Memory, new")                   # a saved note: no suggestion
        self.assertTrue(self.dock.button(dock.MEMORY).has_dot())
        self.dock.set_memory_new(False)
        self.assertEqual(names(dock.MEMORY), "Memory")
        self.dock.set_memory_suggestions(1)
        self.assertEqual(names(dock.LIVE), "Live, Jarvis is working")
        self.assertEqual(self.dock.button(dock.APPROVALS).accessibleName(), "Approvals, 2 waiting")
        self.dock.set_memory_suggestions(3)
        self.dock.set_live_activity(dock.LIVE_HOLD)
        self.assertEqual(names(dock.MEMORY), "Memory, 3 suggestions")
        self.assertEqual(names(dock.LIVE), "Live, Jarvis is sending")   # a Send's hidden hold: no countdown
        self.dock.set_approvals(0)
        self.dock.set_live_activity("unknown")
        self.assertEqual((names(dock.APPROVALS), names(dock.LIVE)), ("Approvals", "Live"))
        self.assertFalse(self.dock.button(dock.LIVE).has_dot())

    def test_counts_pop_only_when_they_arrive(self) -> None:
        approvals = self.dock.button(dock.APPROVALS)
        self.dock.set_approvals(1)
        self.assertTrue(approvals.is_popping())
        wait(450)
        self.dock.set_approvals(12)
        self.assertFalse(approvals.is_popping())
        self.assertEqual(approvals.count(), 12)
        holo.set_reduced_motion(True)
        self.dock.set_followups(2)
        self.assertFalse(self.dock.button(dock.FOLLOWUPS).is_popping())

    def test_new_dots_ping_on_arrival_but_live_stays_still(self) -> None:
        self.dock.set_briefing_new(True)
        self.assertTrue(self.dock.button(dock.BRIEFING).is_pinging())
        self.dock.set_live_activity(dock.LIVE_WORKING)
        self.assertTrue(self.dock.button(dock.LIVE).has_dot())
        self.assertFalse(self.dock.button(dock.LIVE).is_pinging())
        self.assertFalse(self.dock.blinking())
        self.dock.set_live_activity(dock.LIVE_HOLD)            # a Send's hidden hold: a still dot, no blink
        self.assertTrue(self.dock.button(dock.LIVE).has_dot())
        self.assertFalse(self.dock.blinking())

    def test_home_slot_shows_the_orb_state(self) -> None:
        self.dock.set_orb_look(orb.NEEDS)
        self.assertEqual(self.dock.mini_orb().state(), orb.NEEDS)
        self.assertIn("waiting for your decision", self.dock.button(dock.HOME).accessibleDescription())
        self.assertEqual(self.dock.button(dock.HOME).accessibleName(), "Home")

    # ---- hidden slots -------------------------------------------------------------------

    def test_hidden_slots_leave_the_rail_and_the_keyboard_order(self) -> None:
        self.dock.set_slot_visible(dock.MEMORY, False)
        self.dock.set_slot_visible(dock.FOLLOWUPS, False)
        self.dock.set_slot_visible(dock.MEETINGS, False)
        self.dock.set_slot_visible(dock.HOME, False)                        # Home cannot be hidden
        self.assertEqual(self.dock.visible_slots(),
                         [dock.HOME, dock.BRIEFING, dock.APPROVALS, dock.LIVE, dock.SETTINGS])
        self.focus(dock.LIVE)
        QTest.keyClick(self.dock.button(dock.LIVE), Qt.Key.Key_Down)
        self.assertTrue(self.dock.button(dock.SETTINGS).hasFocus())
        self.dock.activate(dock.MEMORY)
        self.assertEqual(self.activated, [])
        self.dock.set_slot_visible(dock.MEMORY, True)
        self.assertIn(dock.MEMORY, self.dock.visible_slots())

    def test_a_hidden_current_slot_loses_its_highlight(self) -> None:
        self.dock.set_current(dock.FOLLOWUPS, animate=False)
        self.focus(dock.FOLLOWUPS)
        self.dock.set_slot_visible(dock.FOLLOWUPS, False)
        self.assertIsNone(self.dock.highlight_rect())
        self.assertNotEqual(self.dock.roving(), dock.FOLLOWUPS)
        self.assertTrue(self.dock.button(self.dock.roving()).hasFocus())

    # ---- shortcuts ------------------------------------------------------------------------

    def test_shortcuts_open_visible_slots(self) -> None:
        shortcuts = self.dock.install_shortcuts(self.window)
        keys = [s.key() for s in shortcuts]
        self.assertEqual(keys, [QKeySequence(dock.SHORTCUTS[k]) for k in dock.SLOTS])
        self.assertTrue(all(s.context() == Qt.ShortcutContext.WindowShortcut for s in shortcuts))
        shortcuts[dock.SLOTS.index(dock.APPROVALS)].activated.emit()
        self.dock.set_slot_visible(dock.FOLLOWUPS, False)
        shortcuts[dock.SLOTS.index(dock.FOLLOWUPS)].activated.emit()
        self.assertEqual(self.activated, [dock.APPROVALS])

    def test_paints(self) -> None:
        self.dock.set_approvals(3)
        self.dock.set_briefing_new(True)
        self.focus(dock.LIVE)
        self.assertFalse(self.window.grab().isNull())


if __name__ == "__main__":
    unittest.main()
