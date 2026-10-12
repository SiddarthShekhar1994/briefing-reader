"""Tests for briefing_reader.pictures: the LIVE view's pictures as plain data (Qt-free).

Email pictures (a thread Ask read, what a Reply / Email card sends), calendar change pictures on their
day (add, to-do block, move, cancel, RSVP; other events only from a read that covers the day), the
week strip, the page picture builders, the stamps, cost / without_text / dropped, KnownDays, and
that no repr names any content. Every name, address, id and text is invented.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from briefing_reader import live, pictures
from briefing_reader.actions import IDN_WARNING, parse_action_line
from briefing_reader.ask.mail import MailMessage, MailPerson, MailThread
from briefing_reader.gcal import CalendarEvent, EventBrief
from briefing_reader.gmail import MailView

PDT = timezone(timedelta(hours=-7))
PST = timezone(timedelta(hours=-8))
EDT = timezone(timedelta(hours=-4))
NOW = datetime(2026, 10, 9, 13, 30, tzinfo=PDT)        # a Friday
MARK = "Zq7marker"                                      # invented text that must never reach a repr
MOVE = ("Move: acct=work | event=evt0001aa | cal=primary | when=2026-10-09 15:00-16:00 | notify=all | "
        "title=Project sync | at=2026-10-09 14:00-15:00 | link= | body=")
CANCEL = ("Cancel: acct=work | event=evt0002aa | cal=primary | notify=all | title=Design review | "
          "at=2026-10-09 11:00-12:00 | link= | body=")
RSVP = ("RSVP: acct=work | event=evt0003aa | cal=primary | answer=maybe | notify=all | title=Team lunch | "
        "at=2026-10-09 12:30-13:30 | due= | link= | body=")
TODO = ("Todo: title=Lab report | due=2026-10-10 | block=2026-10-09 19:00-21:00 | acct=work | link=")


def line(text: str):
    action = parse_action_line(text)
    assert not action.error, action.error
    return action


def at(hour: int, minute: int = 0, day: int = 9) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=PDT)


def person(name: str, address: str) -> MailPerson:
    return MailPerson(address, name)


def message(n: int, *, text: str = "Hello", subject: str = "Budget", to_more: int = 0, cut: bool = False,
            day: int = 8) -> MailMessage:
    return MailMessage(f"g{n}", sender=person(f"Person {n}", f"person{n}@example.com"),
                       to=(person("Ana Lima", "ana@example.com"), person("", "lee@example.org")),
                       cc=(person("Sam Ortiz", "sam@example.com"),) if n == 0 else (),
                       sent_at=datetime(2026, 10, day, 9, n, tzinfo=PDT), subject=subject, text=text,
                       to_more=to_more, cut=cut)


def thread(*messages: MailMessage, left_out: int = 0) -> MailThread:
    return MailThread("work", "t0001", "Budget", tuple(messages), left_out=left_out)


def event(title: str, start: datetime, end: datetime, event_id: str = "") -> EventBrief:
    return EventBrief(event_id, "primary", title, start=start, end=end)


class EmailPictureTests(unittest.TestCase):
    def test_the_newest_message_as_a_mail_client_shows_it(self) -> None:
        older = message(0, text="first")
        newest = message(1, text="Hi Ana,\nthe numbers are in.\n", to_more=3)
        picture = pictures.email_picture(thread(older, newest), tz=PDT)
        self.assertEqual((picture.kind, picture.caption, picture.state),
                         (pictures.KIND_EMAIL, "Email", live.PICTURE_READY))
        head = picture.mail.head
        self.assertEqual(head.sender, "Person 1 <person1@example.com>")
        self.assertEqual(head.to, "Ana Lima <ana@example.com>, lee@example.org, +3 more")
        self.assertEqual(head.cc, "")
        self.assertEqual(head.when, "Thu Oct 8 2026, 9:01 AM")
        self.assertEqual(head.subject, "Budget")
        self.assertEqual(picture.mail.body, "Hi Ana,\nthe numbers are in.\n")
        self.assertFalse(picture.mail.outgoing)
        self.assertEqual(picture.mail.account, "work")
        self.assertEqual(picture.note, pictures.NOTE_EMAIL)
        self.assertEqual(picture.alt, "Email from Person 1 <person1@example.com>, subject Budget, 1 earlier message")

    def test_hour24_and_the_thread_subject_when_the_message_has_none(self) -> None:
        picture = pictures.email_picture(thread(message(5, subject="")), hour24=True, tz=PDT)
        self.assertEqual(picture.mail.head.when, "Thu Oct 8 2026, 09:05")
        self.assertEqual(picture.mail.head.subject, "Budget")
        empty = MailThread("work", "t2", "", (message(5, subject=""),))
        self.assertEqual(pictures.email_picture(empty, tz=PDT).mail.head.subject, "(no subject)")

    def test_older_messages_newest_first_and_counted_beyond_the_cap(self) -> None:
        messages = [message(n, text=f"text {n}\nsecond line") for n in range(9)]
        picture = pictures.email_picture(thread(*messages, left_out=4), tz=PDT)
        older = picture.mail.older
        self.assertEqual(len(older), pictures.OLDER_SHOWN)
        self.assertEqual([item.sender for item in older], [f"Person {n}" for n in (7, 6, 5, 4, 3)])
        self.assertEqual(older[0].when, "Oct 8, 9:07 AM")
        self.assertEqual(older[0].snippet, "text 7 second line")
        self.assertEqual(picture.mail.older_more, 9 - 1 - pictures.OLDER_SHOWN + 4)
        self.assertIn("12 earlier messages", picture.alt)

    def test_a_long_body_is_cut_and_counted(self) -> None:
        text = "x" * (pictures.BODY_CAP + 250)
        picture = pictures.email_picture(thread(message(1, text=text, cut=True)), tz=PDT)
        self.assertEqual(len(picture.mail.body), pictures.BODY_CAP)
        self.assertEqual((picture.mail.body_chars, picture.mail.body_cut), (len(text), 250))
        self.assertTrue(picture.mail.shortened)

    def test_bidi_and_control_characters_stay_visible(self) -> None:
        picture = pictures.email_picture(thread(message(1, subject="Invoice \u202egnp.exe", text="a\u202eb\x07c")),
                                         tz=PDT)
        self.assertEqual(picture.mail.head.subject, "Invoice <U+202E>gnp.exe")
        self.assertEqual(picture.mail.body, "a<U+202E>b<U+0007>c")
        self.assertNotIn("\u202e", picture.alt)

    def test_an_empty_thread_has_no_picture(self) -> None:
        self.assertIsNone(pictures.email_picture(thread()))
        self.assertIsNone(pictures.email_picture(object()))

    def test_the_more_count_survives_a_long_recipient_line(self) -> None:
        people = tuple(person(f"Person Number{n} Example", f"person.number{n}@example.com") for n in range(10))
        newest = MailMessage("g9", sender=person("Sam Ortiz", "sam@example.com"), to=people, cc=people[:9],
                             sent_at=datetime(2026, 10, 8, 9, 0, tzinfo=PDT), subject="All hands", text="Hi",
                             to_more=6, cc_more=0)
        head = pictures.email_picture(thread(newest), tz=PDT).mail.head
        for text, everyone in ((head.to, 16), (head.cc, 9)):
            with self.subTest(text=text[:20]):
                self.assertLessEqual(len(text), pictures.HEAD_CAP)
                listed = text.count("@example.com>")
                self.assertGreater(listed, 0)
                self.assertTrue(text.endswith(f", +{everyone - listed} more"), text[-40:])
                self.assertNotIn("...", text)   # whole people only: nobody is cut in half
        short = pictures._people((person("Ana Lima", "ana@example.com"),), 3)
        self.assertEqual(short, "Ana Lima <ana@example.com>, +3 more")
        self.assertEqual(pictures._people((), 4), "+4 more")
        self.assertEqual(pictures._people((), 0), "")
        giant = pictures._people((person("N" * 400, "n@example.com"), person("Ana", "ana@example.com")), 0)
        self.assertLessEqual(len(giant), pictures.HEAD_CAP)
        self.assertTrue(giant.endswith(", +1 more"), giant[-20:])


class OutgoingPictureTests(unittest.TestCase):
    def view(self, **changes) -> MailView:
        values = dict(from_addr="me@example.edu", to="ana@example.edu, ben@example.edu", cc="", subject="Re: Lunch",
                      in_reply_to="<abc@example.com>", references="<abc@example.com>", thread_id="18c0ffee00000001",
                      body="Hi both,\nThursday works.\n")
        values.update(changes)
        return MailView(**values)

    def test_exactly_what_is_sent(self) -> None:
        picture = pictures.outgoing_picture(self.view(), account="work")
        self.assertEqual((picture.kind, picture.caption), (pictures.KIND_OUTGOING, "Email"))
        mail = picture.mail
        self.assertTrue(mail.outgoing)
        self.assertEqual((mail.account, mail.head.sender, mail.head.to, mail.head.cc, mail.head.when),
                         ("work", "me@example.edu", "ana@example.edu, ben@example.edu", "", ""))
        self.assertEqual(mail.head.subject, "Re: Lunch")
        self.assertEqual(mail.body, "Hi both,\nThursday works.\n")
        self.assertEqual(mail.reply_line, "Reply in its Gmail thread - In-Reply-To <abc@example.com>")
        self.assertEqual(picture.note, pictures.NOTE_OUTGOING)
        self.assertEqual(picture.alt, "Email from work (me@example.edu) to ana@example.edu, ben@example.edu, "
                                      "subject Re: Lunch")

    def test_every_recipient_of_a_long_to_and_cc_line_is_in_the_picture(self) -> None:
        # Five addresses of about 70 characters: far over one header line (HEAD_CAP), all of them
        # in the picture, as in the LIVE text field.
        names = [f"firstname.lastname.department{n}@research-division.university-example.edu" for n in range(5)]
        self.assertGreater(len(", ".join(names)), pictures.HEAD_CAP)
        picture = pictures.outgoing_picture(self.view(to=", ".join(names), cc=", ".join(reversed(names))),
                                            account="work")
        head = picture.mail.head
        self.assertEqual(head.to, ", ".join(names))
        self.assertEqual(head.cc, ", ".join(reversed(names)))
        for name in names:
            self.assertIn(name, head.to)
            self.assertIn(name, head.cc)

    def test_a_new_email_and_gmail_filling_the_sender(self) -> None:
        picture = pictures.outgoing_picture(self.view(from_addr="", in_reply_to="", references="", thread_id=""),
                                            account="personal")
        self.assertEqual(picture.mail.reply_line, "New email (a new thread)")
        self.assertEqual(picture.mail.head.sender, pictures.GMAIL_FILLS)
        threaded = pictures.outgoing_picture(self.view(in_reply_to=""), account="work")
        self.assertEqual(threaded.mail.reply_line, "Reply in its Gmail thread")


class KnownDaysTests(unittest.TestCase):
    def test_only_a_read_that_covers_the_whole_day(self) -> None:
        known = pictures.KnownDays()
        known.remember("work", pictures.SOURCE_AGENDA, at(0), at(0, day=10), [event("A", at(9), at(10))], at=NOW)
        self.assertIsNotNone(known.lookup("work", date(2026, 10, 9)))
        self.assertIsNone(known.lookup("work", date(2026, 10, 10)))
        self.assertIsNone(known.lookup("work", date(2026, 10, 8)))
        self.assertIsNone(known.lookup("personal", date(2026, 10, 9)))
        partial = pictures.KnownDays()
        partial.remember("work", pictures.SOURCE_AGENDA, at(6), at(0, day=12), [], at=NOW)
        self.assertIsNone(partial.lookup("work", date(2026, 10, 9)))
        self.assertIsNotNone(partial.lookup("work", date(2026, 10, 10)))

    def test_the_newest_read_per_source_replaces_the_old_one(self) -> None:
        known = pictures.KnownDays()
        known.remember("work", pictures.SOURCE_AGENDA, at(0), at(0, day=10), [event("Old", at(9), at(10))], at=NOW)
        known.remember("work", pictures.SOURCE_AGENDA, at(0), at(0, day=10), [event("New", at(9), at(10))],
                       at=NOW + timedelta(minutes=5))
        day = known.lookup("work", date(2026, 10, 9))
        self.assertEqual([item.title for item in day.events], ["New"])
        known.remember("work", pictures.SOURCE_ASK, at(0), at(0, day=10), [event("Ask", at(9), at(10))],
                       at=NOW + timedelta(minutes=9))
        self.assertEqual(known.lookup("work", date(2026, 10, 9)).source, pictures.SOURCE_ASK)
        known.clear()
        self.assertIsNone(known.lookup("work", date(2026, 10, 9)))

    def test_calendar_events_and_all_day_events(self) -> None:
        known = pictures.KnownDays()
        items = [CalendarEvent("Holiday", all_day_start=date(2026, 10, 9), all_day_end=date(2026, 10, 9)),
                 CalendarEvent("Talk", start=at(16), end=at(17)), "not an event"]
        known.remember("personal", pictures.SOURCE_AGENDA, at(0), at(0, day=11), items, at=NOW)
        day = known.lookup("personal", date(2026, 10, 9))
        self.assertEqual([(item.title, item.start, item.all_day_start) for item in day.events],
                         [("Holiday", None, date(2026, 10, 9)), ("Talk", datetime(2026, 10, 9, 16), None)])

    def test_bad_input_never_raises(self) -> None:
        known = pictures.KnownDays()
        known.remember("work", pictures.SOURCE_AGENDA, None, None, None, at=NOW)   # type: ignore[arg-type]
        self.assertIsNone(known.lookup("work", date(2026, 10, 9)))


class DayPictureTests(unittest.TestCase):
    def known(self, *events, alias: str = "work", source: str = pictures.SOURCE_AGENDA) -> pictures.KnownDays:
        known = pictures.KnownDays()
        known.remember(alias, source, at(0, day=8), at(0, day=12), events, at=NOW)
        return known

    def blocks(self, picture, role: str):
        return [block for block in picture.day.blocks if block.role == role]

    def test_a_calendar_add_is_highlighted(self) -> None:
        picture = pictures.day_change_picture(line("Calendar: Dentist | 2026-10-09 16:30-17:15 | | Main St |"),
                                              now=NOW)
        day = picture.day
        self.assertEqual((picture.kind, picture.caption, day.change, day.headline, day.account),
                         (pictures.KIND_DAY, "Calendar change", pictures.CHANGE_ADD, "Add: Dentist", "personal"))
        (block,) = day.blocks
        self.assertEqual((block.role, block.start_min, block.end_min, block.time_text),
                         (pictures.ROLE_NEW, 16 * 60 + 30, 17 * 60 + 15, "4:30-5:15 PM"))
        self.assertEqual(day.days, ("Fri Oct 9",))
        self.assertEqual((day.first_min, day.last_min), (8 * 60, 18 * 60))
        self.assertFalse(day.others_shown)
        self.assertEqual(day.others_note, "Only this event is drawn: Jarvis has not read the rest of Fri Oct 9")
        self.assertEqual(picture.note, day.others_note)   # never "from the calendar he already read"
        self.assertEqual((day.now_min, day.now_column), (13 * 60 + 30, 0))
        self.assertEqual(picture.alt, "Add: Dentist, Fri Oct 9 4:30-5:15 PM")

    def test_all_day_and_repeating_adds(self) -> None:
        picture = pictures.day_change_picture(line("Calendar: Fair | 2026-10-10 | | |"), now=NOW)
        (block,) = picture.day.blocks
        self.assertTrue(block.all_day)
        self.assertEqual(block.time_text, "all day")
        self.assertEqual(picture.day.now_column, -1)
        weekly = pictures.day_change_picture(
            line("Calendar: Chess club | 2026-10-09 15:00-16:00 | weekly until 2026-12-11 |"), now=NOW)
        self.assertEqual(weekly.day.blocks[0].time_text, "3:00-4:00 PM - repeats weekly until 2026-12-11")

    def test_a_todo_block_and_a_todo_without_one(self) -> None:
        picture = pictures.day_change_picture(line(TODO), now=NOW)
        self.assertEqual((picture.day.change, picture.day.headline, picture.day.account),
                         (pictures.CHANGE_TODO, "To-do block: Lab report", "personal"))
        self.assertEqual(picture.day.last_min, 22 * 60)   # 21:00 + 30 min, to the hour
        self.assertIsNone(pictures.day_change_picture(line(TODO.replace("2026-10-09 19:00-21:00", "")), now=NOW))

    def test_a_move_on_the_same_day(self) -> None:
        picture = pictures.day_change_picture(line(MOVE), now=NOW)
        (before,) = self.blocks(picture, pictures.ROLE_BEFORE)
        (after,) = self.blocks(picture, pictures.ROLE_AFTER)
        self.assertEqual((before.column, before.start_min, before.time_text), (0, 14 * 60, "2:00-3:00 PM"))
        self.assertEqual((after.column, after.start_min, after.time_text), (0, 15 * 60, "3:00-4:00 PM"))
        self.assertEqual(picture.day.days, ("Fri Oct 9",))
        self.assertEqual(picture.day.account, "work")
        self.assertEqual(picture.alt, "Move: Project sync from Fri Oct 9 2:00-3:00 PM to Fri Oct 9 3:00-4:00 PM")

    def test_a_move_to_another_day_has_two_columns(self) -> None:
        picture = pictures.day_change_picture(line(MOVE.replace("at=2026-10-09", "at=2026-10-08")), now=NOW)
        self.assertEqual(picture.day.days, ("Thu Oct 8", "Fri Oct 9"))
        self.assertEqual(self.blocks(picture, pictures.ROLE_BEFORE)[0].column, 0)
        self.assertEqual(self.blocks(picture, pictures.ROLE_AFTER)[0].column, 1)
        self.assertEqual(picture.day.now_column, 1)

    def test_googles_time_wins_over_the_line_and_an_unknown_old_time(self) -> None:
        class Check:
            title = "Project sync (Google)"
            start, end = at(10), at(11)
            all_day_start = all_day_end = None

        picture = pictures.day_change_picture(line(MOVE), now=NOW, check=Check())
        (before,) = self.blocks(picture, pictures.ROLE_BEFORE)
        self.assertEqual((before.start_min, before.title), (10 * 60, "Project sync (Google)"))
        unknown = pictures.day_change_picture(line(MOVE.replace("at=2026-10-09 14:00-15:00", "at=")), now=NOW)
        (before,) = self.blocks(unknown, pictures.ROLE_BEFORE)
        self.assertTrue(before.timeless)
        self.assertEqual(before.time_text, "old time not known")

    @staticmethod
    def check(start: datetime, end: datetime, title: str = "Project sync"):
        class Check:
            all_day_start = all_day_end = None

        found = Check()
        found.title, found.start, found.end = title, start, end
        return found

    def test_googles_times_are_drawn_as_the_wall_time_they_carry(self) -> None:
        # The calendar is in New York (Google answers in its zone, -04:00), the PC in Pacific
        # (-07:00): the card's naive times, what the executor sends and the check line are all
        # New York wall times, so the picture is too - a one-hour move, not a four-hour one.
        moved = MOVE.replace("2026-10-09", "2026-10-10")
        picture = pictures.day_change_picture(line(moved), now=NOW,
                                              check=self.check(datetime(2026, 10, 10, 14, tzinfo=EDT),
                                                               datetime(2026, 10, 10, 15, tzinfo=EDT)))
        (before,) = self.blocks(picture, pictures.ROLE_BEFORE)
        (after,) = self.blocks(picture, pictures.ROLE_AFTER)
        self.assertEqual((before.start_min, before.time_text), (14 * 60, "2:00-3:00 PM"))
        self.assertEqual((after.start_min, after.time_text), (15 * 60, "3:00-4:00 PM"))
        # Today on a calendar in another zone: the PC's "now" would sit at the wrong hour, so none.
        today = pictures.day_change_picture(line(MOVE), now=NOW,
                                            check=self.check(datetime(2026, 10, 9, 14, tzinfo=EDT),
                                                             datetime(2026, 10, 9, 15, tzinfo=EDT)))
        self.assertEqual(today.day.now_column, -1)
        same_zone = pictures.day_change_picture(line(MOVE), now=NOW, check=self.check(at(14), at(15)))
        self.assertEqual((same_zone.day.now_column, same_zone.day.now_min), (0, 13 * 60 + 30))

    def test_a_daylight_saving_change_does_not_shift_googles_times(self) -> None:
        # Read on Oct 9 (-07:00); the event is after the change (Mon Nov 2, -08:00 from Google).
        moved = MOVE.replace("when=2026-10-09 15:00-16:00", "when=2026-11-02 11:00-12:00").replace(
            "at=2026-10-09 14:00-15:00", "at=2026-11-02 09:00-10:00")
        picture = pictures.day_change_picture(line(moved), now=NOW, check=self.check(
            datetime(2026, 11, 2, 9, tzinfo=PST), datetime(2026, 11, 2, 10, tzinfo=PST)))
        (before,) = self.blocks(picture, pictures.ROLE_BEFORE)
        self.assertEqual((before.start_min, before.time_text), (9 * 60, "9:00-10:00 AM"))
        self.assertEqual(picture.day.days, ("Mon Nov 2",))
        late = pictures.day_change_picture(line(CANCEL), now=NOW, check=self.check(
            datetime(2026, 11, 2, 23, 30, tzinfo=PST), datetime(2026, 11, 2, 23, 50, tzinfo=PST), "Late call"))
        self.assertEqual(late.day.days, ("Mon Nov 2",))   # not Tue Nov 3 at 12:30 AM
        (block,) = self.blocks(late, pictures.ROLE_CANCEL)
        self.assertEqual((block.start_min, block.time_text), (23 * 60 + 30, "11:30-11:50 PM"))
        strip = pictures.week_picture({"work": [event("Standup", datetime(2026, 11, 2, 9, tzinfo=PST),
                                                      datetime(2026, 11, 2, 9, 30, tzinfo=PST))]},
                                      datetime(2026, 10, 29, tzinfo=PDT), datetime(2026, 11, 5, tzinfo=PST),
                                      now=datetime(2026, 10, 29, 10, tzinfo=PDT))
        (column,) = [column for column in strip.week.columns if column.label == "Mon 2"]
        self.assertEqual(column.blocks, ((9 * 60, 9 * 60 + 30),))

    def test_a_read_from_before_the_change_drawn_after_it(self) -> None:
        # Today's agenda read on Oct 31 at 11 PM (-07:00), the picture made on Nov 1 (-08:00):
        # the events keep their own times, and the moved one is not drawn twice.
        known = pictures.KnownDays()
        known.remember("work", pictures.SOURCE_AGENDA, datetime(2026, 10, 31, tzinfo=PDT),
                       datetime(2026, 11, 3, tzinfo=PST),
                       [CalendarEvent("Project sync", start=datetime(2026, 11, 1, 9, tzinfo=PST),
                                      end=datetime(2026, 11, 1, 10, tzinfo=PST)),
                        CalendarEvent("Lunch", start=datetime(2026, 11, 1, 12, tzinfo=PST),
                                      end=datetime(2026, 11, 1, 13, tzinfo=PST))],
                       at=datetime(2026, 10, 31, 23, tzinfo=PDT))
        moved = MOVE.replace("when=2026-10-09 15:00-16:00", "when=2026-11-01 15:00-16:00").replace(
            "at=2026-10-09 14:00-15:00", "at=2026-11-01 09:00-10:00")
        picture = pictures.day_change_picture(line(moved), now=datetime(2026, 11, 1, 10, tzinfo=PST), known=known)
        spans = sorted((block.role, block.title, block.start_min) for block in picture.day.blocks)
        self.assertEqual(spans, [("after", "Project sync", 15 * 60), ("before", "Project sync", 9 * 60),
                                 ("other", "Lunch", 12 * 60)])

    @unittest.skipUnless(datetime(2026, 10, 31, 23).astimezone().utcoffset()
                         != datetime(2026, 11, 1, 10).astimezone().utcoffset(),
                         "this PC's time zone has no daylight saving change on Nov 1 2026")
    def test_the_read_time_keeps_this_pcs_own_rules(self) -> None:
        # The app's clocks: datetime.now().astimezone() (a fixed offset that changes at the switch).
        read_at = datetime(2026, 10, 31, 23).astimezone()
        now = datetime(2026, 11, 1, 10).astimezone()
        known = pictures.KnownDays()
        known.remember("work", pictures.SOURCE_AGENDA, datetime(2026, 10, 31).astimezone(),
                       datetime(2026, 11, 3).astimezone(), [], at=read_at)
        moved = MOVE.replace("2026-10-09", "2026-11-01")
        picture = pictures.day_change_picture(line(moved), now=now, known=known)
        # read the evening before: the note says so
        self.assertEqual(picture.day.others_note,
                         "Other events as Jarvis read them at 11:00 PM (yesterday's agenda)")
        self.assertEqual((picture.day.now_column, picture.day.now_min), (0, 10 * 60))

    def test_a_read_from_an_earlier_day_says_which_day(self) -> None:
        # A read is kept for the whole session (and an agenda refresh that fails keeps the old one):
        # the note never makes an old read look fresh.
        known = pictures.KnownDays()
        known.remember("work", pictures.SOURCE_ASK, at(0, day=8), at(0, day=16), [event("Lunch", at(12), at(13))],
                       at=NOW - timedelta(hours=25))
        picture = pictures.day_change_picture(line(MOVE), now=NOW, known=known)
        self.assertEqual(picture.day.others_note,
                         "Other events as Jarvis read them at 12:30 PM (Ask's calendar read yesterday)")
        agenda = pictures.KnownDays()
        agenda.remember("work", pictures.SOURCE_AGENDA, at(0, day=6), at(0, day=16), [], at=at(14, 41, day=6))
        picture = pictures.day_change_picture(line(MOVE), now=NOW, known=agenda)
        self.assertEqual(picture.day.others_note,
                         "Other events as Jarvis read them at 2:41 PM (the agenda of Tue Oct 6)")
        # each column's read says its own day; a read of today stays as it was
        both = pictures.KnownDays()
        both.remember("work", pictures.SOURCE_AGENDA, at(0, day=8), at(0, day=12), [], at=at(14, 41, day=8))
        both.remember("work", pictures.SOURCE_ASK, at(0, day=12), at(0, day=20), [], at=NOW - timedelta(minutes=5))
        far = MOVE.replace("when=2026-10-09 15:00-16:00", "when=2026-10-13 15:00-16:00")
        picture = pictures.day_change_picture(line(far), now=NOW, known=both)
        self.assertEqual(picture.day.others_note,
                         "Other events as Jarvis read them: Fri Oct 9 at 2:41 PM (yesterday's agenda); "
                         "Tue Oct 13 at 1:25 PM (Ask's calendar read)")
        self.assertEqual(picture.note, picture.day.others_note)

    def test_the_zone_of_the_pcs_own_times(self) -> None:
        self.assertIsNone(pictures._zone(None))
        self.assertIsNone(pictures._zone(datetime(2026, 10, 9, 13, 30)))
        machine = datetime(2026, 10, 9, 13, 30).astimezone()
        self.assertIsNone(pictures._zone(machine))   # this PC's rules, daylight saving included
        offset = machine.utcoffset() + timedelta(hours=5)
        other = datetime(2026, 10, 9, 13, 30, tzinfo=timezone(offset))
        self.assertIs(pictures._zone(other), other.tzinfo)

    def test_cancel_with_the_lines_time_and_with_none(self) -> None:
        picture = pictures.day_change_picture(line(CANCEL), now=NOW)
        (block,) = picture.day.blocks
        self.assertEqual((block.role, block.start_min, picture.day.headline),
                         (pictures.ROLE_CANCEL, 11 * 60, "Cancel: Design review"))
        none = pictures.day_change_picture(line(CANCEL.replace("at=2026-10-09 11:00-12:00", "at=")), now=NOW)
        (block,) = none.day.blocks
        self.assertTrue(block.timeless)
        self.assertEqual(none.day.days, ("Day not known",))
        self.assertEqual(none.day.others_note, "Only this event is drawn: Jarvis does not know its day")

    def test_rsvp_chips(self) -> None:
        for answer, chip in (("yes", "YES"), ("no", "NO"), ("maybe", "MAYBE")):
            picture = pictures.day_change_picture(line(RSVP.replace("answer=maybe", f"answer={answer}")), now=NOW)
            (block,) = picture.day.blocks
            self.assertEqual((block.role, block.chip, picture.day.headline),
                             (pictures.ROLE_RSVP, chip, f"Answer {chip}: Team lunch"))

    def test_other_events_only_from_a_covering_read_and_never_the_event_itself(self) -> None:
        known = self.known(event("Standup", at(9), at(9, 15), "e1"), event("Project sync", at(14), at(15), "evt0001aa"),
                           event("1:1", at(14, 30), at(15), "e3"), event("Tomorrow", at(9, day=10), at(10, day=10)))
        picture = pictures.day_change_picture(line(MOVE), now=NOW, known=known)
        others = self.blocks(picture, pictures.ROLE_OTHER)
        self.assertEqual([block.title for block in others], ["Standup", "1:1"])
        self.assertTrue(picture.day.others_shown)
        self.assertEqual(picture.day.others_note, "Other events as Jarvis read them at 1:30 PM (today's agenda)")
        self.assertEqual(picture.note, picture.day.others_note)   # SPEC 3.3: the note is the coverage line
        self.assertTrue(picture.alt.endswith("; 2 other events that day"))
        other_account = pictures.day_change_picture(line(MOVE), now=NOW, known=self.known(alias="personal"))
        self.assertFalse(other_account.day.others_shown)
        self.assertEqual(self.blocks(other_account, pictures.ROLE_OTHER), [])

    def test_an_agenda_read_of_calendars_in_two_time_zones_is_drawn_in_one_frame(self) -> None:
        # The agenda asks Google for no time zone: each [agenda] calendar comes in its OWN zone. A
        # team calendar kept 3 h east is drawn at its time in the account's frame (the card's), and
        # the card's own event is still matched (not drawn twice).
        team = "team.calendar@example.com"
        items = [CalendarEvent("Standup", start=at(10), end=at(11), calendar_id="primary"),
                 CalendarEvent("Team sync", start=datetime(2026, 10, 9, 15, tzinfo=EDT),
                               end=datetime(2026, 10, 9, 16, tzinfo=EDT), calendar_id=team),
                 CalendarEvent("Offsite", start=datetime(2026, 10, 9, 20, tzinfo=EDT),
                               end=datetime(2026, 10, 9, 21, tzinfo=EDT), calendar_id=team)]
        known = pictures.KnownDays()
        known.remember("personal", pictures.SOURCE_AGENDA, at(0), at(0, day=12), items, at=NOW)
        day = known.lookup("personal", date(2026, 10, 9))
        self.assertEqual([(item.title, item.start) for item in day.events],
                         [("Standup", datetime(2026, 10, 9, 10)), ("Team sync", datetime(2026, 10, 9, 12)),
                          ("Offsite", datetime(2026, 10, 9, 17))])
        cancel = ("Cancel: acct=personal | event=evt0002aa | cal=team.calendar@example.com | notify=all | "
                  "title=Team sync | at=2026-10-09 12:00-13:00 | link= | body=")
        picture = pictures.day_change_picture(line(cancel), now=NOW, known=known)
        drawn = [(block.role, block.title, block.start_min) for block in picture.day.blocks]
        self.assertEqual(sorted(drawn), sorted([(pictures.ROLE_OTHER, "Standup", 10 * 60),
                                                (pictures.ROLE_OTHER, "Offsite", 17 * 60),
                                                (pictures.ROLE_CANCEL, "Team sync", 12 * 60)]))
        # [agenda] calendars naming the owner's own calendar by its address (no "primary"): his
        # calendar, in this PC's frame, stays the frame although the team calendar has more events
        by_address = pictures.KnownDays()
        by_address.remember("personal", pictures.SOURCE_AGENDA, at(0), at(0, day=12),
                            [CalendarEvent("Standup", start=at(10), end=at(11), calendar_id="owner@example.com"),
                             *items[1:]], at=NOW)
        self.assertEqual([(item.title, item.start) for item in by_address.lookup("personal", date(2026, 10, 9)).events],
                         [("Standup", datetime(2026, 10, 9, 10)), ("Team sync", datetime(2026, 10, 9, 12)),
                          ("Offsite", datetime(2026, 10, 9, 17))])
        # one calendar in another zone than this PC (an account kept in another zone): as it carries it
        alone = pictures.KnownDays()
        alone.remember("personal", pictures.SOURCE_AGENDA, at(0), at(0, day=12),
                       [CalendarEvent("Call", start=datetime(2026, 10, 9, 15, tzinfo=EDT),
                                      end=datetime(2026, 10, 9, 16, tzinfo=EDT), calendar_id="primary")], at=NOW)
        self.assertEqual([item.start for item in alone.lookup("personal", date(2026, 10, 9)).events],
                         [datetime(2026, 10, 9, 15)])

    def test_without_ids_the_same_title_and_start_is_the_event(self) -> None:
        known = self.known(CalendarEvent("Dentist", start=at(16, 30), end=at(17, 15)),
                           CalendarEvent("Dentist", start=at(8), end=at(9)), alias="personal")
        picture = pictures.day_change_picture(line("Calendar: Dentist | 2026-10-09 16:30-17:15 | | |"), now=NOW,
                                              known=known)
        self.assertEqual([block.start_min for block in self.blocks(picture, pictures.ROLE_OTHER)], [8 * 60])

    def test_a_partly_known_move_says_which_day_was_not_read(self) -> None:
        known = pictures.KnownDays()
        known.remember("work", pictures.SOURCE_ASK, at(0), at(0, day=10), [event("Lunch", at(12), at(13))], at=NOW)
        picture = pictures.day_change_picture(line(MOVE.replace("at=2026-10-09", "at=2026-10-08")), now=NOW,
                                              known=known)
        self.assertEqual(picture.day.others_note, "Other events as Jarvis read them at 1:30 PM (Ask's calendar read); "
                                                  "Jarvis has not read the rest of Thu Oct 8")

    def test_the_footer_names_each_read_the_columns_came_from(self) -> None:
        known = pictures.KnownDays()
        known.remember("work", pictures.SOURCE_ASK, at(0, day=8), at(0, day=24), [event("Far", at(10, day=23),
                                                                                         at(11, day=23))],
                       at=NOW - timedelta(minutes=25))
        known.remember("work", pictures.SOURCE_AGENDA, at(0), at(0, day=23), [event("Near", at(10), at(11))],
                       at=NOW - timedelta(minutes=5))
        far = MOVE.replace("when=2026-10-09 15:00-16:00", "when=2026-10-23 15:00-16:00")
        picture = pictures.day_change_picture(line(far), now=NOW, known=known)
        self.assertEqual([block.title for block in self.blocks(picture, pictures.ROLE_OTHER)], ["Near", "Far"])
        self.assertEqual(picture.day.others_note,
                         "Other events as Jarvis read them: Fri Oct 9 at 1:25 PM (today's agenda); "
                         "Fri Oct 23 at 1:05 PM (Ask's calendar read)")

    def test_short_events_one_after_the_other_sit_side_by_side(self) -> None:
        known = self.known(event("Standup", at(9), at(9, 15)), event("Check-in", at(9, 15), at(9, 30)),
                           event("Later", at(10), at(10, 15)))
        cancel = CANCEL.replace("at=2026-10-09 11:00-12:00", "at=2026-10-09 09:30-09:45")
        picture = pictures.day_change_picture(line(cancel), now=NOW, known=known)
        lanes = {block.title: (block.lane, block.lanes) for block in picture.day.blocks}
        self.assertEqual(lanes["Standup"], (0, 2))
        self.assertEqual(lanes["Check-in"], (1, 2))
        self.assertEqual(lanes["Design review"], (0, 2))   # 9:30: the Standup's lane is free again
        self.assertEqual(lanes["Later"], (0, 1))

    def test_lanes_for_a_squeezed_drawing(self) -> None:
        # hud re-lanes a squeezed thumbnail: its shortest block then stands for more than 30 minutes.
        known = self.known(event("Standup", at(9), at(9, 30)), event("Office hours", at(9, 30), at(10)),
                           event("Review", at(10), at(10, 30)))
        picture = pictures.day_change_picture(line(CANCEL), now=NOW, known=known)
        natural = {block.title: (block.lane, block.lanes) for block in picture.day.blocks}
        self.assertEqual(natural["Standup"], (0, 1))
        self.assertEqual(natural["Office hours"], (0, 1))
        squeezed = {block.title: (block.lane, block.lanes)
                    for block in pictures.with_lanes(picture.day.blocks, 54)}
        self.assertEqual(squeezed["Standup"], (0, 2))
        self.assertEqual(squeezed["Office hours"], (1, 2))
        self.assertEqual(squeezed["Review"], (0, 2))
        self.assertEqual(squeezed["Design review"], (0, 1))   # 11:00, alone
        self.assertEqual(pictures.with_lanes(picture.day.blocks), picture.day.blocks)

    def test_the_hours_widen_and_overlaps_get_lanes(self) -> None:
        known = self.known(event("Early", at(6, 10), at(7)), event("Late", at(21), at(22, 40)),
                           event("A", at(14), at(16)), event("B", at(14, 30), at(15)), event("C", at(15), at(17)))
        picture = pictures.day_change_picture(line(CANCEL), now=NOW, known=known)
        self.assertEqual((picture.day.first_min, picture.day.last_min), (5 * 60, 24 * 60))
        lanes = {block.title: (block.lane, block.lanes) for block in picture.day.blocks}
        self.assertEqual(lanes["A"], (0, 2))
        self.assertEqual(lanes["B"], (1, 2))
        self.assertEqual(lanes["C"], (1, 2))
        self.assertEqual(lanes["Early"], (0, 1))

    def test_events_crossing_midnight_are_clamped(self) -> None:
        known = self.known(event("Night shift", at(22, day=8), at(2)), event("Party", at(23), at(1, day=10)))
        picture = pictures.day_change_picture(line(CANCEL), now=NOW, known=known)
        spans = {block.title: (block.start_min, block.end_min) for block in self.blocks(picture, pictures.ROLE_OTHER)}
        self.assertEqual(spans, {"Night shift": (0, 120), "Party": (23 * 60, 24 * 60)})

    def test_others_are_capped(self) -> None:
        known = self.known(*[event(f"E{n}", at(8) + timedelta(minutes=10 * n), at(8) + timedelta(minutes=10 * n + 5))
                             for n in range(pictures.DAY_OTHERS_CAP + 7)])
        picture = pictures.day_change_picture(line(CANCEL), now=NOW, known=known)
        self.assertEqual(len(self.blocks(picture, pictures.ROLE_OTHER)), pictures.DAY_OTHERS_CAP)
        self.assertEqual(picture.day.others_more, 7)

    def test_other_kinds_have_no_day_picture(self) -> None:
        reply = line("Email: acct=work | to=ana@example.edu | cc= | subject=Hi | due= | link= | body=Hello")
        self.assertIsNone(pictures.day_change_picture(reply, now=NOW))


class WeekPictureTests(unittest.TestCase):
    def test_seven_days_from_today_with_counts(self) -> None:
        events = {"work": [event("Standup", at(9, day=day), at(9, 15, day=day)) for day in range(7, 20)]
                  + [EventBrief("x", "primary", "Offsite", all_day_start=date(2026, 10, 12),
                                all_day_end=date(2026, 10, 13))],
                  "personal": [event("Gym", at(18), at(19))]}
        picture = pictures.week_picture(events, at(0, day=6), at(0, day=21), now=NOW)
        week = picture.week
        self.assertEqual((picture.kind, picture.caption, picture.note),
                         (pictures.KIND_WEEK, "Calendar", pictures.NOTE_WEEK))
        self.assertEqual([column.label for column in week.columns],
                         ["Fri 9", "Sat 10", "Sun 11", "Mon 12", "Tue 13", "Wed 14", "Thu 15"])
        self.assertTrue(week.columns[0].today)
        self.assertEqual(week.columns[0].count, 2)
        self.assertEqual(week.columns[0].blocks, ((9 * 60, 9 * 60 + 15), (18 * 60, 19 * 60)))
        self.assertEqual([column.all_day for column in week.columns], [0, 0, 0, 1, 1, 0, 0])
        self.assertEqual(week.columns[3].titles, ("Offsite", "Standup"))
        self.assertEqual(week.total, 15)
        self.assertEqual(week.later, 4)   # Standups on Oct 16-19
        self.assertEqual(week.accounts, ("work", "personal"))

    def test_the_window_end_limits_the_columns(self) -> None:
        picture = pictures.week_picture({"work": []}, at(0, day=8), at(0, day=12), now=NOW)
        self.assertEqual([column.label for column in picture.week.columns], ["Fri 9", "Sat 10", "Sun 11"])
        self.assertEqual(picture.week.total, 0)

    def test_nothing_read_no_picture(self) -> None:
        self.assertIsNone(pictures.week_picture({}, at(0), at(0, day=12), now=NOW))
        self.assertIsNone(pictures.week_picture({"work": []}, at(0, day=1), at(0, day=5), now=NOW))


class PagePictureTests(unittest.TestCase):
    def test_pending_ready_unavailable(self) -> None:
        pending = pictures.page_pending("https://www.example.org/visit")
        self.assertEqual((pending.kind, pending.state, pending.caption, pending.note),
                         (pictures.KIND_PAGE, live.PICTURE_PENDING, "Page: www.example.org", pictures.NOTE_OPENING))
        ready = pictures.page_ready("https://www.example.org/visit", image=b"\xff\xd8jpeg", width=960, height=600,
                                    final_url="https://WWW.example.org/visit#top", took_ms=1200)
        self.assertEqual((ready.state, ready.note, ready.page.final_host),
                         (live.PICTURE_READY, pictures.NOTE_OPENED, ""))
        self.assertEqual((ready.page.width, ready.page.height, ready.page.image), (960, 600, b"\xff\xd8jpeg"))
        gone = pictures.page_unavailable("https://www.example.org/visit", "the page did not load on this PC")
        self.assertEqual((gone.state, gone.note), (live.PICTURE_UNAVAILABLE,
                                                   "Picture unavailable: the page did not load on this PC"))

    def test_redirect_still_loading_and_idn_notes(self) -> None:
        ready = pictures.page_ready("https://xn--bcher-kva.example/", image=b"x", width=10, height=10,
                                    final_url="https://www.example.net/landing", still_loading=True, blocked=3)
        self.assertEqual(ready.page.final_host, "www.example.net")
        self.assertEqual(ready.note, f"{pictures.NOTE_OPENED} - redirected to www.example.net - still loading after "
                                     f"15 s - {IDN_WARNING}")
        self.assertTrue(ready.page.idn)
        self.assertEqual(ready.page.blocked, 3)
        self.assertIn("redirected to www.example.net", ready.alt)


class SearchPictureTests(unittest.TestCase):
    """A web search drawn as a compact results page: built only from what the search returned."""

    HITS = tuple((f"Result {n} - Example University", f"https://www{n}.example.edu/admissions?term={n}")
                 for n in range(1, 13))

    def test_the_query_and_the_results_in_order(self) -> None:
        picture = pictures.search_picture("Example University spring deadline", self.HITS[:3])
        self.assertEqual((picture.kind, picture.state, picture.caption, picture.note),
                         (pictures.KIND_SEARCH, live.PICTURE_READY, pictures.CAPTION_SEARCH, pictures.NOTE_SEARCH))
        self.assertIn(pictures.KIND_SEARCH, pictures.MODEL_KINDS)          # [live] pictures decides
        search = picture.search
        self.assertEqual(search.query, "Example University spring deadline")
        self.assertEqual([(hit.title, hit.url, hit.host, hit.snippet) for hit in search.hits],
                         [(f"Result {n} - Example University", f"https://www{n}.example.edu/admissions?term={n}",
                           f"www{n}.example.edu", "") for n in (1, 2, 3)])
        self.assertEqual((search.more, search.unread, search.only, search.never), (0, False, "", ""))
        self.assertEqual(picture.alt, "Web search for Example University spring deadline: 3 results: "
                                      "www1.example.edu, www2.example.edu, www3.example.edu")

    def test_at_most_ten_drawn_and_the_rest_counted(self) -> None:
        search = pictures.search_picture("deadline", self.HITS, only=("example.edu", "example.org"),
                                         never=("example.com",)).search
        self.assertEqual((len(search.hits), search.more), (pictures.SEARCH_HITS_SHOWN, 2))
        self.assertEqual((search.only, search.never), ("example.edu, example.org", "example.com"))

    def test_each_address_as_its_site_and_the_rest(self) -> None:
        # A results page shows whose site it is first: the host (no "https://", never a user name),
        # then the rest of the address; the address as listed stays the url.
        hits = [("A", "https://Admissions.Example.edu/spring/apply?term=2027#dates"),
                ("B", "https://www.example.org/"), ("C", "https://www.example.org"),
                ("D", "http://old.example.org/a"), ("E", "https://www.example.org:8443/x"),
                ("F", "https://someone@pass.example.edu/login"), ("G", "mailto:ana@example.org"),
                ("H", "https://[::1/broken")]
        search = pictures.search_picture("deadline", hits).search
        self.assertEqual([(hit.site, hit.path) for hit in search.hits],
                         [("admissions.example.edu", "/spring/apply?term=2027#dates"),
                          ("www.example.org", ""), ("www.example.org", ""),
                          ("http://old.example.org", "/a"), ("www.example.org:8443", "/x"),
                          ("pass.example.edu", "/login"), ("", ""), ("", "")])
        self.assertEqual(search.hits[0].url, "https://Admissions.Example.edu/spring/apply?term=2027#dates")
        rlo = chr(0x202E)
        odd = pictures.search_picture("deadline", [("A", f"https://a.example.org/p{rlo}q")]).search.hits[0]
        self.assertEqual(odd.path, "/p<U+202E>q")                         # cleaned like every string

    def test_snippets_only_when_the_search_gave_one(self) -> None:
        search = pictures.search_picture("deadline", [("A", "https://a.example.org/", "Apply by March 1."),
                                                      ("B", "https://b.example.org/")]).search
        self.assertEqual([hit.snippet for hit in search.hits], ["Apply by March 1.", ""])

    def test_nothing_listed_unread_and_no_query(self) -> None:
        empty = pictures.search_picture("deadline", [])
        self.assertEqual((empty.search.hits, empty.search.unread), ((), False))
        self.assertIn("no pages listed", empty.alt)
        unread = pictures.search_picture("deadline", [], unread=True)
        self.assertTrue(unread.search.unread)
        self.assertIn("results Jarvis could not list", unread.alt)
        self.assertFalse(pictures.search_picture("deadline", self.HITS[:1], unread=True).search.unread)
        self.assertIsNone(pictures.search_picture("", self.HITS))
        self.assertIsNone(pictures.search_picture(None, self.HITS))
        odd = pictures.search_picture("deadline", [None, ("only a title",), (3, "https://c.example.org/"), ("D", "")])
        self.assertEqual([(hit.title, hit.host) for hit in odd.search.hits], [("(no title)", "c.example.org")])

    def test_every_string_is_cleaned_and_capped(self) -> None:
        rlo = chr(0x202E)   # RIGHT-TO-LEFT OVERRIDE (kept out of the source as a character)
        picture = pictures.search_picture(f"dead{rlo}line\nnext", [(f"Ti{rlo}tle " + "x" * 400,
                                                                       "https://a.example.org/" + "p" * 400,
                                                                       f"snip{rlo}pet")])
        search = picture.search
        self.assertEqual(search.query, f"dead<U+202E>line{live.LINE_BREAK}next")
        hit = search.hits[0]
        self.assertTrue(hit.title.startswith("Ti<U+202E>tle "))
        self.assertLessEqual(len(hit.title), pictures.SEARCH_TITLE_CAP)
        self.assertLessEqual(len(hit.url), pictures.SEARCH_URL_CAP)
        self.assertEqual(hit.snippet, "snip<U+202E>pet")
        self.assertNotIn(rlo, picture.alt)
        self.assertLessEqual(len(pictures.search_picture("q" * 900, []).search.query), pictures.QUERY_CAP)

    def test_cost_without_text_and_repr(self) -> None:
        picture = pictures.search_picture(MARK, [(MARK, f"https://{MARK}.example.org/", MARK * 3)])
        bare = picture.without_text()
        self.assertEqual((bare.search.hits[0].title, bare.search.hits[0].snippet), (MARK, ""))
        self.assertEqual(bare.search.query, MARK)
        self.assertLess(bare.cost, picture.cost)
        self.assertGreater(picture.cost, 2 * (4 * len(MARK)))
        for item in (picture, picture.search, *picture.search.hits):
            self.assertNotIn(MARK.casefold(), repr(item).casefold(), type(item).__name__)
        dropped = picture.dropped(live.PICTURE_MEMORY)
        self.assertEqual((dropped.state, dropped.search), (live.PICTURE_DROPPED, None))
        self.assertEqual(pictures.stamp(picture, live.STATUS_OK, ""), ("", ""))


class StampTests(unittest.TestCase):
    def setUp(self) -> None:
        view = MailView("me@example.edu", "ana@example.edu", "", "Hi", "", "", "", "Hello")
        self.mail = pictures.outgoing_picture(view, account="work")
        self.move = pictures.day_change_picture(line(MOVE), now=NOW)
        self.rsvp = pictures.day_change_picture(line(RSVP.replace("maybe", "yes")), now=NOW)
        self.add = pictures.day_change_picture(line("Calendar: Dentist | 2026-10-09 16:30-17:15 | | |"), now=NOW)
        self.cancel = pictures.day_change_picture(line(CANCEL), now=NOW)

    def test_the_table(self) -> None:
        stamp = pictures.stamp
        self.assertEqual(stamp(self.mail, live.STATUS_RUNNING, "PREVIEW"), ("WILL BE SENT", pictures.TONE_WILL))
        self.assertEqual(stamp(self.move, live.STATUS_RUNNING, "PREVIEW"), ("WILL MOVE", pictures.TONE_WILL))
        self.assertEqual(stamp(self.add, live.STATUS_RUNNING, "PREVIEW"), ("WILL BE ADDED", pictures.TONE_WILL))
        self.assertEqual(stamp(self.cancel, live.STATUS_RUNNING, "PREVIEW"), ("WILL BE CANCELLED", pictures.TONE_WILL))
        self.assertEqual(stamp(self.rsvp, live.STATUS_RUNNING, "PREVIEW"), ("WILL ANSWER YES", pictures.TONE_WILL))
        self.assertEqual(stamp(self.mail, live.STATUS_RUNNING, "SENDING"), ("SENDING NOW", pictures.TONE_DOING))
        self.assertEqual(stamp(self.move, live.STATUS_RUNNING, "SENDING"), ("CHANGING NOW", pictures.TONE_DOING))
        self.assertEqual(stamp(self.add, live.STATUS_RUNNING, "SENDING"), ("ADDING NOW", pictures.TONE_DOING))
        self.assertEqual(stamp(self.mail, live.STATUS_RUNNING, "CHANGED SINCE"),
                         ("NOT WHAT THE COUNTDOWN SHOWED", pictures.TONE_CHECK))
        self.assertEqual(stamp(self.mail, live.STATUS_OK, "SENT EXACTLY THIS"), ("SENT", pictures.TONE_DONE))
        self.assertEqual(stamp(self.add, live.STATUS_OK, "ADDED"), ("ADDED", pictures.TONE_DONE))
        self.assertEqual(stamp(self.move, live.STATUS_OK, "CHANGED"), ("MOVED", pictures.TONE_DONE))
        self.assertEqual(stamp(self.cancel, live.STATUS_OK, "CHANGED"), ("CANCELLED", pictures.TONE_DONE))
        self.assertEqual(stamp(self.rsvp, live.STATUS_OK, "CHANGED"), ("ANSWERED YES", pictures.TONE_DONE))
        self.assertEqual(stamp(self.move, live.STATUS_OK, "NOTHING SENT"), ("NOTHING CHANGED", pictures.TONE_DONE))
        self.assertEqual(stamp(self.move, live.STATUS_OK, "SENT - NO CHANGE"), ("NOTHING CHANGED", pictures.TONE_DONE))
        self.assertEqual(stamp(self.mail, live.STATUS_WARN, "UNKNOWN"),
                         ("MAY HAVE GONE OUT - CHECK", pictures.TONE_CHECK))
        # CHECK: it went out but not as the countdown showed, or Google had it already (see the note).
        self.assertEqual(stamp(self.mail, live.STATUS_WARN, "CHECK"), ("CHECK THE NOTE", pictures.TONE_CHECK))
        self.assertEqual(stamp(self.move, live.STATUS_WARN, "CHECK"), ("CHECK THE NOTE", pictures.TONE_CHECK))
        self.assertEqual(stamp(self.mail, live.STATUS_FAILED, "NOT SENT"), ("NOT SENT", pictures.TONE_FAILED))
        self.assertEqual(stamp(self.move, live.STATUS_BLOCKED, ""), ("NOT CHANGED", pictures.TONE_FAILED))
        self.assertEqual(stamp(self.mail, live.STATUS_CANCELLED, "PREVIEW"),
                         ("UNDONE - NOT SENT", pictures.TONE_UNDONE))
        self.assertEqual(stamp(self.move, live.STATUS_CANCELLED, ""), ("UNDONE - NOT CHANGED", pictures.TONE_UNDONE))

    def test_other_pictures_have_no_stamp(self) -> None:
        email = pictures.email_picture(thread(message(1)), tz=PDT)
        self.assertEqual(pictures.stamp(email, live.STATUS_RUNNING, "PREVIEW"), ("", ""))
        self.assertEqual(pictures.stamp(None, live.STATUS_OK, ""), ("", ""))
        self.assertEqual(pictures.stamp(self.mail.dropped("x"), live.STATUS_OK, "SENT EXACTLY THIS"), ("", ""))


class PictureObjectTests(unittest.TestCase):
    def test_cost_counts_the_image_and_the_strings(self) -> None:
        small = pictures.page_ready("https://www.example.org/", image=b"", width=0, height=0)
        big = pictures.page_ready("https://www.example.org/", image=b"x" * 50_000, width=960, height=600)
        self.assertEqual(big.cost - small.cost, 50_000 + 2 * (len(big.alt) - len(small.alt)))
        email = pictures.email_picture(thread(message(1, text="y" * 1000)), tz=PDT)
        self.assertGreater(email.cost, 2000)

    def test_without_text_keeps_the_headers_only(self) -> None:
        email = pictures.email_picture(thread(message(0, text=MARK), message(1, text=MARK * 3)), tz=PDT)
        bare = email.without_text()
        self.assertEqual((bare.mail.body, bare.mail.body_kept, bare.mail.body_chars), ("", False, 3 * len(MARK)))
        self.assertEqual(bare.mail.head, email.mail.head)
        self.assertEqual([item.snippet for item in bare.mail.older], [""])
        self.assertLess(bare.cost, email.cost)
        week = pictures.week_picture({"work": []}, at(0), at(0, day=12), now=NOW)
        self.assertIs(week.without_text(), week)

    def test_dropped_releases_the_data(self) -> None:
        ready = pictures.page_ready("https://www.example.org/", image=b"x" * 10_000, width=960, height=600)
        gone = ready.dropped(live.PICTURE_MEMORY)
        self.assertEqual((gone.state, gone.kind, gone.page), (live.PICTURE_DROPPED, pictures.KIND_PAGE, None))
        self.assertEqual(gone.note, "Picture not kept: " + live.PICTURE_MEMORY)
        self.assertLess(gone.cost, 1000)

    def test_no_repr_names_content(self) -> None:
        email = pictures.email_picture(thread(message(1, text=MARK, subject=MARK)), tz=PDT)
        view = MailView(f"{MARK}@example.edu", f"{MARK}@example.org", "", MARK, "", "", "", MARK)
        outgoing = pictures.outgoing_picture(view, account="work")
        known = pictures.KnownDays()
        known.remember("work", pictures.SOURCE_AGENDA, at(0), at(0, day=10), [event(MARK, at(9), at(10))], at=NOW)
        day = pictures.day_change_picture(line(CANCEL.replace("Design review", MARK)), now=NOW, known=known)
        week = pictures.week_picture({"work": [event(MARK, at(9), at(10))]}, at(0), at(0, day=12), now=NOW)
        page = pictures.page_ready(f"https://{MARK}.example.org/{MARK}", image=b"x", width=1, height=1)
        read = pictures.CalendarRead(at(0), at(0, day=12), (("work", (event(MARK, at(9), at(10)),)),), NOW)
        objects = [email, email.mail, email.mail.head, outgoing, outgoing.mail, day, day.day, *day.day.blocks, week,
                   week.week, *week.week.columns, page, page.page, read, known, known.lookup("work", date(2026, 10, 9)),
                   *known.lookup("work", date(2026, 10, 9)).events]
        for item in objects:
            self.assertNotIn(MARK.casefold(), repr(item).casefold(), type(item).__name__)
            self.assertNotIn(MARK.casefold(), str(item).casefold(), type(item).__name__)


class PlanPictureTests(unittest.TestCase):
    """A day plan drawn on its day: the events read (other roles; declined and free ones say so, R21)
    and the proposed blocks (new), stamped PROPOSED: nothing is added before each block's own click."""

    def events(self) -> dict:
        import dataclasses

        return {"personal": [event("Dentist", at(11), at(11, 45), "e1"),
                             EventBrief("e2", "primary", "Conference", all_day_start=date(2026, 10, 9),
                                        all_day_end=date(2026, 10, 9))],
                "work": [dataclasses.replace(event("Team lunch", at(12), at(13), "e3"), self_response="declined"),
                         dataclasses.replace(event("Office hours", at(16), at(17), "e4"), transparent=True),
                         dataclasses.replace(event("Home", at(9), at(17), "e5"), event_type="workingLocation"),
                         event("Next week", at(9, day=16), at(10, day=16), "e6")]}

    def blocks(self) -> list:
        import dataclasses

        return [dataclasses.replace(line("Calendar: Work on: Lab report | 2026-10-09 14:00-15:30 | | | For: d1"),
                                    mode="plan"),
                dataclasses.replace(line("Calendar: Gym | 2026-10-10 07:00-08:00 | | | For: words"), mode="plan")]

    def test_roles_titles_and_caption(self) -> None:
        picture = pictures.plan_picture(self.events(), self.blocks(), day=date(2026, 10, 9), now=NOW)
        self.assertEqual((picture.kind, picture.caption, picture.state),
                         (pictures.KIND_DAY, "Your day plan", live.PICTURE_READY))
        self.assertEqual(picture.note, "Drawn by Jarvis from the calendar he just read and the proposed blocks - "
                                       "nothing added yet")
        day = picture.day
        self.assertEqual((day.change, day.days, day.account), (pictures.CHANGE_PLAN, ("Fri Oct 9",), "personal"))
        roles = {block.title: block.role for block in day.blocks}
        self.assertEqual(roles, {"Dentist": pictures.ROLE_OTHER, "Conference": pictures.ROLE_OTHER,
                                 "Team lunch (declined)": pictures.ROLE_OTHER,
                                 "Office hours (free)": pictures.ROLE_OTHER, "Home (free)": pictures.ROLE_OTHER,
                                 "Work on: Lab report": pictures.ROLE_NEW})
        self.assertEqual(day.headline, "Day plan for Fri Oct 9: 1 block proposed")
        self.assertEqual(day.now_column, 0)
        self.assertEqual(pictures.stamp(picture, live.STATUS_OK, ""), (pictures.PLAN_STAMP, pictures.TONE_WILL))
        self.assertEqual(pictures.stamp(picture, live.STATUS_WARN, ""), (pictures.PLAN_STAMP, pictures.TONE_WILL))

    def test_the_day_alone(self) -> None:
        """The calendar.read picture (and a check that kept no block) proposes nothing: captioned as
        read, never stamped PROPOSED."""
        picture = pictures.plan_picture(self.events(), (), day=date(2026, 10, 9), now=NOW)
        self.assertFalse(any(block.role == pictures.ROLE_NEW for block in picture.day.blocks))
        self.assertEqual(picture.day.headline, "Fri Oct 9 as Jarvis read it")
        self.assertEqual((picture.caption, picture.note), ("Your day as read",
                                                           "Drawn by Jarvis from the calendar he just read"))
        self.assertEqual(picture.day.others_note, pictures.NOTE_PLAN_DAY)
        for status in (live.STATUS_OK, live.STATUS_WARN, live.STATUS_RUNNING):
            self.assertEqual(pictures.stamp(picture, status, ""), ("", ""))
        self.assertEqual(picture.day.account, "personal")
        # A meeting brief draws its meeting's day on that meeting's own calendar.
        work = pictures.plan_picture(self.events(), (), day=date(2026, 10, 9), now=NOW, account="work")
        self.assertEqual((work.day.account, work.caption), ("work", "Your day as read"))
        tomorrow = pictures.plan_picture(self.events(), self.blocks(), day=date(2026, 10, 10), now=NOW)
        self.assertEqual(tomorrow.day.now_column, -1)
        self.assertEqual([block.title for block in tomorrow.day.blocks], ["Gym"])
        self.assertEqual(tomorrow.caption, "Your day plan")
        self.assertEqual(pictures.stamp(tomorrow, live.STATUS_OK, ""), (pictures.PLAN_STAMP, pictures.TONE_WILL))

    def test_now_is_drawn_in_the_personal_calendars_zone(self) -> None:
        """The blocks' times are wall times in the personal calendar's zone, so "now" (and which day is
        today) is too: 1:30 PM Friday in Los Angeles is 5:30 AM Saturday in Tokyo."""
        from unittest import mock

        from briefing_reader import gcal

        def tokyo(zone: str, moment: datetime) -> timedelta | None:
            return timedelta(hours=9) if zone == "Asia/Tokyo" else None

        block = parse_action_line("Calendar: Gym | 2026-10-10 07:00-08:00 | | | For: words")
        with mock.patch.object(gcal, "zone_offset", tokyo):
            saturday = pictures.plan_picture({}, [block], day=date(2026, 10, 10), now=NOW, zone="Asia/Tokyo")
            friday = pictures.plan_picture({}, [], day=date(2026, 10, 9), now=NOW, zone="Asia/Tokyo")
        self.assertEqual((saturday.day.now_column, saturday.day.now_min), (0, 5 * 60 + 30))
        self.assertEqual(saturday.day.days, ("Sat Oct 10",))
        self.assertEqual(friday.day.now_column, -1)                    # Friday is over in Tokyo
        here = pictures.plan_picture({}, [], day=date(2026, 10, 9), now=NOW)
        self.assertEqual((here.day.now_column, here.day.now_min), (0, 13 * 60 + 30))

    def test_no_repr_names_content(self) -> None:
        picture = pictures.plan_picture({"work": [event(MARK, at(9), at(10))]}, [], day=date(2026, 10, 9), now=NOW)
        for item in (picture, picture.day, *picture.day.blocks):
            self.assertNotIn(MARK.casefold(), repr(item).casefold())


if __name__ == "__main__":
    unittest.main()
