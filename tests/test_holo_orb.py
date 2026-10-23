"""Tests for the Hologram orb (briefing_reader.orb): states, transitions, frame rates, when it stops, the
voice level, reduced motion, the paint budget, the mini orb and the snapshot the switch to the desktop flies.

Qt runs offscreen in this process and shows nothing.
"""

from __future__ import annotations

import time
import unittest

from PySide6.QtCore import QEvent, QEventLoop, QObject, Qt, QTimer
from PySide6.QtGui import QKeyEvent, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from briefing_reader import config, holo, hud, orb

_app: QApplication | None = None
_GROUND = holo.GroundCache()


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_holo_orb", "-platform", "offscreen"])
    hud.load_fonts(config.PROJECT_ROOT / "fonts")


def settle(rounds: int = 4) -> None:
    for _ in range(rounds):
        QApplication.processEvents()


def wait(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class Stage(QWidget):
    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        _GROUND.paint_slice(painter, self.rect(), self.rect(), self.size(), self.devicePixelRatioF())
        painter.end()


class PaintCounter(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def eventFilter(self, _watched, event) -> bool:  # noqa: N802 - Qt override
        if event.type() == QEvent.Type.Paint:
            self.count += 1
        return False


class _OrbCase(unittest.TestCase):
    def setUp(self) -> None:
        holo.set_reduced_motion(False)
        orb.set_session_locked(False)
        orb.frame_clock().set_deep(False)       # the clock is shared: no test starts in deep idle
        orb.note_activity()
        self.addCleanup(holo.set_reduced_motion, None)
        self.addCleanup(orb.set_session_locked, False)
        self.addCleanup(orb.frame_clock().set_deep, False)

    def stage(self, px: int = orb.HERO) -> tuple[Stage, orb.OrbWidget]:
        stage = Stage()
        stage.resize(600, 420)
        widget = orb.OrbWidget(px, stage)
        widget.set_ground(orb.ground_painter(_GROUND))
        widget.move(40, 20)
        self.addCleanup(stage.deleteLater)
        self.addCleanup(stage.hide)
        stage.show()
        settle()
        return stage, widget


class StateTests(_OrbCase):
    def test_state_words(self) -> None:
        self.assertEqual(orb.state_word(orb.IDLE), "STANDBY")
        self.assertEqual(orb.state_word(orb.NEEDS), "NEEDS YOU")
        self.assertEqual(orb.state_word(orb.WAITING), "WAITING FOR THE AM BRIEFING")
        self.assertEqual(orb.state_word(orb.SPEAKING, muted=True), "MUTED")
        self.assertEqual(orb.state_word(orb.IDLE, offline=True), "OFFLINE VOICE")
        self.assertEqual(orb.state_word(orb.SPEAKING, offline=True), "SPEAKING")

    def test_set_state_and_accessible_name(self) -> None:
        widget = orb.OrbWidget(orb.HERO)
        self.addCleanup(widget.deleteLater)
        self.assertEqual(widget.accessibleName(), "Jarvis, standby")
        widget.set_state(orb.SPEAKING)
        self.assertEqual(widget.look(), (orb.SPEAKING, False, False))
        self.assertEqual(widget.accessibleName(), "Jarvis, speaking")
        widget.set_state(orb.MUTED)
        self.assertEqual(widget.look(), (orb.IDLE, True, False))
        self.assertEqual(widget.accessibleName(), "Jarvis, muted")
        widget.set_state(orb.NEEDS)
        self.assertEqual(widget.look(), (orb.NEEDS, True, False))          # the modifier stays
        widget.set_look(orb.NEEDS)
        self.assertEqual(widget.accessibleName(), "Jarvis, needs you")
        self.assertIn("waiting for your decision", widget.accessibleDescription())
        widget.set_state("nonsense")
        self.assertEqual(widget.state(), orb.IDLE)

    def test_frame_interval_per_state(self) -> None:
        widget = orb.OrbWidget(orb.HERO)
        self.addCleanup(widget.deleteLater)
        # Round 1 decision: 10 fps at rest (spec 12.9 said 15), 30 fps listening / thinking / speaking.
        for state, ms in ((orb.IDLE, 100), (orb.LISTENING, 33), (orb.THINKING, 33), (orb.SPEAKING, 33),
                          (orb.NEEDS, 100), (orb.WAITING, 100), (orb.MUTED, 100), (orb.OFFLINE, 100)):
            widget.set_state(state)
            widget.settle()
            self.assertEqual(widget.frame_interval(), ms, state)
        widget.set_look(orb.SPEAKING, muted=True)
        widget.settle()
        self.assertEqual(widget.frame_interval(), 33)        # muted, but the briefing plays: the crown moves

    def test_transitions_run_at_30_fps_then_drop(self) -> None:
        widget = orb.OrbWidget(orb.HERO)
        self.addCleanup(widget.deleteLater)
        widget.set_state(orb.LISTENING)
        self.assertEqual(widget.frame_interval(), 33)
        widget.set_state(orb.IDLE)
        self.assertEqual(widget.frame_interval(), 33)        # the 800 ms change to idle
        widget.settle()
        self.assertEqual(widget.frame_interval(), 100)

    def test_scales_cross_fade_over_800_ms(self) -> None:
        widget = orb.OrbWidget(orb.HERO)
        self.addCleanup(widget.deleteLater)
        t0 = widget._now()
        widget.set_state(orb.LISTENING)
        mid = widget.sample(t0 + 0.3)
        self.assertTrue(0.86 < mid.scales[0] < 1.0, mid.scales[0])
        self.assertTrue(0.6 < mid.opacities[0] < 1.0)
        end = widget.sample(t0 + 0.9)
        self.assertAlmostEqual(end.scales[0], 0.86)
        self.assertAlmostEqual(end.opacities[0], 1.0)
        self.assertAlmostEqual(end.halo_level, 0.85)

    def test_needs_arc_draws_in_and_fades_out(self) -> None:
        widget = orb.OrbWidget(orb.HERO)
        self.addCleanup(widget.deleteLater)
        t0 = widget._now()
        widget.set_state(orb.NEEDS)
        self.assertLess(widget.sample(t0 + 0.1).arc, 1.0)
        full = widget.sample(t0 + 1.05)
        self.assertEqual((full.arc, full.alert), (1.0, 1.0))
        self.assertEqual(full.halos[-1][0][0], holo.AMBER)
        t1 = widget._now()
        widget.set_state(orb.IDLE)
        self.assertTrue(0.0 < widget.sample(t1 + 0.2).alert < 1.0)
        self.assertEqual(widget.sample(t1 + 0.45).alert, 0.0)

    def test_ripples_finish_and_no_new_one_starts(self) -> None:
        widget = orb.OrbWidget(orb.HERO)
        self.addCleanup(widget.deleteLater)
        t0 = widget._now()
        widget.set_state(orb.LISTENING)
        self.assertTrue(widget.sample(t0 + 0.7).ripples)
        widget.set_state(orb.IDLE)                             # leaves listening a moment after t0
        self.assertTrue(widget.sample(t0 + 0.9).ripples)       # the running ripple finishes
        self.assertFalse(widget.sample(t0 + 2.4).ripples)      # and none starts after leaving

    def test_thinking_boost_eases_in_without_jumps(self) -> None:
        widget = orb.OrbWidget(orb.HERO)
        self.addCleanup(widget.deleteLater)
        t0 = widget._now()
        widget.sample(t0)
        widget.set_state(orb.THINKING)
        self.assertTrue(0.0 < widget._tw["boost"].value(t0 + 0.3) < 1.0)
        angles = []
        for i in range(1, 40):
            angles.append(widget.sample(t0 + i * 0.033).angles[3])
        steps = [abs((b - a + 180) % 360 - 180) for a, b in zip(angles, angles[1:])]
        self.assertLess(max(steps), 360 / 1.2 * 0.034 + 13 * 0.034 + 0.5)    # never more than one frame of speed
        self.assertGreater(steps[-1], steps[0])                               # and it accelerates

    def test_level_drives_crown_and_core_with_synthetic_fallback(self) -> None:
        widget = orb.OrbWidget(orb.HERO)
        self.addCleanup(widget.deleteLater)
        widget.set_state(orb.SPEAKING)
        widget.settle()
        t0 = widget._now()
        frame = None
        for i in range(1, 20):
            widget.set_level(1.0)
            frame = widget.sample(t0 + i * 0.033)
        self.assertGreater(widget.level(), 0.95)
        self.assertGreater(frame.voice, 0.9)
        self.assertGreater(min(frame.bars), 14.0)
        self.assertAlmostEqual(frame.core_scale, 1.0 + 0.18 * widget.level(), delta=0.03)
        for i in range(20, 40):
            widget.set_level(0.0)
            frame = widget.sample(t0 + i * 0.033)
        self.assertLess(widget.level(), 0.05)
        self.assertLess(max(frame.bars), 6.0)
        widget.set_level(None)
        frame = widget.sample(t0 + 2.0)
        frame = widget.sample(t0 + 2.4)
        self.assertLess(frame.voice, 0.05)                                   # the synthetic curve took over
        self.assertTrue(0.0 <= frame.level <= 1.0)
        widget.set_level(7.0)
        self.assertEqual(widget._level_target, 1.0)

    def test_reduced_motion_paints_a_still_frame(self) -> None:
        holo.set_reduced_motion(True)
        widget = orb.OrbWidget(orb.HERO)
        self.addCleanup(widget.deleteLater)
        widget.set_state(orb.SPEAKING)
        first, second = widget.sample(10.0), widget.sample(11.3)
        self.assertEqual(first.angles, second.angles)
        self.assertEqual((first.core_scale, second.core_scale), (1.0, 1.0))
        self.assertEqual(first.bars, second.bars)
        self.assertEqual(first.ripples, ())

    def test_sizes_and_level_of_detail(self) -> None:
        hero, compact = orb.OrbWidget(orb.HERO), orb.OrbWidget(orb.COMPACT)
        self.addCleanup(hero.deleteLater)
        self.addCleanup(compact.deleteLater)
        self.assertEqual((hero.detail(), compact.detail()), ("full", "compact"))
        self.assertEqual(hero.width(), orb.widget_side(orb.HERO))
        self.assertEqual(hero.orb_rect().width(), orb.HERO)
        self.assertGreater(hero.width(), orb.HERO * 1.08)                # room for the halo
        hero.set_orb_size(orb.COMPACT)
        self.assertEqual(hero.detail(), "compact")

    def test_slow_paints_drop_only_the_fine_layers(self) -> None:
        widget = orb.OrbWidget(orb.HERO)
        self.addCleanup(widget.deleteLater)
        widget._paint_ms.extend([3.0] * 60)
        widget._maybe_degrade()
        self.assertEqual(widget.detail(), "full")
        with self.assertLogs("briefing_reader.orb", level="INFO") as logs:
            widget._paint_ms.extend([5.0] * 60)
            widget._maybe_degrade()
        self.assertEqual(widget.detail(), "reduced")
        self.assertIn("Orb: compact layers (paint 5.0 ms)", logs.output[0])
        frame = widget.sample()
        self.assertEqual((frame.compact, frame.fine), (False, False))      # hero strokes, no fine layers
        self.assertFalse(widget.grab().isNull())

    def test_active_looks_turn_the_ring_layer_at_the_idle_rate(self) -> None:
        _stage, widget = self.stage()
        clock = [50.0]
        widget._now = lambda: clock[0]
        widget.set_look(orb.SPEAKING)
        widget.grab()                             # a transition frame paints every ring itself
        self.assertEqual(widget.ring_layer_builds(), 0)
        clock[0] += 2.0                           # the look's transitions are over
        widget.grab()
        self.assertEqual(widget.ring_layer_builds(), 1)
        # Speaking boosts the rings (part B of the motion prototype): the layer then turns every
        # RING_LAYER_BOOST_MS (20 fps) instead of the idle rate; frames inside that reuse it.
        for step in (0.02, 0.02):
            clock[0] += step
            widget.grab()
        self.assertEqual(widget.ring_layer_builds(), 1)
        clock[0] += orb.RING_LAYER_BOOST_MS / 1000.0
        widget.grab()
        self.assertEqual(widget.ring_layer_builds(), 2)
        # The layered frame looks like the frame painted ring by ring at the same moment.
        layered = widget.grab().toImage()
        widget._layered = lambda _f: False
        widget._last_advance = None
        plain = widget.grab().toImage()
        self.assertEqual(layered.size(), plain.size())
        worst = max(abs(layered.pixelColor(x, y).lightness() - plain.pixelColor(x, y).lightness())
                    for x in range(0, layered.width(), 7) for y in range(0, layered.height(), 7))
        self.assertLessEqual(worst, 3)
        del widget._layered
        for state in (orb.THINKING, orb.IDLE):    # boosted rings and the sweep; idle rates: per frame
            widget.set_look(state)
            widget.settle()
            builds = widget.ring_layer_builds()
            for _ in range(3):
                clock[0] += 0.2
                widget.grab()
            self.assertEqual(widget.ring_layer_builds(), builds, state)

    def test_every_look_paints(self) -> None:
        _stage, widget = self.stage()
        for px in (orb.HERO, orb.COMPACT):
            widget.set_orb_size(px)
            for state in orb.STATES:
                widget.set_state(state)
                widget.set_phase(1.7)
                image = widget.grab().toImage()
                off = round(10 * px / 360 * image.devicePixelRatio())  # inside the core, clear of the slash
                center = image.pixelColor(image.width() // 2 + off, image.height() // 2 + off)
                if state == orb.MUTED:
                    self.assertLess(center.lightness(), 50, state)      # hollow core
                else:
                    self.assertGreater(center.lightness(), 70, (px, state))
                widget.set_phase(None)

    def test_a_snapshot_is_the_orb_without_its_ground_and_leaves_it_as_it_was(self) -> None:
        stage, widget = self.stage()
        clock = [50.0]
        widget._now = lambda: clock[0]
        widget.set_look(orb.SPEAKING)
        clock[0] += 2.0
        widget.grab()                                 # steady: the ring layer is built (two-rate rendering)
        ground, layer, builds = widget._ground, widget._layer, widget.ring_layer_builds()
        times = widget.paint_times()
        picture = widget.snapshot().toImage()
        grabbed = widget.grab().toImage()
        self.assertEqual(picture.size(), grabbed.size())
        self.assertEqual(picture.pixelColor(1, 1).alpha(), 0)            # no ground: the corners stay clear
        self.assertEqual(grabbed.pixelColor(1, 1).alpha(), 255)          # (the widget paints its ground there)
        center = picture.pixelColor(picture.width() // 2, picture.height() // 2)
        self.assertGreater(center.lightness(), 70)
        self.assertIs(widget._ground, ground)
        self.assertIs(widget._layer, layer)                             # its own layer was never painted over
        self.assertEqual(widget.ring_layer_builds(), builds)
        self.assertEqual(widget.paint_times()[:len(times)], times)
        widget.set_dim(0.5)                                             # quiet hours: half its strength, over nothing
        dimmed = widget.snapshot().toImage().pixelColor(picture.width() // 2, picture.height() // 2)
        self.assertAlmostEqual(dimmed.alpha() / max(1, center.alpha()), 0.5, delta=0.05)
        widget.set_dim(1.0)
        stage.hide()                                                    # hidden: it still takes one
        settle()
        hidden = widget.snapshot().toImage()
        self.assertGreater(hidden.pixelColor(hidden.width() // 2, hidden.height() // 2).lightness(), 70)


class RunTests(_OrbCase):
    def test_runs_only_while_visible(self) -> None:
        stage, widget = self.stage()
        self.assertTrue(widget.is_animating())
        stage.hide()
        settle()
        self.assertFalse(widget.is_animating())
        self.assertFalse(orb.frame_clock().running())
        stage.show()
        settle()
        self.assertTrue(widget.is_animating())
        widget.hide()
        self.assertFalse(widget.is_animating())

    def test_stops_when_minimized(self) -> None:
        stage, widget = self.stage()
        stage.showMinimized()
        wait(50)
        self.assertFalse(widget.is_animating())
        stage.showNormal()
        wait(50)
        self.assertTrue(widget.is_animating())

    def test_stops_while_the_session_is_locked(self) -> None:
        _stage, widget = self.stage()
        orb.set_session_locked(True)
        self.assertFalse(widget.is_animating())
        orb.set_session_locked(False)
        self.assertTrue(widget.is_animating())
        locked = [True]
        orb.set_lock_probe(lambda: locked[0])
        self.addCleanup(orb.set_lock_probe, None)
        orb.frame_clock()._poll_lock()
        self.assertFalse(widget.is_animating())
        locked[0] = False
        orb.frame_clock()._poll_lock()
        self.assertTrue(widget.is_animating())

    def test_reduced_motion_stops_the_clock(self) -> None:
        _stage, widget = self.stage()
        holo.set_reduced_motion(True)
        self.assertFalse(widget.is_animating())
        holo.set_reduced_motion(False)
        self.assertTrue(widget.is_animating())

    def test_clock_rate_follows_the_state(self) -> None:
        _stage, widget = self.stage()
        for state, ms in ((orb.SPEAKING, 33), (orb.IDLE, 100), (orb.MUTED, 100)):
            widget.set_state(state)
            widget.settle()
            widget.update()
            wait(120)
            self.assertEqual(orb.frame_clock().interval(), ms, state)

    def test_speaking_paints_about_30_frames_a_second(self) -> None:
        _stage, widget = self.stage()
        widget.set_state(orb.SPEAKING)
        widget.settle()
        counter = PaintCounter()
        widget.installEventFilter(counter)
        wait(150)
        counter.count = 0
        wait(600)
        self.assertTrue(8 <= counter.count <= 22, counter.count)

    def test_hero_paint_is_under_budget(self) -> None:
        _stage, widget = self.stage()
        widget.set_state(orb.SPEAKING)
        widget.settle()
        widget.set_animated(False)
        best = None
        for _attempt in range(3):          # the best of three medians: a busy machine must not fail it
            times = []
            for _ in range(41):
                t0 = time.perf_counter()
                widget.repaint()
                times.append((time.perf_counter() - t0) * 1000)
            times.sort()
            best = times[20] if best is None else min(best, times[20])
            if best <= 3.0:
                break
        self.assertLessEqual(best, 3.0)
        self.assertLessEqual(widget.backdrop_count(), orb.BREATH_STEPS)

    def test_mini_orb_moves_only_while_active(self) -> None:
        stage = Stage()
        self.addCleanup(stage.deleteLater)
        self.addCleanup(stage.hide)
        mini = orb.MiniOrb(stage)
        stage.show()
        settle()
        self.assertFalse(mini.is_animating())
        self.assertIsNone(mini.frame_interval())
        mini.set_state(orb.THINKING)
        self.assertTrue(mini.is_animating())
        self.assertEqual(mini.frame_interval(), orb.FRAME_MINI_MS)     # 15 fps, as the spec says
        mini.set_look(orb.NEEDS, muted=True)
        self.assertFalse(mini.is_animating())
        self.assertEqual(mini.accessibleName(), "Jarvis, muted")
        self.assertFalse(mini.grab().isNull())



class DeepIdleTests(_OrbCase):
    """After 2 minutes with no look change and no input, a resting orb eases to half speed at 5 fps."""

    def test_two_quiet_minutes_ease_into_deep_idle(self) -> None:
        _stage, widget = self.stage()
        widget.settle()
        self.assertEqual(widget.frame_interval(), orb.FRAME_IDLE_MS)
        clock = orb.frame_clock()
        clock._activity_at = time.monotonic() - orb.DEEP_IDLE_S - 1     # two quiet minutes have passed
        wait(250)                                                      # the next frame notices
        self.assertTrue(clock.deep())
        self.assertEqual(widget.frame_interval(), orb.FRAME_ACTIVE_MS)  # the 1.5 s ease runs smoothly
        widget.settle()
        self.assertEqual(widget.frame_interval(), orb.FRAME_DEEP_MS)    # then 5 fps
        self.assertAlmostEqual(widget.pace(), orb.DEEP_PACE)
        self.assertTrue(widget.is_animating())
        wait(250)
        self.assertEqual(clock.interval(), orb.FRAME_DEEP_MS)

    def test_a_look_change_input_or_activation_ends_it_at_once(self) -> None:
        stage, widget = self.stage()
        clock = orb.frame_clock()
        orb.watch_input()
        for wake in ("look", "key", "click", "activation", "note"):
            with self.subTest(wake=wake):
                clock.set_deep(True)
                widget.settle()
                self.assertEqual(widget.frame_interval(), orb.FRAME_DEEP_MS)
                if wake == "look":
                    widget.set_state(orb.NEEDS)
                elif wake == "key":
                    QApplication.sendEvent(stage, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A,
                                                            Qt.KeyboardModifier.NoModifier, "a"))
                elif wake == "click":
                    QTest.mouseClick(stage, Qt.MouseButton.LeftButton)
                elif wake == "activation":
                    QApplication.sendEvent(stage, QEvent(QEvent.Type.WindowActivate))
                else:
                    orb.note_activity()
                self.assertFalse(clock.deep(), wake)
                widget.settle()
                self.assertEqual(widget.frame_interval(), orb.FRAME_IDLE_MS)
                self.assertEqual(widget.pace(), 1.0)
                widget.set_state(orb.IDLE)

    def test_active_states_keep_30_fps_and_hidden_stays_still(self) -> None:
        stage, widget = self.stage()
        orb.frame_clock().set_deep(True)
        for state in (orb.LISTENING, orb.THINKING, orb.SPEAKING):
            widget.set_state(state)
            orb.frame_clock().set_deep(True)            # the change woke it; deep idle again
            widget.settle()
            self.assertEqual(widget.frame_interval(), orb.FRAME_ACTIVE_MS, state)
        widget.set_state(orb.MUTED)
        orb.frame_clock().set_deep(True)
        widget.settle()
        self.assertEqual(widget.frame_interval(), orb.FRAME_DEEP_MS)
        stage.hide()
        settle()
        self.assertFalse(widget.is_animating())
        self.assertFalse(orb.frame_clock().running())

    def test_the_slower_pace_never_makes_a_ring_jump(self) -> None:
        widget = orb.OrbWidget(orb.HERO)
        self.addCleanup(widget.deleteLater)
        def step(a: float, b: float) -> float:
            return abs(((b - a + 180.0) % 360.0) - 180.0)

        widget.sample(10.0)
        before = widget.sample(10.1).angles[3]
        step_full = step(before, widget.sample(10.2).angles[3])          # 0.1 s at full speed
        orb.frame_clock().set_deep(True)
        widget._tw["pace"].settle()
        widget.sample(10.4)                       # one 0.2 s step (a 5 fps frame) at half speed
        step_deep = step(widget.sample(10.6).angles[3], widget.sample(10.8).angles[3])
        self.assertGreater(step_full, 0.0)
        self.assertAlmostEqual(step_deep, step_full, delta=0.05 * step_full)   # 2x the time at half speed
        self.assertGreater(widget._lag, 0.0)       # the breathing clock fell behind, smoothly

if __name__ == "__main__":
    unittest.main()
