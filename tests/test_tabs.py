"""Tests for the old tab strip widget (hud.TabStrip, no longer in the app since the hologram's Dock), the
conversation (hud.ConversationLog), and how the assistant screen (ui.ReadingView) holds its screens: the
Dock and its shortcuts, the panel that moves between Briefing, Live and Memory, the strip caption and
SECTIONS, and the conversation in Home's Transcript drawer.

Qt runs offscreen in this process with the bundled fonts and shows nothing; every text is invented.
"""

from __future__ import annotations

import time
import unittest
from datetime import datetime

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFontMetricsF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from briefing_reader import briefing_view, config, dock, home, hud, live_view, ui

_app: QApplication | None = None
WHEN = datetime(2026, 10, 8, 14, 41)
LONG_REPLY = ("Right, sir. I've lined up moving Project sync from Thursday at two to Friday at two, because it "
              "clashes with your Studio Weekly Meeting and the Northwind review on Thursday afternoon, and I drafted "
              "a short note to Ana Example letting her know about the change. Both are waiting for your OK under "
              "NEEDS YOUR OK. One question before you approve: should the note also mention that the slides "
              "will be shared on Friday morning? Note: the Northwind review has no room booked yet, so I left it "
              "where it is; tell me if you want me to look for a slot next week instead, or leave it for "
              "Ana to sort out. End of reply.")


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_tabs", "-platform", "offscreen"])
    hud.load_fonts(config.PROJECT_ROOT / "fonts")


def settle(rounds: int = 4) -> None:
    for _ in range(rounds):
        QApplication.processEvents()


class TabStripTests(unittest.TestCase):
    def strip(self, width: int = 400) -> hud.TabStrip:
        strip = hud.TabStrip()
        self.addCleanup(strip.deleteLater)
        strip.resize(width, hud.TabStrip.HEIGHT)
        strip.show()
        settle()
        return strip

    def test_starts_on_jarvis_and_switches(self) -> None:
        strip = self.strip()
        seen: list[int] = []
        strip.currentChanged.connect(seen.append)
        self.assertEqual(strip.current(), hud.TAB_JARVIS)
        self.assertTrue(strip.tab(hud.TAB_JARVIS).isChecked())
        QTest.mouseClick(strip.tab(hud.TAB_BRIEFING), Qt.MouseButton.LeftButton)
        self.assertEqual(strip.current(), hud.TAB_BRIEFING)
        self.assertTrue(strip.tab(hud.TAB_BRIEFING).isChecked())
        self.assertFalse(strip.tab(hud.TAB_JARVIS).isChecked())
        QTest.mouseClick(strip.tab(hud.TAB_BRIEFING), Qt.MouseButton.LeftButton)   # stays current
        self.assertTrue(strip.tab(hud.TAB_BRIEFING).isChecked())
        strip.set_current(hud.TAB_JARVIS)
        strip.set_current(hud.TAB_JARVIS)
        self.assertEqual(seen, [hud.TAB_BRIEFING, hud.TAB_JARVIS])
        strip.set_current(7)   # not a tab: ignored
        self.assertEqual(strip.current(), hud.TAB_JARVIS)

    def test_left_and_right_move_between_focused_tabs(self) -> None:
        strip = self.strip()
        strip.tab(hud.TAB_JARVIS).setFocus(Qt.FocusReason.TabFocusReason)
        QTest.keyClick(strip.tab(hud.TAB_JARVIS), Qt.Key.Key_Right)
        self.assertEqual(strip.current(), hud.TAB_BRIEFING)
        QTest.keyClick(strip.tab(hud.TAB_BRIEFING), Qt.Key.Key_Right)   # stays at the end
        self.assertEqual(strip.current(), hud.TAB_BRIEFING)
        QTest.keyClick(strip.tab(hud.TAB_BRIEFING), Qt.Key.Key_Left)
        self.assertEqual(strip.current(), hud.TAB_JARVIS)

    def test_new_badge_and_accessible_names(self) -> None:
        strip = self.strip()
        briefing = strip.tab(hud.TAB_BRIEFING)
        self.assertEqual(briefing.accessibleName(), "Briefing transcript")
        self.assertEqual(strip.tab(hud.TAB_JARVIS).accessibleName(), "Jarvis conversation")
        plain = briefing.sizeHint().width()
        strip.set_badge(True)
        self.assertTrue(strip.badge())
        self.assertEqual(briefing.accessibleName(), "Briefing transcript, new briefing")
        self.assertIn("new briefing", briefing.toolTip())
        self.assertGreater(briefing.sizeHint().width(), plain + 20)   # room for the NEW pill
        badge = briefing.badge_rect()
        self.assertFalse(badge.isEmpty())
        self.assertGreater(badge.width(), badge.height())               # a pill, not a dot
        strip.set_badge(False)
        self.assertFalse(strip.badge())
        self.assertTrue(briefing.badge_rect().isEmpty())
        self.assertEqual(briefing.sizeHint().width(), plain)

    def test_unread_dot_only_while_jarvis_is_not_current(self) -> None:
        strip = self.strip()
        strip.set_unread(True)
        self.assertFalse(strip.unread())             # JARVIS is current: nothing to flag
        strip.set_current(hud.TAB_BRIEFING)
        strip.set_unread(True)
        self.assertTrue(strip.unread())
        self.assertIn("new entries", strip.tab(hud.TAB_JARVIS).accessibleName())
        strip.set_current(hud.TAB_JARVIS)
        self.assertFalse(strip.unread())             # cleared when JARVIS becomes current

    def test_compact_below_220_px(self) -> None:
        strip = self.strip(400)
        strip.set_badge(True)
        self.assertFalse(strip.is_compact())
        wide = strip.tab(hud.TAB_BRIEFING).sizeHint().width()
        strip.resize(200, hud.TabStrip.HEIGHT)
        settle()
        self.assertTrue(strip.is_compact())
        badge = strip.tab(hud.TAB_BRIEFING).badge_rect()
        self.assertEqual((badge.width(), badge.height()), (8.0, 8.0))   # the pill became a dot
        self.assertLess(strip.tab(hud.TAB_BRIEFING).sizeHint().width(), wide)
        self.assertIn("new briefing", strip.tab(hud.TAB_BRIEFING).accessibleName())
        strip.set_compact(False)   # by hand: no longer follows the width
        strip.resize(150, hud.TabStrip.HEIGHT)
        settle()
        self.assertFalse(strip.is_compact())

    def test_focused_tab_keeps_space(self) -> None:
        strip = self.strip()
        self.assertTrue(ui._keeps_space(strip.tab(hud.TAB_BRIEFING)))

    def test_painting(self) -> None:
        strip = self.strip()
        strip.set_badge(True)
        strip.set_current(hud.TAB_BRIEFING)
        strip.set_unread(True)
        image = strip.grab().toImage()
        self.assertFalse(image.isNull())
        bottom = image.pixelColor(QPoint(strip.tab(hud.TAB_BRIEFING).geometry().center().x(), image.height() - 1))
        self.assertGreater(bottom.blue(), 150)   # the current tab's accent underline


class LiveTabStripTests(unittest.TestCase):
    def strip(self, width: int = 400) -> hud.TabStrip:
        strip = hud.TabStrip(live=True)
        self.addCleanup(strip.deleteLater)
        strip.resize(width, hud.TabStrip.HEIGHT)
        strip.show()
        settle()
        return strip

    def test_the_default_strip_has_no_live_tab(self) -> None:
        strip = hud.TabStrip()
        self.addCleanup(strip.deleteLater)
        self.assertFalse(strip.has_live())
        strip.set_current(hud.TAB_LIVE)
        self.assertEqual(strip.current(), hud.TAB_JARVIS)
        strip.set_live_visible(True)          # nothing to show
        strip.set_activity(hud.ACTIVITY_WORKING)
        self.assertEqual(strip.activity(), "")
        self.assertEqual(strip.natural_width(), strip.tab(hud.TAB_JARVIS).natural_width()
                         + strip.tab(hud.TAB_BRIEFING).natural_width())

    def test_three_tabs_and_left_right_over_them(self) -> None:
        strip = self.strip()
        live = strip.tab(hud.TAB_LIVE)
        self.assertEqual((live.text(), live.accessibleName()), ("Live", "Live steps"))
        seen: list[int] = []
        strip.currentChanged.connect(seen.append)
        strip.tab(hud.TAB_JARVIS).setFocus(Qt.FocusReason.TabFocusReason)
        for _ in range(3):
            QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Right)
        self.assertEqual(strip.current(), hud.TAB_LIVE)          # it stays at the end
        QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Left)
        self.assertEqual(seen, [hud.TAB_BRIEFING, hud.TAB_LIVE, hud.TAB_BRIEFING])
        QTest.mouseClick(live, Qt.MouseButton.LeftButton)
        self.assertEqual(strip.current(), hud.TAB_LIVE)

    def test_a_hidden_live_tab_is_skipped_and_never_current(self) -> None:
        strip = self.strip()
        strip.set_current(hud.TAB_LIVE)
        strip.set_live_visible(False)
        self.assertFalse(strip.live_visible())
        self.assertEqual(strip.current(), hud.TAB_JARVIS)       # it was current: JARVIS instead
        strip.set_current(hud.TAB_LIVE)
        self.assertEqual(strip.current(), hud.TAB_JARVIS)
        strip.set_current(hud.TAB_BRIEFING)
        strip.step(1)
        self.assertEqual(strip.current(), hud.TAB_BRIEFING)     # Right skips the hidden tab
        strip.set_live_visible(True)
        strip.step(1)
        self.assertEqual(strip.current(), hud.TAB_LIVE)

    def test_activity_dot_names_tooltips_and_blink(self) -> None:
        strip = self.strip()
        live = strip.tab(hud.TAB_LIVE)
        plain = live.sizeHint().width()
        strip.set_activity(hud.ACTIVITY_UNREAD)
        self.assertEqual(strip.activity(), hud.ACTIVITY_UNREAD)
        self.assertEqual(live.accessibleName(), "Live steps, new steps")
        self.assertEqual(live.toolTip(), "New steps")
        self.assertEqual(live.badge_rect().width(), 6.0)
        self.assertGreater(live.sizeHint().width(), plain)
        self.assertFalse(hud._blink_clock().active())           # a steady dot
        strip.set_activity(hud.ACTIVITY_WORKING)
        self.assertEqual(live.accessibleName(), "Live steps, Jarvis is working")
        self.assertEqual(live.toolTip(), "Jarvis is working - see every step")
        self.assertTrue(hud._blink_clock().active())            # the dot blinks
        strip.set_current(hud.TAB_LIVE)                          # cleared when LIVE becomes current
        self.assertEqual(strip.activity(), "")
        self.assertFalse(hud._blink_clock().active())
        strip.set_activity(hud.ACTIVITY_WORKING)                 # none while LIVE is current
        self.assertEqual(strip.activity(), "")
        self.assertEqual(live.accessibleName(), "Live steps")
        image = strip.grab().toImage()
        self.assertFalse(image.isNull())

    def test_compact_below_the_natural_width(self) -> None:
        strip = self.strip(400)
        natural = strip.natural_width()
        self.assertGreater(natural, hud.TabStrip.COMPACT_BELOW)
        strip.resize(natural, hud.TabStrip.HEIGHT)
        settle()
        self.assertFalse(strip.is_compact())
        strip.set_badge(True)
        strip.set_activity(hud.ACTIVITY_UNREAD)
        settle()
        self.assertFalse(strip.is_compact())                    # badges never flip it
        for index in (hud.TAB_JARVIS, hud.TAB_BRIEFING, hud.TAB_LIVE):
            tab = strip.tab(index)
            self.assertGreaterEqual(tab.width(), tab.sizeHint().width())
        strip.resize(natural - 1, hud.TabStrip.HEIGHT)
        settle()
        self.assertTrue(strip.is_compact())
        strip.set_live_visible(False)                            # two tabs: the old 220 px rule
        self.assertFalse(strip.is_compact())


class MemoryTabStripTests(unittest.TestCase):
    """The productivity bundle's MEMORY tab: the 4th, hidden until set_memory_visible(True)."""

    def strip(self, width: int = 500, *, visible: bool = True) -> hud.TabStrip:
        strip = hud.TabStrip(live=True, memory=True)
        self.addCleanup(strip.deleteLater)
        strip.set_memory_visible(visible)
        strip.resize(width, hud.TabStrip.HEIGHT)
        strip.show()
        settle()
        return strip

    def test_four_tabs_in_order(self) -> None:
        strip = self.strip()
        self.assertTrue(strip.has_memory())
        self.assertEqual([strip.tab(index).text() for index in hud.TABS], ["Jarvis", "Briefing", "Live", "Memory"])
        memory = strip.tab(hud.TAB_MEMORY)
        self.assertEqual((memory.accessibleName(), memory.toolTip()), ("Memory notes", hud.MEMORY_TAB_TIP))
        strip.tab(hud.TAB_JARVIS).setFocus(Qt.FocusReason.TabFocusReason)
        for _ in range(4):
            QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Right)
        self.assertEqual(strip.current(), hud.TAB_MEMORY)
        QTest.mouseClick(strip.tab(hud.TAB_BRIEFING), Qt.MouseButton.LeftButton)
        QTest.mouseClick(memory, Qt.MouseButton.LeftButton)
        self.assertEqual(strip.current(), hud.TAB_MEMORY)

    def test_hidden_by_default_skipped_and_never_current(self) -> None:
        strip = self.strip(visible=False)
        self.assertFalse(strip.memory_visible())
        self.assertFalse(strip.tab(hud.TAB_MEMORY).isVisible())
        strip.set_current(hud.TAB_MEMORY)
        self.assertEqual(strip.current(), hud.TAB_JARVIS)
        strip.set_current(hud.TAB_LIVE)
        strip.step(1)
        self.assertEqual(strip.current(), hud.TAB_LIVE)          # Right skips the hidden tab
        strip.set_memory_visible(True)
        strip.step(1)
        self.assertEqual(strip.current(), hud.TAB_MEMORY)
        strip.set_memory_visible(False)                          # hidden while current: JARVIS instead
        self.assertEqual(strip.current(), hud.TAB_JARVIS)
        plain = hud.TabStrip(live=True)                          # no memory: nothing to show or set
        self.addCleanup(plain.deleteLater)
        plain.set_memory_visible(True)
        plain.set_memory_unread(True)
        self.assertFalse(plain.has_memory() or plain.memory_visible() or plain.memory_unread())

    def test_unread_dot_while_another_tab_is_current(self) -> None:
        strip = self.strip()
        strip.set_memory_unread(True)
        self.assertTrue(strip.memory_unread())
        self.assertIn("new entries", strip.tab(hud.TAB_MEMORY).accessibleName())
        strip.set_current(hud.TAB_MEMORY)
        self.assertFalse(strip.memory_unread())                  # cleared when MEMORY becomes current
        strip.set_memory_unread(True)
        self.assertFalse(strip.memory_unread())                  # none while current

    def test_compact_then_tight_when_four_do_not_fit(self) -> None:
        strip = self.strip(500)
        natural, compact = strip.natural_width(), strip.compact_width()
        self.assertLess(compact, natural)
        for width, expected in ((natural, (False, False)), (natural - 1, (True, False)), (compact, (True, False)),
                                (compact - 1, (True, True))):
            with self.subTest(width=width):
                strip.resize(width, hud.TabStrip.HEIGHT)
                settle()
                self.assertEqual((strip.is_compact(), strip.is_tight()), expected)
        strip.set_badge(True)
        strip.set_unread(True)
        strip.set_activity(hud.ACTIVITY_UNREAD)
        strip.set_current(hud.TAB_BRIEFING)
        strip.set_memory_unread(True)
        settle()
        right = max(strip.tab(index).geometry().right() for index in hud.TABS)
        self.assertLessEqual(right, strip.width())               # every badge shown and still whole
        strip.set_memory_visible(False)                          # three tabs: no longer tight
        self.assertFalse(strip.is_tight())


class ConversationLogTests(unittest.TestCase):
    def log(self, width: int = 320, height: int = 240) -> hud.ConversationLog:
        log = hud.ConversationLog()
        self.addCleanup(log.deleteLater)
        log.resize(width, height)
        log.show()
        settle()
        return log

    def test_empty_state_and_roles(self) -> None:
        log = self.log()
        self.assertTrue(log.empty_label.isVisible())
        self.assertEqual(log.empty_label.text(), hud.CONVERSATION_EMPTY_TEXT)
        first = log.add(hud.ROLE_YOU, "move my sync to Friday", when=WHEN)
        second = log.add(hud.ROLE_JARVIS, "Right, sir.", when=WHEN, sub="2 proposals")
        settle()
        self.assertFalse(log.empty_label.isVisible())
        self.assertNotEqual(first, second)
        self.assertEqual(log.entries(), [("you", "2:41 PM", "move my sync to Friday", ""),
                                         ("jarvis", "2:41 PM", "Right, sir.", "2 proposals")])
        widget = log.entry(second)
        self.assertEqual(widget.role_label.text(), "JARVIS")
        self.assertEqual(log.entry(first).role_label.text(), "YOU")
        log.set_hour24(True)
        log.add(hud.ROLE_JARVIS, "Later.", when=datetime(2026, 10, 8, 21, 5))
        self.assertEqual(log.entries()[-1][1], "21:05")
        log.clear()
        self.assertEqual(log.entries(), [])
        self.assertFalse(log.empty_label.isHidden())

    def test_markup_stays_literal(self) -> None:
        log = self.log()
        text = '<b>bold</b> <img src="file://203.0.113.9/x.png"> & more'
        entry = log.entry(log.add(hud.ROLE_JARVIS, text, when=WHEN))
        self.assertEqual(entry.text_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(entry.text_label.text(), text)
        self.assertEqual(log.entries()[0][2], text)

    def test_a_600_character_reply_is_shown_whole(self) -> None:
        self.assertGreaterEqual(len(LONG_REPLY), 600)
        log = self.log(300, 200)
        entry = log.entry(log.add(hud.ROLE_JARVIS, LONG_REPLY, when=WHEN, tone=hud.TONE_WARN))
        settle(6)
        label = entry.text_label
        self.assertEqual(label.text(), LONG_REPLY)                         # never elided
        self.assertTrue(label.wordWrap())
        self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()) - 1)
        lines = label.height() / QFontMetricsF(label.font()).lineSpacing()
        self.assertGreater(lines, 8)                                       # many lines, all of them shown
        bar = log.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)                               # the area scrolls
        # Taller than the view: shown from its start (the "JARVIS  time" header at the top), the rest
        # a scroll away; never cut off at the top.
        self.assertLess(bar.value(), bar.maximum())
        self.assertEqual(bar.value(), max(0, entry.y() - 6))
        self.assertGreaterEqual(entry.role_label.mapTo(log.viewport(), entry.role_label.rect().topLeft()).y(), 0)
        self.assertTrue(label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse)

    def test_a_long_reply_after_others_is_followed_from_its_header(self) -> None:
        log = self.log(300, 200)
        for number in range(6):
            log.add(hud.ROLE_YOU if number % 2 else hud.ROLE_JARVIS, f"Short entry {number}.", when=WHEN)
        long_id = log.add(hud.ROLE_JARVIS, LONG_REPLY, when=WHEN)
        settle(6)
        bar = log.verticalScrollBar()
        entry = log.entry(long_id)
        self.assertGreater(entry.height(), log.viewport().height())
        self.assertEqual(bar.value(), entry.y() - 6)                       # its header is the first line seen
        self.assertTrue(log.following())
        log.resize(300, 260)                                               # a taller view: still from its start
        settle(6)
        self.assertEqual(bar.value(), min(bar.maximum(), entry.y() - 6))
        log.add(hud.ROLE_JARVIS, "Done, sir.", when=WHEN)                  # a short one: the bottom again
        settle(6)
        self.assertEqual(bar.value(), bar.maximum())

    def test_a_group_is_followed_from_its_first_entry(self) -> None:
        """follow_from: a question and its choices posted together are read from the question (never
        a choice's bare link at the top of the view); the next entry or the owner's scroll ends it."""
        log = self.log(300, 200)
        for number in range(4):
            log.add(hud.ROLE_JARVIS, f"Short entry {number}.", when=WHEN)
        lead = log.add(hud.ROLE_JARVIS, "Which meeting shall I prepare, sir? Nothing is read until you pick one.",
                       when=WHEN)
        choices = [log.add(hud.ROLE_JARVIS, f"Meeting {number} at {number}:00 PM (work)", when=WHEN,
                           link_text="Prep this one", link_id=f"prep-choice:{number}") for number in (1, 2, 3)]
        log.follow_from(lead)
        settle(6)
        bar = log.verticalScrollBar()
        first, last = log.entry(lead), log.entry(choices[-1])
        self.assertGreater(last.y() + last.height() - first.y(), log.viewport().height())   # the group: too tall
        self.assertEqual(bar.value(), first.y() - 6)                       # the question's header first
        self.assertTrue(log.following())
        for number, entry_id in zip((1, 2, 3), choices):                    # each link names its meeting
            self.assertEqual(log.entry(entry_id).link_button.accessibleName(),
                             f"Prep this one: Meeting {number} at {number}:00 PM (work)")
        self.assertEqual(first.link_button.accessibleName(), "")
        log.resize(300, 260)                                               # a taller view: still from the question
        settle(6)
        self.assertEqual(bar.value(), min(bar.maximum(), first.y() - 6))
        log.add(hud.ROLE_JARVIS, "Done, sir.", when=WHEN)                  # the newest entry alone again
        settle(6)
        self.assertEqual(bar.value(), bar.maximum())
        short = log.add(hud.ROLE_JARVIS, "Is this the meeting you mean, sir?", when=WHEN)
        log.add(hud.ROLE_JARVIS, "Meeting 4 at 4:00 PM (work)", when=WHEN, link_text="Prep this one", link_id="p:4")
        log.follow_from(short)                                             # a group that fits: the bottom
        settle(6)
        self.assertEqual(bar.value(), bar.maximum())
        log.follow_from(lead)
        settle(6)
        self.assertLess(bar.value(), bar.maximum())
        bar.triggerAction(bar.SliderAction.SliderToMinimum)                 # the owner's scroll ends it
        settle()
        self.assertEqual(log._anchor, 0)
        log.follow_from(9999)                                              # a gone entry: the newest alone
        self.assertEqual(log._anchor, 0)
        # A link's accessible name takes the entry's first line only, capped: a brief's "See every
        # step" never reads the whole brief again (the entry itself already carries it).
        brief = log.add(hud.ROLE_JARVIS, "Brief: Meeting 5 - today 5:00-5:30 PM (work)\nWho: Ann (accepted)\n"
                        "Last threads:\n- Ann - \"Budget\", Oct 5: the numbers. Waiting on you.", when=WHEN,
                        link_text="See every step", link_id="live:7")
        name = log.entry(brief).link_button.accessibleName()
        self.assertEqual(name, "See every step: Brief: Meeting 5 - today 5:00-5:30 PM (work)")
        self.assertNotIn("\n", name)
        long_line = log.add(hud.ROLE_JARVIS, "x" * 300 + "\nmore", when=WHEN, link_text="See every step", link_id="l")
        self.assertEqual(log.entry(long_line).link_button.accessibleName(), "See every step: " + "x" * 120)

    def test_tones(self) -> None:
        log = self.log()
        for tone, color in ((hud.TONE_DONE, hud.TEXT_BODY), (hud.TONE_WARN, hud.AMBER), (hud.TONE_ERROR, hud.RED),
                            (hud.TONE_IDLE, hud.TEXT_MUTED), (hud.TONE_GOOD, hud.GREEN)):
            with self.subTest(tone=tone):
                entry = log.entry(log.add(hud.ROLE_JARVIS, "x", when=WHEN, tone=tone))
                qcolor = hud.QColor(color)
                rgba = f"rgba({qcolor.red()}, {qcolor.green()}, {qcolor.blue()}, 255)"
                self.assertIn(rgba, entry.text_label.styleSheet())
                self.assertEqual(entry.tone(), tone)
        self.assertIn(hud.TONE_GOOD, hud.TONES)

    def test_update_text_sub_and_link(self) -> None:
        log = self.log()
        entry_id = log.add(hud.ROLE_JARVIS, "Good morning, sir.", when=WHEN)
        entry = log.entry(entry_id)
        self.assertTrue(entry.link_button.isHidden())
        self.assertTrue(log.update(entry_id, text="Good morning, sir. Your AM briefing is ready to view.",
                                   link_text="View briefing", link_id="view-briefing"))
        self.assertEqual(log.entries()[0][2], "Good morning, sir. Your AM briefing is ready to view.")
        self.assertFalse(entry.link_button.isHidden())
        self.assertEqual(entry.link(), ("View briefing", "view-briefing"))
        clicked: list[str] = []
        log.linkClicked.connect(clicked.append)
        entry.link_button.click()
        self.assertEqual(clicked, ["view-briefing"])
        log.update(entry_id, sub="spoken")
        self.assertEqual(log.entries()[0][3], "spoken")
        self.assertFalse(log.update(9999, text="gone"))
        log.update()   # QWidget.update(): a repaint, not an entry change
        log.update(log.rect())

    def test_cap_of_100_entries(self) -> None:
        log = self.log()
        first = log.add(hud.ROLE_JARVIS, "entry 0", when=WHEN)
        for number in range(1, 105):
            log.add(hud.ROLE_JARVIS, f"entry {number}", when=WHEN)
        self.assertEqual(hud.ConversationLog.MAX_ENTRIES, 100)
        self.assertEqual(len(log.entries()), 100)
        self.assertEqual(log.entries()[0][2], "entry 5")
        self.assertEqual(log.entries()[-1][2], "entry 104")
        self.assertIsNone(log.entry(first))
        self.assertFalse(log.update(first, text="gone"))

    def test_follows_the_newest_entry_unless_the_owner_scrolled(self) -> None:
        log = self.log(300, 160)
        for number in range(12):
            log.add(hud.ROLE_JARVIS, f"Entry number {number} with a few words to wrap.", when=WHEN)
        settle(6)
        bar = log.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        self.assertEqual(bar.value(), bar.maximum())
        # The owner scrolls up: new entries do not pull the view down for 4 s.
        bar.triggerAction(bar.SliderAction.SliderToMinimum)
        settle()
        self.assertEqual(bar.value(), 0)
        self.assertFalse(log.following())
        log.add(hud.ROLE_JARVIS, "Something new.", when=WHEN)
        settle(6)
        self.assertEqual(bar.value(), 0)
        log._user_scrolled_at = time.monotonic() - 5   # the hold has passed
        log.add(hud.ROLE_JARVIS, "And more.", when=WHEN)
        settle(6)
        self.assertEqual(bar.value(), bar.maximum())
        self.assertTrue(log.following())

    def test_entries_added_right_after_the_owner_scrolled_back_down_are_followed(self) -> None:
        # The owner's scroll is judged a moment later (a queued call); entries added before that,
        # not laid out yet, must not move the view first and make it look as if he scrolled away.
        log = self.log(300, 160)
        for number in range(10):
            log.add(hud.ROLE_JARVIS, f"Entry number {number} with a few words to wrap.", when=WHEN)
        settle(6)
        bar = log.verticalScrollBar()
        bar.triggerAction(bar.SliderAction.SliderToMinimum)
        settle()
        self.assertFalse(log.following())
        bar.triggerAction(bar.SliderAction.SliderToMaximum)   # back down, judged after the adds below
        log._user_scrolled_at = time.monotonic() - 5
        for text in ("Done, sir.", "Sent, sir.", "Sorry, sir - I couldn't cancel it."):
            log.add(hud.ROLE_JARVIS, text, when=WHEN)
        settle(6)
        self.assertTrue(log.following())
        self.assertEqual(bar.value(), bar.maximum())

    def test_long_words_can_break(self) -> None:
        log = self.log(200, 200)
        address = "firstname.lastname.longer@departments.example.edu"
        entry = log.entry(log.add(hud.ROLE_JARVIS, f"Sent to {address}.", when=WHEN))
        settle()
        self.assertEqual(entry.text_label.text(), f"Sent to {address}.")
        self.assertLessEqual(entry.text_label.sizeHint().width(), 200)


class ReadingViewScreenTests(unittest.TestCase):
    """The assistant screen in the hologram's round 1 layout: the Dock opens the screens (the old tab
    row and its Ctrl+1..4 are gone, on purpose: HOLOGRAM_SPEC 11.1), the transcript panel moves to the
    Briefing, Live or Memory screen that shows, the conversation lives in Home's Transcript drawer."""

    def window(self, hour24: bool) -> ui.BriefingWindow:
        window = ui.BriefingWindow(10, 30)
        self.addCleanup(window.deleteLater)
        window.set_hour24(hour24)
        window.set_clock(lambda: datetime(2026, 10, 8, 13, 52))
        for name in ("notion", "voice", "calendar"):
            window.header.set_service(name, hud.STATUS_WARN)
        window.reading.set_ask_available(True)
        window.show_reading_view()
        window.resize(ui.READING_MIN_SIZE)
        window.show()
        settle(6)
        return window

    def assert_inside(self, widget, window) -> None:
        corner = widget.mapTo(window, QPoint(widget.width(), widget.height()))
        origin = widget.mapTo(window, QPoint(0, 0))
        self.assertGreaterEqual(origin.x(), 0)
        self.assertGreaterEqual(origin.y(), 0)
        self.assertLessEqual(corner.x(), window.width())
        self.assertLessEqual(corner.y(), window.height())

    def test_the_900_by_600_window_with_both_clocks(self) -> None:
        for hour24 in (False, True):
            with self.subTest(hour24=hour24):
                window = self.window(hour24)
                self.assertEqual((window.width(), window.height()), (900, 600))
                reading = window.reading
                reading.set_new_briefing(True, "Your AM briefing is ready to view")
                settle(4)
                # Home: the Dock, the header, the hero orb, the chip and the command bar, all inside.
                self.assertEqual(reading.current_tab(), ui.SCREEN_HOME)
                for widget in (reading.dock, reading.header, reading.hero, reading.briefing_chip,
                               reading.command_bar, reading.header.close_button, reading.header.clock):
                    self.assertTrue(widget.isVisible())
                    self.assert_inside(widget, window)
                self.assertGreaterEqual(reading.hero.orb_size(), home.ORB_MIN)
                self.assertFalse(reading.conversation.isVisible())     # in the Transcript drawer
                self.assertFalse(reading.transcript_panel.isVisible())  # no panel on Home
                self.assertFalse(window.header.isVisible())             # the hologram's header instead
                # BRIEFING (round 2, HOLOGRAM_SPEC 16.1): the stage - the compact orb and Play, no panel;
                # while it reads, the lens, SECTIONS before the section track and the transport.
                reading.set_tab(hud.TAB_BRIEFING)
                settle(4)
                stage = reading.briefing_stage
                self.assertFalse(reading.transcript_panel.isVisible())
                self.assertFalse(reading.text.isVisible())
                for widget in (stage, stage.orb, reading.play_button):
                    self.assertTrue(widget.isVisible())
                    self.assert_inside(widget, window)
                stage.set_phase(briefing_view.PLAYING)
                settle(4)
                for widget in (reading.play_button, reading.skip_button, reading.everything_button,
                               reading.notion_button, reading.sections_button, stage.track, reading.speech):
                    self.assertTrue(widget.isVisible())
                    self.assert_inside(widget, window)
                self.assertTrue(stage.isAncestorOf(reading.sections_button))
                self.assertFalse(reading.done_button.isVisible())          # Done is the header's close
                stage.set_phase(briefing_view.READY)
                # The conversation is in the drawer: it opens over Home and holds the whole height.
                reading.open_transcript()
                settle(4)
                self.assertEqual(reading.current_tab(), ui.SCREEN_HOME)
                self.assertTrue(reading.conversation.isVisible())
                self.assertGreaterEqual(reading.conversation.height(), 90)
                window.hide()

    def test_a_small_screens_window_keeps_the_controls_whole(self) -> None:
        # A window of 760 x 560 (as on an 808 px screen) and the regular minimum: the Briefing screen's
        # ready view and its reading view (lens, track, transport) stay whole and never overlap.
        window = ui.BriefingWindow(10, 30)
        self.addCleanup(window.deleteLater)
        window.set_clock(lambda: datetime(2026, 10, 8, 13, 52))
        window.show_reading_view()
        window.setMinimumSize(700, 500)
        window.resize(760, 560)
        window.show()
        reading = window.reading
        stage = reading.briefing_stage
        reading.set_header("AM briefing", "Updated today at 10:04 AM")
        reading.set_speech("Alice Chen - Q3 budget review - wants the numbers by Friday, before the check-in "
                           "with the whole project team")
        reading.set_tab(hud.TAB_BRIEFING)
        for size in ((760, 560), (900, 600)):
            window.resize(*size)
            for phase in (briefing_view.READY, briefing_view.PLAYING):
                with self.subTest(size=size, phase=phase):
                    stage.set_phase(phase)
                    settle(8)
                    shown = [widget for widget in (stage.orb, reading.play_button, reading.skip_button,
                                                   reading.everything_button, reading.notion_button,
                                                   reading.sections_button, stage.track, reading.speech)
                             if widget.isVisible()]
                    self.assertEqual(len(shown), 8 if phase == briefing_view.PLAYING else 2)
                    for widget in shown:
                        self.assertTrue(stage.rect().contains(widget.geometry()), type(widget).__name__)
                    if phase == briefing_view.PLAYING:
                        self.assertLess(reading.speech.geometry().bottom(), stage.track.geometry().top())
                        self.assertLess(stage.track.geometry().bottom(), reading.play_button.geometry().top())
                        self.assertGreaterEqual(reading.speech.width(), 200)
        window.hide()

    def test_the_briefing_screen_always_shows_the_stale_warning(self) -> None:
        # Round 2: the Briefing screen's kicker carries the title and "Updated ..."; the stale warning is its
        # quiet line, in the ready view and over the reading lens, at 760 x 560 too.
        window = ui.BriefingWindow(10, 30)
        self.addCleanup(window.deleteLater)
        window.set_clock(lambda: datetime(2026, 10, 8, 13, 52))
        window.reading.set_ask_available(True)
        window.show_reading_view()
        window.setMinimumSize(700, 500)
        window.resize(760, 560)
        window.show()
        reading = window.reading
        stage = reading.briefing_stage
        reading.set_header("AM briefing", "Updated today at 10:04 AM")
        reading.set_tab(hud.TAB_BRIEFING)
        settle(8)
        self.assertEqual(stage.kicker(), "AM BRIEFING \u00b7 UPDATED TODAY AT 10:04 AM")
        self.assertEqual(stage.notice(), ("", False))
        reading.set_header("AM briefing", "Updated today at 10:04 AM", "May be stale - updated yesterday")
        settle(4)
        self.assertEqual(stage.notice(), ("May be stale - updated yesterday", False))
        rects = stage._ready_layout()
        self.assertIn("status", rects)                                     # its own line in the ready view
        self.assertLessEqual(rects["status"].bottom(), reading.play_button.geometry().top())
        reading.set_status("Playing - Your PM briefing is due")
        self.assertEqual(stage.notice()[0], "Your PM briefing is due \u00b7 May be stale - updated yesterday")
        reading.set_status("Playing")                                      # the transport says that itself
        self.assertEqual(stage.notice()[0], "May be stale - updated yesterday")
        stage.set_phase(briefing_view.PLAYING)
        settle(4)
        self.assertGreaterEqual(stage._reading_layout()["lens"].top(), 24 + 20)   # the lens stays under it
        window.hide()

    def test_every_screen_opens_from_the_dock_and_fits(self) -> None:
        window = self.window(False)
        reading = window.reading
        reading.set_memory_available(True)
        reading.set_followups_available(True)
        seen: list[int] = []
        reading.tabChanged.connect(seen.append)
        for size in ((900, 600), (1120, 700)):
            window.resize(*size)
            for slot in dock.SLOTS:
                with self.subTest(size=size, slot=slot):
                    reading.dock.button(slot).click()
                    settle(4)
                    screen = ui._SLOT_SCREENS[slot]
                    self.assertEqual(reading.current_tab(), screen)
                    self.assertEqual(reading.dock.current(), slot)
                    page = reading.screen_widget(screen)
                    self.assertTrue(page.isVisible())
                    self.assert_inside(page, window)
                    self.assertTrue(reading.dock.button(slot).isVisible())
                    self.assert_inside(reading.dock.button(slot), window)
        self.assertEqual(seen[:len(dock.SLOTS)], [ui._SLOT_SCREENS[slot] for slot in dock.SLOTS][1:] + [
            ui.SCREEN_HOME])
        # Round 2: every screen has its own stage (no transcript panel anywhere); the agenda lives in the
        # Day view's drawer.
        reading.set_tab(hud.TAB_MEMORY)
        settle()
        self.assertTrue(reading.memory_screen.isAncestorOf(reading.memory_view))
        self.assertTrue(reading.memory_view.isVisible())
        self.assertFalse(reading.transcript_panel.isVisible())
        reading.set_tab(hud.TAB_LIVE)
        settle()
        self.assertTrue(reading.live_screen.isAncestorOf(reading.live_stage))
        self.assertFalse(reading.transcript_panel.isVisible())
        reading.set_tab(hud.TAB_BRIEFING)
        settle()
        self.assertTrue(reading.briefing_screen.isAncestorOf(reading.briefing_stage))
        self.assertFalse(reading.transcript_panel.isVisible())
        reading.set_tab(ui.SCREEN_APPROVALS)
        settle()
        self.assertTrue(reading.approvals_screen.isAncestorOf(reading.approvals_stage))
        self.assertTrue(reading.approvals_stage.isAncestorOf(reading.approvals))
        reading.set_tab(ui.SCREEN_FOLLOWUPS)
        settle()
        self.assertTrue(reading.followups_screen.isAncestorOf(reading.followups_stage))
        self.assertFalse(reading.followups_screen.isAncestorOf(reading.agenda_panel))
        reading.set_tab(ui.SCREEN_SETTINGS)
        settle()
        self.assertTrue(reading.settings_page.isVisible())
        reading.set_tab(ui.SCREEN_FOLLOWUPS)
        reading.open_day_view()                      # the clock: the Day view over the current screen
        settle()
        self.assertEqual(reading.current_tab(), ui.SCREEN_FOLLOWUPS)
        self.assertTrue(reading.day_drawer.isAncestorOf(reading.agenda_panel))
        self.assertEqual(reading.agenda_panel.mode(), hud.AGENDA_DAY)
        window.hide()

    def test_the_live_screen_its_strip_and_readout(self) -> None:
        """Round 2 (HOLOGRAM_SPEC 16.3): the Live screen is its own stage - the strip (the readout, Pop out,
        Clear), the timeline with ACTIVITY under it and the focal column; no transcript panel."""
        window = self.window(False)
        window.resize(ui.READING_SIZE)
        reading = window.reading
        reading.set_activity_state(hud.ORB_SPEAKING, True)          # the briefing is live
        reading.set_tab(hud.TAB_BRIEFING)
        settle(4)
        self.assertFalse(reading.live_status.isVisible())
        reading.set_tab(hud.TAB_LIVE)
        settle(6)
        stage = reading.live_stage
        self.assertIs(reading.live_page, stage)
        self.assertTrue(stage.isVisible())
        self.assertTrue(reading.live_log.isVisible())
        self.assertTrue(reading.activity_panel.isVisible())       # ACTIVITY's rows stay on Live (spec 11.7)
        self.assertTrue(stage.timeline.isAncestorOf(reading.activity_panel))
        self.assertFalse(reading.transcript_panel.isVisible())
        self.assertFalse(reading.conversation.isVisible())
        self.assertTrue(stage.strip.isAncestorOf(reading.live_status))
        self.assertTrue(reading.live_status.isVisible())
        self.assertEqual(reading.live_status.text(), "IDLE")
        self.assertEqual(reading.live_summary_text(), "Nothing running")
        self.assertEqual(reading.live_status.toolTip(), hud.plain_tooltip("Nothing running"))   # the summary
        self.assertEqual(reading.live_pop_button.text(), ui.POP_OUT_TEXT)           # an icon: its name
        self.assertEqual(reading.live_pop_button.accessibleName(), ui.POP_OUT_TEXT)
        self.assertEqual(reading.live_clear_button.text(), "Clear")
        self.assertEqual(reading.live_clear_button.toolTip(), ui.CLEAR_TIP)
        for button in (reading.live_pop_button, reading.live_clear_button):
            self.assertTrue(button.isVisible())
            self.assertTrue(stage.strip.isAncestorOf(button))
        # The timeline is 300 wide on the left, the focal column beside it, both under the strip.
        self.assertEqual(reading.live_log.width(), live_view.TIMELINE_WIDTH)
        log_top = reading.live_log.mapTo(reading, QPoint(0, 0)).y()
        self.assertGreaterEqual(log_top, stage.strip.mapTo(reading, QPoint(0, stage.strip.height())).y())
        focal = stage.focal.mapTo(reading, QPoint(0, 0))
        self.assertGreater(focal.x(), reading.live_log.mapTo(reading, QPoint(reading.live_log.width(), 0)).x())
        self.assertGreaterEqual(reading.live_room(), ui.LIVE_DOCKED_MIN_PX)
        reading.set_live_popped(True)
        settle()
        self.assertTrue(reading.live_away.isVisible())
        self.assertFalse(reading.live_log.isVisible())
        self.assertEqual(reading.live_away_label.text(), ui.LIVE_AWAY_TEXT)
        self.assertEqual(ui.LIVE_AWAY_TEXT, "LIVE is in its own window, sir.")
        self.assertEqual(reading.live_back_button.text(), "Bring it back")
        self.assertFalse(reading.live_pop_button.isEnabled())
        seen: list[str] = []
        reading.liveDock.connect(lambda: seen.append("dock"))
        reading.liveShowWindow.connect(lambda: seen.append("show"))
        reading.livePopOut.connect(lambda: seen.append("pop"))
        reading.liveClear.connect(lambda: seen.append("clear"))
        reading.live_show_button.click()
        reading.live_back_button.click()
        reading.live_clear_button.click()
        self.assertEqual(seen, ["show", "dock", "clear"])
        reading.set_live_popped(False)
        settle()
        reading.live_pop_button.click()
        self.assertEqual(seen[-1], "pop")
        reading.set_tab(hud.TAB_BRIEFING)
        settle(4)
        self.assertFalse(reading.live_status.isVisible())
        self.assertFalse(reading.live_pop_button.isVisible() or reading.live_clear_button.isVisible())
        # The 900 px window counting down: the readout whole, the buttons whole and inside the strip.
        window.resize(ui.READING_MIN_SIZE)
        reading.set_tab(hud.TAB_LIVE)
        reading.set_live_state(hud.LIVE_COUNTDOWN, "SENDING", deadline=time.monotonic() + 8)
        settle(6)
        self.assertTrue(reading.live_status.text().startswith("SENDING IN "))
        self.assertGreaterEqual(reading.live_status.width(), reading.live_status.sizeHint().width())
        for button in (reading.live_pop_button, reading.live_clear_button):
            self.assertTrue(button.isVisible())
            self.assertGreaterEqual(button.width(), button.sizeHint().width())
        right = reading.live_clear_button.mapTo(stage.strip, QPoint(reading.live_clear_button.width(), 0)).x()
        self.assertLessEqual(right, stage.strip.width())
        reading.set_live_state(hud.LIVE_IDLE)
        settle(6)
        self.assertEqual(reading.live_status.text(), "IDLE")
        window.hide()

    def test_live_unavailable_hides_its_slot(self) -> None:
        window = self.window(False)
        reading = window.reading
        reading.set_tab(hud.TAB_LIVE)
        self.assertEqual(reading.current_tab(), hud.TAB_LIVE)
        reading.set_live_available(False)
        settle()
        self.assertFalse(reading.live_available())
        self.assertFalse(reading.dock.button(dock.LIVE).isVisible())
        self.assertEqual(reading.current_tab(), ui.SCREEN_HOME)
        self.assertFalse(reading.live_page.isVisible())
        window.activateWindow()
        settle()
        QTest.keyClick(window, Qt.Key.Key_4, Qt.KeyboardModifier.ControlModifier)
        settle()
        self.assertEqual(reading.current_tab(), ui.SCREEN_HOME)
        window.hide()

    def test_ctrl_1_to_6_and_ctrl_comma(self) -> None:
        # The Dock's shortcuts replace the tab row's Ctrl+1..4 (JARVIS / BRIEFING / LIVE / MEMORY), on purpose.
        window = self.window(False)
        reading = window.reading
        seen: list[int] = []
        reading.tabChanged.connect(seen.append)
        window.activateWindow()
        settle()
        for key in (Qt.Key.Key_2, Qt.Key.Key_1, Qt.Key.Key_3, Qt.Key.Key_4):
            QTest.keyClick(window, key, Qt.KeyboardModifier.ControlModifier)
            settle()
        self.assertEqual(seen, [hud.TAB_BRIEFING, ui.SCREEN_HOME, ui.SCREEN_APPROVALS, hud.TAB_LIVE])
        QTest.keyClick(window, Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier)
        settle()
        self.assertEqual(reading.current_tab(), ui.SCREEN_SETTINGS)
        self.assertTrue(reading.settings_page.isVisible())
        keys = sorted(shortcut.key().toString() for shortcut in reading.findChildren(ui.QShortcut))
        for key in ("Ctrl+1", "Ctrl+2", "Ctrl+3", "Ctrl+4", "Ctrl+5", "Ctrl+6", "Ctrl+,", "Ctrl+Space"):
            self.assertIn(key, keys)
        QTest.keyClick(window, Qt.Key.Key_Space, Qt.KeyboardModifier.ControlModifier)   # Home, the bar focused
        settle()
        self.assertEqual(reading.current_tab(), ui.SCREEN_HOME)
        self.assertTrue(reading.command_bar.input.hasFocus())
        window.hide()

    def test_ctrl_5_and_the_memory_screen(self) -> None:
        window = self.window(False)
        reading = window.reading
        self.assertFalse(reading.memory_available())             # the bundle off: no Memory
        self.assertFalse(reading.followups_available())
        window.activateWindow()
        settle()
        QTest.keyClick(window, Qt.Key.Key_5, Qt.KeyboardModifier.ControlModifier)
        settle()
        self.assertEqual(reading.current_tab(), ui.SCREEN_HOME)
        reading.set_memory_available(True)
        QTest.keyClick(window, Qt.Key.Key_5, Qt.KeyboardModifier.ControlModifier)
        settle(4)
        self.assertEqual(reading.current_tab(), hud.TAB_MEMORY)
        self.assertTrue(reading.memory_view.isVisible())
        self.assertTrue(reading.memory_screen.isAncestorOf(reading.memory_view))   # its own stage (round 2)
        self.assertFalse(reading.conversation.isVisible())
        self.assertFalse(reading.transcript_panel.isVisible())
        self.assertFalse(reading.sections_button.isVisible())
        window.resize(ui.READING_SIZE)
        settle(6)
        self.assertTrue(reading.memory_view.input.isVisible())     # no suggestion waits: the Add field
        self.assertFalse(reading.memory_view.suggestion_tile.isVisible())
        reading.set_memory_available(False)
        self.assertEqual(reading.current_tab(), ui.SCREEN_HOME)
        window.hide()

    def test_speech_line_falls_back_to_ready_to_view_while_new(self) -> None:
        window = self.window(False)
        reading = window.reading
        reading.set_header("AM briefing", "Updated today at 10:04 AM")
        reading.set_speech("")
        self.assertEqual(reading.speech.text(), "AM briefing")
        reading.set_new_briefing(True, "Your AM briefing is ready to view")
        self.assertEqual(reading.speech.text(), "Your AM briefing is ready to view")
        self.assertTrue(reading.is_new_briefing())
        self.assertEqual(reading.dock.accessible_name(dock.BRIEFING), "Briefing, new")
        reading.set_speech("Two new emails from the department.")
        self.assertEqual(reading.speech.text(), "Two new emails from the department.")
        reading.set_speech("")
        self.assertEqual(reading.speech.text(), "Your AM briefing is ready to view")
        reading.set_new_briefing(False)
        self.assertEqual(reading.speech.text(), "AM briefing")
        self.assertFalse(reading.is_new_briefing())
        self.assertEqual(reading.dock.accessible_name(dock.BRIEFING), "Briefing")
        window.hide()

    def test_unread_and_window_activation_signal(self) -> None:
        window = self.window(False)
        reading = window.reading
        reading.set_tab(hud.TAB_BRIEFING)
        reading.mark_unread()
        self.assertTrue(reading.unread())                          # a dot on Home's Transcript button
        self.assertEqual(reading.stage.transcript_button.accessibleName(), "Transcript, new")
        reading.open_transcript()
        self.assertFalse(reading.unread())
        reading.mark_unread()
        self.assertFalse(reading.unread())                         # the drawer shows it already
        seen: list[bool] = []
        window.activeChanged.connect(seen.append)
        from PySide6.QtCore import QEvent
        QApplication.sendEvent(window, QEvent(QEvent.Type.ActivationChange))
        self.assertEqual(len(seen), 1)
        window.hide()

    def test_memory_followups_and_settings_fit(self) -> None:
        """Round 2, part 2b: Memory, Follow-ups and Settings are their own stages, inside the window at the
        default and the smallest size."""
        window = self.window(False)
        reading = window.reading
        reading.set_memory_available(True)
        reading.set_followups_available(True)
        for size in ((ui.READING_SIZE.width(), ui.READING_SIZE.height()), (900, 600)):
            window.resize(*size)
            for screen, stage in ((hud.TAB_MEMORY, reading.memory_view),
                                  (ui.SCREEN_FOLLOWUPS, reading.followups_stage),
                                  (ui.SCREEN_SETTINGS, reading.settings_page)):
                with self.subTest(size=size, screen=screen):
                    reading.set_tab(screen)
                    settle(4)
                    self.assertTrue(stage.isVisible())
                    self.assert_inside(stage, window)
        self.assertFalse(reading.memory_view.horizontalScrollBar().isVisible())
        window.hide()

    def test_conversation_hour24_follows_the_window(self) -> None:
        window = self.window(True)
        window.reading.conversation.add(hud.ROLE_JARVIS, "x", when=datetime(2026, 10, 8, 13, 5))
        self.assertEqual(window.reading.conversation.entries()[0][1], "13:05")
        self.assertIsInstance(window.reading.strip_caption, QLabel)
        window.hide()


class TelemetryValueColorTests(unittest.TestCase):
    def test_value_color(self) -> None:
        bar = hud.TelemetryBar("Updated", "today 10:04 AM")
        self.addCleanup(bar.deleteLater)
        self.assertEqual(bar.value_color().name(), hud.TEXT_BRIGHT)
        dot = hud.MIDDLE_DOT
        bar.set_value(f"new {dot} today 10:04 AM", 1.0, hud.AMBER, short=f"new {dot} 10:04 AM", value_color=hud.AMBER)
        self.assertEqual(bar.value_color().name(), hud.AMBER)
        bar.set_value("today 10:04 AM", 1.0, hud.TEXT_DIM)
        self.assertEqual(bar.value_color().name(), hud.TEXT_BRIGHT)


if __name__ == "__main__":
    unittest.main()
