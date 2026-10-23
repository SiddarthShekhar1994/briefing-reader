"""Tests for the switch's flight between the window and the desktop HUD (P1.5; SPEC 8.2, hard rules 13 and 17;
Switch.dc.html): alive.WindowFlight's timeline step by step on a fake clock (the window's content fades and its frame
folds into its orb, the orb flies in an arc and lands as the HUD's live orb, the bar's clip opens, what is in it fades
in, the scan line runs, the chips drop in, then the HUD's own windows take over; and the reverse), its window (a
click-through tool window on top that never takes the focus, never the whole screen), its lengths (calm 1.5 x; motion
off or Windows' reduced motion: the instant swap, no flight window at all), and through the app (tests.ui_fakes.
AppHarness): is_hud() and the recorded screen during a flight, a second enter or leave changing nothing, shutdown and
the application quitting mid-flight leaving nothing on screen, the window never closed, the chips landing with the
counts as they are then, the window coming back under the flight on the owner's click (opacity 0, then 1; its screen
assembling), the frame as it left when the hidden window renders blank, repaints only where something moved, and no
window or timer of the flight left once it landed.

Qt runs offscreen in this process and shows nothing. The flight's clock is a fake one and each step is taken by hand
(``tick``): no test waits for it. No OS call is made: the HUD's native seam is a recording fake.
"""

from __future__ import annotations

import unittest
from unittest import mock

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget

from briefing_reader import alive, desktop_ui, dock, holo, hud_overlay, ui
from tests import test_desktop_hud as p14
from tests.ui_fakes import fonts, settle

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_desktop_switch", "-platform", "offscreen"])
    fonts()


def has(flags: Qt.WindowType, flag: Qt.WindowType) -> bool:
    return (flags & flag) == flag


def flights_shown() -> list[QWidget]:
    return [widget for widget in QApplication.topLevelWidgets()
            if widget.objectName() == "JarvisFlight" and widget.isVisible()]


class PaintLog(QObject):
    """The paint events a widget gets, with their rects."""

    def __init__(self) -> None:
        super().__init__()
        self.rects: list[QRect] = []

    def eventFilter(self, _watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if event.type() == QEvent.Type.Paint:
            self.rects.append(QRect(event.rect()))
        return False

    def area(self) -> int:
        return sum(rect.width() * rect.height() for rect in self.rects)

# --------------------------------------------------------------------------
# The flight on its own: the timeline (synthetic pictures)
# --------------------------------------------------------------------------


class FakeOrb:
    """The HUD's live orb as the flight uses it."""

    def __init__(self, px: int = 92) -> None:
        self.px = px
        self.snaps = 0

    def snapshot(self) -> QPixmap:
        self.snaps += 1
        pixmap = QPixmap(104, 104)
        pixmap.fill(QColor(60, 211, 238, 90))
        return pixmap

    def frame_interval(self) -> int:
        return 100

    def orb_size(self) -> int:
        return self.px


def picture(width: int, height: int, color: str = "#0b1520") -> QPixmap:
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(color))
    return pixmap


def scenes(chips: int = 2) -> tuple[alive.WindowScene, alive.FlownOrb, alive.HudScene]:
    window = alive.WindowScene(picture(400, 300), picture(400, 300, "#203040"), QRect(300, 200, 400, 300),
                               QPointF(500.0, 300.0), 200.0)
    flown = alive.FlownOrb(picture(220, 220, "#3cd3ee"), 200.0)
    hud = alive.HudScene(
        bar=QRect(100, 16, 600, 80), panel=alive.FlightLayer(picture(700, 140), QPoint(46, 0)),
        inner=alive.FlightLayer(picture(700, 140, "#102030"), QPoint(46, 0)), bar_glow=None,
        chips=[(alive.FlightLayer(picture(120, 36, "#14202a"), QPoint(160 + 130 * index, 106)), None)
               for index in range(chips)],
        orb=FakeOrb(), orb_center=QPointF(98.0, 52.0), strip=QRect(0, 0, 800, 16))
    return window, flown, hud


class FlightCase(unittest.TestCase):
    def setUp(self) -> None:
        holo.set_reduced_motion(False)
        holo.set_motion_level(holo.MOTION_LIVELY)
        self.addCleanup(holo.set_reduced_motion, None)
        self.addCleanup(holo.set_motion_level, holo.MOTION_LIVELY)
        self.now = [50.0]
        self.flight = alive.WindowFlight(clock=lambda: self.now[0])
        self.addCleanup(self.flight.deleteLater)
        self.addCleanup(self.flight.stop)
        self.signals: list[str] = []
        for name in ("painted", "handover", "windowTime", "finished", "interrupted"):
            getattr(self.flight, name).connect(lambda name=name: self.signals.append(name))

    def fly(self, direction: str, chips: int = 2) -> None:
        window, flown, hud = scenes(chips)
        self.t0 = self.now[0]
        if direction == alive.TO_HUD:
            self.flight.to_hud(window, flown, hud, QRect(0, 0, 800, 600))
        else:
            self.flight.to_window(hud, flown, window, QRect(0, 0, 800, 600))

    def at(self, ms: float) -> None:
        self.now[0] = self.t0 + ms / 1000.0
        self.flight.tick()


class TimelineTests(FlightCase):
    def test_to_the_hud_as_the_prototype_times_it(self) -> None:
        self.fly(alive.TO_HUD)
        flight = self.flight
        self.assertEqual((flight.length(), flight.total_ms(), flight.handover_ms()), (1700, 1700, 1500.0))
        self.at(0)
        self.assertEqual(flight.state("window"), (300.0, 200.0, 400.0, 300.0, 1.0, 1.0))   # as the window was
        self.assertEqual(flight.orb_state()[0], "flown")
        self.assertIsNone(flight.state("bar"))
        self.at(100)
        self.assertEqual(flight.state("window")[:4], (300.0, 200.0, 400.0, 300.0))           # the fold starts now
        self.at(200)
        self.assertEqual(flight.state("window")[4:], (1.0, 0.0))                             # the content has gone
        self.at(559)
        x, y, w, h, frame, _content = flight.state("window")
        self.assertTrue(0.0 < frame < 0.01 and 48.0 <= w < 60.0)                             # folding to 12 %...
        self.assertAlmostEqual((x + w * (500 - 300) / 400), 500.0, delta=0.6)                # ... into the orb
        self.at(560)
        self.assertNotIn("window", flight.parts())
        self.at(340)
        self.assertEqual(flight.orb_state(), ("flown", QPointF(500.0, 300.0), 200.0))
        self.at(570)                                                                          # half way: an arc
        _kind, center, size = flight.orb_state()
        across, up = (500.0 - center.x()) / (500.0 - 98.0), (300.0 - center.y()) / (300.0 - 52.0)
        self.assertGreater(up, across + 0.2)
        self.assertTrue(92.0 < size < 200.0)
        self.at(699)
        self.assertIsNone(flight.state("bar"))
        self.at(720)
        opened, alpha, inner, _n = flight.state("bar")
        self.assertEqual((opened, inner), (0.0, 0.0))
        self.assertAlmostEqual(alpha, 20 / 140, places=3)
        self.at(800)
        self.assertEqual(flight.orb_state(), ("live", QPointF(98.0, 52.0), 92.0))            # landed: the HUD's own
        self.assertGreaterEqual(flight.scenes()[2].orb.snaps, 1)
        self.at(1100)
        self.assertEqual(flight.state("bar")[:2], (1.0, 1.0))
        self.at(1110)
        self.assertEqual(flight.state("bar")[2], 1.0)
        self.at(1000)
        self.assertNotIn("chip0", flight.parts())
        self.at(1079)
        self.assertIn("chip0", flight.parts())
        self.assertNotIn("chip1", flight.parts())                                             # 80 ms after
        self.at(1110)
        self.assertAlmostEqual(flight.state("chip0")[1], 0.5, places=3)
        self.assertLess(flight.state("chip0")[0], 0.0)                                       # dropping in
        self.at(1420)
        self.assertEqual(flight.state("chip0")[:2], (0.0, 1.0))
        head, edge = flight.state("sweep")
        self.assertIsNone(head)                                                               # the head has run
        self.assertGreater(edge, 0.0)
        self.assertNotIn("handover", self.signals)
        self.at(1500)
        self.assertEqual(self.signals.count("handover"), 1)
        self.assertEqual(flight.state("chip1")[:2], (0.0, 1.0))
        self.at(1501)                                                                         # the HUD's own now
        self.assertEqual([part for part in flight.parts() if part != "sweep"], ["orb"])
        self.assertEqual(flight.orb_state()[0], "")                                           # only the glow
        self.at(1699)
        self.assertTrue(flight.is_running() and flight.isVisible())
        self.at(1700)
        self.assertFalse(flight.is_running() or flight.isVisible())
        self.assertEqual(self.signals, ["painted", "handover", "finished"])
        self.assertFalse(flight._timer.isActive())

    def test_the_handover_waits_for_the_last_chip(self) -> None:
        for chips, handover in ((0, 1110.0), (1, 1420.0), (3, 1580.0)):
            self.fly(alive.TO_HUD, chips)
            self.assertEqual(self.flight.handover_ms(), handover, chips)
            self.flight.stop()

    def test_the_sweep_runs_along_the_strip(self) -> None:
        self.fly(alive.TO_HUD)
        self.at(740)
        self.assertNotIn("sweep", self.flight.parts())
        lefts = []
        for ms in (800, 1000, 1200, 1300):
            self.at(ms)
            lefts.append(self.flight.state("sweep")[0][0])
        self.assertEqual(lefts, sorted(lefts))                                                # left to right
        self.assertLess(lefts[0], 0.0)
        self.assertGreater(lefts[-1], 300.0)

    def test_back_to_the_window_as_the_prototype_times_it(self) -> None:
        self.fly(alive.TO_WINDOW)
        flight = self.flight
        self.assertEqual((flight.length(), flight.total_ms()), (1420, 1420))
        self.at(0)
        self.assertEqual(flight.state("bar")[:3], (1.0, 1.0, 1.0))                           # as the HUD was
        self.assertEqual([flight.state(f"chip{index}")[:2] for index in range(2)], [(0.0, 1.0)] * 2)
        self.assertEqual(flight.orb_state(), ("live", QPointF(98.0, 52.0), 92.0))
        self.assertEqual(flight.state("orb")[-1], 0.0)                                       # no glow yet
        self.assertNotIn("window", flight.parts())
        self.assertNotIn("sweep", flight.parts())
        self.at(100)
        self.assertEqual(flight.state("bar")[2], 0.0)                                         # what was in it went
        self.at(160)
        self.assertFalse([part for part in flight.parts() if part.startswith("chip")])
        self.at(340)
        self.assertEqual(flight.orb_state(), ("flown", QPointF(98.0, 52.0), 92.0))
        self.at(400)
        self.assertNotIn("bar", flight.parts())                                               # folded into the orb
        self.at(570)
        _kind, center, _size = flight.orb_state()
        across, down = (center.x() - 98.0) / (500.0 - 98.0), (center.y() - 52.0) / (300.0 - 52.0)
        self.assertGreater(across, down + 0.2)                                                # the mirrored arc
        self.at(620)
        self.assertNotIn("window", flight.parts())
        self.at(700)
        _x, _y, w, _h, frame, content = flight.state("window")
        self.assertEqual(content, 0.0)                                                        # the frame only
        self.assertAlmostEqual(frame, 0.5, places=3)
        self.assertTrue(48.0 < w < 400.0)
        self.at(800)
        self.assertEqual(flight.orb_state(), ("flown", QPointF(500.0, 300.0), 200.0))        # home
        self.assertNotIn("windowTime", self.signals)
        self.at(1100)
        self.assertEqual(self.signals.count("windowTime"), 1)
        self.assertEqual(flight.state("window"), (300.0, 200.0, 400.0, 300.0, 1.0, 0.0))    # whole
        self.at(1260)
        self.assertTrue(0.0 < flight.state("window")[4] < 1.0)                               # fading off it
        orb = flight.state("orb")
        self.assertEqual(orb[5], 1.0)                                                         # the orb stays whole ...
        self.assertTrue(0.0 < orb[6] < 1.0 and 0.0 < orb[7] < 1.0)                           # ... its disc and glow go
        self.at(1420)
        self.assertFalse(flight.is_running() or flight.isVisible())
        self.assertEqual(self.signals, ["painted", "windowTime", "finished"])

    def test_calm_is_one_and_a_half_times_as_long(self) -> None:
        holo.set_motion_level(holo.MOTION_CALM)
        self.fly(alive.TO_HUD)
        self.assertEqual(self.flight.total_ms(), holo.motion_ms(alive.SWITCH_MS))
        self.assertEqual(self.flight.total_ms(), 2550)
        self.at(1700)
        self.assertTrue(self.flight.is_running())
        self.assertAlmostEqual(self.flight.elapsed(), 1700 / 1.5, places=3)
        self.at(2550)
        self.assertFalse(self.flight.is_running())
        self.fly(alive.TO_WINDOW)
        self.assertEqual(self.flight.total_ms(), holo.motion_ms(alive.RETURN_MS))
        self.assertEqual(self.flight.total_ms(), 2130)

    def test_after_tick_runs_at_the_next_step_or_the_stop(self) -> None:
        self.fly(alive.TO_WINDOW)
        calls: list[int] = []
        self.flight.after_tick(lambda: calls.append(1))
        self.assertEqual(calls, [])
        self.at(10)
        self.assertEqual(calls, [1])
        self.flight.after_tick(lambda: calls.append(2))
        self.flight.stop()
        self.assertEqual(calls, [1, 2])
        self.assertNotIn("finished", self.signals)                                            # stop() says nothing

    def test_hidden_from_outside_it_says_so(self) -> None:
        self.fly(alive.TO_HUD)
        self.at(300)
        self.flight.close()
        self.assertEqual(self.signals[-1], "interrupted")
        self.assertFalse(self.flight.is_running())


class FlightWindowTests(FlightCase):
    def test_a_click_through_tool_window_on_top_that_never_takes_the_focus(self) -> None:
        sink = QLineEdit()
        self.addCleanup(sink.deleteLater)
        sink.show()
        sink.activateWindow()
        sink.setFocus()
        settle()
        flight = self.flight
        asked: list[str] = []
        for name in ("activateWindow", "raise_", "setFocus"):
            setattr(flight, name, lambda *_args, n=name: asked.append(n))
        self.fly(alive.TO_HUD)
        settle()
        self.assertEqual(asked, [])                     # never an activation, a raise or a focus of its own
        flags = flight.windowFlags()
        for flag in (Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint,
                     Qt.WindowType.WindowDoesNotAcceptFocus, Qt.WindowType.WindowTransparentForInput):
            self.assertTrue(has(flags, flag), flag)
        self.assertEqual(flight.windowType(), Qt.WindowType.Tool)
        self.assertIsNone(flight.parent())
        for attribute in (Qt.WidgetAttribute.WA_ShowWithoutActivating, Qt.WidgetAttribute.WA_TranslucentBackground,
                          Qt.WidgetAttribute.WA_TransparentForMouseEvents):
            self.assertTrue(flight.testAttribute(attribute), attribute)
        self.assertEqual(flight.focusPolicy(), Qt.FocusPolicy.NoFocus)
        handle = flight.windowHandle()
        self.assertTrue(has(handle.flags(), Qt.WindowType.WindowDoesNotAcceptFocus))
        self.assertIs(handle.property("_q_showWithoutActivating"), True)
        # (The offscreen platform activates every window it shows; Windows honours WindowDoesNotAcceptFocus and
        # _q_showWithoutActivating, as for the HUD's windows.) Nothing in the flight holds the keyboard focus, and
        # the other window's input keeps it as its own.
        focus = QApplication.focusWidget()
        self.assertFalse(focus is not None and (focus is flight or flight.isAncestorOf(focus)))
        self.assertIs(sink.window().focusWidget(), sink)
        self.assertEqual(flight.geometry(), QRect(0, 0, 800, 600))
        self.assertEqual(flight.objectName(), "JarvisFlight")

# --------------------------------------------------------------------------
# Through the app
# --------------------------------------------------------------------------


class FlightApp(p14.HudApp):
    """HudApp with the switch in flight (lively) on a fake clock, stepped by hand."""

    MOTION = holo.MOTION_LIVELY

    def make(self, extra: str = "", **harness: object):
        made = super().make(extra, **harness)
        self.now = [1000.0]
        self.t0 = self.now[0]
        self.desktop.set_flight_clock(lambda: self.now[0])
        self.changes: list[bool] = []
        self.desktop.hudChanged.connect(self.changes.append)
        return made

    def start(self, click: QWidget) -> alive.WindowFlight:
        """The owner's click starting a switch; its flight."""
        self.now[0] += 10.0
        self.t0 = self.now[0]
        QTest.mouseClick(click, Qt.MouseButton.LeftButton)
        flight = self.desktop.flight()
        self.assertIsNotNone(flight)
        settle()
        return flight

    def start_enter(self) -> alive.WindowFlight:
        return self.start(self.reading.dock.button(dock.DESKTOP))

    def start_back(self, chip: str = "") -> alive.WindowFlight:
        return self.start(self.hud().chips[chip] if chip else self.hud().back_button)

    def at(self, ms: float) -> None:
        """``ms`` (real) into the switch: the flight stepped there."""
        self.now[0] = self.t0 + ms / 1000.0
        flight = self.desktop.flight()
        if flight is not None:
            flight.tick()
        settle()

    def land(self) -> None:
        flight = self.desktop.flight()
        self.assertIsNotNone(flight)
        self.at(flight.total_ms())
        self.assertIsNone(self.desktop.flight())
        self.assertFalse(self.desktop.is_switching())


class FlightMotionTests(FlightApp):
    def test_motion_off_and_reduced_motion_swap_at_once_without_a_flight_window(self) -> None:
        for level, reduced in ((holo.MOTION_OFF, False), (holo.MOTION_LIVELY, True)):
            self.make()
            holo.set_motion_level(level)
            holo.set_reduced_motion(reduced)
            self.addCleanup(holo.set_reduced_motion, None)
            with mock.patch.object(alive, "WindowFlight", wraps=alive.WindowFlight) as made:
                QTest.mouseClick(self.reading.dock.button(dock.DESKTOP), Qt.MouseButton.LeftButton)
                self.assert_on_hud()                                       # at once, no settle
                QTest.mouseClick(self.hud().back_button, Qt.MouseButton.LeftButton)
                self.assert_in_window(ui.SCREEN_HOME)
            made.assert_not_called()
            self.assertIsNone(self.desktop.flight())
            self.assertEqual(self.changes, [True, False])

    def test_lively_and_calm_lengths_follow_motion_ms(self) -> None:
        for level, there, back in ((holo.MOTION_LIVELY, 1700, 1420), (holo.MOTION_CALM, 2550, 2130)):
            self.make()
            holo.set_motion_level(level)
            flight = self.start_enter()
            self.assertEqual(flight.total_ms(), holo.motion_ms(alive.SWITCH_MS))
            self.assertEqual(flight.total_ms(), there)
            handover = round(flight.handover_ms() * holo.motion_ms(1000) / 1000)
            self.at(handover - 1)
            self.assertFalse(self.desktop.overlay.is_shown())                 # the HUD waits for the landing
            self.at(handover)
            self.assertTrue(self.desktop.overlay.is_shown() and self.hud().isVisible())
            self.at(there - 1)
            self.assertIs(self.desktop.flight(), flight)
            self.land()
            self.assert_on_hud()
            flight = self.start_back()
            self.assertEqual(flight.total_ms(), back)
            window_time = round((alive.GROW_AT_MS + alive.GROW_MS) * holo.motion_ms(1000) / 1000)
            self.at(window_time - 1)
            self.assertFalse(self.window.isVisible())
            self.at(window_time)
            self.assertTrue(self.window.isVisible())
            self.at(back - 1)
            self.assertIs(self.desktop.flight(), flight)
            self.land()
            self.assert_in_window(ui.SCREEN_HOME)


class FlightRulesTests(FlightApp):
    def test_the_flight_window_never_takes_the_focus_and_is_never_the_whole_screen(self) -> None:
        self.make()
        asked: list[str] = []
        patches = [mock.patch.object(alive.WindowFlight, name, side_effect=lambda *_a, n=name: asked.append(n),
                                     create=False) for name in ("activateWindow", "raise_", "setFocus")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        flight = self.start_enter()
        flags = flight.windowFlags()
        self.assertTrue(all(has(flags, flag) for flag in (
            Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint,
            Qt.WindowType.WindowDoesNotAcceptFocus, Qt.WindowType.WindowTransparentForInput)))
        self.assertEqual(flight.windowType(), Qt.WindowType.Tool)
        self.assertIs(flight.windowHandle().property("_q_showWithoutActivating"), True)
        screen = flight.screen().geometry()
        self.assertTrue(screen.contains(flight.geometry()))
        self.assertNotEqual(flight.geometry(), screen)
        expected = self.window.geometry().united(self.desktop.overlay.reach()).intersected(screen)
        self.assertEqual(flight.geometry(), expected)                     # the window and the HUD's reach only
        self.assertIn(int(flight.winId()), self.native.excluded)         # out of capture, as the HUD's windows
        with mock.patch.object(ui, "force_foreground") as forced:
            self.land()
            self.start_back()
            self.land()
        forced.assert_not_called()
        self.assertEqual(asked, [])                     # neither flight was activated, raised or focused

    def test_a_second_enter_or_leave_during_a_flight_changes_nothing(self) -> None:
        self.make()
        menu = self.c._make_tray_menu()
        self.addCleanup(menu.deleteLater)
        flight = self.start_enter()
        for ms in (0, 300, 900, 1501):
            self.at(ms)
            self.assertFalse(self.desktop.enter_hud(), ms)
            self.assertFalse(self.desktop.leave_hud(), ms)
            self.assertFalse(self.desktop.leave_hud(ui.SCREEN_APPROVALS), ms)
            self.reading.desktopRequested.emit()
            self.hud().bringBackClicked.emit()
            self.hud().chipClicked.emit(hud_overlay.CHIP_APPROVALS)
            self.c._tray_back_action.trigger()
            settle()
            self.assertIs(self.desktop.flight(), flight, ms)
            self.assertTrue(self.desktop.is_hud() and self.desktop.is_switching())
        self.land()
        self.assert_on_hud()
        self.assertEqual(self.changes, [True])
        flight = self.start_back()
        for ms in (0, 500, 1099):
            self.at(ms)
            self.assertFalse(self.desktop.enter_hud(), ms)
            self.assertFalse(self.desktop.leave_hud(), ms)
            self.reading.desktopRequested.emit()
            self.hud().bringBackClicked.emit()
            settle()
            self.assertIs(self.desktop.flight(), flight, ms)
        self.at(1250)                                                     # the window is back; the flight fades
        self.assertFalse(self.desktop.enter_hud())
        self.land()
        self.assert_in_window(ui.SCREEN_HOME)
        self.assertEqual(self.changes, [True, False])
        self.assertEqual(self.closes, [])

    def test_is_hud_and_the_recorded_screen_during_a_flight(self) -> None:
        self.make()
        menu = self.c._make_tray_menu()
        self.addCleanup(menu.deleteLater)
        screens: list[int] = []
        self.reading.tabChanged.connect(screens.append)
        self.start_enter()
        self.assertTrue(self.desktop.is_hud() and self.c._hud_active())   # from the switch's start
        self.assertTrue(self.c._tray_back_action.isVisible())
        self.at(400)
        self.reading.set_tab(ui.SCREEN_APPROVALS)                         # an automatic change mid-flight
        self.assertEqual((self.reading.current_tab(), screens), (ui.SCREEN_HOME, []))
        self.assertEqual(self.desktop.return_screen(), ui.SCREEN_APPROVALS)
        self.land()
        self.start_back()                                                 # Bring Jarvis back: the recorded screen
        self.assertEqual(self.reading.current_tab(), ui.SCREEN_APPROVALS)  # the hidden window turned to it
        self.at(500)
        self.assertTrue(self.desktop.is_hud() and self.c._hud_active())   # still: nothing comes forward by itself
        self.reading.set_tab(ui.SCREEN_LIVE)                              # recorded on the way ...
        self.assertEqual(self.reading.current_tab(), ui.SCREEN_APPROVALS)
        self.at(1099)
        self.assertTrue(self.desktop.is_hud())
        self.assertFalse(self.window.isVisible())
        self.at(1100)
        self.assertFalse(self.desktop.is_hud() or self.c._hud_active())   # the window is back
        self.assertEqual(self.reading.current_tab(), ui.SCREEN_LIVE)       # ... and it wins, as in the window
        self.land()
        self.assert_in_window(ui.SCREEN_LIVE)

    def test_the_window_never_closes(self) -> None:
        self.make()
        with mock.patch.object(self.window, "close") as close:
            for leave in ("", hud_overlay.CHIP_APPROVALS, hud_overlay.CHIP_BRIEFING):
                self.start_enter()
                for ms in (100, 450, 900, 1500):
                    self.at(ms)
                    self.assertFalse(self.window.isVisible())
                self.land()
                self.start_back(leave)
                self.land()
        close.assert_not_called()
        self.assertEqual(self.closes, [])
        self.assertEqual(self.c.state, ui.STATE_READING)

    def test_the_window_hides_at_the_flights_first_frame_and_the_hud_shows_once_all_has_landed(self) -> None:
        self.make()
        overlay = self.desktop.overlay
        hero = self.reading.hero
        hero_center = QPointF(self.window.geometry().topLeft()) + QPointF(hero.mapTo(self.window, QPoint(0, 0))) \
            + QPointF(hero.width() / 2, hero.height() / 2)
        QTest.mouseClick(self.reading.dock.button(dock.DESKTOP), Qt.MouseButton.LeftButton)
        flight = self.desktop.flight()
        self.t0 = self.now[0]
        self.assertTrue(self.window.isVisible() and flight.isVisible())   # the window goes once the flight shows
        settle()
        self.assertFalse(self.window.isVisible())
        self.assertEqual(flight.orb_state(), ("flown", hero_center, float(hero.orb_size())))   # Home's hero flies
        for ms in (339, 800, 1000, 1499):
            self.at(ms)
            self.assertFalse(overlay.is_shown() or self.hud().isVisible(), ms)
            self.assertFalse(any(layer.isVisible() for layer in overlay.layers()), ms)   # no shadow mid-flight
            self.assertEqual(overlay.glows(), [], ms)
        self.assertEqual(flight.orb_state()[:2], ("live", QPointF(self.hud().orb_center())))
        self.at(1500)
        layer = overlay.layer_for(overlay.hud_screen())
        self.assertTrue(self.hud().isVisible() and layer.isVisible())
        self.assertEqual([glow.look for glow in layer.glows()],
                         [hud_overlay.GLOW_BAR, hud_overlay.GLOW_CHIP, hud_overlay.GLOW_CHIP])
        self.assertEqual(overlay.stack_order(), [layer, self.hud()])
        self.at(1501)
        self.assertEqual(flight.orb_state()[0], "")                      # the HUD's own orb from here
        self.land()
        self.assert_on_hud()

    def test_the_chips_land_with_the_counts_as_they_are(self) -> None:
        self.make()
        flight = self.start_enter()
        self.at(300)
        self.c.deny_action(self.c._actions[0].id)                         # decided mid-flight
        settle()
        hud = flight.scenes()[2]
        self.assertEqual(len(hud.chips), 2)
        amber = self.hud().chips[hud_overlay.CHIP_APPROVALS]
        self.assertEqual(amber.text(), ui.needs_ok_chip(1))
        self.assertEqual(hud.chips[1][0].image.toImage(), desktop_ui.render(
            amber, QWidget.RenderFlag.DrawChildren).toImage())            # the flight's chip says it too
        self.reading.set_recording("0:42")                                # a meeting starts recording mid-flight
        settle()
        self.assertEqual(len(flight.scenes()[2].chips), 3)
        self.assertEqual(flight.handover_ms(), 1580.0)                    # the third chip drops in last
        self.at(1500)
        self.assertFalse(self.desktop.overlay.is_shown())
        self.at(1580)
        self.assertEqual(self.hud().chip_texts(), [ui.HOME_BRIEFING_CHIP, ui.needs_ok_chip(1), "REC 0:42"])
        self.land()
        self.assertEqual(self.hud().cut_texts(), [])

    def test_nothing_of_the_flight_runs_or_stays_once_it_landed(self) -> None:
        self.make()
        overlay = self.desktop.overlay
        timers = {window.objectName(): len(window.findChildren(QTimer)) for window in overlay.windows()}
        for direction in ("there", "back"):
            flight = self.start_enter() if direction == "there" else self.start_back()
            timer = flight._timer
            self.assertTrue(timer.isActive())
            self.land()
            self.assertFalse(timer.isActive())
            self.assertFalse(flight.isVisible() or flight.is_running())
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertFalse(shiboken6.isValid(flight))                  # deleted: no window, no timer left
            self.assertEqual(flights_shown(), [])
            self.assertEqual({window.objectName(): len(window.findChildren(QTimer)) for window in overlay.windows()},
                             timers)
            self.assertFalse([t for w in overlay.windows() for t in w.findChildren(QTimer) if t.isActive()])
        self.assertEqual(self.window.windowOpacity(), 1.0)

    def test_it_repaints_only_what_moved(self) -> None:
        self.make()
        flight = self.start_enter()
        log = PaintLog()
        flight.installEventFilter(log)
        whole = flight.width() * flight.height()
        self.at(600)                                                      # the frame has gone; the bar not yet
        before = flight._parts["orb"][1]
        log.rects.clear()
        self.at(650)
        orb = before.united(flight._parts["orb"][1]).translated(-flight.geometry().topLeft())   # was and is
        self.assertEqual(flight.parts(), ["orb"])
        self.assertTrue(log.rects)
        self.assertTrue(all(orb.contains(rect) for rect in log.rects), (orb, log.rects))
        self.assertLess(log.area(), whole / 10)
        log.rects.clear()
        flight.tick()                                                     # nothing moved: nothing repaints
        settle()
        self.assertEqual(log.rects, [])
        self.at(1250)
        log.rects.clear()
        self.at(1260)                                                     # chips dropping, the scan line's edge
        self.assertTrue(log.rects)
        self.assertLess(log.area(), whole / 4)
        self.land()
        log.rects.clear()
        settle()
        self.assertEqual(log.rects, [])

    def test_the_way_back_shows_the_window_under_the_flight_on_the_owners_click(self) -> None:
        self.make()
        self.start_enter()
        self.land()
        calls: list[str] = []
        forward = self.desktop._bring_forward
        self.desktop._bring_forward = lambda window: (calls.append("forward"), forward(window))
        window = self.window

        def opacity(value: float) -> None:
            calls.append(f"opacity {value:g}")
            QWidget.setWindowOpacity(window, value)

        with mock.patch.object(window, "setWindowOpacity", side_effect=opacity), \
                mock.patch.object(ui, "force_foreground") as forced:
            self.start_back(hud_overlay.CHIP_APPROVALS)
            self.assertFalse(self.hud().isVisible() or self.desktop.overlay.is_shown())   # gone at its first frame
            for ms in (300, 800, 1099):
                self.at(ms)
                self.assertFalse(self.window.isVisible(), ms)
            self.assertEqual(calls, [])
            self.assertEqual(self.changes, [True])
            self.at(1100)
            self.assertTrue(self.window.isVisible())
            self.assertEqual(calls[:2], ["opacity 0", "forward"])         # shown at 0 on the owner's click ...
            self.assertTrue(self.reading.transition.is_running())        # ... its screen assembling
            self.assertEqual(self.changes, [True, False])
            self.at(1110)
            self.assertEqual(calls[-1], "opacity 1")                      # ... then 1, under the flight
            self.land()
        forced.assert_not_called()
        self.assertEqual(calls.count("forward"), 1)
        self.assert_in_window(ui.SCREEN_APPROVALS)

    def test_the_meetings_pill_is_held_from_the_switchs_start_until_the_window_is_back(self) -> None:
        self.make()

        class Pill(QWidget):
            def __init__(self) -> None:
                super().__init__()
                self.held: list[bool] = []

            def set_held(self, held: bool) -> None:
                self.held.append(bool(held))

        pill = Pill()
        self.addCleanup(pill.deleteLater)
        self.desktop.set_rec_pill(pill)
        self.assertEqual(pill.held, [])
        self.start_enter()
        self.assertEqual(pill.held, [True])                               # as the bar comes in where it sits
        self.land()
        self.start_back()
        for ms in (0, 600, 1099):
            self.at(ms)
            self.assertNotIn(False, pill.held, ms)                        # held while the window is on its way
        self.at(1100)
        self.assertEqual(pill.held[-1], False)                            # the window is back: let go
        self.land()

    def test_a_blank_render_of_the_hidden_window_flies_back_its_frame_as_it_left(self) -> None:
        self.make()
        self.start_enter()
        left = self.desktop._left_frame
        self.assertIsNotNone(left)
        self.land()
        render = desktop_ui.render

        def blank(widget: QWidget, flags: QWidget.RenderFlag) -> QPixmap:
            pixmap = render(widget, flags)
            pixmap.fill(Qt.GlobalColor.transparent)
            return pixmap

        with mock.patch.object(desktop_ui, "render", side_effect=blank), \
                self.assertLogs("briefing_reader.desktop_ui", level="INFO") as logs:
            flight = self.start_back()
        self.assertIs(flight.scenes()[0].frame, left)
        self.assertIn("rendered blank", "\n".join(logs.output))
        self.land()
        self.assert_in_window(ui.SCREEN_HOME)

    def test_shutdown_mid_flight_stops_it_and_leaves_nothing_on_screen(self) -> None:
        for direction, ms in (("there", 450), ("back", 450), ("back", 1250)):
            self.make()
            if direction == "back":
                self.start_enter()
                self.land()
            flight = self.start_enter() if direction == "there" else self.start_back()
            self.at(ms)
            with mock.patch.object(ui, "_remove_audio_dir_after"), mock.patch.object(self.window, "close") as close:
                self.c.shutdown()
                settle()
            close.assert_not_called()
            self.assertIsNone(self.desktop.flight())
            self.assertFalse(flight.isVisible() or flight.is_running())
            self.assertFalse(self.window.isVisible())
            self.assertEqual(self.window.windowOpacity(), 1.0)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertFalse(shiboken6.isValid(flight))
            shown = [w.objectName() for w in QApplication.topLevelWidgets() if w.isVisible()
                     and w.objectName() in p14.HUD_NAMES | {"JarvisFlight"}]
            self.assertEqual(shown, [], (direction, ms))
            self.assertEqual(self.closes, [])

    def test_the_application_quitting_mid_flight_stops_it_and_brings_nothing_forward(self) -> None:
        for direction in ("there", "back"):
            self.make()
            if direction == "back":
                self.start_enter()
                self.land()
            flight = self.start_enter() if direction == "there" else self.start_back()
            self.at(450)
            self.desktop._bring_forward = forward = mock.Mock()
            with mock.patch.object(ui, "_remove_audio_dir_after"), mock.patch.object(ui, "force_foreground") as forced:
                code, accepted = p14.QuitTests.end_session(self)
            self.assertEqual((code, accepted), (0, [True]), direction)      # the quit went through
            self.assertTrue(self.desktop.quitting())
            self.assertIsNone(self.desktop.flight())
            self.assertFalse(shiboken6.isValid(flight) and (flight.isVisible() or flight.is_running()))
            self.assertFalse(self.window.isVisible() or self.desktop.overlay.is_shown())
            forward.assert_not_called()
            forced.assert_not_called()
            self.assertEqual(self.closes, [])
            self.assertEqual(self.c.state, ui.STATE_QUITTING)


if __name__ == "__main__":
    unittest.main()
