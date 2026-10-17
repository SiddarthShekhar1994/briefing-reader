"""Tests for the LIVE view's pictures (hud): the painters of every kind at several widths and scales,
_LivePicture (the thumbnail, its states, its keys, its accessible name and its place in the Tab
order), PictureViewer (opened larger with Enter or a click, closed with Esc, focus back on the
thumbnail, refreshed when the step's picture changes, closed on Clear), the same in the pop-out
window, and a payload's picture kept in view during a countdown.

Qt runs offscreen with the bundled fonts and shows nothing. Fakes only; every name, address, id and
text is invented.
"""

from __future__ import annotations

import dataclasses
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from PySide6.QtCore import QBuffer, QByteArray, QEvent, QIODevice, QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetricsF, QImage, QKeyEvent, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget

from briefing_reader import hud, live, pictures, ui
from briefing_reader.actions import parse_action_line
from briefing_reader.ask.mail import MailMessage, MailPerson, MailThread
from briefing_reader.gcal import EventBrief
from briefing_reader.gmail import MailView
from tests.ui_fakes import fonts, settle, wait_for

_app: QApplication | None = None
PDT = timezone(timedelta(hours=-7))
NOW = datetime(2026, 10, 9, 13, 30, tzinfo=PDT)
MOVE = ("Move: acct=work | event=evt0001aa | cal=primary | when=2026-10-09 15:00-16:00 | notify=all | "
        "title=Project sync | at=2026-10-09 14:00-15:00 | link= | body=")
CANCEL = ("Cancel: acct=work | event=evt0002aa | cal=primary | notify=all | title=Design review | "
          "at=2026-10-09 11:00-12:00 | link= | body=")
RSVP = ("RSVP: acct=work | event=evt0003aa | cal=primary | answer=yes | notify=all | title=Team lunch | "
        "at=2026-10-09 12:30-13:30 | due= | link= | body=")
ADD = "Calendar: Dentist | 2026-10-09 16:30-17:15 | | Main St |"
URL = "https://www.example.org/visit"


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_live_picture_view", "-platform", "offscreen"])
    fonts()


def at(hour: int, minute: int = 0, day: int = 9) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=PDT)


def jpeg(width: int = 960, height: int = 600) -> bytes:
    """A small invented 'page': bands of colour (no network, no real page)."""
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#f4f1ea"))
    painter = QPainter(image)
    for band in range(0, height, 60):
        painter.fillRect(0, band, width, 20, QColor("#1d4e89") if (band // 60) % 2 else QColor("#c0392b"))
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "JPG", 80)
    return bytes(data.data())


def email_picture() -> pictures.Picture:
    messages = tuple(MailMessage(f"g{n}", sender=MailPerson(f"person{n}@example.com", f"Person {n}"),
                                 to=(MailPerson("ana@example.com", "Ana Lima"),),
                                 sent_at=datetime(2026, 10, 8, 9, n, tzinfo=PDT), subject="Budget",
                                 text="Hi Ana,\n\n" + "The numbers for the quarter are in. " * 30 + "\n\nPerson")
                     for n in range(3))
    return pictures.email_picture(MailThread("work", "t0001", "Budget", messages), tz=PDT)


def outgoing_picture() -> pictures.Picture:
    view = MailView("me@example.edu", "ana@example.edu", "", "Re: Lunch", "<abc@example.com>", "<abc@example.com>",
                    "18c0ffee00000001", "Hi Ana,\nThursday works.\n\nSam\n")
    return pictures.outgoing_picture(view, account="work")


def known() -> pictures.KnownDays:
    days = pictures.KnownDays()
    days.remember("work", pictures.SOURCE_AGENDA, at(0, day=8), at(0, day=12),
                  [EventBrief("e1", "primary", "Standup", start=at(9), end=at(9, 15)),
                   EventBrief("e2", "primary", "1:1 with Lee", start=at(14, 30), end=at(15)),
                   EventBrief("e3", "primary", "Gym", start=at(18), end=at(19))], at=NOW)
    return days


def day_picture(text: str) -> pictures.Picture:
    picture = pictures.day_change_picture(parse_action_line(text), now=NOW, known=known())
    assert picture is not None
    return picture


def week_picture() -> pictures.Picture:
    events = {"work": [EventBrief(f"s{day}", "primary", "Standup", start=at(9, day=day), end=at(10, day=day))
                       for day in range(9, 16)]}
    return pictures.week_picture(events, at(0, day=8), at(0, day=20), now=NOW)


def page_picture() -> pictures.Picture:
    return pictures.page_ready(URL, image=jpeg(), width=960, height=600, final_url="https://www.example.net/x")


def search_picture(count: int = 12) -> pictures.Picture:
    hits = [(f"Spring admissions {n} - Example University", f"https://www{n}.example.edu/admissions/spring-{n}",
             "Applications close on March 1." if n == 1 else "") for n in range(1, count + 1)]
    return pictures.search_picture("Example University spring 2027 application deadline", hits,
                                   only=("example.edu",))


def every_kind() -> dict[str, pictures.Picture]:
    return {"email": email_picture(), "outgoing": outgoing_picture(), "move": day_picture(MOVE),
            "cancel": day_picture(CANCEL), "add": day_picture(ADD), "rsvp": day_picture(RSVP), "week": week_picture(),
            "page": page_picture(), "search": search_picture()}


def room(viewer: hud.PictureViewer) -> int:
    """The widest picture the viewer draws on its screen (PICTURE_VIEWER_SCREEN of it, less its margins)."""
    area = viewer.window().screen().availableGeometry()
    return int(area.width() * hud.PICTURE_VIEWER_SCREEN - 2 * 16 - 14)


def colours(image: QImage, grid: int = 12) -> set[int]:
    found = set()
    for row in range(grid):
        for column in range(grid):
            x = min(image.width() - 1, int((column + 0.5) * image.width() / grid))
            y = min(image.height() - 1, int((row + 0.5) * image.height() / grid))
            found.add(image.pixel(x, y))
    return found


class PainterTests(unittest.TestCase):
    def test_every_kind_paints_at_every_width_and_scale(self) -> None:
        for name, picture in every_kind().items():
            stamp = pictures.stamp(picture, live.STATUS_RUNNING, "PREVIEW")
            for width in (200, 360, 640):
                for dpr in (1.0, 1.5):
                    with self.subTest(kind=name, width=width, dpr=dpr):
                        cap = hud._PICTURE_MAX[picture.kind]
                        pixmap = hud.picture_pixmap(picture, width, dpr, stamp, cap)
                        image = pixmap.toImage()
                        self.assertFalse(image.isNull())
                        self.assertLessEqual(pixmap.deviceIndependentSize().width(), width + 0.5)
                        self.assertLessEqual(pixmap.deviceIndependentSize().height(), cap + 0.5)
                        self.assertAlmostEqual(image.devicePixelRatio(), dpr)
                        self.assertGreater(len(colours(image)), 3, "drawn, not one flat colour")

    def test_heights(self) -> None:
        email = email_picture()
        heights = [hud.picture_height(email, width) for width in (200, 360, 640, 900)]
        self.assertEqual(heights, sorted(heights, reverse=True))      # wider: the text needs fewer lines
        self.assertGreater(heights[0], hud.LIVE_PICTURE_MAIL_MAX_PX)
        page = page_picture()
        sizes = [hud.picture_height(page, width, max_height=hud.LIVE_PICTURE_PAGE_MAX_PX) for width in (200, 360, 640)]
        self.assertEqual(sizes, [125, 225, 400])                       # its shape kept, at most the cap
        move = day_picture(MOVE)
        natural = hud.picture_height(move, 640)
        squeezed = hud.picture_height(move, 640, max_height=300)
        self.assertGreater(natural, 300)
        self.assertLessEqual(squeezed, 300)
        self.assertEqual(hud.picture_height(pictures.page_pending(URL), 640), 0)

    def test_a_cut_email_fades_and_says_how_to_see_all(self) -> None:
        email = email_picture()
        pixmap = hud.picture_pixmap(email, 360, 1.0, ("", ""), hud.LIVE_PICTURE_MAIL_MAX_PX)
        self.assertEqual(pixmap.height(), hud.LIVE_PICTURE_MAIL_MAX_PX)
        whole = hud.picture_pixmap(email, 360, 1.0, ("", ""), None)
        self.assertGreater(whole.height(), hud.LIVE_PICTURE_MAIL_MAX_PX)

    def test_a_cut_calendar_picture_fades_and_says_how_to_see_all_too(self) -> None:
        move = day_picture(MOVE)
        natural = hud.picture_height(move, 360, max_height=150)
        self.assertGreater(natural, 150)   # even squeezed it does not fit 150 px
        drawn: list[str] = []
        real = QPainter.drawText

        def spy(painter, *args):
            drawn.extend(arg for arg in args if isinstance(arg, str))
            return real(painter, *args)

        with mock.patch.object(QPainter, "drawText", spy):
            pixmap = hud.picture_pixmap(move, 360, 1.0, ("", ""), 150)
        self.assertEqual(pixmap.height(), 150)
        self.assertIn(hud.PICTURE_SEE_ALL, drawn)   # never cut without a word

    def test_a_crowded_day_thumbnail_folds_its_all_day_rows_and_keeps_the_change(self) -> None:
        days = pictures.KnownDays()
        days.remember("work", pictures.SOURCE_AGENDA, at(0, day=8), at(0, day=12),
                      [EventBrief(f"a{n}", "primary", f"Holiday {n}", all_day_start=datetime(2026, 10, 9).date(),
                                  all_day_end=datetime(2026, 10, 9).date()) for n in range(10)], at=NOW)
        early = MOVE.replace("when=2026-10-09 15:00-16:00", "when=2026-10-09 23:00-23:30").replace(
            "at=2026-10-09 14:00-15:00", "at=2026-10-09 01:00-01:30")
        picture = pictures.day_change_picture(parse_action_line(early), now=NOW, known=days)
        cap = hud.LIVE_PICTURE_DAY_MAX_PX
        for width in (220, 300, 420):
            with self.subTest(width=width):
                self.assertLessEqual(hud.picture_height(picture, width, max_height=cap), cap)   # it fits now
                span = hud.picture_focus(picture, width, ("", ""), cap)
                self.assertIsNotNone(span)
                self.assertLessEqual(span[1], cap)   # the new time is in the thumbnail
        titles: list[str] = []
        real = hud._paint_day_block

        def spy(painter, rect, block):
            titles.append(block.title)
            return real(painter, rect, block)

        with mock.patch.object(hud, "_paint_day_block", spy):
            hud.picture_pixmap(picture, 300, 1.0, ("", ""), cap)
        self.assertIn("+9 more", titles)
        self.assertEqual(sum(title.startswith("Holiday") for title in titles), 1)
        titles.clear()
        with mock.patch.object(hud, "_paint_day_block", spy):
            hud.picture_pixmap(picture, 300, 1.0, ("", ""), None)   # the viewer: every one of them
        self.assertEqual(sum(title.startswith("Holiday") for title in titles), 10)

    def test_the_all_day_and_no_time_tags_are_never_cut(self) -> None:
        days = pictures.KnownDays()
        days.remember("work", pictures.SOURCE_AGENDA, at(0, day=8), at(0, day=12),
                      [EventBrief("a1", "primary", "Company offsite", all_day_start=datetime(2026, 10, 9).date(),
                                  all_day_end=datetime(2026, 10, 9).date())], at=NOW)
        picture = pictures.day_change_picture(parse_action_line(CANCEL.replace("at=2026-10-09 11:00-12:00", "at=")),
                                              now=NOW, known=days)
        laid: list[tuple[str, str]] = []
        real = hud._TextLines

        class Spy(real):
            __slots__ = ()

            def __init__(self, text, font_name, width, max_lines=0):
                super().__init__(text, font_name, width, max_lines)
                if text in hud._PIC_STRIP_TAGS:
                    laid.append((text, self.shown))

        for hour24 in (False, True):
            for width in (200, 360, 720):
                with mock.patch.object(hud, "_TextLines", Spy):
                    hud.picture_pixmap(picture, width, 1.0, ("", ""), None)
        self.assertTrue(laid)
        self.assertTrue(all(text == shown for text, shown in laid), laid[:4])

    def test_every_recipient_of_an_outgoing_email_shows(self) -> None:
        to = ", ".join(f"firstname.lastname{n}@example-company.com" for n in range(4))
        view = MailView("me@example.edu", to, "", "Plans", "", "", "", "Hello all\n")
        picture = pictures.outgoing_picture(view, account="work")
        laid: list[str] = []
        real = hud._TextLines

        class Spy(real):
            __slots__ = ()

            def __init__(self, text, font_name, width, max_lines=0):
                super().__init__(text, font_name, width, max_lines)
                if text == to:
                    laid.append(self.shown)

        for width in (290, 360, 480):
            with self.subTest(width=width), mock.patch.object(hud, "_TextLines", Spy):
                laid.clear()
                hud.picture_pixmap(picture, width, 1.0, ("", ""), None)
                self.assertTrue(laid)
                self.assertTrue(all(shown == to for shown in laid), laid)

    def test_short_neighbours_and_the_change_never_print_over_each_other(self) -> None:
        days = pictures.KnownDays()
        days.remember("work", pictures.SOURCE_AGENDA, at(0, day=8), at(0, day=12),
                      [EventBrief("e1", "primary", "Standup", start=at(9), end=at(9, 15))], at=NOW)
        cancel = CANCEL.replace("at=2026-10-09 11:00-12:00", "at=2026-10-09 09:15-09:30")
        picture = pictures.day_change_picture(parse_action_line(cancel), now=NOW, known=days)
        cells: dict[str, QRectF] = {}
        real = hud._paint_day_block

        def spy(painter, rect, block):
            cells[block.title] = QRectF(rect)
            return real(painter, rect, block)

        for width, cap in ((720, None), (260, hud.LIVE_PICTURE_DAY_MAX_PX)):
            with self.subTest(width=width), mock.patch.object(hud, "_paint_day_block", spy):
                cells.clear()
                hud.picture_pixmap(picture, width, 1.5, ("", ""), cap)
                self.assertFalse(cells["Standup"].intersects(cells["Design review"]))

    def test_a_squeezed_thumbnail_keeps_short_neighbours_apart(self) -> None:
        # An early and a late event stretch the hours (5-23 h) and two all-day rows take room: the
        # thumbnail squeezes its hours, where a block's least height stands for up to 105 minutes.
        # Lanes for that scale: no two blocks print over each other, the was / new pair included.
        days = pictures.KnownDays()
        days.remember("work", pictures.SOURCE_AGENDA, at(0, day=8), at(0, day=12), [
            EventBrief("e1", "primary", "Early gym", start=at(6), end=at(7)),
            EventBrief("e2", "primary", "Standup", start=at(9), end=at(9, 30)),
            EventBrief("e3", "primary", "Office hours", start=at(9, 30), end=at(10)),
            EventBrief("e4", "primary", "Late call", start=at(21), end=at(22)),
            EventBrief("e5", "primary", "Holiday", all_day_start=datetime(2026, 10, 9).date(),
                       all_day_end=datetime(2026, 10, 9).date()),
            EventBrief("e6", "primary", "Conference", all_day_start=datetime(2026, 10, 8).date(),
                       all_day_end=datetime(2026, 10, 10).date())], at=NOW)
        move = MOVE.replace("when=2026-10-09 15:00-16:00", "when=2026-10-09 10:30-11:00").replace(
            "at=2026-10-09 14:00-15:00", "at=2026-10-09 10:00-10:30")
        cancel = CANCEL.replace("at=2026-10-09 11:00-12:00", "at=2026-10-09 10:00-10:30")
        squeezed = (hud.LIVE_PICTURE_DAY_MAX_PX, hud._PIC_HOUR_PX)
        real = hud._paint_day_block
        cells: list[tuple[str, str, QRectF]] = []

        def spy(painter, rect, block):
            if not block.all_day and not block.timeless:
                cells.append((block.role, block.title, QRectF(rect)))
            return real(painter, rect, block)

        pph_seen: list[float] = []
        real_lanes = pictures.with_lanes

        def lanes(blocks, min_minutes=30):
            pph_seen.append(min_minutes)
            return real_lanes(blocks, min_minutes)

        for name, text in (("move", move), ("cancel", cancel)):
            picture = pictures.day_change_picture(parse_action_line(text), now=NOW, known=days)
            for width in (360, 480, 640):
                with self.subTest(kind=name, width=width), mock.patch.object(hud, "_paint_day_block", spy), \
                        mock.patch.object(pictures, "with_lanes", lanes):
                    cells.clear()
                    pph_seen.clear()
                    hud.picture_pixmap(picture, width, 1.0, ("", ""), squeezed[0])
                    self.assertTrue(pph_seen and pph_seen[0] > 30, pph_seen)   # squeezed: lanes re-made
                    self.assertEqual(len(cells), 6 if name == "move" else 5)   # 4 others, the change
                    for index, (role, title, rect) in enumerate(cells):
                        for other_role, other_title, other in cells[index + 1:]:
                            self.assertFalse(rect.intersects(other),
                                             f"{role} {title} {rect} / {other_role} {other_title} {other}")
                    # the picture's own focus marks are the same blocks (picture_focus goes through it)
                    self.assertIsNotNone(hud.picture_focus(picture, width, ("", ""), squeezed[0]))

    def test_a_day_pictures_note_is_not_drawn_in_it_a_second_time(self) -> None:
        # The coverage line (others_note) is the picture's note: under the thumbnail, above the
        # viewer's picture - the picture itself does not grow by it at any width.
        picture = day_picture(MOVE)
        self.assertEqual(picture.note, picture.day.others_note)
        longer = pictures.Picture(picture.kind, picture.caption, picture.state, note=picture.note, alt=picture.alt,
                                  day=dataclasses.replace(picture.day, others_note="Other events " * 40))
        for width in (260, 420, 720):
            with self.subTest(width=width):
                self.assertEqual(hud.picture_height(longer, width), hud.picture_height(picture, width))

    def test_a_narrow_week_strip_uses_one_label_form(self) -> None:
        metrics = QFontMetricsF(hud._LiveFonts.get().pic_small)
        labels = ["Fri 9", "Sat 10", "Sun 11", "Mon 12", "Tue 13", "Wed 14", "Thu 15"]
        for room in [metrics.horizontalAdvance(text) + extra for text in ("F 9", "S 10", "Fri 9", "Sat 10", "10")
                     for extra in (-0.5, 0.5)]:
            with self.subTest(room=room):
                shown = hud.week_labels(labels, room, metrics)
                forms = {"full" if " " in text and len(text.split()[0]) > 1 else "letter" if " " in text else "day"
                         for text in shown}
                self.assertEqual(len(forms), 1, shown)
                self.assertTrue(all(text.split()[-1] in label or text.endswith("\u2026")
                                    for text, label in zip(shown, labels)), shown)
        self.assertEqual(hud.week_labels(labels, 1000, metrics), labels)

    def test_a_narrow_week_keeps_the_day_of_the_month(self) -> None:
        # A narrow LIVE column: "Thu 8", else "T 8", else "8" - never "Th..." without its date.
        from PySide6.QtGui import QFontMetricsF

        metrics = QFontMetricsF(hud._LiveFonts.get().pic_small)
        full, short = metrics.horizontalAdvance("Thu 8"), metrics.horizontalAdvance("T 8")
        self.assertEqual(hud.week_label("Thu 8", full + 1, metrics), "Thu 8")
        self.assertEqual(hud.week_label("Thu 8", short + 1, metrics), "T 8")
        self.assertEqual(hud.week_label("Thu 8", metrics.horizontalAdvance("8") + 1, metrics), "8")
        self.assertEqual(hud.week_label("Thu 28", 1, metrics), metrics.elidedText("28", Qt.TextElideMode.ElideRight, 1))
        self.assertEqual(hud.week_label("today", 1, metrics), metrics.elidedText("today", Qt.TextElideMode.ElideRight, 1))


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class LogCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.stream = live.LiveStream(clock=self.clock, wall=lambda: NOW)
        self.version = 0

    def log(self, width: int = 560, height: int = 700) -> hud.LiveLog:
        log = hud.LiveLog(clock=self.clock)
        self.addCleanup(log.deleteLater)
        log.resize(width, height)
        log.show()
        settle()
        self.view = log
        return log

    def apply(self, log: hud.LiveLog | None = None) -> None:
        changes = self.stream.changes(self.version)
        self.version = changes.version
        (log or self.view).apply(changes)
        settle(4)

    def payload(self, picture: pictures.Picture, *, status_text: str = "PREVIEW") -> live.LiveStep:
        task = self.stream.task(live.TASK_ACTION, "Email work: Lunch")
        task.step(live.ACTION_APPROVED, "You approved", status=live.STATUS_OK, fields=[("Card", "ASK EMAIL")])
        step = task.step(live.ACTION_PAYLOAD, "Will send exactly this", status_text=status_text,
                         fields=[("From", "me@example.edu"), ("Gmail thread", "18c0ffee00000001")])
        step.block("Message", "Hi Ana,\nThursday works.\n", start_open=True)
        step.picture(picture)
        self.task = task
        return step


class LivePictureTests(LogCase):
    def test_the_picture_comes_first_in_its_steps_details(self) -> None:
        log = self.log()
        step = self.payload(outgoing_picture())
        self.apply()
        widget = log.step_widget(step.id).picture_widget()
        self.assertIsNotNone(widget)
        details = log.step_widget(step.id).details
        self.assertLess(widget.y(), details.fields.y())
        self.assertEqual(widget.stamp(), ("WILL BE SENT", pictures.TONE_WILL))
        self.assertEqual(widget.accessibleName(),
                         "Email picture: Email from work (me@example.edu) to ana@example.edu, subject Re: Lunch, "
                         "WILL BE SENT. Press Enter to see it larger")
        self.assertEqual(widget.accessibleDescription(), pictures.NOTE_OUTGOING)
        self.assertEqual(widget.cursor().shape(), Qt.CursorShape.PointingHandCursor)
        self.assertGreaterEqual(widget.height(), widget.heightForWidth(widget.width()))
        image = widget.grab().toImage()
        self.assertGreater(len(colours(image)), 3)

    def test_a_finished_step_with_a_picture_starts_open(self) -> None:
        log = self.log()
        task = self.stream.task(live.TASK_ASK, "Ask")
        step = task.step(live.CALENDAR_READ, "Calendar", status=live.STATUS_OK)
        step.picture(week_picture())
        task.finish(live.STATUS_OK)
        self.apply()
        self.assertIsNotNone(log.step_widget(step.id).picture_widget())

    def test_heights_within_the_caps(self) -> None:
        log = self.log(900, 900)
        task = self.stream.task(live.TASK_ASK, "Every kind")
        steps = {}
        for name, picture in every_kind().items():
            steps[name] = task.step(live.MAIL_THREAD, name)
            steps[name].picture(picture)
        self.apply()
        for name, step in steps.items():
            widget = log.step_widget(step.id).picture_widget()
            with self.subTest(kind=name):
                cap = hud._PICTURE_MAX[widget.picture().kind]
                for width in (200, 360, 640, 800):
                    thumb = widget._thumb(width)
                    self.assertLessEqual(thumb[0], min(width, hud.LIVE_PICTURE_MAX_W))
                    self.assertLessEqual(thumb[1], cap)
                    self.assertLessEqual(widget.heightForWidth(width), cap + 120)

    def test_pending_unavailable_and_dropped(self) -> None:
        log = self.log()
        task = self.stream.task(live.TASK_WEB, "Web research")
        pending = task.step(live.WEB_FETCH, "Read a page")
        pending.picture(pictures.page_pending(URL))
        gone = task.step(live.WEB_FETCH, "Read another page")
        gone.picture(pictures.page_unavailable(URL, "the page did not load on this PC"))
        dropped = task.step(live.WEB_FETCH, "And another")
        dropped.picture(page_picture().dropped(live.PICTURE_MEMORY))
        self.apply()
        for step, state, text in ((pending, live.PICTURE_PENDING, pictures.NOTE_OPENING),
                                  (gone, live.PICTURE_UNAVAILABLE, "Picture unavailable: the page did not load"),
                                  (dropped, live.PICTURE_DROPPED, "Picture not kept: dropped to save memory")):
            with self.subTest(state=state):
                widget = log.step_widget(step.id).picture_widget()
                self.assertEqual(widget.picture().state, state)
                self.assertIn(text, widget.accessibleName())
                self.assertEqual(widget.accessibleDescription(), "")   # said once, in the name
                self.assertNotIn("Press Enter", widget.accessibleName())
                self.assertEqual(widget.cursor().shape(), Qt.CursorShape.ArrowCursor)
                widget.setFocus(Qt.FocusReason.TabFocusReason)
                QTest.keyClick(widget, Qt.Key.Key_Return)
                QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(20, 40))
                settle()
                self.assertIsNone(log.picture_viewer())
                self.assertFalse(log.open_picture(step.id))
        widget = log.step_widget(pending.id).picture_widget()
        self.assertGreaterEqual(widget.height(), hud.LIVE_PICTURE_PENDING_PX)
        self.assertTrue(hud._blink_clock().active())           # its glyph blinks while it waits
        pending.picture(page_picture())
        self.apply()
        self.assertEqual(widget.picture().state, live.PICTURE_READY)

    def test_enter_and_click_open_the_viewer_and_esc_closes_it(self) -> None:
        log = self.log()
        step = self.payload(outgoing_picture())
        self.apply()
        widget = log.step_widget(step.id).picture_widget()
        widget.setFocus(Qt.FocusReason.TabFocusReason)
        QTest.keyClick(widget, Qt.Key.Key_Return)
        settle()
        viewer = log.picture_viewer()
        self.assertIsNotNone(viewer)
        self.assertTrue(viewer.isVisible())
        self.assertEqual(viewer.step_id(), step.id)
        self.assertIs(viewer.parentWidget(), log.window())
        self.assertEqual(viewer.windowTitle(), "Email - Jarvis")
        self.assertEqual(viewer.accessibleName(), "Email picture, larger")
        self.assertEqual(viewer.canvas.accessibleName(), viewer.picture().alt)
        self.assertEqual(viewer.close_button.accessibleName(), "Close")
        self.assertFalse(viewer.isModal())
        self.assertIs(QApplication.focusWidget(), viewer.scroll)
        self.assertEqual(viewer.canvas.width(), min(hud.PICTURE_VIEWER_MODEL_W, room(viewer)))
        QTest.keyClick(viewer.scroll, Qt.Key.Key_Escape)
        settle()
        self.assertFalse(viewer.isVisible())
        self.assertIs(QApplication.focusWidget(), widget)
        self.assertTrue(widget.focus_ring())
        for key in (Qt.Key.Key_Space, Qt.Key.Key_Enter):
            with self.subTest(key=key):
                QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(40, 60))
                settle()
                self.assertTrue(viewer.isVisible())
                QTest.keyClick(viewer.scroll, key)
                settle()
                self.assertFalse(viewer.isVisible())
        QTest.keyClick(widget, Qt.Key.Key_Space)
        settle()
        self.assertTrue(viewer.isVisible())
        QTest.keyClick(viewer.scroll, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
        settle()
        self.assertFalse(viewer.isVisible())

    def test_the_viewer_names_what_has_the_focus_and_a_held_enter_keeps_it_open(self) -> None:
        log = self.log()
        step = self.payload(outgoing_picture())
        self.apply()
        widget = log.step_widget(step.id).picture_widget()
        widget.setFocus(Qt.FocusReason.TabFocusReason)
        QTest.keyClick(widget, Qt.Key.Key_Return)
        settle()
        viewer = log.picture_viewer()
        focus = QApplication.focusWidget()
        self.assertIs(focus, viewer.scroll)
        self.assertEqual(focus.accessibleName(), "Email picture")
        self.assertEqual(focus.accessibleDescription(), viewer.picture().alt)
        # said once: the dialog that comes up has its name only (its description would repeat the alt)
        self.assertEqual(viewer.accessibleName(), "Email picture, larger")
        self.assertEqual(viewer.accessibleDescription(), "")
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):   # the key still held: repeats
            with self.subTest(key=key):
                event = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier, "", True)
                QApplication.sendEvent(viewer.scroll, event)
                settle()
                self.assertTrue(viewer.isVisible())
        QTest.keyClick(viewer.scroll, Qt.Key.Key_Return)   # pressed again: it closes
        settle()
        self.assertFalse(viewer.isVisible())
        self.assertEqual(viewer.scroll.accessibleName(), "")
        self.assertEqual(viewer.scroll.accessibleDescription(), "")

    def test_jarvis_closing_the_viewer_takes_no_focus_from_another_window(self) -> None:
        log = self.log()
        task = self.stream.task(live.TASK_WEB, "Web")
        step = task.step(live.WEB_FETCH, "Read a page", status=live.STATUS_WARN)
        step.picture(page_picture())
        self.apply()
        self.assertTrue(log.open_picture(step.id))
        viewer = log.picture_viewer()
        other = QWidget()
        self.addCleanup(other.deleteLater)
        field = QLineEdit(other)
        other.show()
        other.activateWindow()
        field.setFocus()
        settle()
        activated: list[bool] = []
        with mock.patch.object(viewer, "isActiveWindow", return_value=False), \
                mock.patch.object(hud.LiveLog, "activateWindow", lambda _self: activated.append(True)):
            step.picture(pictures.page_unavailable(URL, "the request was cancelled"))
            self.apply()
        self.assertFalse(viewer.isVisible())
        self.assertEqual(activated, [])
        self.assertIsNot(QApplication.focusWidget(), log.step_widget(step.id).picture_widget())
        other.hide()

    def test_the_viewer_draws_again_on_a_screen_with_another_scale(self) -> None:
        log = self.log()
        task = self.stream.task(live.TASK_ASK, "Ask")
        step = task.step(live.MAIL_THREAD, "Read email thread")
        step.picture(email_picture())
        self.apply()
        self.assertTrue(log.open_picture(step.id))
        canvas = log.picture_viewer().canvas
        size = canvas.size()
        self.assertAlmostEqual(canvas.pixmap().devicePixelRatio(), canvas.devicePixelRatioF())
        canvas.devicePixelRatioF = lambda: 2.0   # moved to a 200 % screen
        canvas.grab()
        self.assertAlmostEqual(canvas.pixmap().devicePixelRatio(), 2.0)
        self.assertEqual(canvas.size(), size)
        log.picture_viewer().close()

    def test_the_viewer_scrolls_with_the_keys(self) -> None:
        log = self.log()
        task = self.stream.task(live.TASK_ASK, "Ask")
        step = task.step(live.MAIL_THREAD, "Read email thread")
        step.picture(email_picture())
        self.apply()
        self.assertTrue(log.open_picture(step.id))
        viewer = log.picture_viewer()
        viewer.resize(viewer.width(), 260)
        settle()
        bar = viewer.scroll.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        QTest.keyClick(viewer.scroll, Qt.Key.Key_End)
        self.assertEqual(bar.value(), bar.maximum())
        QTest.keyClick(viewer.scroll, Qt.Key.Key_Home)
        self.assertEqual(bar.value(), 0)
        QTest.keyClick(viewer.scroll, Qt.Key.Key_PageDown)
        self.assertGreater(bar.value(), 0)
        viewer.close()

    def test_the_viewer_follows_its_step_and_closes_on_clear(self) -> None:
        log = self.log()
        step = self.payload(outgoing_picture())
        self.apply()
        self.assertTrue(log.open_picture(step.id))
        viewer = log.picture_viewer()
        self.assertEqual(viewer.stamp(), ("WILL BE SENT", pictures.TONE_WILL))
        step.done(live.STATUS_OK, status_text="SENT EXACTLY THIS")
        self.apply()
        self.assertEqual(viewer.stamp(), ("SENT", pictures.TONE_DONE))
        self.assertIn("SENT", viewer.caption_label.text())
        self.task.finish(live.STATUS_OK)   # a finished task keeps its picture: the viewer stays
        self.apply()
        self.assertTrue(viewer.isVisible())
        self.stream.clear()                # Clear: the step is gone, so is the viewer
        self.apply()
        self.assertFalse(viewer.isVisible())
        self.assertIsNone(log.step_widget(step.id))
        # Closed, the viewer keeps nothing of the picture (it is reused for the next one).
        self.assertIsNone(viewer.picture())
        self.assertTrue(viewer.canvas.pixmap().isNull())
        self.assertEqual(viewer.caption_label.text(), "")

    def test_a_step_that_lost_its_picture_lets_it_go(self) -> None:
        log = self.log()
        task = self.stream.task(live.TASK_ASK, "Ask")
        step = task.step(live.MAIL_THREAD, "Read email thread", key="thread")
        step.picture(email_picture())
        self.apply()
        widget = log.step_widget(step.id).picture_widget()
        self.assertIsNotNone(widget)
        task.step(live.MAIL_THREAD, "Read email thread again", key="thread", fields=[("Subject", "Budget")])
        self.apply()
        details = log.step_widget(step.id).details
        self.assertTrue(details.picture.isHidden())
        self.assertIsNone(details.picture.picture())
        self.assertNotIn(details.picture, details.focusables())

    def test_the_viewer_closes_when_the_picture_is_not_kept_and_on_log_clear(self) -> None:
        log = self.log()
        task = self.stream.task(live.TASK_WEB, "Web")
        step = task.step(live.WEB_FETCH, "Read a page")
        step.picture(page_picture())
        self.apply()
        self.assertTrue(log.open_picture(step.id))
        viewer = log.picture_viewer()
        self.assertEqual(viewer.canvas.width(), min(960, room(viewer)))   # a page at its own width
        step.picture(pictures.page_unavailable(URL, "the request was cancelled"))
        self.apply()
        self.assertFalse(viewer.isVisible())
        step.picture(page_picture())
        self.apply()
        self.assertTrue(log.open_picture(step.id))
        log.clear()
        self.assertFalse(viewer.isVisible())

    def test_tab_order_row_picture_links_blocks(self) -> None:
        log = self.log()
        task = self.stream.task(live.TASK_ACTION, "Move work: Project sync")
        step = task.step(live.ACTION_PAYLOAD, "Will change exactly this", status_text="PREVIEW",
                         fields=[live.Field("Event", "Open the event", link="tab:jarvis")])
        step.block("Exact request to Google", "{}", start_open=False)
        step.picture(day_picture(MOVE))
        self.apply()
        widget = log.step_widget(step.id)
        order = [item for item in log.focusables() if item.isVisibleTo(log)]
        index = order.index(widget.row)
        self.assertIs(order[index + 1], widget.picture_widget())
        self.assertIs(order[index + 2], widget.details.fields.links()[0])
        self.assertIs(order[index + 3], next(iter(widget.details.blocks.values())).toggle)
        widget.row.setFocus(Qt.FocusReason.TabFocusReason)
        self.assertTrue(log.focusNextPrevChild(True))
        self.assertIs(QApplication.focusWidget(), widget.picture_widget())

    def countdown(self, picture: pictures.Picture, width: int = 560, height: int = 260):
        self.stream = live.LiveStream(clock=self.clock, wall=lambda: NOW)
        self.version = 0
        log = self.log(width, height)
        task = self.stream.task(live.TASK_ACTION, "Change work: Project sync")
        task.step(live.ACTION_APPROVED, "You approved", status=live.STATUS_OK,
                  fields=[("Card", "ASK MOVE"), ("Account", "work"), ("Card says", "Project sync")])
        payload = task.step(live.ACTION_PAYLOAD, "Will change exactly this", status_text="PREVIEW",
                            fields=[("Event", "Project sync"), ("New time", "Fri Oct 9 3:00-4:00 PM")])
        payload.picture(picture)
        task.step(live.ACTION_HOLD, "Undo countdown", status_text="SENDING", deadline=self.clock.t + 10)
        self.apply()
        settle(6)
        log.scroll_to_newest()
        settle()
        widget = log.step_widget(payload.id).picture_widget()
        return log, widget, widget.mapTo(log.widget(), QPoint(0, 0)).y()

    def test_a_short_view_shows_the_change_itself_during_a_countdown(self) -> None:
        # The picture's top is the chip, the headline, the day and the morning hours: in a short LIVE
        # tab the changed blocks (the ghost and the new time, the cancel, the answer, the new block)
        # are what is in view.
        # A taller view scrolls only as far as the change needs: when the change fits below the
        # picture's top, that top (the stamp, the headline, the day) stays in view too.
        for name, text in (("move", MOVE), ("cancel", CANCEL), ("rsvp", RSVP), ("add", ADD)):
            for height in (130, 165, 260, 360, 405, 460):
                with self.subTest(kind=name, height=height):
                    log, widget, top = self.countdown(day_picture(text), height=height)
                    first, last = widget.focus_span()
                    bar = log.verticalScrollBar()
                    view = log.viewport().height()
                    seen = (bar.value(), bar.value() + view)
                    if last - first <= view - 12:
                        self.assertGreaterEqual(top + first, seen[0])
                        self.assertLessEqual(top + last, seen[1])
                    else:
                        self.assertTrue(seen[0] <= top + first <= seen[1])
                    if last + hud.LiveLog._CHANGE_MARGIN <= view - 12:
                        self.assertLessEqual(bar.value(), top)   # the picture's top is in view
                    log.hide()

    def test_an_email_payload_keeps_its_top_in_view_during_a_countdown(self) -> None:
        log, widget, top = self.countdown(outgoing_picture())
        self.assertIsNone(widget.focus_span())
        bar = log.verticalScrollBar()
        self.assertLessEqual(bar.value(), top)
        self.assertGreaterEqual(bar.value() + log.viewport().height(), top + 40)
        tall, widget, top = self.countdown(day_picture(MOVE), height=900)   # it all fits: the whole task
        bar = tall.verticalScrollBar()
        self.assertLessEqual(bar.value(), top)
        self.assertGreaterEqual(bar.value() + tall.viewport().height(), top + widget.height())

    def test_a_dropped_picture_is_let_go_while_its_step_is_closed(self) -> None:
        image = jpeg()
        limits = live.LiveLimits(max_total_picture_bytes=int(len(image) * 1.5))
        self.stream = live.LiveStream(clock=self.clock, wall=lambda: NOW, limits=limits)
        log = self.log()
        first = self.stream.task(live.TASK_WEB, "Web research")
        step = first.step(live.WEB_FETCH, "Read a page", status=live.STATUS_OK)
        step.picture(pictures.page_ready(URL, image=image, width=960, height=600))
        first.finish(live.STATUS_OK)
        self.apply()
        widget = log.step_widget(step.id)
        thumbnail = widget.picture_widget()
        self.assertIsNotNone(thumbnail)
        thumbnail.grab()
        self.assertTrue(thumbnail._pixmaps)
        held = thumbnail.picture()
        second = self.stream.task(live.TASK_WEB, "Another research")
        other = second.step(live.WEB_FETCH, "Read a page", status=live.STATUS_OK)
        other.picture(pictures.page_ready(URL + "/2", image=image, width=960, height=600))   # over the cap
        self.apply()
        self.assertEqual(step.view().picture.state, live.PICTURE_DROPPED)
        self.assertFalse(widget.is_open())                     # a dropped picture's step closes ...
        self.assertTrue(widget.details.isHidden())
        self.assertIsNone(widget.picture_widget())
        kept = widget.details.picture
        self.assertIsNot(kept.picture(), held)                 # ... and lets its bytes go
        self.assertEqual(kept.picture().state, live.PICTURE_DROPPED)
        self.assertIsNone(kept.picture().page)
        self.assertEqual(kept._pixmaps, {})


class PopOutPictureTests(LogCase):
    def test_the_pop_out_opens_its_own_viewer(self) -> None:
        window = ui.LiveWindow()
        self.addCleanup(window.deleteLater)
        window.show()
        settle()
        step = self.payload(day_picture(CANCEL))
        changes = self.stream.changes(0)
        window.log.apply(changes)
        settle(4)
        # Round 2 (HOLOGRAM_SPEC 16.3): the pop-out's timeline keeps the details collapsed; the picture is
        # its focal frame, full width, which opens it larger (and gets the focus back).
        self.assertIsNone(window.log.step_widget(step.id).picture_widget())
        widget = window.focal.frame
        self.assertEqual(widget.step_id(), step.id)
        self.assertLessEqual(widget.width(), window.log.viewport().width() + 32)
        widget.setFocus(Qt.FocusReason.TabFocusReason)
        QTest.keyClick(widget, Qt.Key.Key_Return)
        settle()
        viewer = window.log.picture_viewer()
        self.assertIsNotNone(viewer)
        self.assertIs(viewer.parentWidget(), window)
        self.assertEqual(viewer.windowTitle(), "Calendar change - Jarvis")
        self.assertTrue(wait_for(viewer.isVisible, 1))
        viewer.close()
        settle()
        self.assertIs(QApplication.focusWidget(), widget)
        window.hide()

    def test_thumbnail_sizes_are_measured_once_per_width(self) -> None:
        log = self.log()
        task = self.stream.task(live.TASK_ASK, "Ask")
        step = task.step(live.MAIL_THREAD, "Read email thread")
        step.picture(email_picture())
        self.apply()
        widget = log.step_widget(step.id).picture_widget()
        with mock.patch.object(hud, "picture_height", wraps=hud.picture_height) as measured:
            for _ in range(5):
                widget.repaint()
            widget._thumb(widget.width())
        self.assertEqual(measured.call_count, 0)   # measured when it was first shown


class SearchAddressTests(unittest.TestCase):
    """A result's address line in a search picture: the site first (no "https://"), then the rest of
    the address, cut at its end; a site too wide is cut in its middle, so the end of the host (whose
    site it is) always shows."""

    def hit(self, url: str) -> pictures.SearchHit:
        picture = pictures.search_picture("deadline", [("Admissions", url)])
        return picture.search.hits[0]

    def test_the_site_then_the_rest(self) -> None:
        hit = self.hit("https://admissions.example.edu/spring/apply?term=2027")
        site, rest = hud.search_address_lines(hit, 400)
        self.assertEqual(site.shown, "admissions.example.edu")
        self.assertEqual(rest.shown, "/spring/apply?term=2027")
        root, nothing = hud.search_address_lines(self.hit("https://www.example.org/"), 400)
        self.assertEqual((root.shown, nothing), ("www.example.org", None))
        plain, _rest = hud.search_address_lines(self.hit("http://old.example.org/a"), 400)
        self.assertEqual(plain.shown, "http://old.example.org")                  # not https: said so

    def test_a_long_host_keeps_its_end(self) -> None:
        host = "admissions.the-very-long-name-of-the-example-university-of-natural-history.example.edu"
        hit = self.hit(f"https://{host}/spring/apply")
        for width in (180.0, 260.0, 360.0):
            with self.subTest(width=width):
                site, rest = hud.search_address_lines(hit, width)
                self.assertTrue(site.shown.endswith(".example.edu"), site.shown)   # whose site it is
                self.assertTrue(site.shown.startswith("admissions."), site.shown)
                self.assertLessEqual(site.natural_width, width)
                self.assertEqual(site.line_count, 1)
                self.assertIsNone(rest)                                        # no room left for the rest
        site, rest = hud.search_address_lines(self.hit("https://a.example.edu/" + "x" * 300), 200.0)
        self.assertEqual(site.shown, "a.example.edu")
        self.assertTrue(rest.shown.startswith("/xxx") and rest.line_count == 1)
        self.assertLessEqual(site.natural_width + rest.natural_width, 200.0)

    def test_an_address_without_a_host_is_drawn_as_listed(self) -> None:
        hit = pictures.SearchHit(title="Notes", url="urn:example:notes", host="")
        only, rest = hud.search_address_lines(hit, 300)
        self.assertEqual((only.shown, rest), ("urn:example:notes", None))


class SearchPictureViewTests(LogCase):
    """A web search drawn as a compact results page: cut with a fade in the thumbnail, whole in the
    viewer; and the check's "picture:<step id>" links, followed inside LIVE."""

    def test_heights_the_fade_and_the_whole_results_page(self) -> None:
        picture = search_picture()
        heights = [hud.picture_height(picture, width) for width in (200, 360, 640)]
        self.assertEqual(heights, sorted(heights, reverse=True))           # wider: titles need fewer lines
        self.assertGreater(heights[-1], hud.LIVE_PICTURE_SEARCH_MAX_PX)   # 10 results: more than a thumbnail
        cut = hud.picture_pixmap(picture, 360, 1.0, ("", ""), hud.LIVE_PICTURE_SEARCH_MAX_PX)
        self.assertEqual(cut.height(), hud.LIVE_PICTURE_SEARCH_MAX_PX)
        whole = hud.picture_pixmap(picture, 360, 1.0, ("", ""), None)
        self.assertEqual(whole.height(), heights[1])
        small = search_picture(1)
        self.assertLess(hud.picture_height(small, 360), hud.LIVE_PICTURE_SEARCH_MAX_PX)
        for empty in (pictures.search_picture("museum", []), pictures.search_picture("museum", [], unread=True)):
            image = hud.picture_pixmap(empty, 360, 1.5, ("", ""), hud.LIVE_PICTURE_SEARCH_MAX_PX).toImage()
            self.assertGreater(len(colours(image)), 3)
        self.assertEqual(hud.picture_height(picture.dropped("x"), 360), 0)

    def test_the_search_step_shows_it_and_enter_opens_it_larger(self) -> None:
        log = self.log()
        task = self.stream.task(live.TASK_WEB, "Web research")
        step = task.step(live.WEB_SEARCH, "Web search 1", status=live.STATUS_OK, summary="12 results",
                         fields=[("Query (exact)", "Example University spring 2027 application deadline")])
        step.picture(search_picture())
        self.apply()
        widget = log.step_widget(step.id).picture_widget()
        self.assertIsNotNone(widget)                                       # open by itself, the picture first
        self.assertTrue(widget.ready())
        self.assertLess(widget.y(), log.step_widget(step.id).details.fields.y())
        self.assertLessEqual(widget._thumb(widget.width())[1], hud.LIVE_PICTURE_SEARCH_MAX_PX)
        self.assertTrue(widget.accessibleName().startswith(
            "Web search picture: Web search for Example University spring 2027 application deadline: 12 results"))
        self.assertGreater(len(colours(widget.grab().toImage())), 3)
        widget.setFocus(Qt.FocusReason.TabFocusReason)
        QTest.keyClick(widget, Qt.Key.Key_Return)
        settle()
        viewer = log.picture_viewer()
        self.assertTrue(viewer is not None and viewer.isVisible())
        self.assertEqual(viewer.windowTitle(), "Web search - Jarvis")
        self.assertEqual(viewer.canvas.width(), min(hud.PICTURE_VIEWER_MODEL_W, room(viewer)))
        self.assertGreater(viewer.canvas.height(), hud.LIVE_PICTURE_SEARCH_MAX_PX)   # every result drawn
        QTest.keyClick(viewer.scroll, Qt.Key.Key_Escape)
        settle()
        self.assertIs(QApplication.focusWidget(), widget)

    def test_a_picture_link_opens_the_picture_or_shows_its_step(self) -> None:
        log = self.log(560, 400)
        task = self.stream.task(live.TASK_WEB, "Web research")
        read = task.step(live.WEB_FETCH, "Page read 1", status=live.STATUS_OK)
        read.picture(page_picture())
        for number in range(6):
            task.step(live.WEB_FETCH, f"Filler {number}", status=live.STATUS_OK, fields=[("Size", "1 byte")])
        source = task.step(live.WEB_SOURCE, "Source [2]: museum.example.net", status=live.STATUS_OK)
        source.picture(pictures.page_pending("https://museum.example.net/hours"))
        check = task.step(live.RESEARCH_VALIDATE, "Checking the answer", status=live.STATUS_WARN, fields=[
            live.Field("Picture [1]", "Page read 1: www.example.org", link=f"picture:{read.id}"),
            live.Field("Picture [2]", "Source [2]: museum.example.net", link=f"picture:{source.id}"),
            live.Field("Elsewhere", "JARVIS tab", link="tab:jarvis")])
        self.apply()
        outside: list[str] = []
        log.linkClicked.connect(outside.append)
        details = log.step_widget(check.id).details
        details.fields.value_widget("Picture [1]").click()                 # READY: shown larger
        settle()
        viewer = log.picture_viewer()
        self.assertTrue(viewer is not None and viewer.isVisible() and viewer.step_id() == read.id)
        viewer.close()
        settle()
        log.step_widget(source.id).set_open(False)                         # closed by hand
        log.verticalScrollBar().setValue(0)
        settle()
        details.fields.value_widget("Picture [2]").click()                 # not READY: its step, opened
        settle(6)
        target = log.step_widget(source.id)
        self.assertTrue(target.is_open())
        self.assertFalse(viewer.isVisible())
        top = target.mapTo(log.widget(), QPoint(0, 0)).y()
        bar = log.verticalScrollBar()
        self.assertLessEqual(abs(bar.value() - min(bar.maximum(), max(0, top - 6))), 2)
        self.assertFalse(log.following())                                  # it holds there
        details.fields.value_widget("Elsewhere").click()                   # any other link goes out
        settle()
        self.assertEqual(outside, ["tab:jarvis"])
        link = details.fields.value_widget("Picture [2]")                  # its name says label, value, action
        self.assertEqual(link.accessibleName(),
                         f"Picture [2]: Source [2]: museum.example.net - {hud.LIVE_PICTURE_LINK_WORDS}")
        self.assertEqual(details.fields.value_widget("Elsewhere").accessibleName(), "Elsewhere: JARVIS tab")
        check.field("Picture [2]", "Source [2]: shop.example.net", link=f"picture:{source.id}")
        self.apply()
        self.assertIs(details.fields.value_widget("Picture [2]"), link)   # the same row: a new value only
        self.assertEqual(link.accessibleName(),
                         f"Picture [2]: Source [2]: shop.example.net - {hud.LIVE_PICTURE_LINK_WORDS}")
        log.follow_link("picture:999999")                                  # a step that is gone: nothing
        log.follow_link("picture:x")
        settle()
        self.assertEqual(outside, ["tab:jarvis"])
        self.assertEqual(live.result_link("picture:12"), "picture:12")
        self.assertEqual(live.result_link("picture:12x"), "")

    def link_and_sources(self) -> tuple[hud.LiveLog, QWidget, live.LiveStep]:
        """A short log: the check with its "Picture [2]" link first, then two source steps with page
        pictures (so the second source's thumbnail is far below the link)."""
        log = self.log(560, 400)
        task = self.stream.task(live.TASK_WEB, "Web research")
        check = task.step(live.RESEARCH_VALIDATE, "Checking the answer", status=live.STATUS_OK, fields=[
            live.Field("Answer", "Open daily from 10 AM.")])
        sources = []
        for number, host in ((1, "ai.example.edu"), (2, "pass.example.edu")):
            source = task.step(live.WEB_SOURCE, f"Source [{number}]: {host}", status=live.STATUS_OK)
            source.picture(page_picture())
            sources.append(source)
        check.field("Picture [2]", "Source [2]: pass.example.edu", link=f"picture:{sources[1].id}")
        self.apply()
        log.step_widget(check.id).set_open(True)
        settle(4)
        link = log.step_widget(check.id).details.fields.value_widget("Picture [2]")
        log.verticalScrollBar().setValue(0)
        settle()
        return log, link, sources[1]

    def in_view(self, log: hud.LiveLog, widget: QWidget) -> bool:
        top = widget.mapTo(log.viewport(), QPoint(0, 0)).y()
        return top >= 0 and top + widget.height() <= log.viewport().height()

    def test_closing_a_picture_opened_from_its_link_gives_the_focus_back_to_the_link(self) -> None:
        log, link, source = self.link_and_sources()
        self.assertTrue(self.in_view(log, link))
        thumbnail = log.step_widget(source.id).picture_widget()
        self.assertFalse(self.in_view(log, thumbnail))                     # far below the link
        link.setFocus(Qt.FocusReason.TabFocusReason)
        settle()
        self.assertTrue(link.focus_ring())
        QTest.keyClick(link, Qt.Key.Key_Return)                             # Enter on the link: the picture larger
        settle()
        viewer = log.picture_viewer()
        self.assertTrue(viewer is not None and viewer.isVisible() and viewer.step_id() == source.id)
        log.verticalScrollBar().setValue(log.verticalScrollBar().maximum())   # the log moved meanwhile
        settle()
        self.assertFalse(self.in_view(log, link))
        QTest.keyClick(viewer.scroll, Qt.Key.Key_Escape)
        settle(4)
        self.assertFalse(viewer.isVisible())
        self.assertIs(QApplication.focusWidget(), link)                     # back on the link, not the thumbnail
        self.assertTrue(link.focus_ring())                                  # with its ring (it had one)
        self.assertTrue(self.in_view(log, link))                            # scrolled back into view

    def test_a_click_on_the_link_comes_back_to_it_too_and_a_thumbnail_to_itself(self) -> None:
        log, link, source = self.link_and_sources()
        QTest.mouseClick(link, Qt.MouseButton.LeftButton, pos=QPoint(4, link.height() // 2))
        settle()
        viewer = log.picture_viewer()
        self.assertTrue(viewer is not None and viewer.isVisible())
        viewer.close()
        settle(4)
        self.assertIs(QApplication.focusWidget(), link)
        self.assertFalse(link.focus_ring())                                 # a click: no ring
        thumbnail = log.step_widget(source.id).picture_widget()
        log.ensureWidgetVisible(thumbnail)
        thumbnail.setFocus(Qt.FocusReason.TabFocusReason)
        QTest.keyClick(thumbnail, Qt.Key.Key_Return)                        # opened from the thumbnail ...
        settle()
        self.assertTrue(viewer.isVisible())
        QTest.keyClick(viewer.scroll, Qt.Key.Key_Escape)
        settle(4)
        self.assertIs(QApplication.focusWidget(), thumbnail)                # ... back to the thumbnail

    def test_a_link_to_a_picture_not_ready_takes_the_keyboard_focus_to_it(self) -> None:
        """A "Picture [n]" link to a picture still on its way (a source is pictured only after the
        check) or unavailable (past the cap): its step comes into view and the keyboard focus goes
        along, to that picture with its ring - never left on the link scrolled out of view. A click
        moves no focus."""
        log = self.log(560, 400)
        task = self.stream.task(live.TASK_WEB, "Web research")
        check = task.step(live.RESEARCH_VALIDATE, "Checking the answer", status=live.STATUS_OK, fields=[
            live.Field("Answer", "Open daily from 10 AM.")])
        for number, host in ((1, "ai.example.edu"), (2, "pass.example.edu")):
            task.step(live.WEB_SOURCE, f"Source [{number}]: {host}", status=live.STATUS_OK).picture(page_picture())
        pending = task.step(live.WEB_SOURCE, "Source [3]: shop.example.net", status=live.STATUS_OK)
        pending.picture(pictures.page_pending("https://shop.example.net/hours"))
        over = task.step(live.WEB_SOURCE, "Source [4]: cafe.example.com", status=live.STATUS_OK)
        over.picture(pictures.page_unavailable("https://cafe.example.com/", "at most 4 page pictures per research"))
        check.field("Picture [3]", "Source [3]: shop.example.net", link=f"picture:{pending.id}")
        check.field("Picture [4]", "Source [4]: cafe.example.com", link=f"picture:{over.id}")
        self.apply()
        log.step_widget(check.id).set_open(True)
        settle(4)
        fields = log.step_widget(check.id).details.fields
        for name, step in (("Picture [3]", pending), ("Picture [4]", over)):
            log.verticalScrollBar().setValue(0)
            settle()
            link = fields.value_widget(name)
            self.assertTrue(self.in_view(log, link))
            link.setFocus(Qt.FocusReason.TabFocusReason)
            settle()
            QTest.keyClick(link, Qt.Key.Key_Return)
            settle(6)
            target = log.step_widget(step.id)
            picture = target.picture_widget()
            self.assertIsNone(log.picture_viewer(), name)                    # not READY: nothing larger
            self.assertFalse(self.in_view(log, link), name)                  # the link scrolled away ...
            self.assertIs(QApplication.focusWidget(), picture, name)         # ... the focus went along
            self.assertTrue(picture.focus_ring(), name)                      # with its ring (the link had one)
            self.assertTrue(self.in_view(log, picture) and self.in_view(log, target.row), name)
            QTest.keyClick(picture, Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
            settle()
            self.assertIs(QApplication.focusWidget(), target.row, name)      # Tab goes on from there
            self.assertTrue(self.in_view(log, target.row), name)
        quiet = log.step_widget(over.id).row                                 # no ring: none after it either
        log.verticalScrollBar().setValue(0)
        settle()
        link = fields.value_widget("Picture [3]")
        link.setFocus(Qt.FocusReason.OtherFocusReason)
        log.follow_link(f"picture:{pending.id}", link)
        settle(6)
        picture = log.step_widget(pending.id).picture_widget()
        self.assertIs(QApplication.focusWidget(), picture)
        self.assertFalse(picture.focus_ring())
        quiet.setFocus(Qt.FocusReason.TabFocusReason)
        log.verticalScrollBar().setValue(0)
        settle()
        QTest.mouseClick(link, Qt.MouseButton.LeftButton, pos=QPoint(4, link.height() // 2))
        settle(6)
        self.assertIs(QApplication.focusWidget(), quiet)                     # a click: the focus stays put
        self.assertTrue(self.in_view(log, log.step_widget(pending.id).row))  # (the step still comes into view)


if __name__ == "__main__":
    unittest.main()
