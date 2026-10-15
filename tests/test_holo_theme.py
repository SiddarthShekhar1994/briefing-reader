"""Tests for the Hologram theme kit (briefing_reader.holo): tokens, type roles, easing, icons, tiles, buttons.

Qt runs offscreen in this process with the bundled fonts and shows nothing.
"""

from __future__ import annotations

import unittest

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from briefing_reader import config, holo, hud

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_holo_theme", "-platform", "offscreen"])
    hud.load_fonts(config.PROJECT_ROOT / "fonts")


def settle(rounds: int = 4) -> None:
    for _ in range(rounds):
        QApplication.processEvents()


class TokenTests(unittest.TestCase):
    def test_colour_tokens_follow_the_spec(self) -> None:
        self.assertEqual(holo.VOID, "#05080C")
        self.assertEqual(holo.HOLO, "#3CD3EE")
        self.assertEqual(holo.AMBER, "#F2B350")
        self.assertEqual(holo.INK_4, "#7F9DA8")
        self.assertAlmostEqual(holo.LINE.alphaF(), 0.20, places=2)
        self.assertAlmostEqual(holo.LINE_STRONG.alphaF(), 0.33, places=2)
        self.assertEqual(holo.LINE.rgb(), QColor(holo.HOLO).rgb())

    def test_mix_blends_colour_and_alpha(self) -> None:
        mid = holo.mix(holo.alpha("#000000", 0.0), holo.alpha("#FFFFFF", 1.0), 0.5)
        self.assertAlmostEqual(mid.alphaF(), 0.5, places=2)
        self.assertAlmostEqual(mid.redF(), 0.5, places=2)
        self.assertEqual(holo.mix(holo.HOLO, holo.AMBER, 1.0).rgb(), QColor(holo.AMBER).rgb())

    def test_count_text_caps_at_nine(self) -> None:
        self.assertEqual([holo.count_text(n) for n in (0, 1, 9, 10, 42)], ["0", "1", "9", "9+", "9+"])


class TypeTests(unittest.TestCase):
    def test_roles_set_size_weight_and_tracking(self) -> None:
        caption = holo.font("caption")
        self.assertEqual(caption.pixelSize(), 24)
        self.assertEqual(caption.letterSpacingType(), QFont.SpacingType.AbsoluteSpacing)
        self.assertAlmostEqual(caption.letterSpacing(), 0.48, delta=1 / 64)      # Qt keeps 1/64 px
        label = holo.font("label")
        self.assertEqual(label.pixelSize(), 11)
        self.assertEqual(label.weight(), QFont.Weight(500))
        self.assertEqual(holo.font("button", 600).weight(), QFont.Weight(600))
        self.assertEqual(holo.line_height("body"), 24)

    def test_caps_roles_upper_case_their_text(self) -> None:
        self.assertEqual(holo.role_text("label", "Needs your OK"), "NEEDS YOUR OK")
        self.assertEqual(holo.role_text("caption", "Good evening, sir."), "Good evening, sir.")


class EasingTests(unittest.TestCase):
    def test_bezier_runs_from_zero_to_one(self) -> None:
        for curve in (holo.EASE_STD, holo.EASE_OUT, holo.EASE_IN, holo.CSS_EASE, holo.CSS_EASE_IN_OUT):
            ease = holo.bezier(curve)
            self.assertEqual(ease(0.0), 0.0)
            self.assertEqual(ease(1.0), 1.0)
            values = [ease(i / 20) for i in range(21)]
            self.assertEqual(values, sorted(values), curve)

    def test_back_curve_overshoots(self) -> None:
        ease = holo.bezier(holo.EASE_BACK)
        self.assertGreater(max(ease(i / 50) for i in range(51)), 1.0)

    def test_standard_curve_is_fast_out(self) -> None:
        self.assertGreater(holo.bezier(holo.EASE_STD)(0.3), 0.6)

    def test_reduced_motion_override_emits_changes(self) -> None:
        seen: list[bool] = []
        holo.motion_settings().changed.connect(seen.append)
        self.addCleanup(holo.motion_settings().changed.disconnect, seen.append)
        self.addCleanup(holo.set_reduced_motion, None)
        holo.set_reduced_motion(False)
        holo.set_reduced_motion(True)
        self.assertTrue(holo.reduced_motion())
        holo.set_reduced_motion(False)
        self.assertFalse(holo.reduced_motion())
        self.assertEqual(seen[-2:], [True, False])


class IconTests(unittest.TestCase):
    def test_every_icon_fits_its_24_unit_box(self) -> None:
        for name in (*holo.ICONS, *holo.FILLED_ICONS):
            box = holo.icon_path(name).boundingRect()
            self.assertGreater(box.width() + box.height(), 4, name)
            self.assertTrue(-0.5 <= box.left() and box.right() <= 24.5 and -0.5 <= box.top() and box.bottom() <= 24.5,
                            (name, box))

    def test_arcs_follow_svg_semantics(self) -> None:
        ellipse = holo.svg_path("M5 6a7 2.8 0 1 0 14 0a7 2.8 0 1 0-14 0z").boundingRect()
        self.assertAlmostEqual(ellipse.left(), 5.0, places=1)
        self.assertAlmostEqual(ellipse.right(), 19.0, places=1)
        self.assertAlmostEqual(ellipse.top(), 3.2, places=1)
        self.assertAlmostEqual(ellipse.bottom(), 8.8, places=1)
        corner = holo.svg_path("M7 3.5h7l4 4V20a.5.5 0 0 1-.5.5h-10").boundingRect()
        self.assertAlmostEqual(corner.bottom(), 20.5, places=1)

    def test_relative_and_smooth_commands(self) -> None:
        path = holo.svg_path("M16 9.5c1 .8 1.5 1.6 1.5 2.5s-.5 1.7-1.5 2.5")
        self.assertAlmostEqual(path.currentPosition().x(), 16.0, places=3)
        self.assertAlmostEqual(path.currentPosition().y(), 14.5, places=3)
        with self.assertRaises(ValueError):
            holo.svg_path("10 10")


class PaintTests(unittest.TestCase):
    def image(self, w: int = 200, h: int = 80) -> QImage:
        image = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(holo.VOID))
        return image

    def test_tiles_paint_their_state(self) -> None:
        for state in holo.TILE_STYLES:
            image = self.image()
            painter = QPainter(image)
            style = holo.paint_tile(painter, QRectF(0, 0, 200, 80), state)
            painter.end()
            self.assertIs(style, holo.TILE_STYLES[state])
            inside = image.pixelColor(100, 40)
            self.assertNotEqual(inside.rgb(), QColor(holo.VOID).rgb(), state)
        image = self.image()
        painter = QPainter(image)
        holo.paint_tile(painter, QRectF(0, 0, 200, 80), holo.TILE_NEEDS)
        painter.end()
        corner = image.pixelColor(1, 12)                       # the amber bracket's leg
        self.assertGreater(corner.red(), corner.blue())

    def test_ground_cache_reuses_its_pixmap(self) -> None:
        cache = holo.GroundCache()
        first = cache.pixmap(QSize(300, 200), 1.0)
        self.assertIs(cache.pixmap(QSize(300, 200), 1.0), first)
        self.assertIsNot(cache.pixmap(QSize(320, 200), 1.0), first)
        self.assertEqual(cache.pixmap(QSize(300, 200), 1.5).width(), 450)

    def test_badges_paint_and_measure(self) -> None:
        image = self.image(300, 40)
        painter = QPainter(image)
        width = holo.paint_status_badge(painter, QPointF(2, 20), "needs your ok", holo.TONE_AMBER, dot=True)
        rect = holo.paint_count_badge(painter, QPointF(290, 4), 12)
        holo.paint_new_dot(painter, QPointF(200, 20))
        painter.end()
        self.assertAlmostEqual(width, holo.status_badge_width("needs your ok", True))
        self.assertGreaterEqual(rect.width(), holo.COUNT_BADGE)
        self.assertEqual(rect.height(), holo.COUNT_BADGE)


class ButtonTests(unittest.TestCase):
    def setUp(self) -> None:
        holo.set_reduced_motion(False)
        self.addCleanup(holo.set_reduced_motion, None)

    def host(self) -> QWidget:
        host = QWidget()
        self.addCleanup(host.deleteLater)
        host.resize(400, 120)
        host.show()
        host.activateWindow()
        settle()
        return host

    def test_sizes_include_the_ring_margin(self) -> None:
        m = 2 * holo.HoloButton.MARGIN
        self.assertEqual(holo.HoloButton("Edit", holo.GHOST).sizeHint().height(), holo.SIZE_MD + m)
        self.assertEqual(holo.HoloButton("Edit", holo.GHOST, size=holo.SIZE_SM).sizeHint().height(),
                         holo.SIZE_SM + m)
        self.assertGreaterEqual(holo.HoloButton("OK", holo.AMBER_COMMIT).sizeHint().width(), 64 + m)
        self.assertEqual(holo.HoloButton("Mute", holo.ICON, icon="speaker", diameter=34).sizeHint(),
                         QSize(34 + m, 34 + m))
        self.assertEqual(holo.HoloButton("Mute", holo.ICON, icon="speaker").accessibleName(), "Mute")

    def test_enter_clicks_and_keyboard_focus_shows_the_ring(self) -> None:
        host = self.host()
        button = holo.HoloButton("Send", holo.AMBER_COMMIT, icon="send", parent=host)
        clicks: list[bool] = []
        button.clicked.connect(lambda: clicks.append(True))
        button.show()
        settle()
        button.setFocus(Qt.FocusReason.TabFocusReason)
        settle()
        self.assertTrue(button.has_focus_ring())
        QTest.keyClick(button, Qt.Key.Key_Return)
        QTest.qWait(250)
        self.assertEqual(clicks, [True])
        button.clearFocus()
        button.setFocus(Qt.FocusReason.MouseFocusReason)
        self.assertFalse(button.has_focus_ring())

    def test_variants_change_colours_on_hover_and_press(self) -> None:
        button = holo.HoloButton("Play", holo.GO)
        rest = button.colors()
        self.assertEqual(rest[0].rgb(), QColor(holo.HOLO).rgb())
        button._hover = 1.0
        self.assertEqual(button.colors()[0].rgb(), QColor(holo.HOLO_HI).rgb())
        button.setDown(True)
        self.assertEqual(button.colors()[0].rgb(), QColor(holo.HOLO_LO).rgb())

    def test_every_variant_paints(self) -> None:
        host = self.host()
        for variant in holo.VARIANTS:
            button = holo.HoloButton("Go", variant, icon="send" if variant != holo.LINK else None, parent=host)
            button.resize(button.sizeHint())
            self.assertFalse(button.grab().isNull(), variant)
        disabled = holo.HoloButton("Send", holo.AMBER_COMMIT, parent=host)
        disabled.setEnabled(False)
        self.assertFalse(disabled.grab().isNull())

    def test_action_chip_pings_once_per_arrival(self) -> None:
        host = self.host()
        chip = holo.ActionChip("Briefing ready", holo.CHIP_CYAN, ping=True, parent=host)
        chip.show()
        settle()
        chip.start_ping()
        self.assertTrue(chip.is_pinging())
        holo.set_reduced_motion(True)
        quiet = holo.ActionChip("Briefing ready", ping=True, parent=host)
        quiet.start_ping()
        self.assertFalse(quiet.is_pinging())
        self.assertGreater(chip.sizeHint().width(), quiet.sizeHint().width() - 1)


if __name__ == "__main__":
    unittest.main()
