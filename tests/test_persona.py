"""Tests for briefing_reader.persona (greeting and announcement half) and briefing_reader.speech."""

from __future__ import annotations

import random
import subprocess
import sys
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path

from briefing_reader import persona, speech
from briefing_reader.persona import (
    AFTERNOON,
    EVENING,
    GREETINGS,
    LATE,
    MORNING,
    NEUTRAL,
    announcement,
    approvals_sentence,
    bucket_for,
    compose_opening,
    count_words,
    cut_title,
    next_event_sentence,
    pick_greeting,
    salutation,
    sentences,
    spoken_clock,
    with_address,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 8, hour, minute)


class PoolTests(unittest.TestCase):
    def test_pool_size_groups_and_ids(self) -> None:
        self.assertGreaterEqual(len(GREETINGS), 20)
        self.assertEqual(len(GREETINGS), 24)
        counts = Counter(line.bucket for line in GREETINGS)
        for bucket in (MORNING, AFTERNOON, EVENING, LATE):
            with self.subTest(bucket=bucket):
                self.assertGreaterEqual(counts[bucket], 4)
        self.assertGreaterEqual(counts[NEUTRAL], 4)
        ids = [line.id for line in GREETINGS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(persona.GREETINGS_BY_ID), set(ids))

    def test_every_line_is_ascii_short_and_addresses_once(self) -> None:
        for line in GREETINGS:
            with self.subTest(id=line.id):
                self.assertTrue(line.template.isascii())
                self.assertEqual(line.template.count("{sir}"), 1)
                self.assertLessEqual(len(line.text()), 60)
                self.assertNotIn("{", line.text())
                self.assertLessEqual(len(sentences(line.text())), 2)
                self.assertNotIn("{", line.text(""))

    def test_stable_ids(self) -> None:
        self.assertEqual(persona.GREETINGS_BY_ID["m3"].text(), "Good morning, sir. Ready when you are.")
        self.assertEqual(persona.GREETINGS_BY_ID["n1"].text(), "At your service, sir.")
        self.assertEqual(persona.GREETINGS_BY_ID["l3"].text(), "Still up, sir? I'm here.")


class BucketTests(unittest.TestCase):
    def test_boundaries(self) -> None:
        cases = {(4, 59): LATE, (5, 0): MORNING, (11, 59): MORNING, (12, 0): AFTERNOON,
                 (16, 59): AFTERNOON, (17, 0): EVENING, (21, 59): EVENING, (22, 0): LATE,
                 (0, 0): LATE, (23, 59): LATE}
        for (hour, minute), bucket in cases.items():
            with self.subTest(time=f"{hour:02d}:{minute:02d}"):
                self.assertEqual(bucket_for(at(hour, minute)), bucket)
        self.assertEqual(bucket_for(13), AFTERNOON)


class PickTests(unittest.TestCase):
    def test_never_the_last_one_and_avoids_the_last_five_while_possible(self) -> None:
        rng = random.Random(1234)
        recent: list[str] = []
        for draw in range(500):
            now = at(draw % 24, 7)
            line = pick_greeting(now, recent, rng)
            with self.subTest(draw=draw):
                self.assertNotIn(line.id, recent[-5:])
                if recent:
                    self.assertNotEqual(line.id, recent[-1])
                self.assertIn(line.bucket, (bucket_for(now), NEUTRAL))
            recent = (recent + [line.id])[-5:]

    def test_share_of_the_time_group(self) -> None:
        rng = random.Random(7)
        picks = [pick_greeting(at(9), (), rng) for _ in range(2000)]
        timed = sum(1 for line in picks if line.bucket == MORNING) / len(picks)
        self.assertAlmostEqual(timed, 0.6, delta=0.05)
        self.assertEqual({line.bucket for line in picks}, {MORNING, NEUTRAL})
        self.assertEqual(len({line.id for line in picks}), 5 + 7)   # every line can come up

    def test_group_exhausted_uses_the_other_group(self) -> None:
        # Every afternoon line was heard recently: the neutral lines remain (and vice versa).
        afternoon = [line.id for line in GREETINGS if line.bucket == AFTERNOON]
        rng = random.Random(3)
        for _ in range(50):
            self.assertEqual(pick_greeting(at(14), afternoon, rng).bucket, NEUTRAL)

    def test_never_the_last_even_with_a_long_history(self) -> None:
        everything = [line.id for line in GREETINGS if line.bucket in (LATE, NEUTRAL)]
        rng = random.Random(5)
        for _ in range(100):
            self.assertNotEqual(pick_greeting(at(23), everything, rng).id, everything[-1])

    def test_malformed_recent_is_tolerated(self) -> None:
        line = pick_greeting(at(9), ["zz", 3, None], random.Random(1))   # type: ignore[list-item]
        self.assertIn(line.bucket, (MORNING, NEUTRAL))


class AddressTests(unittest.TestCase):
    def test_sir(self) -> None:
        self.assertEqual(with_address("Good morning, {sir}.", "sir"), "Good morning, sir.")
        self.assertEqual(with_address("{Sir}, your AM briefing is ready to view.", "sir"),
                         "Sir, your AM briefing is ready to view.")

    def test_boss(self) -> None:
        self.assertEqual(with_address("Good morning, {sir}.", "boss"), "Good morning, boss.")
        self.assertEqual(with_address("{Sir}, your AM briefing is ready to view.", "boss"),
                         "Boss, your AM briefing is ready to view.")
        self.assertEqual(with_address("Hello, {sir}.", "Mr. Smith"), "Hello, Mr. Smith.")

    def test_empty_address(self) -> None:
        cases = {
            "Good morning, {sir}.": "Good morning.",
            "How can I help, {sir}?": "How can I help?",
            "Morning, {sir}. What's first on the list?": "Morning. What's first on the list?",
            "Still up, {sir}? I'm here.": "Still up? I'm here.",
            "{Sir}, your AM briefing is ready to view.": "Your AM briefing is ready to view.",
            "{Sir}, I can't tell whether it was added.": "I can't tell whether it was added.",
            "Done, {sir} - I've added it.": "Done - I've added it.",
        }
        for template, expected in cases.items():
            with self.subTest(template=template):
                self.assertEqual(with_address(template, ""), expected)
                self.assertEqual(with_address(template, "   "), expected)

    def test_every_greeting_without_address(self) -> None:
        for line in GREETINGS:
            with self.subTest(id=line.id):
                text = line.text("")
                self.assertNotIn(",.", text)
                self.assertNotIn(" ,", text)
                self.assertNotIn(" ?", text)
                self.assertTrue(text[0].isupper())


class AnnouncementTests(unittest.TestCase):
    def test_alone_and_after_a_greeting(self) -> None:
        self.assertEqual(announcement("AM", "sir"), "Sir, your AM briefing is ready to view.")
        self.assertEqual(announcement("PM", "sir"), "Sir, your PM briefing is ready to view.")
        self.assertEqual(announcement(None, "sir"), "Sir, your briefing is ready to view.")
        self.assertEqual(announcement("AM", "sir", after_greeting=True), "Your AM briefing is ready to view.")
        self.assertEqual(announcement("PM", "", after_greeting=True), "Your PM briefing is ready to view.")
        self.assertEqual(announcement(None, "sir", after_greeting=True), "Your briefing is ready to view.")
        self.assertEqual(announcement("AM", ""), "Your AM briefing is ready to view.")
        self.assertEqual(announcement("AM", "boss"), "Boss, your AM briefing is ready to view.")
        self.assertEqual(announcement("noon", "sir"), "Sir, your briefing is ready to view.")


class FactTests(unittest.TestCase):
    def test_approvals_sentence(self) -> None:
        self.assertEqual(approvals_sentence(0), "")
        self.assertEqual(approvals_sentence(1), "One proposal is waiting for your OK.")
        self.assertEqual(approvals_sentence(3), "Three proposals are waiting for your OK.")
        self.assertEqual(approvals_sentence(10), "Ten proposals are waiting for your OK.")
        self.assertEqual(approvals_sentence(12), "12 proposals are waiting for your OK.")
        self.assertEqual(approvals_sentence(2, "sir"), "Two proposals are waiting for your OK, sir.")
        self.assertEqual(count_words(7), "seven")

    def test_spoken_clock(self) -> None:
        self.assertEqual(spoken_clock(at(15)), "3 PM")
        self.assertEqual(spoken_clock(at(14, 30)), "2:30 PM")
        self.assertEqual(spoken_clock(at(0, 5)), "12:05 AM")
        self.assertEqual(spoken_clock(at(12)), "12 PM")
        self.assertEqual(spoken_clock(at(9, 0)), "9 AM")

    def test_next_event_sentence(self) -> None:
        self.assertEqual(next_event_sentence("Project sync", at(15)), "Your next event is Project sync at 3 PM.")
        self.assertEqual(next_event_sentence("  Lab   meeting ", at(9, 30)), "Your next event is Lab meeting at 9:30 AM.")
        self.assertEqual(next_event_sentence("", at(15)), "")
        # Never an address or a link aloud.
        self.assertEqual(next_event_sentence("Call ana@example.edu", at(15)), "")
        self.assertEqual(next_event_sentence("Join https://meet.example.com/abc", at(15)), "")

    def test_long_titles_are_cut_at_a_word(self) -> None:
        title = "Quarterly planning review with the whole department and the visiting committee members"
        cut = cut_title(title)
        self.assertLessEqual(len(cut), 60)
        self.assertTrue(title.startswith(cut))
        self.assertTrue(title[len(cut)] == " ")
        self.assertEqual(cut_title("x" * 80), "x" * 60)
        self.assertIn(cut, next_event_sentence(title, at(15)))


class ComposeTests(unittest.TestCase):
    def test_at_most_three_sentences(self) -> None:
        greeting = "Morning, sir. What's first on the list?"
        cases = [
            (greeting, "", "", greeting),
            ("Good morning, sir.", "Your AM briefing is ready to view.", "",
             "Good morning, sir. Your AM briefing is ready to view."),
            (greeting, "Your AM briefing is ready to view.", "One proposal is waiting for your OK.",
             "Morning, sir. Your AM briefing is ready to view. One proposal is waiting for your OK."),
            (greeting, "", "Your next event is Project sync at 3 PM.",
             "Morning, sir. Your next event is Project sync at 3 PM."),
        ]
        for greet, announce, context, expected in cases:
            with self.subTest(expected=expected):
                text = compose_opening(greet, announce, context)
                self.assertEqual(text, expected)
                self.assertLessEqual(len(sentences(text)), 3)

    def test_a_question_or_a_second_ready_gives_way_to_the_plain_salutation(self) -> None:
        ready = announcement("AM", "sir", after_greeting=True)
        plain = "Good afternoon, sir."
        cases = [
            ("How can I help, sir?", ready, "", "Good afternoon, sir. Your AM briefing is ready to view."),
            ("What can I do for you, sir?", "", approvals_sentence(1),
             "Good afternoon, sir. One proposal is waiting for your OK."),
            ("Ready when you are, sir.", ready, "", "Good afternoon, sir. Your AM briefing is ready to view."),
            ("Ready when you are, sir.", "", approvals_sentence(2),
             "Ready when you are, sir. Two proposals are waiting for your OK."),      # no second "ready"
            ("Working late, sir?", ready, "", "Good afternoon, sir. Your AM briefing is ready to view."),
            ("Welcome back, sir.", ready, "", "Welcome back, sir. Your AM briefing is ready to view."),
            ("How can I help, sir?", "", "", "How can I help, sir?"),                # alone: as it is
        ]
        for greet, announce, context, expected in cases:
            with self.subTest(greet=greet, announce=announce, context=context):
                self.assertEqual(compose_opening(greet, announce, context, plain=plain), expected)

    def test_every_greeting_combined_opens_with_a_statement(self) -> None:
        ready = announcement("PM", "sir", after_greeting=True)
        for hour in (9, 14, 19, 23, 2):
            plain = salutation(at(hour)).text()
            for line in GREETINGS:
                for announce, context in ((ready, ""), ("", approvals_sentence(3)), (ready, approvals_sentence(3))):
                    with self.subTest(hour=hour, id=line.id, announce=announce, context=context):
                        text = compose_opening(line.text(), announce, context, plain=plain)
                        first = sentences(text)[0]
                        self.assertFalse(first.endswith("?"), text)
                        if announce:
                            self.assertEqual(text.lower().count("ready"), 1, text)
                        self.assertLessEqual(len(sentences(text)), 3)

    def test_salutation_by_hour(self) -> None:
        cases = [(5, "m1", "Good morning, sir."), (11, "m1", "Good morning, sir."), (12, "a1", "Good afternoon, sir."),
                 (16, "a1", "Good afternoon, sir."), (17, "e1", "Good evening, sir."), (21, "e1", "Good evening, sir."),
                 (22, "e1", "Good evening, sir."), (23, "e1", "Good evening, sir."), (0, "n3", "Hello, sir."),
                 (4, "n3", "Hello, sir.")]
        for hour, line_id, text in cases:
            with self.subTest(hour=hour):
                line = salutation(at(hour, 30))
                self.assertEqual((line.id, line.text()), (line_id, text))
                self.assertFalse(line.text().endswith("?"))
        self.assertEqual(salutation(at(9)).text(""), "Good morning.")

    def test_every_greeting_combined(self) -> None:
        for line in GREETINGS:
            for announce in ("", announcement("AM", "sir", after_greeting=True)):
                for context in ("", approvals_sentence(3)):
                    with self.subTest(id=line.id, announce=announce, context=context):
                        text = compose_opening(line.text(), announce, context)
                        self.assertLessEqual(len(sentences(text)), 3)
                        self.assertTrue(text.startswith(sentences(line.text())[0]))
                        self.assertTrue(text.isascii())


class SpeechModuleTests(unittest.TestCase):
    def test_kinds(self) -> None:
        self.assertEqual(speech.KINDS, ("greeting", "announce", "ack", "reply", "result", "notice"))

    def test_silent_voice_never_speaks(self) -> None:
        voice = speech.SilentVoice()
        self.assertIsInstance(voice, speech.Voice)
        self.assertFalse(voice.speaking)
        self.assertFalse(voice.say(speech.KIND_GREETING, "Good morning, sir."))
        self.assertFalse(voice.say(speech.KIND_ANNOUNCE, "x", key="announce:2026-10-08 AM"))
        voice.cancel_key("announce:2026-10-08 AM")
        voice.stop()
        voice.pump()
        voice.owner_acted()
        self.assertFalse(voice.muted)
        voice.set_muted(True)
        self.assertTrue(voice.muted)
        self.assertFalse(voice.speaking)
        voice.shutdown()


class ModuleHygieneTests(unittest.TestCase):
    def test_qt_free(self) -> None:
        code = ("import sys, briefing_reader.persona, briefing_reader.speech; "
                "assert not any(m.startswith('PySide6') for m in sys.modules)")
        result = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    def test_sources_are_ascii(self) -> None:
        for module in (persona, speech):
            with self.subTest(module=module.__name__):
                self.assertTrue(Path(module.__file__).read_bytes().isascii())


# --------------------------------------------------------------------------
# Implementer B: results, Ask and safety
# --------------------------------------------------------------------------

BODY = "MARKER-BODY Hi Sam, the slides are attached."
RECIPIENT = "sam.example@example.com"
NOW_B = datetime(2026, 10, 8, 14, 0)          # a Thursday afternoon
STATUSES = ("created", "sent", "exists", "failed", "unknown")


class Outcome:
    """The surface of ask.planner.AskOutcome that persona reads."""

    def __init__(self, say: str = "", question: str = "", cards: tuple = (), message: str = "",
                 search: str = "") -> None:
        self.say, self.question, self.cards, self.message, self.search = say, question, cards, message, search


class Card:
    def __init__(self, error: str = "", body: str = BODY, title: str = "Card title") -> None:
        self.error, self.body, self.title = error, body, title


class SpokenWhenTests(unittest.TestCase):
    def test_table(self) -> None:
        cases = [
            (datetime(2026, 10, 8, 15, 0), False, "today at 3 PM"),
            (datetime(2026, 10, 9, 14, 30), False, "tomorrow at 2:30 PM"),
            (datetime(2026, 10, 10, 9, 0), False, "on Saturday at 9 AM"),        # 2 days ahead
            (datetime(2026, 10, 14, 12, 0), False, "on Wednesday at 12 PM"),     # 6 days ahead
            (datetime(2026, 10, 15, 0, 0), False, "on October 15 at 12 AM"),    # 7 days: the date
            (datetime(2026, 10, 7, 15, 0), False, "on October 7 at 3 PM"),       # past
            (datetime(2026, 10, 8, 0, 0), True, "today"),
            (datetime(2026, 10, 9, 0, 0), True, "tomorrow"),
            (datetime(2026, 10, 12, 0, 0), True, "on Monday"),
            (datetime(2026, 10, 20, 0, 0), True, "on October 20"),
        ]
        for start, all_day, expected in cases:
            with self.subTest(start=start, all_day=all_day):
                self.assertEqual(persona.spoken_when(start, None, NOW_B, all_day=all_day), expected)

    def test_plain_dates_none_and_aware_times(self) -> None:
        from datetime import date, timedelta, timezone

        self.assertEqual(persona.spoken_when(date(2026, 10, 9), None, NOW_B), "tomorrow")
        self.assertEqual(persona.spoken_when(None, None, NOW_B), "")
        pdt = timezone(timedelta(hours=-7))
        utc_start = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)    # 2 PM in PDT
        self.assertEqual(persona.spoken_when(utc_start, None, NOW_B.replace(tzinfo=pdt)), "tomorrow at 2 PM")


class OutcomeTextTests(unittest.TestCase):
    def test_the_spec_examples(self) -> None:
        self.assertEqual(persona.outcome_text("move", "sent", "Project sync", when="on Friday at 2 PM"),
                         "Done, sir - I've moved 'Project sync' to Friday at 2 PM.")
        self.assertEqual(persona.outcome_text("calendar", "created", "Review chapter 4 notes",
                                              when="tomorrow at 3 PM"),
                         "Done, sir - I've added 'Review chapter 4 notes' to your calendar for tomorrow at 3 PM.")
        self.assertEqual(persona.outcome_text("email", "sent", "Lunch Friday"),
                         "Sent, sir - your email 'Lunch Friday' is on its way.")
        self.assertEqual(persona.outcome_text("reply", "sent", "Re: plans"),
                         "Sent, sir - your reply to 'plans' is on its way.")
        self.assertEqual(persona.outcome_text("cancel", "sent", ""), "Done, sir - I've cancelled the meeting.")
        self.assertEqual(persona.outcome_text("email", "sent", ""), "Sent, sir - your email is on its way.")
        self.assertEqual(persona.outcome_text("reply", "sent", ""), "Sent, sir - your reply is on its way.")
        self.assertEqual(persona.outcome_text("todo", "created", "Problem set 3", when="today at 7 PM"),
                         "Done, sir - I've blocked time today at 7 PM for 'Problem set 3'.")
        self.assertEqual(persona.outcome_text("calendar", "exists", "Study block"),
                         "'Study block' was already on your calendar, sir - nothing new was added.")
        self.assertEqual(persona.outcome_text("move", "unknown", "Project sync"),
                         "Sir, I can't tell whether 'Project sync' was moved. Please check your calendar before "
                         "trying again.")
        self.assertEqual(persona.outcome_text("email", "failed", "Lunch Friday"),
                         "Sorry, sir - I couldn't send 'Lunch Friday'. The card says why.")

    def test_rsvp_answers(self) -> None:
        self.assertEqual(persona.outcome_text("rsvp", "sent", "Speaker series", answer="yes"),
                         "Done, sir - I've accepted 'Speaker series'.")
        self.assertEqual(persona.outcome_text("rsvp", "sent", "Speaker series", answer="no"),
                         "Done, sir - I've declined 'Speaker series'.")
        self.assertEqual(persona.outcome_text("rsvp", "sent", "Speaker series", answer="maybe"),
                         "Done, sir - I've answered maybe to 'Speaker series'.")
        self.assertEqual(persona.outcome_text("rsvp", "sent", "", answer="yes"),
                         "Done, sir - I've accepted the invitation.")

    def test_every_kind_and_status_with_and_without_a_title(self) -> None:
        for kind in persona.OUTCOME_KINDS:
            for status in STATUSES:
                for title in ("Project sync", ""):
                    for address in ("sir", "boss", ""):
                        with self.subTest(kind=kind, status=status, title=title, address=address):
                            text = persona.outcome_text(kind, status, title, when="on Friday at 2 PM",
                                                        answer="yes", address=address)
                            self.assertTrue(text.isascii() and text.strip() == text)
                            self.assertNotIn("{", text)
                            self.assertNotIn("''", text)                 # an empty title is never quoted
                            if title:
                                self.assertIn(f"'{title}'", text)
                            self.assertEqual(text.count("sir") + text.count("Sir"), 1 if address == "sir" else 0)
                            self.assertEqual(text.count("boss") + text.count("Boss"), 1 if address == "boss" else 0)
                            self.assertLessEqual(len(text), 400)
                            self.assertTrue(text.endswith("."))
                            if status in ("created", "sent") or (status == "exists" and kind not in ("calendar", "todo")):
                                self.assertTrue(text.startswith(("Done", "Sent")))
                            else:
                                self.assertFalse(text.startswith(("Done", "Sent")))

    def test_tones(self) -> None:
        self.assertEqual([persona.outcome_tone(status) for status in STATUSES],
                         ["good", "good", "good", "error", "warn"])
        self.assertEqual(persona.outcome_tone("something else"), "error")

    def test_no_body_address_link_or_id_ever(self) -> None:
        # outcome_text takes a title or subject only (never a body or recipients); even those are
        # scrubbed of addresses, links and ids.
        titles = (f"Lunch with {RECIPIENT}", "See https://docs.example.com/d/abc?x=1", "Ref jts0001aa0123456789",
                  f"Plans <{RECIPIENT}>")
        for kind in persona.OUTCOME_KINDS:
            for status in STATUSES:
                for title in titles:
                    with self.subTest(kind=kind, status=status, title=title):
                        text = persona.outcome_text(kind, status, title, when="today at 3 PM")
                        for bad in ("@", "example.com", "https", "jts0001aa0123456789"):
                            self.assertNotIn(bad, text)
        for kind in ("email", "reply"):
            text = persona.not_sent_text(kind, f"Re: {RECIPIENT}")
            self.assertNotIn("@", text)

    def test_long_titles_are_cut(self) -> None:
        title = "A very long meeting title " * 6
        text = persona.outcome_text("calendar", "created", title, when="today at 3 PM")
        quoted = text.split(" '", 1)[1].split("' ", 1)[0]
        self.assertLessEqual(len(quoted), 60)
        self.assertTrue(title.startswith(quoted))

    def test_not_sent(self) -> None:
        self.assertEqual(persona.not_sent_text("email", "Lunch Friday"),
                         "Sir, I didn't send 'Lunch Friday': the sending account changed. Please check From and "
                         "try again.")
        self.assertEqual(persona.not_sent_text("reply", ""),
                         "Sir, I didn't send your reply: the sending account changed. Please check From and try "
                         "again.")
        self.assertEqual(persona.not_sent_text("email", "", address=""),
                         "I didn't send the email: the sending account changed. Please check From and try again.")


class SubjectPrefixTests(unittest.TestCase):
    def test_re_and_fwd_are_never_said(self) -> None:
        self.assertEqual(persona.outcome_text("reply", "sent", "Re: Budget review"),
                         "Sent, sir - your reply to 'Budget review' is on its way.")
        self.assertEqual(persona.outcome_text("reply", "unknown", "RE: re: Lab schedule"),
                         "Sir, I can't tell whether your reply to 'Lab schedule' was sent. Please check your Sent "
                         "mail before trying again.")
        self.assertEqual(persona.outcome_text("email", "failed", "Fwd: Notes"),
                         "Sorry, sir - I couldn't send 'Notes'. The card says why.")
        self.assertEqual(persona.outcome_text("reply", "sent", "AW: Termin"),
                         "Sent, sir - your reply to 'Termin' is on its way.")
        self.assertEqual(persona.not_sent_text("reply", "Re: Lunch Friday"),
                         "Sir, I didn't send your reply to 'Lunch Friday': the sending account changed. Please check "
                         "From and try again.")
        self.assertEqual(persona.outcome_text("reply", "sent", "Re:"), "Sent, sir - your reply to 'Re:' is on its way.")
        # Only mail: an event may be called that.
        self.assertEqual(persona.outcome_text("cancel", "sent", "Re: org meeting"),
                         "Done, sir - I've cancelled 'Re: org meeting'.")


class ClaimsDoneTests(unittest.TestCase):
    def test_positives(self) -> None:
        for text in ("Done.", "done - moved it", "Sent!", "Booked the room.", "I've sent the email to Sam.",
                     "I have moved Project sync to Friday.", "It has been moved.", "It's been cancelled.",
                     "The meeting is now cancelled.", "Both are now scheduled.", "Your reply has been sent.",
                     "I\u2019ve added it to your calendar.", "They have been rescheduled.", "I've now emailed Ana.",
                     "Moved Jarvis test sync to Friday at 2 PM and drafted a note to Sam.",
                     "Right, sir. Cancelled the study group.", "Sent.",
                     # Found by the review: other ways of saying it happened.
                     "All done, sir.", "All set, sir - your meeting is on Friday.",
                     "I've put 'Review chapter 4 notes' on your calendar for tomorrow at three.",
                     "I've created the event.", "I've updated your calendar.", "Your email to Ana is on its way, sir.",
                     "It's on your calendar, sir.", "I've taken care of it, sir.", "Consider it done, sir.",
                     "I've blocked an hour tomorrow.", "I've told Ana you'll be late.", "I've notified Ana.",
                     "The meeting was moved to Friday.", "Project sync is now on Friday at two.",
                     "Your reply has gone out.", "I've let Ana know.", "I moved it to Friday.", "Sorted, sir.",
                     "I've gone ahead and booked it."):
            with self.subTest(text=text):
                self.assertTrue(persona.claims_done(text))

    def test_negatives(self) -> None:
        for text in ("I've lined up a move", "Both are waiting for your OK.", "Right, sir. I've lined up moving "
                     "Project sync to Friday at two, and a short note to Ana. Both are waiting for your OK.",
                     "Shall I move it?", "I drafted a note.", "Nothing is done until you approve.", "",
                     "Your sync is scheduled for Friday; shall I move it?", "Movement class at 3 PM.",
                     "Shall I also block an hour on Friday morning, or keep it free for the gym?",
                     "You have nothing on your calendar tomorrow.", "Nothing is on your calendar after three.",
                     "Shall I set up a block?", "Should I put it on Friday?", "Shall I let Ana know?",
                     "I've found two emails from Ana.", "I've checked your calendar: you're free at two.",
                     "I've drafted a reply to Ana; it's waiting for your OK.", "I've written a short note to Ana.",
                     "Would you like me to tell Ana?", "The set list is long.", "Shall I make a block for it?",
                     "I can move it to Friday or Monday."):
            with self.subTest(text=text):
                self.assertFalse(persona.claims_done(text))


class AskSpeechTests(unittest.TestCase):
    def test_say_and_question_never_the_note_search_or_cards(self) -> None:
        outcome = Outcome("Right, sir. I've lined up moving Project sync to Friday", "Shall I tell Sam?",
                          cards=(Card(), Card()), message="MARKER-NOTE mail could not be read",
                          search="from:sam MARKER-SEARCH")
        speech_ = persona.ask_reply_speech(outcome)
        self.assertEqual(speech_.text, "Right, sir. I've lined up moving Project sync to Friday. Shall I tell Sam?")
        self.assertFalse(speech_.guarded)
        for bad in ("MARKER", "Card title"):
            self.assertNotIn(bad, speech_.text)

    def test_cards_only(self) -> None:
        self.assertEqual(persona.ask_reply_speech(Outcome(cards=(Card(),))).text,
                         "One proposal is waiting for your OK, sir.")
        self.assertEqual(persona.ask_reply_speech(Outcome(cards=(Card(), Card(), Card(error="no")))).text,
                         "Two proposals are waiting for your OK, sir.")
        self.assertEqual(persona.ask_reply_speech(Outcome(cards=(Card(),)), address="").text,
                         "One proposal is waiting for your OK.")
        self.assertEqual(persona.ask_reply_speech(Outcome()).text,
                         "I couldn't find anything to propose for that, sir.")

    def test_the_guard(self) -> None:
        guarded = persona.ask_reply_speech(Outcome("Done - I've moved Project sync to Friday.", cards=(Card(), Card())))
        self.assertEqual(guarded, ("I've lined up two proposals for your OK, sir.", True))
        one = persona.ask_reply_speech(Outcome("It has been sent.", cards=(Card(),)))
        self.assertEqual(one.text, "I've lined up one proposal for your OK, sir.")
        nothing = persona.ask_reply_speech(Outcome("Sent the note.", question=""))
        self.assertEqual(nothing, ("Here's what I found, sir.", True))

    def test_the_question_is_always_said(self) -> None:
        # The planner's limits: say up to 300 characters, question up to 200; together over 400.
        say = ("Right, sir. I've lined up moving Review chapter 4 notes from tomorrow at three to Thursday at four, "
               "because it clashes with your Studio Weekly Meeting and the Northwind planning call at three, and "
               "Thursday afternoon is clear. It's waiting for your OK under NEEDS YOUR OK.")
        question = ("Shall I also block an hour on Friday morning so you can finish the reading before section, or "
                    "would you rather keep Friday morning free for the gym and the lab meeting as usual?")
        self.assertGreater(len(say) + len(question), 400)
        text = persona.ask_reply_speech(Outcome(say, question, cards=(Card(),))).text
        self.assertLessEqual(len(text), 400)
        self.assertTrue(text.endswith(question), text)
        self.assertTrue(text.startswith("Right, sir. I've lined up moving Review chapter 4 notes"), text)
        long_say = "Right, sir. " + "I've lined up a long and careful plan for the week ahead. " * 8
        long_question = "Shall I " + "also look at the other days and " * 6 + "tell Ana?"
        text = persona.ask_reply_speech(Outcome(long_say.strip(), long_question[:200], cards=(Card(),))).text
        self.assertLessEqual(len(text), 400)
        self.assertIn("Shall I also look", text)
        self.assertTrue(text.startswith("Right, sir."))

    def test_a_question_claiming_it_was_done_is_not_said(self) -> None:
        speech_ = persona.ask_reply_speech(Outcome("Right, sir.", "I've already told Ana, haven't I?", cards=(Card(),)))
        self.assertEqual(speech_, ("Right, sir.", True))

    def test_after_mail_was_read_the_say_is_short_and_never_quotes_it(self) -> None:
        class Mail:
            threads, messages = 1, 2

        quoting = Outcome('Ana wrote: "Hi there, can we move lunch to one on Friday? I have a dentist appointment." '
                          "I've lined up a reply saying yes.", "Shall I copy Sam?", cards=(Card(),))
        quoting.mail = Mail()
        text = persona.ask_reply_speech(quoting).text
        self.assertEqual(text, "I've lined up a reply saying yes. Shall I copy Sam?")
        long_say = Outcome("Right, sir. " + "Ana's note is about the lab schedule and the room booking. " * 6, "")
        long_say.mail = Mail()
        text = persona.ask_reply_speech(long_say).text
        self.assertLessEqual(len(text), persona.MAIL_SAY_LIMIT)
        self.assertTrue(text.endswith("."))
        only_quote = Outcome('She said: "Please send the slides before the meeting tomorrow."', "", cards=(Card(),))
        only_quote.mail = Mail()
        self.assertEqual(persona.ask_reply_speech(only_quote).text, "One proposal is waiting for your OK, sir.")
        unread = Outcome('Ana wrote: "Hi there, can we move lunch to one on Friday?"', "")   # no mail read: as written
        self.assertEqual(persona.ask_reply_speech(unread).text, 'Ana wrote: "Hi there, can we move lunch to one on Friday?"')

    def test_scrubbed_and_capped(self) -> None:
        long_say = ("Right, sir. " + f"I'll write to {RECIPIENT} about https://docs.example.com/x. " * 20).strip()
        text = persona.ask_reply_speech(Outcome(long_say, cards=(Card(),))).text
        self.assertLessEqual(len(text), 400)
        self.assertNotIn("@", text)
        self.assertNotIn("https", text)
        self.assertTrue(text.endswith("."))

    def test_an_answer_that_repeats_a_memory_note_gives_counts_only(self) -> None:
        notes = ("Ana is my project lead", "Gym on Tuesdays")
        said = persona.ask_reply_note_speech
        self.assertEqual(said(Outcome("Ana is your project lead, sir."), notes=notes), "Here's what I found, sir.")
        self.assertEqual(said(Outcome("Right, sir.", "Shall I tell Ana, your project lead?"), notes=notes),
                         "I have a question for you, sir; it's on screen.")
        self.assertEqual(said(Outcome("Your gym is on Tuesdays, so Friday it is.", cards=(Card(), Card())),
                              notes=notes), "Two proposals are waiting for your OK, sir.")
        self.assertEqual(said(Outcome("Ana is your project lead."), "", notes=notes), "Here's what I found.")
        self.assertEqual(said(Outcome("Your Friday is free.", cards=(Card(),)), notes=notes), "")   # as before
        self.assertEqual(said(Outcome("Ana is your project lead.")), "")                          # no notes given
        self.assertEqual(said(Outcome(cards=(Card(),)), notes=notes), "")
        for text in (persona.NOTE_QUESTION, persona.NOTE_FOUND):
            self.assertFalse(persona.claims_done(text))

    def test_failures(self) -> None:
        self.assertEqual(persona.ask_failure_speech("timeout"), "Sorry, sir - that took too long. Nothing was proposed.")
        self.assertEqual(persona.ask_failure_speech("not_signed_in"),
                         "Sir, Claude Code needs you to sign in again. The details are under the bar.")
        self.assertEqual(persona.ask_failure_speech("not_subscription"), persona.ask_failure_speech("not_signed_in"))
        self.assertEqual(persona.ask_failure_speech("caps"), "Sir, we've reached the Ask limit for now.")
        self.assertEqual(persona.ask_failure_speech("limit"), persona.ask_failure_speech("caps"))
        self.assertEqual(persona.ask_failure_speech("garbled"), "Sorry, sir - something went wrong. Nothing was proposed.")
        self.assertEqual(persona.ask_failure_speech("timeout", address=""), "Sorry - that took too long. Nothing was "
                         "proposed.")
        for kind in ("cancelled", "empty", "busy"):
            self.assertEqual(persona.ask_failure_speech(kind), "")

    def test_failure_kinds_match_the_ask_modules(self) -> None:
        from briefing_reader.ask import planner, stream

        self.assertEqual((stream.TIMEOUT, stream.NOT_SIGNED_IN, stream.LIMIT, stream.CANCELLED),
                         ("timeout", "not_signed_in", "limit", "cancelled"))
        self.assertEqual((planner.CAPS, planner.EMPTY, planner.NOT_SUBSCRIPTION), ("caps", "empty", "not_subscription"))

    def test_acks(self) -> None:
        self.assertEqual(len(persona.ACKS), 4)
        self.assertEqual([with_address(ack) for ack in persona.ACKS],
                         ["Right away, sir.", "One moment, sir.", "On it, sir.", "Let me see, sir."])
        rng = random.Random(3)
        last = ""
        for _ in range(200):
            ack = persona.pick_ack(last, rng)
            self.assertIn(ack, persona.ACKS)
            self.assertNotEqual(ack, last)
            last = ack
        self.assertEqual(len({persona.pick_ack("", random.Random(n)) for n in range(40)}), 4)


class ScrubTests(unittest.TestCase):
    def test_addresses_links_and_ids(self) -> None:
        text = persona.scrub_for_speech(f"Write to {RECIPIENT}, see https://docs.example.com/d/abc?x=1. "
                                        "Thread 18f2a3b4c5d6e7f8a9 and event jts0001aa_0123456789 and www.example.org.")
        self.assertEqual(text, "Write to that address, see the link. Thread that item and event that item and the link.")

    def test_long_words_stay(self) -> None:
        self.assertEqual(persona.scrub_for_speech("Responsibilities and internationalization."),
                         "Responsibilities and internationalization.")

    def test_a_stray_at_sign_is_said(self) -> None:
        self.assertEqual(persona.scrub_for_speech("Ping @sam"), "Ping at sam")

    def test_spaced_and_spelled_addresses_go(self) -> None:
        self.assertEqual(persona.scrub_for_speech("Write to ana @ example.edu, sir."), "Write to that address, sir.")
        self.assertEqual(persona.scrub_for_speech("Write to ana @example.edu"), "Write to that address")
        self.assertEqual(persona.scrub_for_speech("Write to ana at example.edu."), "Write to that address.")
        self.assertEqual(persona.scrub_for_speech("Meet at 3.30 at the lab."), "Meet at 3.30 at the lab.")
        self.assertEqual(persona.scrub_for_speech("Lunch at noon."), "Lunch at noon.")

    def test_cut_at_a_sentence_within_the_limit(self) -> None:
        text = "First sentence here. " + "Second sentence is rather long and goes on. " * 20
        cut = persona.scrub_for_speech(text, limit=120)
        self.assertLessEqual(len(cut), 120)
        self.assertTrue(cut.endswith("."))
        self.assertTrue(text.startswith(cut))

    def test_cut_at_a_word_without_a_sentence_end(self) -> None:
        cut = persona.scrub_for_speech("word " * 200, limit=50)
        self.assertLessEqual(len(cut), 51)
        self.assertTrue(cut.endswith("word."))

    def test_whitespace_collapses_and_short_text_is_kept(self) -> None:
        self.assertEqual(persona.scrub_for_speech("  Good \n morning,\tsir.  "), "Good morning, sir.")
        self.assertEqual(persona.scrub_for_speech(""), "")


class ResearchSpeechTests(unittest.TestCase):
    """Web research's spoken answer: the answer without its [n] marks, then the sources and cards."""

    ANSWER = "The Example Museum opens at 10 AM on Saturday [1]. It closes at 6 PM [2]."

    def test_the_answer_then_the_tails(self) -> None:
        said = persona.research_reply_speech(self.ANSWER, sources=2, cards=1)
        self.assertEqual(said, persona.ReplySpeech(
            "The Example Museum opens at 10 AM on Saturday. It closes at 6 PM. The sources are on screen, sir. "
            "One proposal is waiting for your OK.", False))
        self.assertEqual(persona.research_reply_speech(self.ANSWER, sources=2, cards=0).text,
                         "The Example Museum opens at 10 AM on Saturday. It closes at 6 PM. The sources are on screen, sir.")
        self.assertEqual(persona.research_reply_speech("Open from 10 [1][2]", sources=0, cards=2).text,
                         "Open from 10. I couldn't find a source I could show you, sir. Two proposals are waiting for "
                         "your OK.")
        self.assertNotIn("[", persona.research_reply_speech("A [12] b [3].", sources=1, cards=0).text)
        self.assertIn("[123]", persona.research_reply_speech("Room [123].", sources=1, cards=0).text)

    def test_the_address_is_said_once(self) -> None:
        for address, expected in (("", "It opens at 10. The sources are on screen."),
                                  ("boss", "It opens at 10. The sources are on screen, boss.")):
            with self.subTest(address=address):
                self.assertEqual(persona.research_reply_speech("It opens at 10 [1].", sources=1, cards=0,
                                                               address=address).text, expected)
        empty = persona.research_reply_speech("", sources=2, cards=0, address="boss")
        self.assertEqual(empty.text, "I couldn't find an answer to that on the web, boss. The sources are on screen.")

    def test_nothing_found(self) -> None:
        self.assertEqual(persona.research_reply_speech("", sources=0, cards=0),
                         persona.ReplySpeech("I couldn't find an answer to that on the web, sir.", False))
        self.assertEqual(persona.research_reply_speech("  [1] ", sources=0, cards=0).text,
                         "I couldn't find an answer to that on the web, sir.")

    def test_first_person_claims_are_guarded_facts_are_not(self) -> None:
        for answer in ("I've booked your tickets [1].", "Done. The museum opens at 10 [1].", "I added it to your calendar.",
                       "All set: tickets are booked.", "I have now emailed the museum."):
            with self.subTest(answer=answer):
                said = persona.research_reply_speech(answer, sources=1, cards=2)
                self.assertTrue(said.guarded)
                self.assertEqual(said.text, "Here's what I found, sir. The details are on screen. Two proposals are "
                                            "waiting for your OK.")
        for answer in ("The match was cancelled [1].", "The museum has been moved to a new site [1].",
                       "The exhibition is now open daily [1].", "Tickets were sold out last week [2]."):
            with self.subTest(answer=answer):
                said = persona.research_reply_speech(answer, sources=1, cards=0)
                self.assertFalse(said.guarded)
                self.assertTrue(said.text.startswith(answer.replace(" [1]", "").replace(" [2]", "")))
        self.assertTrue(persona.claims_done("The match was cancelled."))   # the planner's guard is unchanged
        self.assertFalse(persona.claims_done("The match was cancelled.", first_person_only=True))
        self.assertTrue(persona.claims_done("I moved it.", first_person_only=True))
        guarded = persona.research_reply_speech("I've booked it.", sources=0, cards=0)
        self.assertEqual(guarded.text, "Here's what I found, sir. The details are on screen. I couldn't find a source "
                                       "I could show you.")

    def test_addresses_links_and_the_length(self) -> None:
        said = persona.research_reply_speech("Write to info@example.org or see https://www.example.org/visit [1].",
                                             sources=1, cards=0)
        self.assertEqual(said.text, "Write to that address or see the link. The sources are on screen, sir.")
        long = persona.research_reply_speech(("This is a long sentence about the museum [1]. " * 30).strip(), sources=3,
                                             cards=3)
        self.assertLessEqual(len(long.text), persona.SPEECH_LIMIT)
        self.assertTrue(long.text.endswith("The sources are on screen, sir. Three proposals are waiting for your OK."))
        self.assertTrue(long.text.startswith("This is a long sentence about the museum."))

    def test_failure_speech(self) -> None:
        self.assertEqual(persona.ask_failure_speech("research_cap"),
                         "Sorry, sir - that needed more searching than I allow. Nothing was proposed.")
        self.assertEqual(persona.ask_failure_speech("research_off", "boss"), "Boss, web research is turned off.")
        self.assertEqual(persona.ask_failure_speech("research_off", ""), "Web research is turned off.")


class MemorySpeechTests(unittest.TestCase):
    """What Jarvis says and shows about notes (memory): times and counts only, never the note."""

    def test_spoken_sentences(self) -> None:
        cases = ((persona.memory_saved_speech, "Noted, sir.", "Noted."),
                 (persona.memory_forgotten_speech, "Forgotten, sir.", "Forgotten."),
                 (persona.memory_refused_speech, "I'd rather not keep that, sir.", "I'd rather not keep that."))
        for builder, with_sir, plain in cases:
            with self.subTest(builder=builder.__name__):
                self.assertEqual(builder("sir"), with_sir)
                self.assertEqual(builder(""), plain)
                self.assertFalse(persona.claims_done(with_sir))
                self.assertTrue(with_sir.isascii())

    def test_entry_texts(self) -> None:
        self.assertEqual(persona.memory_saved_text("Ana is my project lead"), 'Noted, sir: "Ana is my project lead."')
        self.assertEqual(persona.memory_saved_text("Is Ana the lead?", ""), 'Noted: "Is Ana the lead?"')
        self.assertEqual(persona.memory_forgotten_text("Gym on Tuesdays."), 'Forgotten, sir: "Gym on Tuesdays."')
        self.assertEqual(persona.memory_suggestion_text("Ana Lima organizes the weekly Project sync"),
                         'Shall I remember this, sir? "Ana Lima organizes the weekly Project sync."')
        texts = {reason: persona.memory_refusal_text(reason, "sir", kept="Ana is my lead", cap=200, max_notes=100)
                 for reason in ("empty", "secret", "digits", "long", "full", "read_only", "duplicate")}
        self.assertEqual(texts["secret"], "I'd rather not keep passwords, codes or account numbers, sir; nothing "
                                          "was saved.")
        self.assertEqual(texts["empty"], texts["secret"])
        self.assertEqual(texts["digits"], texts["secret"])
        self.assertEqual(texts["long"], "That's longer than I keep (200 characters), sir; nothing was saved.")
        self.assertEqual(texts["full"], "Your memory is full (100 notes), sir - delete some under MEMORY first.")
        self.assertEqual(texts["read_only"], "memory.json can't be read, so I won't change it, sir.")
        self.assertEqual(texts["duplicate"], 'I already have that, sir: "Ana is my lead"')
        self.assertEqual(persona.memory_refusal_text("full", "", max_notes=50),
                         "Your memory is full (50 notes) - delete some under MEMORY first.")
        self.assertEqual(persona.SPOKEN_REFUSALS, ("empty", "secret", "digits"))
        for text in texts.values():
            self.assertFalse(persona.claims_done(text))


NL = chr(10)


class PlanSpeechTests(unittest.TestCase):
    """The day plan's words: spoken sentences built from counts and times only (never a title or the
    planner's say), every one checked against claims_done; the entry texts of spec 9.3."""

    def test_spoken_sentences(self) -> None:
        start = datetime(2026, 10, 7, 10, 30)
        cases = [
            (persona.plan_result_speech(4, start, "today"),
             "I've lined up four blocks for today, the first at 10:30 AM. Each one waits for your OK, sir."),
            (persona.plan_result_speech(1, start.replace(hour=15, minute=0), "tomorrow", ""),
             "I've lined up one block for tomorrow, at 3 PM. It waits for your OK."),
            (persona.plan_result_speech(0, None, "today"), "I couldn't fit a block into today, sir."),
            (persona.plan_question_speech(), "I have a question before I plan, sir; it's on screen."),
            (persona.plan_no_run_speech("day_full"), "Your day is full, sir."),
            (persona.plan_no_run_speech("nothing", "boss", "tomorrow"),
             "Nothing on your list needs time tomorrow, boss."),
            (persona.plan_no_run_speech("part_over"), ""),
        ]
        for said, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(said, expected)
                self.assertFalse(persona.claims_done(said))
                self.assertTrue(said.isascii())

    def test_no_run_texts(self) -> None:
        self.assertEqual(persona.plan_no_run_text("day_full", hint_tomorrow=True),
                         "Your day is full, sir - there's no free stretch of half an hour left. "
                         'Type "plan tomorrow" for tomorrow.')
        self.assertEqual(persona.plan_no_run_text("day_full", label="tomorrow", late=True),
                         "It's late for today, so I looked at tomorrow. Tomorrow is full, sir - there's "
                         "no free stretch of half an hour left.")              # no plan promised; "sir" once
        self.assertEqual(persona.plan_no_run_text("nothing", label="tomorrow", late=True),
                         "It's late for today, so I looked at tomorrow. Nothing on your list needs time tomorrow, "
                         'sir. Tell me what to fit in: "plan tomorrow, gym and reading".')
        self.assertEqual(persona.plan_no_run_text("nothing", "", label="this afternoon"),
                         'Nothing on your list needs time this afternoon. Tell me what to fit in: '
                         '"plan my afternoon, gym and reading".')
        self.assertEqual(persona.plan_no_run_text("part_over", part="morning"),
                         'Your morning is over, sir; type "plan my day" for the rest of it.')
        self.assertEqual(persona.plan_no_run_text("no_calendar"), persona.PLAN_NO_CALENDAR_TEXT)
        self.assertIn("day_start and day_end", persona.plan_no_run_text("no_hours", label="this evening"))
        for kind in persona.PLAN_NO_RUN_KINDS:
            self.assertFalse(persona.claims_done(persona.plan_no_run_text(kind)))

    def test_the_answer_entry(self) -> None:
        blocks = ["9:45-11:00 AM  Work on: Lab report", "3:30-4:30 PM  Read chapter 4"]
        self.assertEqual(persona.plan_answer_text(blocks, "Lab report first.", label="today").split(NL),
                         ["Here's a plan for today:", *blocks, "Lab report first.", persona.PLAN_ANSWER_TAIL])
        late = persona.plan_answer_text(blocks[:1], "", label="tomorrow", late=True).split(NL)
        self.assertEqual(late, ["It's late for today, sir - here's a plan for tomorrow:", blocks[0],
                                persona.PLAN_ANSWER_TAIL])                       # said once, not twice
        question = persona.plan_answer_text([], "Both need the same hour.", "Which comes first?").split(NL)
        self.assertEqual(question, ["Both need the same hour.", "Question: Which comes first?",
                                    'Answer with "plan my day, <your answer>".'])
        # R46: the hint plans the window that was asked about again.
        for label, typed in (("tomorrow", "plan tomorrow"), ("this afternoon", "plan my afternoon"),
                             ("this morning", "plan my morning"), ("this evening", "plan my evening"),
                             ("today", "plan my day")):
            with self.subTest(label=label):
                asked = persona.plan_answer_text([], "", "Which first?", label=label).split(NL)
                self.assertEqual(asked[-1], f'Answer with "{typed}, <your answer>".')
        late_question = persona.plan_answer_text([], "Both fit.", "Which first?", label="tomorrow", late=True)
        self.assertEqual(late_question.split(NL), ["It's late for today, so I looked at tomorrow.", "Both fit.",
                                                   "Question: Which first?",
                                                   'Answer with "plan tomorrow, <your answer>".'])
        refused = persona.plan_answer_text([], "I tried.", refused=2).split(NL)
        self.assertEqual(refused, ["I tried.", "No block fitted (see the cards)."])


class PrepSpeechTests(unittest.TestCase):
    """The meeting brief's words: spoken sentences carry the meeting's time only (never its title, a
    guest, a subject or the brief), every one checked against claims_done."""

    def test_spoken_sentences(self) -> None:
        two = datetime(2026, 10, 7, 14, 0)
        cases = [
            (persona.prep_result_speech(two), "Your brief for the 2 PM meeting is ready, sir."),
            (persona.prep_result_speech(two, in_jarvis_tab=False),
             "Your brief for the 2 PM meeting is in the Transcript, sir."),
            (persona.prep_result_speech(two.replace(hour=9, minute=30), "", day="tomorrow"),
             "Your brief for tomorrow's 9:30 AM meeting is ready."),
            (persona.prep_result_speech(None), "Your brief for the meeting is ready, sir."),
            (persona.prep_no_run_speech("no_match"), "I don't see a meeting like that today or tomorrow, sir."),
            (persona.prep_no_run_speech("gone"), "That meeting is no longer on your calendar, sir."),
            (persona.prep_no_run_speech("ended", "boss"), "That meeting has ended, boss."),
            (persona.prep_no_run_speech("choices", choices=2), "Which meeting, sir? The choices are on screen."),
            (persona.prep_no_run_speech("choices", choices=1), "Is this the meeting you mean, sir? It's on screen."),
        ]
        for said, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(said, expected)
                self.assertFalse(persona.claims_done(said))
                self.assertTrue(said.isascii())

    def test_entry_texts(self) -> None:
        two = datetime(2026, 10, 7, 14, 0)
        self.assertEqual(persona.prep_choice_text("Project sync", two, "work"), "Project sync at 2:00 PM (work)")
        self.assertEqual(persona.prep_choice_text("Project sync", two, "work", day="tomorrow", hour24=True),
                         "Project sync at 14:00 (work), tomorrow")
        self.assertEqual(persona.prep_choices_text(2), "Which meeting shall I prepare, sir? Nothing is read until you "
                                                       "pick one.")
        self.assertEqual(persona.prep_choices_text(1, ""), "Is this the meeting you mean? Nothing is read until you "
                                                           "click.")
        self.assertEqual(persona.prep_no_run_text("no_calendar"),
                         "I couldn't read your calendar just now, sir; nothing else was read.")
        self.assertEqual(persona.prep_no_run_text("no_calendar", connect=True),
                         "Meeting prep needs your calendar - click Connect under TODAY.")
        self.assertEqual(persona.prep_no_run_words("no_thread"), "no recent thread")
        self.assertEqual(persona.prep_no_run_words("odd_kind"), "odd kind")


class OfferSpeechTests(unittest.TestCase):
    """The offers' words (spec 4, 9.3, 11): one sentence from times and counts only, never a title,
    guest or anything read; every one checked against claims_done; the entries and sub lines."""

    def test_spoken_sentences(self) -> None:
        one, two = datetime(2026, 10, 7, 13, 0), datetime(2026, 10, 7, 14, 0)
        cases = [
            (persona.plan_offer_speech(), "Shall I plan your day, sir?"),
            (persona.plan_offer_speech(""), "Shall I plan your day?"),
            (persona.prep_offer_speech(two, 15), "Your meeting at 2 PM starts in about 15 minutes, sir. Shall I "
                                                 "prepare a brief?"),
            (persona.prep_offer_speech(two.replace(minute=30), 7, "boss"),
             "Your meeting at 2:30 PM starts in about seven minutes, boss. Shall I prepare a brief?"),
            (persona.prep_offer_speech(two, 1, ""), "Your meeting at 2 PM starts in about one minute. Shall I prepare "
                                                    "a brief?"),
            (persona.prep_offers_speech([one, two]),
             "You have meetings at 1 PM and 2 PM, sir. Shall I prepare briefs?"),
            (persona.prep_offers_speech([one, two, two.replace(hour=15, minute=30)], ""),
             "You have meetings at 1 PM, 2 PM and 3:30 PM. Shall I prepare briefs?"),
        ]
        for said, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(said, expected)
                self.assertFalse(persona.claims_done(said))
                self.assertTrue(said.isascii())

    def test_entry_texts_and_sub_lines(self) -> None:
        two = datetime(2026, 10, 7, 14, 0)
        dot = chr(0xB7)
        self.assertEqual(persona.plan_offer_text(), "Shall I plan your day, sir? I'd line up time blocks around your "
                         "meetings for your to-dos and deadlines. Nothing goes on your calendar until you approve "
                         "each block.")
        self.assertEqual(persona.prep_offer_text("Project sync", two, "work", 15, 3),
                         "Project sync at 2:00 PM (work) starts in 15 minutes, with 3 guests. Shall I prepare a "
                         "brief? I'd read your latest thread with each guest and send them to your Claude.")
        self.assertTrue(persona.prep_offer_text("", two, "", 1, 1, hour24=True).startswith(
            "(No title) at 14:00 starts in 1 minute, with 1 guest."))
        # Digits on screen even for ten minutes or less (number words are for speech only, spec 9.3 / 11).
        self.assertTrue(persona.prep_offer_text("Project sync", two, "work", 5, 3).startswith(
            "Project sync at 2:00 PM (work) starts in 5 minutes, with 3 guests."))
        self.assertIn("starts in 10 minutes,", persona.prep_offer_text("Project sync", two, "work", 10, 3))
        self.assertEqual(persona.plan_offer_expired_text(datetime(2026, 10, 7, 10, 14)),
                         'Offered at 10:14 AM - expired. Type "plan my day" any time.')
        self.assertEqual(persona.plan_offer_planned_text(datetime(2026, 10, 7, 10, 31)),
                         "You planned your day at 10:31 AM.")
        self.assertEqual(persona.prep_offer_ended_text("Project sync", two), "Project sync at 2:00 PM has ended.")
        self.assertEqual(persona.brief_ready_text(datetime(2026, 10, 7, 13, 47)), "Brief ready below (1:47 PM).")
        self.assertEqual(persona.offer_cost_sub(17, 20, 40), f"1 planner run {dot} 17 of 20 left today")
        self.assertEqual(persona.offer_cost_sub(17, 20, 12), f"1 planner run {dot} 12 Ask runs left today")
        self.assertEqual(persona.offer_cost_sub(17, 20, 40, prep=True, as_of="1:40 PM"),
                         f"1 planner run (none without recent mail) {dot} 17 of 20 left today {dot} as of 1:40 PM")
        self.assertEqual(persona.prod_hint_text(), "Prep offers and follow-ups work while I'm open or minimized, "
                                                   "sir - minimize instead of Done to keep them.")
        self.assertEqual(persona.DONE_PREP_TOOLTIP,
                         "Closes Jarvis - meeting prep offers stop until you open him again")


class FollowupSentenceTests(unittest.TestCase):
    def test_the_nudge_sentences_say_no_subject_and_claim_nothing(self) -> None:
        spoken = persona.nudge_result_speech("sir")
        self.assertEqual(spoken, "I've drafted a reply; it's waiting for your OK, sir.")
        self.assertFalse(persona.claims_done(spoken))
        self.assertEqual(persona.nudge_result_speech(""), "I've drafted a reply; it's waiting for your OK.")
        self.assertEqual(persona.nudge_drafted_text("Budget numbers", "work"),
                         'I\'ve drafted a reply in "Budget numbers" (work). It waits for your OK under NEEDS YOUR OK.')
        for kind, text in (("replied_since", "You've replied since, sir - nothing to draft."),
                           ("answered_since", "They've answered since, sir - see the thread."),
                           ("changed", "This thread changed since Jarvis listed it, sir.")):
            with self.subTest(kind=kind):
                self.assertEqual(persona.nudge_no_run_text(kind, "sir"), text)
                self.assertEqual(persona.nudge_no_run_text(kind, "sir", listed=True), text + " I'm updating the list.")
                self.assertFalse(persona.claims_done(text))
        self.assertEqual(persona.owes_answer_text("Ana Lima", "Budget numbers", "Oct 8"),
                         'Ana Lima owes you an answer in "Budget numbers" (Oct 8).')

    def test_the_typed_followups_texts(self) -> None:
        at = datetime(2026, 10, 10, 10, 41)
        self.assertEqual(persona.followups_text(3, 1, at, "sir"),
                         "3 threads wait on your answer and 1 on someone else's (checked 10:41 AM).")
        self.assertEqual(persona.followups_text(1, 0, at, "sir"),
                         "1 thread waits on your answer and 0 on someone else's (checked 10:41 AM).")
        self.assertEqual(persona.followups_text(0, 0, at, "sir"),
                         "Nothing waits on you or on anyone else, sir (checked 10:41 AM).")
        self.assertEqual(persona.followups_text(0, 0, at, "sir", partial=True),
                         "Nothing waits on you or on anyone else, sir (checked 10:41 AM) (partial - Gmail asked me "
                         "to slow down).")
        self.assertEqual(persona.followups_scanning_text("sir"),
                         "I'm checking your mail headers now, sir - FOLLOW-UPS fills in shortly.")
        self.assertEqual(persona.followups_confirm_text("work"), 'Confirm which Google account "work" is first.')


if __name__ == "__main__":
    unittest.main()
