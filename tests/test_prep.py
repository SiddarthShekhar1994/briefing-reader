"""Tests for briefing_reader.ask.prep: which meeting typed words name, whom a brief looks up and with
which search, the <people> rows, the run's answer read and checked, and the brief's text.

Every name, address, title and id is invented (example.com / example.org / example.edu)."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from briefing_reader.actions import email_address, parse_action_line
from briefing_reader.ask import prep
from briefing_reader.ask.mail import (MAX_QUERY_CHARS, MailMessage, MailPerson, MailThread, QueryRefused,
                                      check_query)
from briefing_reader.ask.modes import EventRef, ThreadRef
from briefing_reader.ask.stream import GARBLED, GUARD, MAX_TURNS, AskFailure, InitSummary
from briefing_reader.ask.validate import UNKNOWN_THREAD
from briefing_reader.gcal import EventBrief, Guest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PDT = timezone(timedelta(hours=-7))
NOW = datetime(2026, 10, 7, 9, 30, tzinfo=PDT)        # a Wednesday
ZONE = "America/Los_Angeles"
OWN = frozenset({"you@example.edu", "you@example.com"})
INIT = InitSummary("none", ("StructuredOutput",), 0, "claude-sonnet-4-5", "2.1.293", True)
INIT_NO_TOOL = InitSummary("none", (), 0, "claude-sonnet-4-5", "2.1.293", False)


def at(hour: int, minute: int = 0, days: int = 0) -> datetime:
    return (NOW + timedelta(days=days)).replace(hour=hour, minute=minute)


def meeting(title: str = "Project sync", start: datetime | None = None, minutes: int = 30, *,
            event_id: str = "evt0001", guests: tuple[Guest, ...] | None = None, organizer: str = "ana@example.edu",
            organizer_name: str = "Ana Lima", self_response: str = "accepted", organizer_self: bool = False
            ) -> EventBrief:
    start = start or at(14)
    if guests is None:
        guests = (Guest("ben@example.org", "Ben Ode", "accepted"), Guest("cy@example.edu", "Cy Park", "needsAction"))
    return EventBrief(event_id, "primary", title, start=start, end=start + timedelta(minutes=minutes),
                      organizer_self=organizer_self, organizer_email=organizer, organizer_name=organizer_name,
                      guests=guests, self_response=self_response)


def message(sender: str = "ana@example.edu", to: str = "you@example.edu", cc: str = "", subject: str = "Budget numbers",
            text: str = "Could you send the Q3 numbers?", gmail_id: str = "msg0000777") -> MailMessage:
    return MailMessage(gmail_id, "CAB7@mail.example.edu", MailPerson(sender, "Ana Lima"), (MailPerson(to),),
                       (MailPerson(cc),) if cc else (), sent_at=datetime(2026, 10, 8, 16, 0, tzinfo=timezone.utc),
                       subject=subject, text=text)


def thread(thread_id: str = "thr0000777", *, sender: str = "ana@example.edu", to: str = "you@example.edu",
           cc: str = "", subject: str = "Budget numbers", account: str = "work",
           text: str = "Could you send the Q3 numbers?", messages: tuple[MailMessage, ...] = ()) -> MailThread:
    messages = messages or (message(sender, to, cc, subject, text, "msg" + thread_id[3:]),)
    return MailThread(account, thread_id, subject, messages)


def result(structured=None, **changes):  # type: ignore[no-untyped-def]
    event = {"type": "result", "subtype": "success", "is_error": False, "result": "", "num_turns": 2}
    if structured is not None:
        event["structured_output"] = structured
    event.update(changes)
    return event


GOOD = {"say": "Ana waits on the numbers.",
        "threads": [{"thread": "thr0000777", "summary": "She asked for the Q3 numbers.", "waiting_on": "owner"}],
        "open": [{"text": "Send Ana the Q3 numbers.", "thread": "thr0000777"}],
        "points": ["Agree the date for the demo."], "lines": []}


class ResolveMeetingTests(unittest.TestCase):
    def resolve(self, words: str, *briefs: EventBrief, now: datetime = NOW):  # type: ignore[no-untyped-def]
        return prep.resolve_meeting(words, {"work": list(briefs)}, now, OWN, zone=ZONE)

    def test_time_words(self) -> None:
        sync = meeting(start=at(14))
        other = meeting("Lab meeting", at(15, 30), event_id="evt0002")
        for words in ("my 2 PM", "2 pm", "2pm", "14:00", "2", "my 2 PM meeting", "2:05 pm", "1:55 p.m."):
            with self.subTest(words=words):
                found = self.resolve(words, sync, other)
                self.assertEqual((found.how, [ref.event_id for ref in found.refs]), (prep.MATCH_TIME, ["evt0001"]))
        self.assertEqual([ref.event_id for ref in self.resolve("3:30", sync, other).refs], ["evt0002"])
        self.assertEqual(self.resolve("my 4 PM", sync, other).refs, ())          # +-10 minutes only

    def test_a_bare_hour_may_be_am_or_pm(self) -> None:
        early = meeting("Early call", at(2), event_id="evt0003")
        late = meeting(start=at(14))
        found = self.resolve("2", early, late)
        self.assertEqual([ref.event_id for ref in found.refs], ["evt0003", "evt0001"])   # both: the owner picks
        self.assertEqual([ref.event_id for ref in self.resolve("2 am", early, late).refs], ["evt0003"])

    def test_tomorrow(self) -> None:
        today = meeting(start=at(14))
        tomorrow = meeting(start=at(14, days=1), event_id="evt0002")
        self.assertEqual([ref.event_id for ref in self.resolve("2 PM tomorrow", today, tomorrow).refs], ["evt0002"])
        self.assertEqual([ref.event_id for ref in self.resolve("tomorrow's meeting", today, tomorrow).refs],
                         ["evt0002"])
        self.assertEqual(self.resolve("tomorrow's Project sync", today, tomorrow).refs[0].event_id, "evt0002")

    def test_next(self) -> None:
        past = meeting("Standup", at(9), event_id="evt0000")
        sync = meeting(start=at(14))
        later = meeting("Lab meeting", at(16), event_id="evt0002")
        for words in ("", "next", "my next meeting", "the next call", "my meeting"):
            with self.subTest(words=words):
                found = self.resolve(words, later, sync, past)
                self.assertEqual((found.how, [ref.event_id for ref in found.refs]), (prep.MATCH_NEXT, ["evt0001"]))

    def test_title_words_all_or_some(self) -> None:
        """R5: every typed word in the title goes straight on; only some asks first."""
        sync = meeting(start=at(14))
        dana = meeting("Call with Dana", at(16), event_id="evt0002")
        found = self.resolve("Project sync", sync, dana)
        self.assertEqual((found.how, [ref.event_id for ref in found.refs]), (prep.MATCH_TITLE_ALL, ["evt0001"]))
        found = self.resolve("the Project sync meeting", sync, dana)   # "meeting" is a typed word the title lacks
        self.assertEqual((found.how, [ref.event_id for ref in found.refs]), (prep.MATCH_TITLE_SOME, ["evt0001"]))
        found = self.resolve("budget call", meeting("Budget review", at(11), event_id="evt0003"))
        self.assertEqual((found.how, [ref.event_id for ref in found.refs]), (prep.MATCH_TITLE_SOME, ["evt0003"]))
        found = self.resolve("my call with Ben", sync, dana)
        self.assertEqual((found.how, [ref.event_id for ref in found.refs]), (prep.MATCH_TITLE_SOME, ["evt0002"]))
        found = self.resolve("my call with Dana", sync, dana)             # "call" still picks "Call with Dana"
        self.assertEqual((found.how, [ref.event_id for ref in found.refs]), (prep.MATCH_TITLE_ALL, ["evt0002"]))
        found = self.resolve("project review", sync, dana)
        self.assertEqual((found.how, len(found.refs)), (prep.MATCH_TITLE_SOME, 1))

    def test_none_and_several(self) -> None:
        self.assertEqual(self.resolve("Zebra review", meeting()).refs, ())
        many = [meeting(start=at(10 + number), event_id=f"evt000{number}") for number in range(5)]
        found = self.resolve("Project sync", *many)
        self.assertEqual(len(found.refs), prep.MAX_CHOICES)                       # at most 3, by start
        self.assertEqual([ref.event_id for ref in found.refs], ["evt0000", "evt0001", "evt0002"])

    def test_only_qualifying_meetings(self) -> None:
        declined = meeting(self_response="declined")
        alone = meeting(guests=(), organizer_self=True, organizer="you@example.edu")
        own_only = meeting(guests=(Guest("you@example.com", "Me", "accepted"),), organizer_self=True,
                           organizer="you@example.edu")
        whole_day = EventBrief("evt0009", "primary", "Project sync", all_day_start=NOW.date(),
                               all_day_end=NOW.date(), guests=(Guest("ben@example.org", "Ben", "accepted"),))
        later = meeting(start=at(14, days=2))
        for brief in (declined, alone, own_only, whole_day, later):
            with self.subTest(event=brief.event_id):
                self.assertEqual(self.resolve("Project sync", brief).refs, ())
        over = meeting(start=at(8))
        self.assertEqual(self.resolve("Project sync", over).refs, ())            # a title: only meetings not over
        self.assertEqual(len(self.resolve("8 am", over).refs), 1)                 # a time: then "it has ended"

    def test_one_event_on_two_calendars_counts_once(self) -> None:
        found = prep.resolve_meeting("Project sync", {"personal": [meeting()], "work": [meeting()]}, NOW, OWN,
                                     zone=ZONE)
        self.assertEqual([ref.alias for ref in found.refs], ["personal"])

    def test_repr(self) -> None:
        found = self.resolve("Zq7 sync", meeting("Zq7 sync"))
        self.assertEqual(repr(found), "MeetingMatch(1, title_all)")
        self.assertEqual(repr(found.refs[0]), "EventRef(work)")
        target = prep.parse_target("zq7req budget sync tomorrow")             # the typed words: counts only
        self.assertEqual(repr(target), "_Target(title, 0 time(s), 3 word(s))")
        self.assertNotIn("zq7req", repr(target))
        self.assertEqual(repr(prep.parse_target("my 2 PM")), "_Target(time, 1 time(s), 0 word(s))")


class GuestTests(unittest.TestCase):
    def test_order_statuses_and_cap(self) -> None:
        guests = (Guest("dee@example.edu", "Dee Example", "declined"),
                  Guest("cy@example.edu", "Cy Park", "needsAction"),
                  Guest("ana@example.edu", "Ana Lima", "accepted", organizer=True),
                  Guest("eve@example.org", "Eve Example", "tentative"), Guest("ben@example.org", "Ben Ode", "accepted"),
                  Guest("you@example.com", "Me", "accepted"), Guest("class@example.edu", "Class list", "accepted"),
                  Guest("BEN@Example.org", "Ben again", "accepted"))
        lookups = prep.prep_guests(meeting(guests=guests), OWN, 3)
        self.assertEqual([(lookup.guest.address, lookup.status) for lookup in lookups], [
            ("ana@example.edu", prep.PENDING), ("ben@example.org", prep.PENDING), ("you@example.com", prep.OWN),
            ("class@example.edu", prep.GROUP), ("eve@example.org", prep.PENDING),
            ("cy@example.edu", prep.NOT_SEARCHED), ("dee@example.edu", prep.DECLINED)])
        self.assertEqual(lookups[0].guest.role, prep.ROLE_ORGANIZER)
        self.assertEqual(lookups[0].guest.response, "accepted")                  # from the organizer's own entry
        self.assertEqual(lookups[5].why, "only 3 guests are looked up")
        self.assertEqual(repr(lookups[0]), "GuestLookup(pending)")
        self.assertEqual(repr(lookups[0].guest), "Guest(organizer)")

    def test_the_organizer_from_a_guest_flag(self) -> None:
        guests = (Guest("ben@example.org", "Ben Ode", "accepted"),
                  Guest("ana@example.edu", "Ana Lima", "tentative", organizer=True))
        lookups = prep.prep_guests(meeting(guests=guests, organizer="", organizer_name=""), OWN, 5)
        self.assertEqual([(lookup.guest.address, lookup.guest.role) for lookup in lookups],
                         [("ana@example.edu", prep.ROLE_ORGANIZER), ("ben@example.org", prep.ROLE_GUEST)])
        mine = prep.prep_guests(meeting(guests=guests[:1], organizer_self=True, organizer="you@example.edu"), OWN, 5)
        self.assertEqual([lookup.guest.role for lookup in mine], [prep.ROLE_GUEST])


class QueryTests(unittest.TestCase):
    def test_the_query(self) -> None:
        """The address is quoted, so Gmail reads it as one value and nothing in it can open or
        close a group or name an operator; the query has exactly its six parts."""
        from briefing_reader.ask.mail import _TOKEN_RE

        query = prep.prep_query("ana@example.edu", 14)
        self.assertEqual(query, '{from:"ana@example.edu" to:"ana@example.edu" cc:"ana@example.edu"} newer_than:14d')
        self.assertEqual(check_query(query, None), query)
        self.assertEqual(_TOKEN_RE.findall(query), ["{", "from:", '"ana@example.edu"', "to:", '"ana@example.edu"',
                                                    "cc:", '"ana@example.edu"', "}", "newer_than:14d"])
        for days, word in ((1, "3d"), (3, "3d"), (7, "7d"), (30, "14d"), ("x", "14d")):
            with self.subTest(days=days):
                self.assertTrue(prep.prep_query("ana@example.edu", days).endswith(f"newer_than:{word}"))   # R4
        self.assertEqual(prep.prep_query("Ana Lima <Ana.Lima+lab@Example.edu>", 14),
                         '{from:"Ana.Lima+lab@example.edu" to:"Ana.Lima+lab@example.edu" '
                         'cc:"Ana.Lima+lab@example.edu"} newer_than:14d')

    def test_a_long_address_falls_back_to_from(self) -> None:
        address = "a" * 50 + ".lima@example.edu"
        query = prep.prep_query(address, 14)
        self.assertEqual(query, f'from:"{address}" newer_than:14d')
        self.assertLessEqual(len(query), MAX_QUERY_CHARS)
        self.assertTrue(prep.guest_allowed(address, 14))

    def test_guest_allowed(self) -> None:
        """R2: check_query and is_security_sender with no own domains, on every guest."""
        for address in ("ana@example.edu", "o'brien@example.org", "ana.lima+lab@example.edu", "a-b_c%d@example.edu"):
            with self.subTest(address=address):
                self.assertTrue(prep.guest_allowed(address, 14))
        for address in ("colleague@google.com", "someone@accounts.google.com", "pat@paypal.com",
                        "security@example.edu", "alerts@example.org", "password.help@example.org",
                        "bank@example.org", "not an address", "", "li@ex.io"):
            with self.subTest(address=address):
                self.assertFalse(prep.guest_allowed(address, 14))

    def test_an_address_with_search_syntax_is_refused(self) -> None:
        """A calendar guest's address may hold "{", "}" and other RFC atext (actions.email_address
        takes them), which would close the one-guest group early and widen the search to anyone's
        mail: such an address is no plain mailbox, so it is never searched (guest_allowed False) and
        prep_query refuses to build a query from it at all."""
        for address in ("chase}OR{owner@example.edu", "paypal}{x@example.com", "}{from@example.com",
                        "ana}OR{bob@example.com", "{ana}@example.com", "abc}-{xyz@example.org", "a*b@example.com",
                        "a=b@example.com", "x#y@example.com", "a/b@example.com", "q?r@example.com",
                        "a!b@example.com", "a`b@example.com", "a~b@example.com", "a&b@example.com",
                        "a$b@example.com", "a^b@example.com", "a{b@example.com"):
            with self.subTest(address=address):
                self.assertTrue(email_address(address), "a valid address for mail")   # the gate is prep's
                self.assertEqual(prep.plain_address(address), "")
                self.assertFalse(prep.guest_allowed(address, 14))
                with self.assertRaises(QueryRefused):
                    prep.prep_query(address, 14)
        for address in ("", "not an address", "a@b", "me@"):
            with self.subTest(address=address):
                with self.assertRaises(QueryRefused):
                    prep.prep_query(address, 14)

    def test_the_owners_own_security_domain_is_no_exemption(self) -> None:
        """An owner who works at a security provider: the follow-up list exempts his own domain
        (is_security_sender with own_domains); prep never does (R2)."""
        from briefing_reader.ask.mail import is_security_sender

        self.assertFalse(is_security_sender("colleague@google.com", own_domains=("google.com",)))
        self.assertFalse(prep.guest_allowed("colleague@google.com", 14))

    def test_index_lookup(self) -> None:
        """R3: the meeting's account only (another only with other_accounts), the newest ref."""
        refs = {"ana@example.edu": (ThreadRef("work", "t1", at(8, days=-2)), ThreadRef("personal", "t2", at(9)),
                                    ThreadRef("work", "t3", at(8)), ThreadRef("work", "t4", at(7, days=-1)))}
        found = prep.index_lookup(refs, "Ana@Example.edu", "work", other_accounts=False)
        self.assertEqual(found.thread_id, "t3")
        self.assertIsNone(prep.index_lookup(refs, "ana@example.edu", "school", other_accounts=False))
        self.assertEqual(prep.index_lookup(refs, "ana@example.edu", "school", other_accounts=True).thread_id, "t2")
        self.assertIsNone(prep.index_lookup(refs, "ben@example.org", "work", other_accounts=True))


class PeopleTests(unittest.TestCase):
    def test_people_block(self) -> None:
        guests = (Guest("ana@example.edu", "Ana Lima", "accepted", organizer=True),
                  Guest("ben@example.org", "Ben | Ode", "accepted"), Guest("cy@example.edu", "", "needsAction"),
                  Guest("zed@example.org", "Zed", "accepted"), Guest("you@example.com", "Me", "accepted"))
        given = {"ana@example.edu": (prep.FOUND, "work", "thr1", at(8, days=1)), "ben@example.org": (prep.NONE,),
                 "cy@example.edu": (prep.REFUSED, "", "", None, prep.REFUSED_WHY),
                 "zed@example.org": (prep.NOT_SEARCHED, "", "", None, prep.THREADS_WHY)}
        lookups = [prep.GuestLookup(lookup.guest, *given[lookup.guest.address]) if lookup.guest.address in given
                   else lookup for lookup in prep.prep_guests(meeting(guests=guests), OWN, 5)]
        rows = prep.people_block(lookups, 14, "work").split("\n")
        self.assertEqual(rows[0], "Ana Lima (ana@example.edu) | organizer | accepted | thread found: yes (work, last "
                                  "mail 2026-10-08)")
        self.assertEqual(rows[1], "Ben / Ode (ben@example.org) | guest | accepted | thread found: no (no email in the "
                                  "last 14 days)")
        self.assertEqual(rows[2], "Zed (zed@example.org) | guest | accepted | not searched (only 3 threads are read)")
        self.assertEqual(rows[3], "cy@example.edu | guest | no answer yet | not searched (Jarvis doesn't search that "
                                  "address)")
        self.assertEqual(len(rows), 4)                                             # never the owner himself
        self.assertEqual(prep.people_block([], 14, "work"), "none")
        organizer, = prep.prep_guests(meeting(guests=()), OWN, 5)                 # no guest entry of his own
        self.assertEqual(prep.people_block([organizer], 14, "work"),
                         "Ana Lima (ana@example.edu) | organizer | no answer listed | not searched (mail not read "
                         "for this brief)")
        self.assertEqual(prep.response_words(organizer.guest), "organizer")


class ReadResultTests(unittest.TestCase):
    def test_a_good_answer(self) -> None:
        answer = prep.read_prep_result(result(GOOD), INIT)
        self.assertIsInstance(answer, prep.PrepAnswer)
        self.assertEqual(answer.threads, (("thr0000777", "She asked for the Q3 numbers.", "owner"),))
        self.assertEqual(answer.open, (("Send Ana the Q3 numbers.", "thr0000777"),))
        self.assertEqual((answer.points, answer.lines, answer.say), (("Agree the date for the demo.",), (),
                                                                    "Ana waits on the numbers."))
        self.assertNotIn("Q3", repr(answer))

    def test_shapes_caps_and_enum(self) -> None:
        bad = [
            dict(GOOD, threads=[dict(GOOD["threads"][0], waiting_on="maybe")]),
            dict(GOOD, threads=[dict(GOOD["threads"][0], summary="x" * 301)]),
            dict(GOOD, threads=GOOD["threads"] * 4),
            dict(GOOD, open=[{"text": "x"}] * 1),
            dict(GOOD, open=GOOD["open"] * 7),
            dict(GOOD, points=["x" * 161]),
            dict(GOOD, points=["a"] * 5),
            dict(GOOD, lines=["Reply: x"] * 3),
            dict(GOOD, lines=[3]),
            dict(GOOD, say="x" * 201),
            {key: value for key, value in GOOD.items() if key != "points"},
            dict(GOOD, threads="thr0000777"),
            ["not", "a", "dict"],
        ]
        for value in bad:
            with self.subTest(value=str(value)[:60]):
                found = prep.read_prep_result(result(value), INIT)
                self.assertIsInstance(found, AskFailure)
                self.assertEqual((found.kind, found.detail), (GARBLED, "shape"))

    def test_failures_come_first(self) -> None:
        self.assertEqual(prep.read_prep_result(result(GOOD, subtype="error_max_turns"), INIT).kind, MAX_TURNS)
        self.assertEqual(prep.read_prep_result(result(GOOD), None).kind, GUARD)
        self.assertEqual(prep.read_prep_result(result(), INIT).detail, "no_structured_output")

    def test_the_plain_text_fallback(self) -> None:
        text = "```json\n" + json.dumps(GOOD) + "\n```"
        answer = prep.read_prep_result(result(result=text), INIT_NO_TOOL)
        self.assertTrue(isinstance(answer, prep.PrepAnswer) and answer.via_fallback)
        self.assertEqual(prep.read_prep_result(result(result="Here is a brief."), INIT_NO_TOOL).detail, "fallback")


def lookups_for(found_thread: str = "thr0000777") -> list[prep.GuestLookup]:
    lookups = prep.prep_guests(meeting(), OWN, 5)
    lookups[0] = prep.GuestLookup(lookups[0].guest, prep.FOUND, "work", found_thread)
    lookups[1] = prep.GuestLookup(lookups[1].guest, prep.NONE)
    lookups[2] = prep.GuestLookup(lookups[2].guest, prep.NOT_SEARCHED, why=prep.THREADS_WHY)
    return lookups


class CheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.meeting = prep.meeting_of("work", meeting(), NOW, ZONE)

    def test_check_prep(self) -> None:
        answer = prep.PrepAnswer("", (("thr9999999", "Not read.", "them"), ("thread=thr0000777", "Kept.", "owner"),
                                      ("thr0000777", "A repeat.", "nobody")),
                                 (("Unknown thread item.", "thr9999999"), ("Kept item.", "thr0000777"),
                                  ("From the meeting.", "")), ("Raise the <demo> | date.",), ())
        brief = prep.check_prep(answer, self.meeting, lookups_for(), [thread()], ("Ana Lima is my lead",), zone=ZONE)
        self.assertEqual(brief.dropped, 3)
        self.assertEqual(brief.threads, (("Ana Lima", '"Budget numbers", Oct 8', "Kept.", "owner"),))
        self.assertEqual(brief.open, ("Kept item.", "From the meeting."))
        self.assertEqual(brief.points, ("Raise the \u2039demo\u203a / date.",))          # data(): no block, no bar
        self.assertEqual(brief.missing, (("Ben Ode", "no email in the last 14 days"),
                                         ("Cy Park", "not searched (only 3 threads are read)")))
        self.assertEqual(brief.notes, ("Ana Lima is my lead",))
        self.assertTrue(brief.ran)
        self.assertEqual(repr(brief), "PrepBrief(1 thread(s), 2 open, 1 point(s), 3 dropped, ran)")

    def test_prep_check(self) -> None:
        """8.2: the meeting's account, only the thread's people, never a note in the body or the
        subject (R7: the executor sends the subject too)."""
        check = prep.PrepCheck("work", [thread(cc="cc@example.org")], ("Zq7memo the launch moves to March",))

        def reply(account: str = "work", to: str = "ana@example.edu", body: str = "Thanks",
                  subject: str = "Re: x") -> str:
            return check(parse_action_line(f"Reply: acct={account} | thread=thr0000777 | msgid=<CAB7@mail.example.edu> "
                                           f"| gmid= | to={to} | cc= | subject={subject} | replied=no | due= | link= "
                                           f"| body={body}"))

        self.assertEqual(reply(), "")
        self.assertEqual(reply(to="ana@example.edu, cc@example.org"), "")
        self.assertEqual(reply(account="personal"), prep.REPLY_ACCOUNT)
        self.assertEqual(reply(to="ana@example.edu, ben@example.org"), prep.EXTRA_PEOPLE)
        self.assertEqual(reply(body="Hi Ana, zq7memo the launch moves to march, sorry"), prep.NOTE_LEAK)
        self.assertEqual(reply(subject="Re: zq7memo the launch moves to March"), prep.NOTE_LEAK)
        self.assertEqual(reply(subject="Re: the launch moves"), "")                # 3 of its words only
        email = parse_action_line("Email: acct=work | to=ana@example.edu | cc= | subject=Hi | due= | link= | body=x")
        self.assertEqual(check(email), prep.ONLY_REPLIES)
        self.assertNotEqual(prep.REPLY_ACCOUNT, UNKNOWN_THREAD)

    def test_prep_check_holds_back_another_threads_mail(self) -> None:
        """A Reply in Ana's thread must not carry what Ben wrote in his (the other threads read for
        the brief are not the owner's to share): a whole line of his, or 7 of its words in a row,
        in the body or the subject. Lines of Ana's own thread, the owner's own messages and short
        closers are no leak."""
        shared = "Thanks for the notes from Monday's call."
        ben_lines = f"Zq7lab the lab schedule moves to Thursday mornings, please confirm.\n{shared}\nBest regards,\nBen"
        own_line = "Zq7own I can do Thursday mornings from next week on, Ben."
        ben = thread("thr0000888", messages=(
            message("ben@example.org", subject="Lab schedule", text=ben_lines, gmail_id="m1"),
            message("you@example.edu", to="ben@example.org", subject="Re: Lab schedule", text=own_line, gmail_id="m2")))
        ana = thread(text=f"Could you send the Q3 numbers?\n{shared}")
        check = prep.PrepCheck("work", [ana, ben], ("Ana Lima is my lead",), own=OWN)

        def reply(body: str = "Thanks", subject: str = "Re: Budget numbers", thread_id: str = "thr0000777",
                  to: str = "ana@example.edu") -> str:
            return check(parse_action_line(f"Reply: acct=work | thread={thread_id} | msgid=<CAB7@mail.example.edu> | "
                                           f"gmid= | to={to} | cc= | subject={subject} | replied=no | due= | link= | "
                                           f"body={body}"))

        self.assertEqual(reply("Hi Ana,\\nI'll send the Q3 numbers today.\\nThanks"), "")
        self.assertEqual(reply("Hi Ana, Ben says zq7lab the lab schedule moves to Thursday mornings, please confirm."),
                         prep.MAIL_LEAK)                                              # a whole line of Ben's
        self.assertEqual(reply("Hi Ana, the lab schedule moves to Thursday mornings now."), prep.MAIL_LEAK)   # 7 words
        self.assertEqual(reply("Hi Ana, the lab schedule moves to Fridays."), "")     # fewer than 7 in a row
        self.assertEqual(reply(subject="Re: zq7lab the lab schedule moves to Thursday mornings"), prep.MAIL_LEAK)
        self.assertEqual(reply("Hi Ana,\\nCould you send the Q3 numbers?\\nBest regards,\\nSid"), "")   # her own
        self.assertEqual(reply(f"Hi Ana, {shared} Thanks"), "")                       # a line both threads hold
        self.assertEqual(reply(f"Hi Ana, {own_line}"), "")                             # the owner's own words
        self.assertEqual(reply("Hi Ben, zq7lab the lab schedule moves to Thursday mornings, please confirm. Yes.",
                               subject="Re: Lab schedule", thread_id="thr0000888", to="ben@example.org"), "")
        self.assertEqual(reply("Hi Ana, zq7memo? No: Ana Lima is my lead, as noted."), prep.NOTE_LEAK)   # notes first
        self.assertTrue(prep.mail_leak("one two three four five six seven eight", ["x one two three four five six "
                                                                                   "seven y"]))
        self.assertFalse(prep.mail_leak("Best regards, Ben", ["Best regards, Ben"]))   # under 20 characters
        self.assertFalse(prep.mail_leak("Sent from my phone, sorry", ["Sent from my phone"]))   # 18 characters
        self.assertFalse(prep.mail_leak("Ana, Sent from my iPhone", ["Sent from my iPhone"]))   # 19 characters
        self.assertFalse(prep.mail_leak("Hi Ana, confirmations please", ["Confirmations, please!!"]))   # 2 words
        self.assertTrue(prep.mail_leak("so: Sent from my phone today!", ["Sent from my phone today."]))
        self.assertEqual(prep.mail_lines(ben, OWN), ("Lab schedule", *ben_lines.split("\n")))
        self.assertEqual(prep.mail_lines(ben), ("Lab schedule", *ben_lines.split("\n"), "Re: Lab schedule", own_line))

    def test_thread_dates_follow_the_calendars_zone(self) -> None:
        """The thread's date is read in the calendar's zone like the meeting's times, never this
        PC's: 16:00 UTC is Oct 8 in Los Angeles and Oct 9 in Tokyo."""
        self.assertEqual(prep.thread_label(thread(), ZONE), '"Budget numbers", Oct 8')
        self.assertEqual(prep.thread_label(thread(), "Asia/Tokyo"), '"Budget numbers", Oct 9')
        answer = prep.PrepAnswer("", (), (), (), ())
        tokyo = prep.check_prep(answer, self.meeting, lookups_for(), [thread()], (), zone="Asia/Tokyo")
        self.assertEqual(tokyo.threads[0][1], '"Budget numbers", Oct 9')
        self.assertEqual(prep.thread_label(thread(), "").split(", ")[0], '"Budget numbers"')   # no zone: this PC's

    def test_deterministic_brief(self) -> None:
        """G4: who is coming and what each lookup found, the briefing's cards to a guest, the notes."""
        pending = [parse_action_line("Reply: acct=work | thread=18c0ffee00000002 | msgid=<a@b.example> | gmid= | "
                                     "to=Ben Ode <ben@example.org> | cc= | subject=Re: Lab slots | replied=no | due= | "
                                     "link= | body=Sure"),
                   parse_action_line("Email: acct=work | to=zed@example.org | cc= | subject=Other | due= | link= | "
                                     "body=x")]
        lookups = [prep.GuestLookup(lookup.guest, prep.NONE) if lookup.status == prep.PENDING else lookup
                   for lookup in prep.prep_guests(meeting(), OWN, 5)]
        brief = prep.deterministic_brief(self.meeting, lookups, pending, ("Cy Park prefers mornings", "Unrelated"))
        self.assertFalse(brief.ran)
        self.assertEqual(brief.missing, tuple((name, "no email in the last 14 days")
                                              for name in ("Ana Lima", "Ben Ode", "Cy Park")))
        self.assertEqual(brief.open, ("Re: Lab slots (waiting under NEEDS YOUR OK)",))
        self.assertEqual(brief.notes, ("Cy Park prefers mornings",))
        seven = prep.deterministic_brief(self.meeting, lookups, (), (), "I didn't read mail: the limit", days=7)
        self.assertEqual(seven.missing[0][1], "no email in the last 7 days")
        self.assertIn("I didn't read mail: the limit.", prep.brief_text(seven))
        self.assertEqual(brief.series_id, "")                      # a single meeting
        weekly = prep.meeting_of("work", dataclasses.replace(meeting(), series_id="weekly01"), NOW, ZONE)
        again = prep.deterministic_brief(weekly, lookups, (), ())
        self.assertEqual(again.series_id, "weekly01")              # a brief resets the series' count (R38)
        self.assertNotIn("weekly01", repr(again))

    def test_brief_text(self) -> None:
        """9.3's example."""
        guests = (Guest("ana@example.edu", "Ana Lima", "accepted", organizer=True),
                  Guest("ben@example.org", "Ben Ode", "accepted"), Guest("cy@example.edu", "Cy Park", "needsAction"))
        brief = prep.PrepBrief(EventRef("work", "primary", "evt0001", at(14)), "Project sync", "work", at(14).replace(
            tzinfo=None), at(14, 30).replace(tzinfo=None), "today",
            who=(("Ana Lima", "organizer, accepted"), ("Ben Ode", "accepted"), ("Cy Park", "no answer yet")),
            threads=(("Ana Lima", '"Budget numbers", Oct 8', "she asked for the Q3 numbers", "owner"),),
            missing=(("Ben Ode", "no email in the last 14 days"),
                     ("Cy Park", "not searched (only 3 threads are read)")),
            open=("Send Ana the Q3 numbers before the meeting.",), points=("Agree the date for the demo.",),
            notes=("Ana Lima is my project lead.",), ran=True)
        self.assertEqual(guests[0].organizer, True)
        self.assertEqual(prep.brief_text(brief), "\n".join([
            "Brief: Project sync - today 2:00-2:30 PM (work)",
            "Who: Ana Lima (organizer, accepted) \u00b7 Ben Ode (accepted) \u00b7 Cy Park (no answer yet)",
            "Last threads:",
            '- Ana Lima - "Budget numbers", Oct 8: she asked for the Q3 numbers. Waiting on you.',
            "- Ben Ode - no email in the last 14 days.",
            "- Cy Park - not searched (only 3 threads are read).",
            "Still open:",
            "- Send Ana the Q3 numbers before the meeting.",
            "Points to raise:",
            "- Agree the date for the demo.",
            "From your notes:",
            "- Ana Lima is my project lead."]))
        self.assertIn("today 14:00-14:30 (work)", prep.brief_text(brief, hour24=True))

    def test_meeting_of(self) -> None:
        found = prep.meeting_of("work", meeting(start=at(9, days=1)), NOW, ZONE)
        self.assertEqual((found.day, found.start.hour, found.end.minute), ("tomorrow", 9, 30))
        self.assertEqual(found.ref, EventRef("work", "primary", "evt0001", at(9, days=1)))
        self.assertEqual(repr(found), "PrepMeeting(work)")
        self.assertIsNone(prep.find_event([meeting()], "primary", "evt9999"))
        self.assertIsNotNone(prep.find_event([meeting()], "primary", "evt0001"))


class HygieneTests(unittest.TestCase):
    def test_qt_free_ascii_lf(self) -> None:
        code = ("import sys, briefing_reader.ask.prep, briefing_reader.ask.modes; "
                "assert not any(m.startswith('PySide6') for m in sys.modules)")
        found = subprocess.run([sys.executable, "-B", "-c", code], cwd=PROJECT_ROOT, capture_output=True, text=True,
                               timeout=60)
        self.assertEqual(found.returncode, 0, found.stderr[-2000:])
        for path in (Path(prep.__file__), PROJECT_ROOT / "briefing_reader" / "ask" / "assets" / "prep_prompt.md",
                     PROJECT_ROOT / "briefing_reader" / "ask" / "assets" / "prep_schema.json"):
            raw = path.read_bytes()
            self.assertTrue(raw.isascii(), path.name)
            self.assertNotIn(b"\r", raw)


if __name__ == "__main__":
    unittest.main()
