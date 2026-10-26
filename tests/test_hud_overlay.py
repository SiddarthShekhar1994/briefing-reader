"""Tests for the desktop HUD's windows (hud_overlay; SPEC 8.1, P1.3): every window a frameless tool window on top
that never takes the focus (no taskbar button), no focus taken while it shows, capture exclusion only through the
HudNative seam and only while [desktop] hide_from_capture is on, the layout at 1x and 1.5x (and a 1280 x 720 screen
at 150 %) with no word cut, the caption wrapping to three lines with the bar growing (cut only past that, whole in a
tooltip that shows on a window that is never active), the HUD key's keycaps as the prototypes space them (none while
no key can work), the send control in DesktopB's mic-slot style on the HUD only, the chips' words (REC's width held
while the seconds tick; no repaint when nothing changed), the orb with no ground, the result card's place, the decor
layers with the prototypes' shadows and glows under the interactive windows (repainted only when a shape changes),
the stacking order with the meetings RecPill and the Toast, the feature off: no HUD window at all, and what the switch's
flight takes from it (P1.5): its pictures adding up to the HUD as it shows, its reach, the pill held from ``prepare``.

Qt runs offscreen in this process (and in one child at 1.5x) and shows nothing. No OS call is made: a fake seam
records what would be asked, and the real one refuses while the tests run.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import (QAbstractAnimation, QCoreApplication, QEvent, QObject, QPoint, QPointF, QRect, QRectF,
                            QSize, Qt, QTimer)
from PySide6.QtGui import QFontMetricsF, QGuiApplication, QImage, QPainter, QRegion
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget

from briefing_reader import holo, hud, hud_overlay, meetings_view, settings_view, states_view, ui
from briefing_reader import orb as orb_mod
from briefing_reader.config import DesktopConfig, HotkeyConfig
from briefing_reader.desktop import backend as desktop_backend
from briefing_reader.hud_overlay import Bracket, CardButton, CardContent, Tether
from tests.ui_fakes import AppHarness, fonts, settle

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CAPTION = "Good morning. What shall I do, sir?"
DOT = "\u00b7"
OPEN_RESULT = CardContent(f"FILE {DOT} WORD DOCUMENT", "Essay draft.docx",
                          f"Documents \u203a Essays {DOT} edited yesterday, 9:14 PM",
                          (CardButton("open", "Open", holo.GO), CardButton("folder", "Show in folder")),
                          tag="2 other matches", note="Opening something changes nothing, so it happens on one click.",
                          note_tone=hud_overlay.TONE_CYAN)
D5_RESULT = CardContent(f"OPENED IN WORD {DOT} 10:21 AM", "LING essay draft.docx",
                        f"Documents \u203a Uni \u203a LING {DOT} edited yesterday, 9:14 PM",
                        (CardButton("folder", "Show in folder"),), tag="2 other matches",
                        note="Opened LING essay draft.docx in Word. Nothing else changed.",
                        rows=(hud_overlay.CardRow("row:0", "LING essay draft (old).docx",
                                                  f"Downloads {DOT} edited 12 Oct", "Open this instead"),
                              hud_overlay.CardRow("row:1", "LING reading list.pdf",
                                                  f"Documents \u203a Uni \u203a LING {DOT} edited 2 Oct",
                                                  "Open this instead")), note_first=True)
HUD_NAMES = {"JarvisHud", "JarvisResult", "JarvisDecor"}
_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_hud_overlay", "-platform", "offscreen"])
    fonts()


class FakeNative:
    """The HudNative seam, recorded: what Windows would be asked (nothing is). ``other``: the pretend window that has
    the keyboard before the bar takes it; ``keys``: the bar's keyboard calls, in order."""

    def __init__(self, agree: bool = True, other: int = 4242) -> None:
        self.agree = agree
        self.excluded: list[int] = []
        self.stacks: list[list[int]] = []
        self.other = other
        self.keys: list[tuple] = []

    def exclude_from_capture(self, hwnd: int) -> bool:
        self.excluded.append(int(hwnd))
        return self.agree

    def stack(self, hwnds) -> None:
        self.stacks.append([int(hwnd) for hwnd in hwnds])

    def foreground(self) -> int:
        self.keys.append(("foreground",))
        return self.other

    def activate(self, hwnd: int) -> bool:
        self.keys.append(("activate", int(hwnd)))
        return self.agree

    def restore_foreground(self, hwnd: int) -> bool:
        self.keys.append(("restore", int(hwnd)))
        return self.agree


def desktop_config(**desktop: object) -> SimpleNamespace:
    hotkey = desktop.pop("hotkey", HotkeyConfig())
    return SimpleNamespace(desktop=DesktopConfig(**{"enabled": True, **desktop}), hotkey=hotkey)


def close(overlay: hud_overlay.HudOverlay) -> None:
    overlay.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def has(flags: Qt.WindowType, flag: Qt.WindowType) -> bool:
    return (flags & flag) == flag


def rect_list(rect: QRect) -> list[int]:
    return [rect.x(), rect.y(), rect.width(), rect.height()]


class PaintCounter(QObject):
    """Counts the paint events of the widgets it watches (and the rects they repaint)."""

    def __init__(self, *widgets: QWidget) -> None:
        super().__init__()
        self.counts: dict[int, int] = {}
        self.rects: list[QRect] = []
        for widget in widgets:
            self.counts[id(widget)] = 0
            widget.installEventFilter(self)

    def count(self, widget: QWidget) -> int:
        return self.counts.get(id(widget), 0)

    def reset(self) -> None:
        self.counts = dict.fromkeys(self.counts, 0)
        self.rects = []

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if event.type() == QEvent.Type.Paint and id(watched) in self.counts:
            self.counts[id(watched)] += 1
            self.rects.append(QRect(event.rect()))
        return False


LONG_CAPTION = "You have three meetings today. Your project sync with Ana is at 2 PM, and the review moved, sir."


def layout_report() -> dict:
    """The HUD with every chip and the open result's card, built as the app builds them, on a 1280 x 760 work area,
    an 800 x 600 one and a 1280 x 720 screen at 150 % (853 x 432 above its taskbar): their geometry (logical px),
    the lines a long caption takes (wrapped, the bar grown with it) and every word they cut. Run here at 1x and in a
    child process at 1.5x."""
    fonts()
    overlay = hud_overlay.HudOverlay(hud_key="ctrl+alt+h", native=FakeNative())
    try:
        win = overlay.hud
        win.set_caption(CAPTION)
        win.set_briefing_ready(True)
        win.set_pending(2)
        win.set_recording("12:04", warning=meetings_view.CALL_MISSING_TEXT)
        report: dict = {"dpr": 0.0, "cut": [], "areas": {}}
        for name, area in (("wide", QRect(0, 0, 1280, 760)), ("small", QRect(1280, 0, 800, 600)),
                           ("tiny", QRect(2080, 0, 853, 432))):
            win.set_caption(CAPTION)
            win.place_in(area)
            win.show()
            settle()
            report["dpr"] = win.devicePixelRatioF()
            report["cut"] += [f"{name}: {text}" for text in win.cut_texts()]
            report["areas"][name] = {
                "window": rect_list(win.geometry()), "bar": rect_list(win.bar_rect()),
                "orb": [win.orb_center().x(), win.orb_center().y()],
                "row": rect_list(win.bar.geometry()), "caption": rect_list(win.caption.geometry()),
                "back": rect_list(win.back_button.geometry()),
                "chips": [rect_list(chip.geometry()) for chip in win.chips.values() if not chip.isHidden()]}
            win.set_caption(LONG_CAPTION)
            settle()
            report["cut"] += [f"{name} long: {text}" for text in win.cut_texts()]
            report["areas"][name]["long"] = {"lines": win.caption_lines(), "bar": rect_list(win.bar_rect()),
                                             "inside": area.contains(win.geometry())}
        card = overlay.new_card()
        card.set_content(OPEN_RESULT)
        report["cut"] += [f"card: {text}" for text in card.cut_texts()]
        report["card"] = [card.width(), card.height()]
        return report
    finally:
        close(overlay)


_CHILD = """
import json
from tests import test_hud_overlay as t     # the tests package sets the desktop test guard first
from PySide6.QtWidgets import QApplication
app = QApplication(["test_hud_overlay_x15"])
print("REPORT " + json.dumps(t.layout_report()))
"""


class _OverlayTest(unittest.TestCase):
    def overlay(self, *, native: FakeNative | None = None, hud_key: str = "ctrl+alt+h",
                hide_from_capture: bool = True) -> hud_overlay.HudOverlay:
        self.native = native or FakeNative()
        overlay = hud_overlay.HudOverlay(hud_key=hud_key, hide_from_capture=hide_from_capture, native=self.native)
        self.addCleanup(close, overlay)
        return overlay

    def card(self, overlay: hud_overlay.HudOverlay) -> hud_overlay.ResultCard:
        card = overlay.new_card()
        card.set_content(OPEN_RESULT)
        return card


class FeatureOffTests(unittest.TestCase):
    def test_off_creates_no_window_at_all(self) -> None:
        before = set(QApplication.topLevelWidgets())
        configs = (SimpleNamespace(desktop=DesktopConfig(), hotkey=HotkeyConfig()), SimpleNamespace(), None,
                   desktop_config(enabled=False))
        for config in configs:
            self.assertIsNone(hud_overlay.create(config, native=FakeNative()))
        settle()
        self.assertEqual(set(QApplication.topLevelWidgets()) - before, set())

    def test_the_app_with_desktop_off_has_no_hud_window(self) -> None:
        before = set(QApplication.topLevelWidgets())
        harness = AppHarness(self, ask=False)
        harness.reading(autoplay=False)
        self.assertFalse(harness.config.desktop.enabled)
        new = {widget.objectName() for widget in set(QApplication.topLevelWidgets()) - before}
        self.assertFalse(new & HUD_NAMES, new)

    def test_on_creates_the_windows_hidden_until_shown(self) -> None:
        native = FakeNative()
        overlay = hud_overlay.create(desktop_config(), native=native)
        self.assertIsInstance(overlay, hud_overlay.HudOverlay)
        self.addCleanup(close, overlay)
        self.assertIs(overlay.native(), native)
        self.assertFalse(overlay.is_shown())
        self.assertFalse(any(window.isVisible() for window in overlay.windows()))
        self.assertEqual(len(overlay.layers()), len(QGuiApplication.screens()))     # one per screen
        self.assertEqual({window.objectName() for window in overlay.windows()}, {"JarvisHud", "JarvisDecor"})
        self.assertEqual(native.excluded, [])                                      # nothing shown, nothing asked
        self.assertEqual(overlay.hud.keycaps.keys(), ["CTRL", "ALT", "H"])


class FlagTests(_OverlayTest):
    def test_every_window_is_a_frameless_tool_on_top_that_never_takes_the_focus(self) -> None:
        overlay = self.overlay()
        card = self.card(overlay)
        overlay.show_on(None)
        overlay.show_card(card, QRect(80, 300, 200, 120))
        overlay.set_decor([Bracket(QRect(80, 300, 200, 120), tag="WORD")])
        settle()
        shown = [window for window in overlay.windows() if window.isVisible()]
        self.assertEqual(len(shown), 3)                                  # the HUD, the card, one layer
        for window in overlay.windows():
            name = window.objectName()
            flags = window.windowFlags()
            self.assertEqual(window.windowType(), Qt.WindowType.Tool, name)        # no taskbar button
            for flag in (Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint,
                         Qt.WindowType.WindowDoesNotAcceptFocus):
                self.assertTrue(has(flags, flag), (name, flag))
            self.assertTrue(window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating), name)
            self.assertTrue(window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground), name)
            self.assertEqual(window.focusPolicy(), Qt.FocusPolicy.NoFocus, name)
            self.assertIsNone(window.parent(), name)                     # its own window, never Jarvis's child
            through = has(flags, Qt.WindowType.WindowTransparentForInput)
            self.assertEqual(through, isinstance(window, hud_overlay.DecorLayer), name)
        for window in shown:
            handle = window.windowHandle()
            self.assertTrue(has(handle.flags(), Qt.WindowType.WindowDoesNotAcceptFocus), window.objectName())
            self.assertIs(handle.property("_q_showWithoutActivating"), True, window.objectName())
        for layer in overlay.layers():
            self.assertTrue(layer.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
            self.assertEqual(layer.geometry(), layer.screen_ref().geometry())     # the whole screen

    def test_a_second_show_keeps_the_rules(self) -> None:
        overlay = self.overlay()
        overlay.show_on(None)
        overlay.hide()
        overlay.show_on(None)
        settle()
        handle = overlay.hud.windowHandle()
        self.assertTrue(has(handle.flags(), Qt.WindowType.WindowDoesNotAcceptFocus))
        self.assertIs(handle.property("_q_showWithoutActivating"), True)


class FocusTests(_OverlayTest):
    def test_showing_and_clicking_take_no_focus(self) -> None:
        other = QLineEdit()
        self.addCleanup(other.deleteLater)
        other.show()
        other.activateWindow()
        other.setFocus()
        settle()
        overlay = self.overlay()
        card = self.card(overlay)
        win = overlay.hud
        win.set_briefing_ready(True)
        win.set_pending(2)
        win.set_recording("0:42")
        asked: list[tuple[str, str]] = []
        for window in (win, card):
            for name in ("activateWindow", "raise_", "setFocus"):
                setattr(window, name, lambda *args, w=window, n=name: asked.append((w.objectName(), n)))
        overlay.show_on(None)
        overlay.show_card(card, QRect(80, 300, 200, 120))
        settle()
        self.assertEqual(asked, [])                     # never an activation, a raise or a focus of its own
        # (The offscreen platform activates every window it shows; Windows honours WindowDoesNotAcceptFocus and
        # _q_showWithoutActivating, checked in FlagTests.) Nothing in the HUD holds the keyboard focus - only the bar,
        # once the HUD key or a click in its input gives it the keyboard (tests/test_desktop_keys.py).
        self.assertFalse(self._focus_in(overlay))
        for child in win.findChildren(QWidget):
            wanted = Qt.FocusPolicy.ClickFocus if child is win.bar.input else Qt.FocusPolicy.NoFocus
            self.assertEqual(child.focusPolicy(), wanted, type(child).__name__)
        for button in card.buttons().values():
            self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.NoFocus)
        clicked: list[str] = []
        win.chipClicked.connect(clicked.append)
        win.bringBackClicked.connect(lambda: clicked.append("back"))
        card.buttonClicked.connect(clicked.append)
        for chip in win.chips.values():
            QTest.mouseClick(chip, Qt.MouseButton.LeftButton)
        QTest.mouseClick(win.back_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(card.buttons()["open"], Qt.MouseButton.LeftButton)
        settle()
        self.assertEqual(clicked, [hud_overlay.CHIP_BRIEFING, hud_overlay.CHIP_APPROVALS, hud_overlay.CHIP_REC,
                                   "back", "open"])
        self.assertFalse(self._focus_in(overlay))
        self.assertEqual(asked, [])

    @staticmethod
    def _focus_in(overlay: hud_overlay.HudOverlay) -> bool:
        focus = QApplication.focusWidget()
        return focus is not None and any(focus is window or window.isAncestorOf(focus)
                                         for window in overlay.windows())


class CaptureTests(_OverlayTest):
    def test_every_window_shown_is_left_out_of_capture_once(self) -> None:
        overlay = self.overlay()
        card = self.card(overlay)
        overlay.show_on(None)
        settle()
        layer = overlay.layer_for(QGuiApplication.primaryScreen())          # the HUD's shadows show with it
        self.assertEqual(sorted(self.native.excluded), sorted(int(window.winId()) for window in (overlay.hud, layer)))
        overlay.show_card(card, QRect(80, 300, 200, 120))
        overlay.set_decor([Bracket(QRect(80, 300, 200, 120))])
        settle()
        shown = [window for window in overlay.windows() if window.isVisible()]
        self.assertEqual(len(shown), 3)
        self.assertEqual(sorted(self.native.excluded), sorted(int(window.winId()) for window in shown))
        self.assertTrue(all(window.excluded_from_capture() for window in shown))
        overlay.hide()
        overlay.show_on(None)
        settle()
        self.assertEqual(len(self.native.excluded), 3)              # once per native window

    def test_never_asked_while_hide_from_capture_is_off(self) -> None:
        overlay = self.overlay(hide_from_capture=False)
        card = self.card(overlay)
        overlay.show_on(None)
        overlay.show_card(card, QRect(80, 300, 200, 120))
        overlay.set_decor([Bracket(QRect(80, 300, 200, 120))])
        settle()
        self.assertEqual(self.native.excluded, [])
        self.assertFalse(any(window.excluded_from_capture() for window in overlay.windows()))

    def test_create_follows_desktop_hide_from_capture(self) -> None:
        for wanted in (True, False):
            native = FakeNative()
            overlay = hud_overlay.create(desktop_config(hide_from_capture=wanted), native=native)
            self.addCleanup(close, overlay)
            overlay.show_on(None)
            settle()
            shown = sorted(int(window.winId()) for window in overlay.windows() if window.isVisible())
            self.assertEqual(len(shown), 2)                                 # the HUD and the layer of its shadows
            self.assertEqual(sorted(native.excluded), shown if wanted else [])

    def test_a_refusal_is_asked_again_at_the_next_show(self) -> None:
        overlay = self.overlay(native=FakeNative(agree=False))
        overlay.show_on(None)
        overlay.hide()
        overlay.show_on(None)
        settle()
        self.assertEqual(len(self.native.excluded), 4)               # the HUD and its layer, at each show
        self.assertFalse(overlay.hud.excluded_from_capture())
        self.assertFalse(any(layer.excluded_from_capture() for layer in overlay.layers()))

    def test_no_os_call_while_the_tests_run(self) -> None:
        self.assertTrue(desktop_backend.real_backend_blocked())
        self.assertIsInstance(hud_overlay.default_native(), hud_overlay.NoNative)
        with self.assertRaises(RuntimeError):
            hud_overlay.Win32Native()
        overlay = hud_overlay.HudOverlay(hud_key="")
        self.addCleanup(close, overlay)
        self.assertIsInstance(overlay.native(), hud_overlay.NoNative)
        overlay.show_on(None)                                      # NoNative: nothing asked, nothing refused
        settle()
        self.assertFalse(overlay.hud.excluded_from_capture())


class KeyCapsTests(_OverlayTest):
    def test_the_keycaps_say_the_hud_key_and_the_send_arrow_comes_with_text(self) -> None:
        overlay = self.overlay()
        win = overlay.hud
        overlay.show_on(None)
        settle()
        self.assertEqual(win.keycaps.keys(), ["CTRL", "ALT", "H"])
        self.assertEqual(win.keycaps.accessibleName(), "CTRL+ALT+H")
        self.assertTrue(win.keycaps.isVisible())
        self.assertFalse(win.bar.send_button.isVisible())
        self.assertEqual(win.bar.input.placeholderText(), "Ask Jarvis")
        win.bar.set_text("open my essay")
        settle()
        send = win.bar.send_button
        self.assertTrue(win.keycaps.isVisible())                  # DesktopB keeps them while typing
        self.assertTrue(send.isVisible())
        self.assertEqual(send.size(), QSize(28, 28))              # where DesktopB has its mic
        self.assertGreater(send.geometry().left(), win.keycaps.geometry().right())
        self.assertEqual(win.cut_texts(), [])
        win.bar.set_running(True)
        self.assertTrue(send.isVisible())                          # the stop glyph while a request runs
        win.bar.set_running(False)
        win.bar.clear_input()
        settle()
        self.assertTrue(win.keycaps.isVisible())
        self.assertFalse(send.isVisible())

    def test_no_keycaps_while_no_key_can_work(self) -> None:
        shortcut_off = desktop_config(hotkey=HotkeyConfig(enabled=False))
        self.assertEqual(settings_view.effective_hud_key(shortcut_off), "")
        for config in (shortcut_off, desktop_config(hud_key="")):
            overlay = hud_overlay.create(config, native=FakeNative())
            self.addCleanup(close, overlay)
            win = overlay.hud
            overlay.show_on(None)
            settle()
            self.assertEqual(win.keycaps.keys(), [])
            self.assertFalse(win.keycaps.isVisible())
            self.assertEqual(win.bar.field.layout().indexOf(win.keycaps), -1)      # not in the row at all
            for text in ("open my essay", ""):
                win.bar.set_text(text)
                settle()
                self.assertFalse(win.keycaps.isVisible())
            self.assertEqual(win.cut_texts(), [])

    def test_create_takes_the_key_that_can_work(self) -> None:
        overlay = hud_overlay.create(desktop_config(hud_key="ctrl+shift+space"), native=FakeNative())
        self.addCleanup(close, overlay)
        self.assertEqual(overlay.hud.keycaps.keys(), ["CTRL", "SHIFT", "SPACE"])
        self.assertEqual(hud_overlay.hud_key_caps(""), [])

    def test_the_keycaps_are_spaced_as_in_the_prototypes(self) -> None:
        # Switch / DesktopB .qrow gap 10 px; .kbd 6 px padding and a 1 px border each side, a 2 px bottom edge.
        overlay = self.overlay()
        overlay.show_on(None)
        settle()
        caps = overlay.hud.keycaps
        metrics = QFontMetricsF(caps._font)
        widths = [caps._cap_width(key) - metrics.horizontalAdvance(key) for key in caps.keys()]
        self.assertEqual(widths, [14.0, 14.0, 14.0])
        self.assertEqual((caps.GAP, caps.HEIGHT, caps.height()), (10, 19, 19))
        wanted = sum(caps._cap_width(key) for key in caps.keys()) + 2 * 10
        self.assertEqual(caps.width(), math.ceil(wanted))
        self.assertEqual(overlay.hud.cut_texts(), [])

    def test_the_send_control_is_drawn_in_the_mic_slot_style_only_on_the_hud(self) -> None:
        # DesktopB.dc.html's mic slot: an 18 px cyan outline glyph (stroke 1.8), no filled disc; the click area 28 px.
        overlay = self.overlay()
        win = overlay.hud
        overlay.show_on(None)
        win.bar.set_text("open my essay")
        settle()
        send = win.bar.send_button
        self.assertTrue(send.is_hud())
        self.assertEqual(send.size(), QSize(28, 28))
        window_bar = hud.CommandBar()                                  # the window's own bar: its disc as before
        self.addCleanup(window_bar.deleteLater)
        window_bar.set_holo(True)
        window_bar.set_text("open my essay")
        self.assertFalse(window_bar.send_button.is_hud())
        for button, disc in ((send, False), (window_bar.send_button, True)):
            for running in (False, True):                             # the arrow, then the stop square
                button.set_look(True, running)
                dpr = button.devicePixelRatioF()
                image = QImage(round(button.width() * dpr), round(button.height() * dpr),
                               QImage.Format.Format_ARGB32_Premultiplied)
                image.setDevicePixelRatio(dpr)
                image.fill(Qt.GlobalColor.transparent)
                button.render(image, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)   # its own paint only
                edge = image.pixelColor(round(5 * dpr), round(button.height() / 2 * dpr))    # beside the glyph
                self.assertEqual(edge.alpha() > 128, disc, (button.is_hud(), running))
                inked = [image.pixelColor(x, y) for x in range(image.width()) for y in range(image.height())]
                cyan = [c for c in inked if c.alpha() > 128 and c.blue() > 200 and c.green() > 170 and c.red() < 120]
                self.assertTrue(cyan, (button.is_hud(), running))      # the cyan outline (or the cyan disc)
        self.assertEqual(win.cut_texts(), [])


class ChipTests(_OverlayTest):
    def test_the_chips_say_the_app_words_in_order(self) -> None:
        win = self.overlay().hud
        self.assertEqual(win.chip_texts(), [])
        win.set_briefing_ready(True)
        win.set_pending(2)
        win.set_recording("12:04")
        self.assertEqual(win.chip_texts(), [ui.HOME_BRIEFING_CHIP, ui.needs_ok_chip(2), "REC 12:04"])
        win.set_pending(1)
        win.set_recording("12:05", warning=meetings_view.CALL_MISSING_TEXT)
        self.assertEqual(win.chip_texts(), ["Briefing ready", "1 needs your OK", f"REC 12:05 {DOT} No call audio"])
        win.set_pending(0)
        win.set_recording("")
        win.set_briefing_ready(False)
        self.assertEqual(win.chip_texts(), [])
        for count in range(0, 30):
            self.assertEqual(hud_overlay.needs_ok_text(count), ui.needs_ok_chip(count))

    def test_the_chips_sit_under_the_bar_in_a_row(self) -> None:
        overlay = self.overlay()
        win = overlay.hud
        win.place_in(QRect(0, 0, 1280, 760))
        win.set_briefing_ready(True)
        win.set_pending(3)
        win.set_recording("1:02:09")
        bar = win.bar_rect_local()
        x = bar.left() + hud_overlay.ORB_PAD
        for chip in win.chips.values():
            geometry = chip.geometry()
            self.assertEqual((geometry.left(), geometry.top(), geometry.height()),
                             (x, bar.bottom() + 1 + hud_overlay.CHIP_GAP, hud_overlay.CHIP_HEIGHT))
            self.assertEqual(geometry.width(), chip.sizeHint().width())
            x += geometry.width() + hud_overlay.CHIP_GAP
        self.assertEqual(win.cut_texts(), [])

    def test_the_chips_and_the_decor_are_still(self) -> None:
        overlay = self.overlay()
        win = overlay.hud
        win.set_briefing_ready(True)
        win.set_pending(2)
        overlay.show_on(None)
        overlay.set_decor([Bracket(QRect(80, 300, 200, 120))], [Tether((QPoint(180, 300), QPoint(300, 200)))])
        settle()
        for widget in (*win.chips.values(), win.back_button, win.caption, win.keycaps, *overlay.layers()):
            self.assertEqual(widget.findChildren(QTimer), [], type(widget).__name__)
            self.assertEqual(widget.findChildren(QAbstractAnimation), [], type(widget).__name__)
        panel = win.background()
        for elapsed in ("0:42", "0:43", "11:59", "1:00:00"):               # REC's tick, each second
            win.set_recording(elapsed)
            settle()
        self.assertIs(win.background(), panel)                           # the panel is painted once, not per tick

    def test_the_rec_chip_keeps_its_width_while_the_seconds_tick(self) -> None:
        overlay = self.overlay()
        win = overlay.hud
        overlay.show_on(None)
        chip = win.chips[hud_overlay.CHIP_REC]
        widths = set()
        for second in range(4, 20):                                     # 12:04 .. 12:19, as each second ticks
            win.set_recording(f"12:{second:02d}")
            settle()
            widths.add(chip.width())
        self.assertEqual(len(widths), 1, sorted(widths))                # tabular figures: it never twitches
        self.assertEqual(chip.shown_text(), "REC 12:19")
        self.assertEqual(win.cut_texts(), [])

    def test_a_chip_whose_words_did_not_change_is_not_repainted(self) -> None:
        overlay = self.overlay()
        win = overlay.hud
        win.set_briefing_ready(True)
        win.set_pending(2)
        win.set_recording("12:04")
        overlay.show_on(None)
        settle()
        briefing, approvals, rec = (win.chips[key] for key in hud_overlay.CHIP_KEYS)
        counter = PaintCounter(briefing, approvals, rec)
        for second in range(5, 11):                                     # six REC ticks; the count unchanged
            win.set_recording(f"12:{second:02d}")
            win.set_pending(2)
            win.set_briefing_ready(True)
            settle()
        self.assertEqual((counter.count(briefing), counter.count(approvals)), (0, 0))
        self.assertGreaterEqual(counter.count(rec), 6)                   # only REC, whose time changed
        counter.reset()
        for _ in range(10):                                             # glances that change nothing
            win.set_pending(2)
            win.set_recording("12:10")
            settle()
        self.assertEqual([counter.count(chip) for chip in (briefing, approvals, rec)], [0, 0, 0])
        win.set_pending(3)
        settle()
        self.assertGreaterEqual(counter.count(approvals), 1)             # a real change still repaints
        self.assertEqual(approvals.text(), ui.needs_ok_chip(3))


class OrbTests(_OverlayTest):
    def test_the_orb_is_the_compact_one_with_no_ground_never_resized(self) -> None:
        overlay = self.overlay()
        win = overlay.hud
        orb = win.orb
        self.assertEqual((orb.orb_size(), orb.size()), (92, QSize(104, 104)))
        self.assertEqual(orb.detail(), "compact")
        self.assertIsNone(orb._ground)                                        # the window is translucent
        self.assertFalse(orb.testAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent))
        overlay.show_on(None)
        settle()
        self.assertTrue(orb_mod.watching(win.windowHandle()))                 # input there ends deep idle
        for base in (orb_mod.SPEAKING, orb_mod.THINKING, orb_mod.NEEDS, orb_mod.IDLE):
            win.set_look(base)
            QTest.qWait(80)
            self.assertEqual(orb.size(), QSize(104, 104), base)
        center = win.orb_center() - win.pos()
        self.assertEqual(orb.geometry().center(), center - QPoint(1, 1))   # 104 px centred on the orb's point

    def test_the_state_word_follows_the_orb(self) -> None:
        win = self.overlay().hud
        for (base, muted, offline), word in (((orb_mod.IDLE, False, False), "STANDBY"),
                                             ((orb_mod.NEEDS, False, False), "NEEDS YOU"),
                                             ((orb_mod.THINKING, False, False), "THINKING"),
                                             ((orb_mod.SPEAKING, False, False), "SPEAKING"),
                                             ((orb_mod.WAITING, False, False), "STANDBY"),
                                             ((orb_mod.IDLE, True, False), "MUTED"),
                                             ((orb_mod.IDLE, False, True), "OFFLINE VOICE")):
            win.set_look(base, muted=muted, offline=offline)
            self.assertEqual(win.state_word(), word)
            self.assertEqual(win.orb.look(), (base, muted, offline))


class CaptionTests(_OverlayTest):
    def test_the_bar_status_shows_in_the_caption_line_without_readouts(self) -> None:
        win = self.overlay().hud
        win.place_in(QRect(0, 0, 1280, 760))
        win.set_caption(CAPTION)
        self.assertEqual(win.caption_text(), CAPTION)
        win.bar.set_status("Ask's daily limit reached; try again after 9 AM (20 planner runs a day: [ask] "
                           "max_per_day).", hud.TONE_WARN)
        self.assertEqual(win.caption_text(), "Ask's daily limit reached; try again after 9 AM.")
        self.assertFalse(win.bar.status_label.isVisibleTo(win.bar))          # the row has no status line
        self.assertFalse(win.bar.meta_label.isVisibleTo(win.bar))
        win.bar.set_status("", hud.TONE_IDLE)
        self.assertEqual(win.caption_text(), CAPTION)
        win.bar.set_hint("Ask Jarvis")
        self.assertEqual(win.caption_text(), CAPTION)                          # a hint is not news

    def test_a_long_caption_wraps_to_three_lines_and_the_bar_grows_with_it(self) -> None:
        # Switch.dc.html .bcap: the caption wraps (font 300 14px/1.4) and .crow grows with it.
        overlay = self.overlay()
        win = overlay.hud
        win.set_briefing_ready(True)
        overlay.show_on(None)
        win.place_in(QRect(0, 0, 1280, 760))
        settle()
        chip = win.chips[hud_overlay.CHIP_BRIEFING]
        one = (win.bar_rect_local().height(), win.height(), chip.geometry().top())
        self.assertEqual(one[0], hud_overlay.BAR_HEIGHT)
        sentence = "You have three meetings today. Your project sync with Ana is at 2 PM, and the review moved, sir."
        for lines, text in ((2, sentence), (3, f"{sentence} Shall I open the agenda for you?")):
            win.set_caption(text)
            settle()
            self.assertEqual(win.caption_lines(), lines, text)
            self.assertEqual(win.caption.shown_text(), text)                      # whole: nothing cut
            self.assertEqual(win.cut_texts(), [])
            grown = 20 * (lines - 1)                                              # a 20 px line each
            self.assertEqual((win.bar_rect_local().height(), win.height(), chip.geometry().top()),
                             (one[0] + grown, one[1] + grown, one[2] + grown))
            self.assertEqual(win.caption.height(), hud_overlay.caption_height(lines) - 1)
            self.assertFalse(chip.geometry().intersects(win.bar_rect_local()))   # the chips move down with it
        win.set_caption("On your desktop, sir.")
        settle()
        self.assertEqual((win.caption_lines(), win.bar_rect_local().height(), win.height()), (1, one[0], one[1]))

    def test_a_longer_caption_is_cut_after_three_lines_and_whole_in_its_tooltip(self) -> None:
        win = self.overlay().hud
        win.place_in(QRect(0, 0, 1280, 760))
        long = " ".join(["Your essay draft is open in Word, and two other files of that name are in Documents."] * 4)
        win.set_caption(long)
        self.assertEqual(win.caption_lines(), hud_overlay.CAPTION_LINES)
        lines = win.caption.lines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[-1].endswith("\u2026"))                         # cut only past the third line
        self.assertFalse(any(line.endswith("\u2026") for line in lines[:-1]))
        self.assertIn("two other files", win.caption.toolTip())
        self.assertEqual(win.caption.toolTip().count("two other files"), 4)     # the whole text
        self.assertEqual(win.cut_texts(), ["caption"])
        # Never the active window (WindowDoesNotAcceptFocus): Qt shows a tooltip only for the active window or one
        # that always shows them.
        self.assertTrue(win.testAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips))


class LayoutTests(_OverlayTest):
    def test_the_bar_is_centred_16_px_from_the_top_with_the_orb_at_its_left_end(self) -> None:
        win = self.overlay().hud
        win.set_caption(CAPTION)
        for area in (QRect(0, 0, 1536, 912), QRect(1536, 0, 1280, 760), QRect(0, 0, 800, 600),
                     QRect(-1280, 40, 1280, 720)):
            win.place_in(area)
            bar = win.bar_rect()
            self.assertEqual(bar.size(), QSize(min(760, area.width() - 2 * (54 + 16)), 81), area)
            self.assertEqual(bar.top(), area.top() + 16)
            self.assertLessEqual(abs(bar.center().x() - area.center().x()), 1, area)
            self.assertEqual(win.orb_center(), QPoint(bar.left() - 2, bar.top() + 36))
            self.assertTrue(area.contains(win.geometry()), area)
            row = win.bar.geometry()
            local = win.bar_rect_local()
            self.assertEqual((row.left(), row.top(), row.height()), (local.left() + 62, local.top() + 1, 44))
            caption = win.caption.geometry()
            self.assertEqual((caption.left(), caption.top()), (row.left(), row.bottom() + 2))
            back = win.back_button.geometry()
            self.assertEqual(back.right(), local.right() - 1 - hud_overlay.BACK_GAP)
            self.assertLessEqual(abs(back.center().y() - local.center().y()), 1)
            self.assertLess(row.right(), back.left() - hud_overlay.BACK_GAP)
            self.assertEqual(win.cut_texts(), [], area)

    def test_on_the_screen_of_the_jarvis_window(self) -> None:
        overlay = self.overlay()
        window = QWidget()
        self.addCleanup(window.deleteLater)
        window.resize(400, 300)
        screen = hud_overlay.screen_for(window)
        self.assertIs(screen, window.screen())
        overlay.show_on(screen)
        settle()
        self.assertIs(overlay.hud_screen(), screen)
        self.assertEqual(overlay.hud.area(), screen.availableGeometry())
        self.assertTrue(screen.availableGeometry().contains(overlay.hud.geometry()))
        self.assertIs(hud_overlay.screen_for(None), QGuiApplication.primaryScreen())

    def test_the_fills_are_opaque_and_the_margins_clear(self) -> None:
        overlay = self.overlay()
        win = overlay.hud
        win.set_briefing_ready(True)
        overlay.show_on(None)
        settle()
        bar = win.bar_rect_local()
        for dpr in (1.0, 1.5):
            image = win.background(dpr).toImage()
            self.assertEqual(image.size(), QSize(round(win.width() * dpr), round(win.height() * dpr)))

            def alpha(x: float, y: float) -> int:
                return image.pixelColor(round(x * dpr), round(y * dpr)).alpha()

            for x, y in ((bar.center().x(), bar.top() + 22), (bar.right() - 40, bar.bottom() - 10),
                         (bar.left() + 300, bar.top() + 60)):
                self.assertEqual(alpha(x, y), 255, (dpr, x, y))                # opaque: never a blur
            for x, y in ((win.width() - 1, 2), (bar.left() + 400, win.height() - 3), (2, win.height() - 3)):
                self.assertEqual(alpha(x, y), 0, (dpr, x, y))                  # clear: clicks pass through
        grab = win.grab().toImage()
        chip = win.chips[hud_overlay.CHIP_BRIEFING].geometry()
        dpr = grab.devicePixelRatio()
        inside = grab.pixelColor(round((chip.left() + chip.width() // 2) * dpr), round(chip.center().y() * dpr))
        self.assertEqual(inside.alpha(), 255)

    def test_the_layout_at_one_and_a_half_cuts_no_word(self) -> None:
        here = layout_report()
        self.assertEqual(here["dpr"], 1.0)
        self.assertEqual(here["cut"], [])
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR="1.5",
                   QT_QPA_FONTDIR=r"C:\Windows\Fonts")
        env.pop("QT_SCREEN_SCALE_FACTORS", None)
        done = subprocess.run([sys.executable, "-B", "-c", _CHILD], cwd=PROJECT_ROOT, env=env, capture_output=True,
                              text=True, timeout=180)
        lines = [line for line in done.stdout.splitlines() if line.startswith("REPORT ")]
        self.assertTrue(lines, done.stdout[-2000:] + done.stderr[-2000:])
        there = json.loads(lines[-1][len("REPORT "):])
        self.assertEqual(there["dpr"], 1.5)
        self.assertEqual(there["cut"], [])
        for name in ("wide", "small", "tiny"):
            for key in ("bar", "orb"):
                self.assertEqual(there["areas"][name][key], here["areas"][name][key], (name, key))
            self.assertEqual(there["areas"][name]["window"][:2], here["areas"][name]["window"][:2])
            self.assertEqual(len(there["areas"][name]["chips"]), 3)
            for report in (here, there):                              # the long caption wraps, never cut
                long = report["areas"][name]["long"]
                self.assertIn(long["lines"], (2, 3), (name, long))
                self.assertEqual(long["bar"][3], 81 + 20 * (long["lines"] - 1), (name, long))
                self.assertTrue(long["inside"], (name, long))


class CardTests(_OverlayTest):
    def test_the_card_shows_its_rows_and_its_buttons_emit_their_keys(self) -> None:
        overlay = self.overlay()
        card = self.card(overlay)
        self.assertEqual([button.text() for button in card.buttons().values()], ["Open", "Show in folder"])
        self.assertEqual(card.cut_texts(), [])
        rows = [card.row_rect(name) for name in ("kicker", "title", "meta", "buttons", "note")]
        self.assertTrue(all(not row.isEmpty() for row in rows))
        self.assertEqual([row.top() for row in rows], sorted(row.top() for row in rows))
        self.assertEqual(card.width(), hud_overlay.CARD_WIDTH)
        self.assertEqual(card.height(), round(rows[-1].bottom() + hud_overlay.CARD_PAD[0]))
        self.assertEqual(card.accessibleName(), "Essay draft.docx")
        keys: list[str] = []
        card.buttonClicked.connect(keys.append)
        overlay.show_on(None)
        overlay.show_card(card, QRect(80, 300, 200, 120))
        settle()
        QTest.mouseClick(card.buttons()["folder"], Qt.MouseButton.LeftButton)
        self.assertEqual(keys, ["folder"])
        card.set_content(CardContent("OPENED IN WORD", "Essay draft.docx", note="Opened in Word. Nothing else "
                                     "changed.", tone=hud_overlay.TONE_DONE))
        self.assertEqual(card.buttons(), {})
        self.assertTrue(card.row_rect("buttons").isEmpty())
        self.assertEqual(card.cut_texts(), [])

    def test_the_d5_card_note_first_then_show_in_folder_then_open_this_instead_rows(self) -> None:
        """P1.9: what happened right under the meta, then the buttons and the tag, then the rows - each with its own
        NoFocus link button, emitting the row's key; the meta line has the whole width."""
        overlay = self.overlay()
        card = overlay.new_card()
        card.set_content(D5_RESULT)
        names = ("kicker", "title", "meta", "note", "buttons", "row:0", "row:1")
        rows = [card.row_rect(name) for name in names]
        self.assertTrue(all(not row.isEmpty() for row in rows))
        self.assertEqual([row.top() for row in rows], sorted(row.top() for row in rows))
        self.assertEqual(card.height(), round(rows[-1].bottom() + hud_overlay.CARD_PAD[0]))
        self.assertEqual(card.cut_texts(), [])
        self.assertEqual(list(card.row_buttons()), ["row:0", "row:1"])
        for key, button in card.row_buttons().items():
            self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.NoFocus)
            self.assertEqual(button.text(), "Open this instead")
            self.assertTrue(card.row_rect(key).contains(QRectF(button.geometry()).center()))
        self.assertEqual(card.row_buttons()["row:1"].accessibleName(), "Open this instead: LING reading list.pdf")
        keys: list[str] = []
        card.buttonClicked.connect(keys.append)
        overlay.show_on(None)
        overlay.show_card(card, QRect(20, 300, 200, 120))
        settle()
        QTest.mouseClick(card.row_buttons()["row:1"], Qt.MouseButton.LeftButton)
        QTest.mouseClick(card.buttons()["folder"], Qt.MouseButton.LeftButton)
        self.assertEqual(keys, ["row:1", "folder"])
        self.assertIsNone(QApplication.focusWidget())                        # nothing took the focus
        long = CardContent("OPENED IN WORD", "x.docx", rows=(hud_overlay.CardRow(
            "row:0", "A very long file name that goes on and on and on and on.docx", "Downloads", "Open this instead"),))
        card.set_content(long)
        self.assertEqual(card.cut_texts(), ["row:0"])                        # cut in the middle: said, never hidden
        self.assertEqual(card.row_buttons().keys(), {"row:0"})

    def test_a_right_button_sits_at_the_rows_right_end(self) -> None:
        card = self.overlay().new_card()
        card.set_content(CardContent("EXCEL WORKBOOK \u00b7 NEEDS YOUR OK", "Grades macro.xlsm",
                                     buttons=(CardButton("dismiss", "Not now", holo.AMBER_GHOST),
                                              CardButton("open", "Open", holo.AMBER_COMMIT, right=True)),
                                     tone=hud_overlay.TONE_AMBER))
        buttons = card.buttons()
        margin = holo.HoloButton.MARGIN
        self.assertEqual(buttons["dismiss"].geometry().left() + margin, hud_overlay.CARD_PAD[1])
        self.assertEqual(buttons["open"].geometry().right() + 1 - margin, hud_overlay.CARD_WIDTH - hud_overlay.CARD_PAD[1])
        self.assertEqual(card.cut_texts(), [])

    def test_beside_its_target_never_over_it_and_clear_of_the_hud(self) -> None:
        card = self.card(self.overlay())
        area = QRect(0, 0, 1280, 712)
        hud_rect = QRect(232, 0, 816, 143)
        for target, side in ((QRect(100, 200, 300, 200), "right"), (QRect(900, 200, 360, 200), "left"),
                             (QRect(0, 200, 1280, 200), "below"), (QRect(0, 420, 1280, 200), "above"),
                             (QRect(638, 664, 48, 46), "right"), (QRect(200, 10, 300, 120), "right")):
            rect = card.place_beside(target, area, [hud_rect])
            self.assertEqual(rect.topLeft(), card.pos())
            self.assertTrue(area.contains(rect), (target, rect))
            self.assertFalse(rect.intersects(target), (target, rect))
            self.assertFalse(rect.intersects(hud_rect), (target, rect))
            got = ("right" if rect.left() > target.right() else "left" if rect.right() < target.left()
                   else "below" if rect.top() > target.bottom() else "above")
            self.assertEqual(got, side, (target, rect))

    def test_a_new_card_shows_only_once_placed_and_then_with_the_hud(self) -> None:
        overlay = self.overlay()
        card = self.card(overlay)
        overlay.show_on(None)
        settle()
        self.assertFalse(card.isVisible())
        overlay.show_card(card, QRect(80, 300, 200, 120))
        settle()
        self.assertTrue(card.isVisible())
        overlay.hide()
        self.assertFalse(card.isVisible())
        overlay.show_on(None)
        settle()
        self.assertTrue(card.isVisible())
        overlay.remove_card(card)
        self.assertEqual(overlay.cards(), [])
        self.assertNotIn(card, overlay.stack_order())

    def test_show_card_keeps_out_of_the_bar_and_the_chips(self) -> None:
        overlay = self.overlay()
        card = self.card(overlay)
        overlay.hud.set_pending(2)
        overlay.show_on(None)
        settle()
        area = overlay.hud.area()
        target = QRect(area.left() + 40, area.top() + 4, 120, 90)       # right under the bar
        rect = overlay.show_card(card, target)
        settle()
        self.assertTrue(card.isVisible())
        self.assertFalse(rect.intersects(overlay.hud.occupied_rect()))
        self.assertFalse(rect.intersects(target))


class DecorTests(_OverlayTest):
    def test_a_layer_draws_only_what_is_on_its_screen_and_repaints_only_on_a_change(self) -> None:
        overlay = self.overlay()
        layer = overlay.layers()[0]
        layer.setGeometry(QRect(1000, 0, 800, 600))                  # as if it were a second screen
        inside = Bracket(QRect(1100, 100, 200, 100), tag="WORD")
        outside = Bracket(QRect(100, 100, 200, 100))
        across = Bracket(QRect(900, 100, 200, 100))                  # over two screens: drawn on each
        tether = Tether((QPoint(1200, 150), QPoint(1250, 300)), label="word")
        far = Tether((QPoint(10, 10), QPoint(60, 60)))
        self.assertTrue(layer.set_decor([inside, outside, across], [tether, far]))
        self.assertEqual(layer.brackets(), (inside, across))
        self.assertEqual(layer.tethers(), (tether,))
        self.assertFalse(layer.set_decor([inside, outside, across], [tether, far]))     # the same: no repaint
        self.assertTrue(layer.set_decor([], [], glow=True))
        self.assertTrue(layer.glowing() and layer.has_decor())
        self.assertTrue(layer.set_decor())
        self.assertFalse(layer.has_decor())

    def test_layers_show_only_with_something_to_draw_and_stay_under_the_rest(self) -> None:
        layer = hud_overlay.DecorLayer(QGuiApplication.primaryScreen(), native=FakeNative())
        self.addCleanup(layer.deleteLater)
        self.assertFalse(layer.has_decor())                                   # nothing to draw: it never shows
        overlay = self.overlay()
        card = self.card(overlay)
        overlay.show_on(None)
        overlay.show_card(card, QRect(80, 300, 200, 120))
        settle()
        primary = overlay.layer_for(QGuiApplication.primaryScreen())
        self.assertTrue(primary.isVisible())                                  # the shadows under the bar and card
        self.assertEqual(primary.brackets(), ())
        overlay.set_decor([Bracket(QRect(80, 300, 200, 120))], [Tether((QPoint(180, 300), QPoint(300, 260)))])
        settle()
        self.assertTrue(primary.isVisible())
        order = overlay.stack_order()
        self.assertEqual(order, [primary, card, overlay.hud])
        self.assertEqual(self.native.stacks[-1], [int(window.winId()) for window in order])
        overlay.set_decor()
        settle()
        self.assertTrue(primary.isVisible() and primary.glows())               # the shadows stay with the HUD
        overlay.hide()
        self.assertFalse(any(window.isVisible() for window in overlay.windows()))
        self.assertEqual(primary.glows(), ())                                 # none while the HUD is away
        self.assertEqual(overlay.stack_order(), [])

    def test_the_shadows_and_glows_live_in_the_layer_under_the_hud_and_its_cards(self) -> None:
        # Switch.dc.html .bar, .hchip (.amb) and .glass box-shadows, painted under the interactive windows by the
        # click-through layer - never by those windows (any alpha above 0 there would catch clicks).
        overlay = self.overlay()
        win = overlay.hud
        win.set_caption(CAPTION)
        win.set_briefing_ready(True)
        win.set_pending(2)
        win.set_recording("12:04")
        card = self.card(overlay)
        overlay.show_on(None)
        area = overlay.hud.area()
        target = QRect(area.left() + 80, area.top() + 360, 200, 120)
        overlay.show_card(card, target)
        settle()
        layer = overlay.layer_for(QGuiApplication.primaryScreen())
        self.assertTrue(layer.isVisible())
        self.assertTrue(layer.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        self.assertTrue(has(layer.windowFlags(), Qt.WindowType.WindowTransparentForInput))
        looks = [(glow.look, glow.tone) for glow in layer.glows()]
        self.assertEqual(looks, [(hud_overlay.GLOW_BAR, hud_overlay.TONE_CYAN),
                                 (hud_overlay.GLOW_CHIP, hud_overlay.TONE_CYAN),
                                 (hud_overlay.GLOW_CHIP, hud_overlay.TONE_AMBER),
                                 (hud_overlay.GLOW_CHIP, hud_overlay.TONE_CYAN),
                                 (hud_overlay.GLOW_CARD, hud_overlay.TONE_CYAN)])
        self.assertEqual(layer.glows()[0].rect, win.bar_rect())
        self.assertEqual(layer.glows()[-1].rect, card.geometry())
        self.assertEqual(overlay.stack_order(), [layer, card, win])          # under every interactive window
        image = layer.grab().toImage()
        dpr = image.devicePixelRatio()

        def alpha(point: QPoint) -> int:
            local = point - layer.geometry().topLeft()
            return image.pixelColor(round(local.x() * dpr), round(local.y() * dpr)).alpha()

        bar = win.bar_rect()
        chip = win.chips[hud_overlay.CHIP_APPROVALS]
        chip_rect = QRect(chip.mapToGlobal(QPoint(0, 0)), chip.size())
        for point in (QPoint(bar.center().x(), bar.bottom() + 12), QPoint(bar.right() + 6, bar.center().y()),
                      QPoint(chip_rect.center().x(), chip_rect.bottom() + 6),
                      QPoint(card.geometry().center().x(), card.geometry().bottom() + 14)):
            self.assertGreater(alpha(point), 10, point)                      # a soft shadow beside each
        self.assertEqual(alpha(QPoint(bar.center().x(), bar.bottom() + 120)), 0)  # and nothing far from them
        panel = win.background().toImage()                                    # the HUD window itself: clear margins
        self.assertEqual(panel.pixelColor(panel.width() - 1, 2).alpha(), 0)
        self.assertFalse(has(win.windowFlags(), Qt.WindowType.WindowTransparentForInput))
        self.assertEqual(card.cut_texts(), [])

    def test_the_layer_repaints_only_when_a_shape_changes(self) -> None:
        overlay = self.overlay()
        win = overlay.hud
        win.set_caption(CAPTION)
        win.set_pending(2)
        win.set_recording("12:04")
        overlay.show_on(None)
        settle()
        layer = overlay.layer_for(QGuiApplication.primaryScreen())
        counter = PaintCounter(layer)
        for second in range(5, 11):                                         # REC's tick: same width (tabular)
            win.set_recording(f"12:{second:02d}")
            win.set_pending(2)                                               # a glance that changes nothing
            win.set_look(orb_mod.SPEAKING if second % 2 else orb_mod.IDLE)   # the orb's look: still no shape
            settle()
        self.assertEqual(counter.count(layer), 0)
        win.set_caption(LONG_CAPTION)                                        # the caption grows: the bar grows
        settle()
        self.assertGreaterEqual(counter.count(layer), 1)
        self.assertTrue(all(rect.width() < layer.width() for rect in counter.rects))   # only where it changed
        self.assertEqual(layer.glows()[0].rect, win.bar_rect())
        counter.reset()
        card = self.card(overlay)
        overlay.show_card(card, QRect(80, 300, 200, 120))                     # a card placed: its shadow
        settle()
        self.assertGreaterEqual(counter.count(layer), 1)
        self.assertEqual(layer.glows()[-1].rect, card.geometry())
        overlay.hide()
        self.assertFalse(layer.isVisible())
        overlay.shutdown()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(overlay.layers(), [])


class NeighbourTests(_OverlayTest):
    def test_the_rec_pill_is_held_while_the_hud_shows_and_the_toast_stays_off_the_bar(self) -> None:
        pill = meetings_view.RecPill()
        toast = states_view.Toast()
        for widget in (pill, toast):
            self.addCleanup(widget.deleteLater)
            self.addCleanup(widget.hide)
        pill.show_recording("12:04")
        toast.post("Your AM briefing is ready, sir.", "View", "view")
        overlay = self.overlay()
        card = self.card(overlay)
        overlay.set_neighbours(toast=toast, rec_pill=pill)
        overlay.show_on(None)
        overlay.show_card(card, QRect(80, 300, 200, 120))
        settle()
        win = overlay.hud
        self.assertTrue(pill.geometry().intersects(win.occupied_rect()))     # top centre: where the bar is
        self.assertFalse(pill.isVisible())
        self.assertTrue(pill.is_held())
        pill.show_recording("12:05")                                          # the meetings tick, each second
        settle()
        self.assertFalse(pill.isVisible())
        self.assertEqual(pill.text(), "Recording 12:05")                      # it keeps its time
        self.assertTrue(toast.isVisible())
        self.assertFalse(toast.geometry().intersects(win.occupied_rect()))   # bottom right: never near the bar
        order = overlay.stack_order()
        layer = overlay.layer_for(QGuiApplication.primaryScreen())           # the shadows, under everything
        self.assertEqual(order, [layer, card, toast, win])                  # nothing of Jarvis's over the bar
        self.assertEqual(self.native.stacks[-1], [int(window.winId()) for window in order])
        overlay.hide()
        self.assertFalse(pill.is_held())
        pill.show_recording("12:06")
        settle()
        self.assertTrue(pill.isVisible())

    def test_a_pill_given_while_the_hud_shows_is_held_at_once(self) -> None:
        pill = meetings_view.RecPill()
        self.addCleanup(pill.deleteLater)
        self.addCleanup(pill.hide)
        overlay = self.overlay()
        overlay.show_on(None)
        pill.show_recording("0:01")
        settle()
        overlay.set_neighbours(rec_pill=pill)
        self.assertFalse(pill.isVisible())
        overlay.set_neighbours()                                              # let go of it
        self.assertFalse(pill.is_held())
        self.assertEqual(overlay.neighbours(), (None, None))

    def test_a_pill_that_cannot_be_held_is_hidden_and_never_over_the_bar(self) -> None:
        other = QWidget(None, meetings_view.PILL_FLAGS)
        self.addCleanup(other.deleteLater)
        other.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        other.show()
        overlay = self.overlay()
        overlay.set_neighbours(rec_pill=other)
        overlay.show_on(None)
        settle()
        self.assertFalse(other.isVisible())
        other.show()
        overlay.restack()
        layer = overlay.layer_for(QGuiApplication.primaryScreen())
        self.assertEqual(overlay.stack_order(), [layer, other, overlay.hud])


class SwitchTests(_OverlayTest):
    """What the switch's flight (alive.WindowFlight; P1.5) takes from the HUD: its pictures, its reach, and the pill
    held from the switch's start until the window is back."""

    def hud(self) -> tuple[hud_overlay.HudOverlay, hud_overlay.HudWindow]:
        overlay = self.overlay()
        win = overlay.hud
        win.set_caption(CAPTION)
        win.set_briefing_ready(True)
        win.set_pending(2)
        return overlay, win

    def test_prepare_places_it_hidden_and_the_scene_is_the_hud_as_it_will_show(self) -> None:
        overlay, win = self.hud()
        overlay.prepare(None)
        self.assertFalse(win.isVisible() or overlay.is_shown() or any(w.isVisible() for w in overlay.windows()))
        area = win.area()
        self.assertTrue(area.isValid())
        scene = overlay.flight_scene()
        self.assertFalse(win.isVisible())                                     # taking its pictures shows nothing
        origin = win.geometry().topLeft()
        glows = win.glows()
        self.assertEqual(scene.bar, win.bar_rect())
        self.assertEqual(scene.bar_glow.rect(), hud_overlay.glow_extent(glows[0]))
        self.assertEqual([layer.rect() for layer, _glow in scene.chips],
                         [chip.geometry().translated(origin) for chip in win.chips.values() if not chip.isHidden()])
        self.assertEqual([glow.rect() for _layer, glow in scene.chips], [hud_overlay.glow_extent(g) for g in glows[1:]])
        self.assertEqual((scene.panel.rect(), scene.inner.rect()), (win.geometry(), win.geometry()))
        self.assertIs(scene.orb, win.orb)
        self.assertEqual(scene.orb_center, QPointF(win.orb_center()))
        self.assertEqual(scene.strip, QRect(area.left(), area.top(), area.width(), hud_overlay.BAR_TOP))

    def test_the_flights_pictures_add_up_to_the_hud_as_it_shows(self) -> None:
        overlay, win = self.hud()
        overlay.prepare(None)
        dpr = win.devicePixelRatioF()
        panel, inner, chips = win.flight_pictures(dpr)
        self.assertEqual(len(chips), 2)
        overlay.show_on(None)
        settle()
        shown = win.grab().toImage().convertToFormat(QImage.Format.Format_ARGB32)
        composed = QImage(shown.size(), QImage.Format.Format_ARGB32_Premultiplied)
        composed.setDevicePixelRatio(dpr)
        composed.fill(Qt.GlobalColor.transparent)
        painter = QPainter(composed)
        painter.drawPixmap(QPoint(0, 0), panel)
        painter.drawPixmap(QPoint(0, 0), inner)
        for pixmap, rect in chips:
            painter.drawPixmap(rect.topLeft(), pixmap)
        painter.end()
        composed = composed.convertToFormat(QImage.Format.Format_ARGB32)
        bar = win.bar_rect_local()
        worst, seen = 0, 0
        areas = [QRect(bar.left() + hud_overlay.ORB_PAD, bar.top(), bar.width() - hud_overlay.ORB_PAD, bar.height()),
                 *(rect for _pixmap, rect in chips)]              # clear of the orb: its disc and frames fly apart
        for area in areas:
            for y in range(area.top(), area.bottom(), 2):
                for x in range(area.left(), area.right(), 3):
                    a, b = shown.pixelColor(round(x * dpr), round(y * dpr)), composed.pixelColor(round(x * dpr),
                                                                                                round(y * dpr))
                    worst = max(worst, *(abs(p - q) for p, q in zip(a.getRgb(), b.getRgb())))
                    seen += 1
        self.assertGreater(seen, 1000)
        self.assertLessEqual(worst, 2)                             # the same pixels (8-bit rounding of the overs)
        disc = QPoint(hud_overlay.ORB_LEFT - 2, hud_overlay.BAR_TOP + hud_overlay.ORB_DROP)   # left of the bar
        self.assertEqual(panel.toImage().pixelColor(disc).alpha(), 0)          # the panel has no disc
        self.assertGreater(win.background().toImage().pixelColor(disc).alpha(), 150)      # the window's paint has

    def test_the_reach_covers_three_caption_lines_and_the_shadows_never_the_whole_screen(self) -> None:
        overlay, win = self.hud()
        overlay.show_on(None)
        settle()
        reach = overlay.reach()
        self.assertTrue(reach.contains(win.geometry()))
        for glow in overlay.glows():
            self.assertTrue(reach.contains(hud_overlay.glow_extent(glow)), glow.look)
        win.set_caption(LONG_CAPTION)                               # three lines: the window grows down
        settle()
        self.assertEqual(win.caption_lines(), 3)
        self.assertTrue(reach.contains(win.geometry()))
        for glow in overlay.glows():
            self.assertTrue(reach.contains(hud_overlay.glow_extent(glow)), glow.look)
        screen = QGuiApplication.primaryScreen().geometry()
        self.assertLess(reach.intersected(screen).height(), screen.height() // 2)

    def test_the_pill_is_held_from_prepare_until_release(self) -> None:
        pill, other = meetings_view.RecPill(), meetings_view.RecPill()
        for widget in (pill, other):
            self.addCleanup(widget.deleteLater)
            self.addCleanup(widget.hide)
        overlay = self.overlay()
        overlay.set_neighbours(rec_pill=pill)
        self.assertFalse(pill.is_held())
        overlay.prepare(None)                                       # the switch to the HUD starts
        self.assertTrue(pill.is_held())
        overlay.set_neighbours(rec_pill=other)                      # a recording started on the way
        self.assertTrue(other.is_held())
        self.assertFalse(pill.is_held())
        overlay.show_on(None)
        overlay.hide(release=False)                                 # the switch back starts: still held
        self.assertTrue(other.is_held())
        other.show_recording("0:07")
        settle()
        self.assertFalse(other.isVisible())
        overlay.release()                                           # the window is back
        self.assertFalse(other.is_held())
        overlay.show_on(None)
        overlay.release()                                           # never while the HUD shows
        self.assertTrue(other.is_held())


if __name__ == "__main__":
    unittest.main()
