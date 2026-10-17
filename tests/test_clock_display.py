"""Tests for the clock texts on screen, on the 12-hour and the 24-hour clock.

The STATUS value (ui._short_updated and its short form in TelemetryBar), the
SECTIONS intro time (ui._clock_text), the TODAY time split
(hud._split_meridiem), the ACTIVITY stamps and columns and the header clock.
Qt runs offscreen in this process with the bundled fonts and shows nothing;
no Notion, no Google, no .env. "Now" is fixed (Tuesday 2026-10-06 09:00 PDT).
"""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone

from PySide6.QtGui import QFontMetricsF
from PySide6.QtWidgets import QApplication, QLabel

from briefing_reader import config, holo, hud, ui
from briefing_reader.models import BriefingHeader

PDT = timezone(timedelta(hours=-7))
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=PDT)
_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_clock_display", "-platform", "offscreen"])
    hud.load_fonts(config.PROJECT_ROOT / "fonts")


def updated(moment: datetime | None) -> BriefingHeader:
    return BriefingHeader(updated_raw="x" if moment is not None else None, updated_at=moment, run="AM")


def at(month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=PDT)


class ShortUpdatedTests(unittest.TestCase):
    CASES = [  # updated at, 12-hour, 24-hour, short form
        (at(10, 6, 10, 4), "today 10:04 AM", "today 10:04", "today 10:04 AM"),
        (at(10, 6, 0, 5), "today 12:05 AM", "today 00:05", "today 12:05 AM"),
        (at(10, 5, 23, 31), "yesterday 11:31 PM", "yesterday 23:31", "Mon 11:31 PM"),
        (at(10, 5, 12, 0), "yesterday 12:00 PM", "yesterday 12:00", "Mon 12:00 PM"),
        (at(10, 2, 9, 0), "Oct 2 9:00 AM", "Oct 2 09:00", "Oct 2"),
        (at(9, 12, 12, 59), "Sep 12 12:59 PM", "Sep 12 12:59", "Sep 12"),
    ]

    def test_12_hour(self) -> None:
        for moment, expected, _, _ in self.CASES:
            with self.subTest(moment=moment):
                self.assertEqual(ui._short_updated(updated(moment), NOW), expected)

    def test_24_hour(self) -> None:
        for moment, _, expected, _ in self.CASES:
            with self.subTest(moment=moment):
                self.assertEqual(ui._short_updated(updated(moment), NOW, True), expected)

    def test_short_form(self) -> None:
        for moment, _, _, expected in self.CASES:
            with self.subTest(moment=moment):
                self.assertEqual(ui._short_updated(updated(moment), NOW, compact=True), expected)
        self.assertEqual(ui._short_updated(updated(at(10, 5, 23, 31)), NOW, True, compact=True), "Mon 23:31")

    def test_in_the_time_zone_of_now(self) -> None:
        late = datetime(2026, 10, 6, 6, 31, tzinfo=timezone.utc)      # 23:31 PDT the day before
        self.assertEqual(ui._short_updated(updated(late), NOW), "yesterday 11:31 PM")
        self.assertEqual(ui._short_updated(updated(late), NOW, True), "yesterday 23:31")

    def test_not_yet(self) -> None:
        self.assertEqual(ui._short_updated(updated(None), NOW), "not yet")
        self.assertEqual(ui._short_updated(updated(None), NOW, compact=True), "not yet")


class ClockTextTests(unittest.TestCase):
    def test_both_clocks_in_the_time_zone_of_now(self) -> None:
        moment = datetime(2026, 10, 6, 17, 4, tzinfo=timezone.utc)   # 10:04 PDT
        self.assertEqual(ui._clock_text(moment, NOW), "10:04 AM")
        self.assertEqual(ui._clock_text(moment, NOW, True), "10:04")
        self.assertEqual(ui._clock_text(at(10, 6, 13, 5), NOW), "1:05 PM")
        self.assertEqual(ui._clock_text(at(10, 6, 13, 5), NOW, True), "13:05")


class SplitMeridiemTests(unittest.TestCase):
    def test_split(self) -> None:
        cases = {"12:30 PM": ("12:30", "PM"), "9:00 AM": ("9:00", "AM"), "09:00": ("09:00", ""),
                 "23:59": ("23:59", ""), "ALL DAY": ("ALL DAY", ""), "": ("", ""), "PM": ("PM", "")}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(hud._split_meridiem(text), expected)


class TelemetryBarShortFormTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bar = hud.TelemetryBar("Updated")
        self.addCleanup(self.bar.deleteLater)
        metrics = QFontMetricsF(hud.mono_font(11))
        self.fits = math.ceil(metrics.horizontalAdvance("Updated") + metrics.horizontalAdvance("yesterday 11:31 PM")
                              + hud.TelemetryBar._MIN_GAP)

    def test_the_short_form_only_when_the_value_does_not_fit(self) -> None:
        self.bar.set_value("yesterday 11:31 PM", 1.0, short="Mon 11:31 PM")
        self.bar.resize(self.fits, 20)
        self.assertEqual(self.bar.shown_value(), "yesterday 11:31 PM")
        self.bar.resize(self.fits - 1, 20)
        self.assertEqual(self.bar.shown_value(), "Mon 11:31 PM")
        self.assertEqual(self.bar.value(), "yesterday 11:31 PM")

    def test_a_value_without_a_short_form_is_always_shown(self) -> None:
        self.bar.set_value("yesterday 11:31 PM", 1.0, short="Mon 11:31 PM")
        self.bar.set_value("yesterday 23:31", 1.0)
        self.bar.resize(40, 20)
        self.assertEqual(self.bar.shown_value(), "yesterday 23:31")


class ActivityLogTests(unittest.TestCase):
    def columns(self, hour24: bool) -> tuple[list[str], list[tuple[int, int]]]:
        log = hud.ActivityLog()
        self.addCleanup(log.deleteLater)
        log.set_hour24(hour24)
        for moment in (datetime(2026, 10, 6, 9, 5), datetime(2026, 10, 6, 12, 59), datetime(2026, 10, 6, 0, 0)):
            log.add(hud.TAG_DONE, "Fetched briefing", "47 lines", when=moment)
        widths = []
        for row in log._rows:
            time_label, tag_label = (row.layout().itemAt(i).widget() for i in (0, 1))
            self.assertIsInstance(time_label, QLabel)
            widths.append((time_label.maximumWidth(), tag_label.maximumWidth()))
        return [entry[0] for entry in log.entries()], widths

    def check_columns(self, hour24: bool, widest_time: str) -> None:
        """The hologram look (HOLOGRAM_SPEC 2): the time a readout, the tag a caps label; both columns
        from their fonts, the same on every row."""
        _stamps, widths = self.columns(hour24)
        time_width, tag_width = widths[0]
        self.assertEqual(set(widths), {(time_width, tag_width)})        # every row has the same columns
        self.assertGreaterEqual(time_width, QFontMetricsF(holo.font("readout")).horizontalAdvance(widest_time))
        label = QFontMetricsF(holo.font("label"))
        widest_tag = max(label.horizontalAdvance(tag.upper()) for tag in hud.TAG_TONES)
        self.assertGreaterEqual(tag_width, widest_tag)
        self.assertLessEqual(tag_width, math.ceil(widest_tag) + 3)

    def test_12_hour_stamps_and_columns_from_the_font(self) -> None:
        stamps, _widths = self.columns(False)
        self.assertEqual(stamps, ["12:00 AM", "12:59 PM", "9:05 AM"])
        self.check_columns(False, "12:59 PM")

    def test_24_hour_stamps_and_columns_from_the_font(self) -> None:
        stamps, _widths = self.columns(True)
        self.assertEqual(stamps, ["00:00", "12:59", "09:05"])
        self.check_columns(True, "23:59")


class HeaderClockTests(unittest.TestCase):
    def header(self, hour24: bool | None) -> hud.HeaderBar:
        header = hud.HeaderBar()
        self.addCleanup(header.deleteLater)
        if hour24 is not None:
            header.set_hour24(hour24)
        header.set_clock(lambda: datetime(2026, 10, 4, 13, 52))
        return header

    def test_texts_and_accessible_name(self) -> None:
        for hour24, time_text in ((None, "1:52 PM"), (False, "1:52 PM"), (True, "13:52")):
            with self.subTest(hour24=hour24):
                header = self.header(hour24)
                self.assertEqual(header.clock_texts(), ("SUN 04 OCT", time_text))
                self.assertEqual(header._clock.accessibleName(), f"SUN 04 OCT {time_text}")

    def test_only_the_12_hour_clock_has_level_4(self) -> None:
        for hour24, level in ((False, 4), (True, 3)):
            with self.subTest(hour24=hour24):
                header = self.header(hour24)
                header.resize(300, header.height())         # too narrow for any level
                for name in ("notion", "voice", "calendar"):
                    header.set_service(name, hud.STATUS_OK)  # fits the bar to its width
                self.assertEqual(header.compact_level(), level)
                self.assertEqual(header.minimumSizeHint().width(), header._required_width(level))


if __name__ == "__main__":
    unittest.main()
