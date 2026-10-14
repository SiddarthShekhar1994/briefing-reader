"""The follow-up draft (spec 3.4, 7.4, 8.3) and the follow-up parts of meeting prep (3.3 step 6b and 11):
the nudge stdin, one full read, the guard (no run when the thread changed, R47), one run, the one Reply
card checked (that thread, that message, its people, never a note, R7), ISO-1 with follow-up
sentinels; prep's participants index never bypasses guest_allowed, never crosses accounts by
default and gives one thread per guest (R2, R3); a brief's "owes you an answer" (R32); the plan's
two counts. FakeRunner only; invented data."""

from __future__ import annotations

import logging
import unittest
from datetime import timedelta

from briefing_reader import live
from briefing_reader.actions import REPLY, parse_action_line
from briefing_reader.ask import dayplan, modes, planner
from briefing_reader.ask.context import AccountInfo
from briefing_reader.ask.mail import thread_from_api
from briefing_reader.ask.modes import (
    FIXED_COMMANDS,
    MODE_NUDGE,
    ORIGIN_BRIEF,
    ORIGIN_LIST,
    ModeInputs,
    ModeRequest,
    ThreadRef,
    ThreadTarget,
    TodoFact,
)
from tests.ask_fakes import NOW, FakeReader, gmail_message, gmail_thread, plan_stream, stream
from tests.test_ask_planner import (
    ANA_QUERY,
    BEN_QUERY,
    FORCED_COMMAND,
    PREP_CONFIG,
    PlannerTestCase,
    budget_thread,
    prep_meeting,
    prep_request,
)

OWN = frozenset({"you@example.edu", "you@example.com"})
AT = 1791200000000                      # Mon Oct 5 2026 (NOW is Wed Oct 7, 9:30 AM PDT)
REPLY_LINE = ("Reply: acct=work | thread=thr0000777 | msgid=<CAB7@mail.example.edu> | gmid= | to=ana@example.edu | "
              "cc= | subject=Re: Budget review | replied=no | due= | link= | body=Hi Ana,\\nConfirmed.\\nThanks")
SAY = "I drafted a short answer about the Q4 budget."


def waiting_target(**changes) -> ThreadTarget:  # type: ignore[no-untyped-def]
    values = dict(alias="work", thread_id="thr0000777", kind="waiting", last_gmail_id="msg0000777")
    values.update(changes)
    return ThreadTarget(**values)


def nudge_request(target: ThreadTarget | None = None, *, origin: str = ORIGIN_LIST, typed: str = "") -> ModeRequest:
    return ModeRequest(MODE_NUDGE, origin, typed=typed, thread=target or waiting_target())


def owed_thread(*more: dict, thread_id: str = "thr0000888") -> dict:
    mine = gmail_message("msg0000881", sender="You <you@example.edu>", to="Ana Lima <ana@example.edu>",
                         cc="ben@example.org", subject="Lab budget", body="Here are the numbers.",
                         msgid="<CAB8@mail.example.edu>", at_ms=AT, labels=("SENT",))
    return gmail_thread(thread_id, [mine, *more])


class NudgeRunTests(PlannerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_config(PREP_CONFIG)
        self.reader = FakeReader("work", {"thr0000777": budget_thread(), "thr0000888": owed_thread()})
        self.sources.readers = {"work": self.reader}

    def nudge(self, ask, request=None):  # type: ignore[no-untyped-def]
        self.stream = live.LiveStream()
        task = self.stream.task(live.TASK_NUDGE, "Draft a reply")
        stages: list[str] = []
        outcome = ask.plan_mode(request or nudge_request(), live=task, on_stage=stages.append)
        return outcome, stages

    def test_a_draft_end_to_end(self) -> None:
        ask = self.make(plan_stream({"say": SAY, "question": "", "lines": [REPLY_LINE]}))
        ask.memory = lambda: ("I prefer mornings",)
        outcome, stages = self.nudge(ask)
        self.assertTrue(outcome.ok, outcome)
        self.assertEqual((outcome.mode, outcome.runs, outcome.note_kind), (MODE_NUDGE, 1, ""))
        self.assertEqual(stages, [planner.STAGE_THREAD, planner.STAGE_PLANNING])
        self.assertEqual((self.reader.reads, self.reader.searches), (["thr0000777"], []))   # one full read
        card, = outcome.cards
        self.assertEqual((card.kind, card.mode, card.account, card.error), (REPLY, MODE_NUDGE, "work", ""))
        self.assertEqual(card.unverified, frozenset({"ana@example.edu"}))   # a fixed command: never typed
        self.assertEqual(outcome.target, waiting_target())
        stdin = self.stdin()
        self.assertTrue(stdin.startswith("<now>\nWednesday 2026-10-07 09:30 (time zone America/Los_Angeles)\n</now>\n"
                                         "<owner>sir</owner>\n<accounts>\nwork | address=you@example.edu | "
                                         "calendar=yes | send_mail=yes | read_mail=yes | "
                                         "time_zone=America/Los_Angeles\n</accounts>\n<followup>kind=waiting on you | "
                                         "last from=them | days=2 | acct=work | thread=thr0000777</followup>\n"
                                         "<mail>\n"), stdin)
        self.assertIn("thread | acct=work | thread=thr0000777 | subject=Budget review\nmessage | gmid=msg0000777 | "
                      "msgid=CAB7@mail.example.edu | date=", stdin)
        self.assertIn("  Can you confirm the Q4 budget by Friday?\nend of thread\n</mail>\n<memory>\n", stdin)
        self.assertTrue(stdin.endswith(f"<command>\n{FIXED_COMMANDS[(MODE_NUDGE, 'waiting')]}\n</command>\n"))
        self.assertEqual(stdin.count("<command>"), 1)
        self.assertNotIn("personal", stdin.split("</accounts>")[0])          # the thread's account only
        started = self.runner.started[0]
        self.assertEqual(started.argv[started.argv.index("--system-prompt-file") + 1],
                         str(planner.cli.NUDGE_PROMPT_FILE))
        schema = self.schema()
        self.assertEqual(schema["title"], "JarvisNudge")
        self.assertNotIn("remember", schema["properties"])                  # a run with mail never suggests
        self.assertEqual(self.usage.counts(NOW, kind="nudge"), (1, 1))     # a bundle run
        self.assertIn("mode nudge", outcome.summary())
        self.assertNotIn("Budget", outcome.summary())
        kinds = [step.kind for step in self.stream.snapshot()[0].steps]
        self.assertEqual(kinds, [live.ASK_CHECKS, live.MAIL_THREAD, live.PLANNER_RUN, live.ASK_VALIDATE])

    def test_an_owed_nudge_answers_the_owners_last_message(self) -> None:
        line = ("Reply: acct=work | thread=thr0000888 | msgid=<CAB8@mail.example.edu> | gmid= | to=ana@example.edu | "
                "cc=ben@example.org | subject=Re: Lab budget | replied=no | due= | link= | "
                "body=Hi Ana,\\nJust checking "
                "in on the numbers.\\nThanks")
        ask = self.make(plan_stream({"say": SAY, "question": "", "lines": [line]}))
        target = ThreadTarget("work", "thr0000888", "owed", "msg0000881")
        outcome, _stages = self.nudge(ask, nudge_request(target))
        card, = outcome.cards
        self.assertEqual(card.error, "")
        self.assertIn("<followup>kind=owed to you | last from=owner | days=2 | acct=work | "
                      "thread=thr0000888</followup>", self.stdin())
        self.assertTrue(self.stdin().endswith(f"{FIXED_COMMANDS[(MODE_NUDGE, 'owed')]}\n</command>\n"))

    def test_the_guard_stops_with_no_run(self) -> None:
        mine = gmail_message("msg0000778", sender="you@example.edu", to="ana@example.edu", subject="Re: Budget review",
                             body="Confirmed.", msgid="<CAB9@mail.example.edu>", at_ms=AT + 3600_000, labels=("SENT",))
        theirs = gmail_message("msg0000882", sender="Ana Lima <ana@example.edu>", to="you@example.edu",
                               subject="Re: Lab budget", body="Got them.", msgid="<CAB10@mail.example.edu>",
                               at_ms=AT + 3600_000)
        self.reader.threads["thr0000777"] = gmail_thread("thr0000777", [budget_thread()["messages"][0], mine])
        self.reader.threads["thr0000888"] = owed_thread(theirs)
        cases = ((nudge_request(), "replied_since", "You've replied since, sir - nothing to draft. I'm updating the "
                                                    "list."),
                 (nudge_request(ThreadTarget("work", "thr0000888", "owed", "msg0000881")), "answered_since",
                  "They've answered since, sir - see the thread. I'm updating the list."),
                 (nudge_request(waiting_target(last_gmail_id="msg0000999"), origin=ORIGIN_BRIEF), "changed",
                  "This thread changed since Jarvis listed it, sir."))
        for request, kind, text in cases:
            with self.subTest(kind=kind):
                ask = self.make()
                with self.assertLogs("briefing_reader", level="INFO") as logs:
                    outcome, _stages = self.nudge(ask, request)
                self.assertEqual((outcome.ok, outcome.note_kind, outcome.message, outcome.runs), (True, kind, text, 0))
                self.assertEqual(self.runner.started, [])
                self.assertIn(f"Nudge: no run ({kind})", "\n".join(logs.output))

    def test_refusals_read_nothing(self) -> None:
        ask = self.make()
        self.sources.readers = {}
        outcome, _stages = self.nudge(ask)
        self.assertEqual((outcome.note_kind, outcome.runs), ("unreadable", 0))
        self.sources.readers = {"work": self.reader}
        self.reader.threads.pop("thr0000777")
        outcome, _stages = self.nudge(self.make())
        self.assertEqual((outcome.note_kind, outcome.message), ("gone", "That thread is gone from your mail, sir."))
        self.write_config(PREP_CONFIG.replace("max_per_hour = 20", "max_per_hour = 1"))
        ask = self.make()
        self.usage.finish(self.usage.start(), outcome="ok", duration_ms=1000)   # this hour's one run is used
        outcome, _stages = self.nudge(ask)
        self.assertEqual((outcome.ok, outcome.kind), (False, planner.CAPS))
        self.assertEqual(self.reader.reads, ["thr0000777"])   # only the second case read (and found nothing)

    def test_a_draft_that_repeats_a_note_is_held_back(self) -> None:
        note = "Ana Lima is my project lead for the Q4 budget"
        line = REPLY_LINE.replace("Confirmed.", "Ana Lima is my project lead for the Q4 budget, so confirmed.")
        ask = self.make(plan_stream({"say": SAY, "question": "", "lines": [line]}))
        ask.memory = lambda: (note,)
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            outcome, _stages = self.nudge(ask)
        self.assertEqual(outcome.cards[0].error, modes.NUDGE_NOTE_LEAK)
        self.assertIn("Nudge: a draft held back (note)", "\n".join(logs.output))
        ask = self.make(plan_stream({"say": SAY, "question": "", "lines": [line]}))
        ask.memory = lambda: (note,)
        words = "say that Ana Lima is my project lead for the Q4 budget"
        outcome, _stages = self.nudge(ask, nudge_request(typed=words))
        self.assertEqual(outcome.cards[0].error, "")             # the owner's words asked for it

    def test_iso_1_follow_up_data_never_reaches_web_research(self) -> None:
        thread = gmail_thread("thr0000777", [gmail_message(
            "msg0000777", sender="Zq7 Sender <zq7sender@example.org>", to="you@example.edu", subject="Zq7subj numbers",
            body="Zq7body", msgid="<CAB7@mail.example.edu>", at_ms=AT)])
        self.reader.threads["thr0000777"] = thread
        line = REPLY_LINE.replace("to=ana@example.edu", "to=zq7sender@example.org")
        ask = self.make(plan_stream({"say": SAY, "question": "", "lines": [line]}), stream("research_success"))
        outcome, _stages = self.nudge(ask)
        self.assertTrue(outcome.ok, outcome)
        self.assertTrue(ask.plan(FORCED_COMMAND).ok)
        research = self.runner.started[-1]
        for marker in ("zq7", "<followup>", "thr0000777"):
            self.assertNotIn(marker, research.stdin.casefold())
            self.assertNotIn(marker, " ".join(research.argv).casefold())
            self.assertNotIn(marker, " ".join(f"{k}={v}" for k, v in research.env.items()).casefold())


class GuardAndTargetTests(unittest.TestCase):
    def thread(self, *more: dict):  # type: ignore[no-untyped-def]
        return thread_from_api(gmail_thread("thr0000777", [budget_thread()["messages"][0], *more]), "work")

    def test_auto_ids_and_automated_mail_never_change_a_thread(self) -> None:
        ooo = gmail_message("msg0000779", sender="Ana Lima <ana@example.edu>", to="you@example.edu", subject="Away",
                            body="Away", msgid="<CAB11@mail.example.edu>", at_ms=AT + 7200_000)
        ooo["payload"]["headers"].append({"name": "Auto-Submitted", "value": "auto-replied"})
        mine = gmail_message("msg0000780", sender="you@example.edu", to="ana@example.edu", subject="Accepted: Sync",
                             body="", msgid="<CAB12@mail.example.edu>", at_ms=AT + 7300_000, labels=("SENT",))
        self.assertEqual(modes.nudge_guard(self.thread(ooo, mine), waiting_target(), OWN), "")
        listed = waiting_target(auto_ids=frozenset({"msg0000781"}))
        theirs = gmail_message("msg0000781", sender="ana@example.edu", to="you@example.edu", subject="More",
                               body="More", msgid="<CAB13@mail.example.edu>", at_ms=AT + 7400_000)
        self.assertEqual(modes.nudge_guard(self.thread(theirs), listed, OWN), "")
        self.assertEqual(modes.nudge_guard(self.thread(theirs), waiting_target(), OWN), modes.GUARD_CHANGED)

    def test_owed_target_from_jarvis_s_own_headers(self) -> None:
        owed = thread_from_api(owed_thread(), "work")
        target = modes.owed_target(owed, "work", OWN)
        self.assertEqual((target.kind, target.last_gmail_id, target.thread_id), ("owed", "msg0000881", "thr0000888"))
        self.assertIsNone(modes.owed_target(self.thread(), "work", OWN))   # they wrote last
        rsvp = gmail_message("msg0000882", sender="you@example.edu", to="ana@example.edu", subject="Accepted: Sync",
                             body="", msgid="<CAB14@mail.example.edu>", at_ms=AT + 60_000, labels=("SENT",))
        self.assertIsNone(modes.owed_target(self.thread(rsvp), "work", OWN))   # an RSVP never decides (R17)

    def test_check_nudge(self) -> None:
        thread = self.thread()
        target = waiting_target()

        def card(line: str):  # type: ignore[no-untyped-def]
            return parse_action_line(line)

        check = modes.NudgeCheck(target, thread, ("Ana Lima is my project lead",), own=OWN)
        self.assertEqual(check(card(REPLY_LINE)), "")
        self.assertEqual(check(card(REPLY_LINE.replace("thread=thr0000777", "thread=thr0000999"))),
                         modes.NUDGE_OTHER_THREAD)
        self.assertEqual(check(card(REPLY_LINE.replace("acct=work", "acct=personal"))), modes.NUDGE_OTHER_THREAD)
        self.assertEqual(check(card(REPLY_LINE.replace("<CAB7@", "<CAB9@"))), modes.NUDGE_WRONG_MESSAGE)
        self.assertEqual(check(card(REPLY_LINE.replace("cc= |", "cc=eve@example.org |"))), modes.NUDGE_EXTRA_PEOPLE)
        self.assertEqual(check(card(REPLY_LINE.replace("cc= |", "cc=m@evil.example |"))), "")   # in that message
        self.assertEqual(check(card(REPLY_LINE.replace("to=ana@example.edu", "to=ana@example.edu, you@example.edu"))),
                         modes.NUDGE_OWN_ADDRESS)
        leak = card(REPLY_LINE.replace("Confirmed.", "Ana Lima is my project lead."))
        self.assertEqual(check(leak), modes.NUDGE_NOTE_LEAK)
        allowed = modes.NudgeCheck(target, thread, ("Ana Lima is my project lead",), own=OWN,
                                   owner_words="tell her Ana Lima is my project lead")
        self.assertEqual(allowed(leak), "")

    def test_typed_words_make_an_address_typed(self) -> None:
        account = AccountInfo("work", "you@example.edu", "yes", True, "yes", "America/Los_Angeles")
        request = nudge_request(typed="ask her to loop in ben@example.org")
        text, index = modes.build_nudge_prompt(NOW, "sir", account, self.thread(), waiting_target(), (), request,
                                               zone="America/Los_Angeles")
        self.assertIn("The owner's words: ask her to loop in ben@example.org\n</command>", text)
        self.assertTrue(index.typed("ben@example.org"))
        self.assertFalse(index.typed("ana@example.edu"))
        self.assertTrue(index.knows("ana@example.edu"))
        self.assertNotIn("<memory>", text)
        self.assertEqual(repr(waiting_target()), "ThreadTarget(work, waiting)")


class PrepIndexTests(PlannerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_config(PREP_CONFIG)
        self.reader = FakeReader("work", {"thr0000777": budget_thread(), "thr0000888": owed_thread(),
                                          "thr0000999": budget_thread()},
                                 found_for={"ben@example.org": ["thr0000999"]})
        self.sources.readers = {"work": self.reader}
        self.sources.briefs = {"work": [prep_meeting()]}

    def prep(self, ask, participants):  # type: ignore[no-untyped-def]
        self.stream = live.LiveStream()
        task = self.stream.task(live.TASK_PREP, "prep me")
        return ask.plan_mode(prep_request("my 2 PM"), inputs=ModeInputs(participants=participants), live=task)

    def ref(self, alias: str, thread_id: str, days: int = 1) -> ThreadRef:
        return ThreadRef(alias, thread_id, NOW - timedelta(days=days))

    def test_the_index_comes_before_a_search_and_one_thread_per_guest(self) -> None:
        participants = {"ana@example.edu": (self.ref("work", "thr0000777"), self.ref("work", "thr0000888", 2),
                                            self.ref("work", "thr0000999", 3))}
        ask = self.make(stream("prep_ok"))
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            outcome = self.prep(ask, participants)
        self.assertTrue(outcome.ok, outcome)
        self.assertNotIn(ANA_QUERY, self.reader.searches)        # Ana from the header list: no search
        self.assertIn(BEN_QUERY, self.reader.searches)           # the next guest still gets a lookup (R3)
        self.assertEqual(self.reader.reads[:1], ["thr0000777"])  # only Ana's newest of her three threads
        self.assertNotIn("thr0000888", self.reader.reads)
        self.assertIn("1 index hit(s)", "\n".join(logs.output))
        people = next(step for step in self.stream.snapshot()[0].steps if step.kind == live.PREP_PEOPLE)
        self.assertTrue(any("from Jarvis's header list" in item.note for item in people.items))

    def test_another_accounts_thread_is_never_read_by_default(self) -> None:
        participants = {"ana@example.edu": (self.ref("personal", "thr0000888"),)}
        ask = self.make(stream("prep_ok"))
        outcome = self.prep(ask, participants)
        self.assertTrue(outcome.ok, outcome)
        self.assertNotIn("thr0000888", self.reader.reads)
        self.assertIn(ANA_QUERY, self.reader.searches)           # searched on the meeting's account instead

    def test_a_security_guest_in_the_index_is_never_read(self) -> None:
        from briefing_reader.gcal import Guest

        guests = (Guest("alerts@google.com", "Security", "accepted"),)
        self.sources.briefs = {"work": [prep_meeting(guests=guests, organizer=("", ""))]}
        participants = {"alerts@google.com": (self.ref("work", "thr0000777"),)}
        ask = self.make()
        outcome = self.prep(ask, participants)
        self.assertTrue(outcome.ok, outcome)
        self.assertEqual((self.reader.reads, self.reader.searches, self.runner.started), ([], [], []))
        self.assertEqual(outcome.note_kind, "no_thread")

    def test_a_brief_offers_a_nudge_only_when_jarvis_s_headers_agree(self) -> None:
        participants = {"ana@example.edu": (self.ref("work", "thr0000888"),)}
        answer = {"say": "Ana has not answered.", "points": [], "open": [], "lines": [],
                  "threads": [{"thread": "thr0000888", "summary": "You sent Ana the numbers.", "waiting_on": "them"}]}
        ask = self.make(plan_stream(answer))
        outcome = self.prep(ask, participants)
        self.assertTrue(outcome.ok, outcome)
        (who, subject, when, target), = outcome.brief.nudges
        self.assertEqual((who, subject, when), ("Ana Lima", "Lab budget", "Oct 5"))
        self.assertEqual((target.kind, target.last_gmail_id, target.alias), ("owed", "msg0000881", "work"))
        self.assertNotIn("Ana", repr(outcome.brief))
        # The planner says "them" but Jarvis's headers show Ana wrote last: no nudge entry.
        self.reader.threads["thr0000888"] = owed_thread(gmail_message(
            "msg0000883", sender="ana@example.edu", to="you@example.edu", subject="Re: Lab budget", body="Thanks",
            msgid="<CAB15@mail.example.edu>", at_ms=AT + 60_000))
        outcome = self.prep(self.make(plan_stream(answer)), participants)
        self.assertEqual(outcome.brief.nudges, ())


    def test_a_nudge_from_another_account_s_thread_keeps_that_account(self) -> None:
        """R32 with prep_other_accounts (R3): the thread read on "personal" is nudged on "personal"."""
        self.write_config(PREP_CONFIG + "prep_other_accounts = true\n")
        personal = FakeReader("personal", {"thr0000888": owed_thread()})
        self.sources.readers["personal"] = personal
        self.sources.accounts_list[0] = AccountInfo("personal", "you@example.com", "yes", True, "yes",
                                                    "America/Los_Angeles")
        participants = {"ana@example.edu": (self.ref("personal", "thr0000888"),)}
        answer = {"say": "Ana has not answered.", "points": [], "open": [], "lines": [],
                  "threads": [{"thread": "thr0000888", "summary": "You sent Ana the numbers.", "waiting_on": "them"}]}
        outcome = self.prep(self.make(plan_stream(answer)), participants)
        self.assertTrue(outcome.ok, outcome)
        self.assertEqual(personal.reads, ["thr0000888"])
        self.assertNotIn("thr0000888", self.reader.reads)
        (_who, _subject, _when, target), = outcome.brief.nudges
        self.assertEqual((target.alias, target.thread_id, target.kind), ("personal", "thr0000888", "owed"))

    def test_an_index_thread_older_than_the_prep_lookback_is_searched_instead(self) -> None:
        """R4: the header list covers scan_days (14 here); prep_lookback_days 3 never reads a thread whose
        newest message is 10 days old from it."""
        self.write_config(PREP_CONFIG + "prep_lookback_days = 3\n")
        participants = {"ana@example.edu": (self.ref("work", "thr0000888", 10),)}
        outcome = self.prep(self.make(stream("prep_ok")), participants)
        self.assertTrue(outcome.ok, outcome)
        self.assertNotIn("thr0000888", self.reader.reads)
        self.assertIn(ANA_QUERY.replace("newer_than:14d", "newer_than:3d"), self.reader.searches)
        within = {"ana@example.edu": (self.ref("work", "thr0000888", 2),)}
        self.reader.reads.clear()
        self.reader.searches.clear()
        self.assertTrue(self.prep(self.make(stream("prep_ok")), within).ok)
        self.assertEqual(self.reader.reads[:1], ["thr0000888"])        # inside the lookback: the index hit
        self.assertFalse(any("ana@example.edu" in query for query in self.reader.searches))


class PlanCountsTests(unittest.TestCase):
    def test_the_plan_gets_two_counts_only(self) -> None:
        inputs = ModeInputs(todos=(TodoFact("t1", "a1", "Read chapter 4", "2026-10-11", "ask"),),
                            followup_counts=(4, 2))
        rows = dict(dayplan.todo_rows(inputs))
        self.assertEqual(rows["e1"], "e1 | emails | 4 wait on your answer | 2 owed to you")
        self.assertEqual(dayplan.todo_rows(ModeInputs())[:1], ())
        self.assertFalse(any(row_id.startswith("e") for row_id, _text in dayplan.todo_rows(ModeInputs(todos=(
            TodoFact("t1", "a1", "Read", "2026-10-11", "ask"),)))))


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
