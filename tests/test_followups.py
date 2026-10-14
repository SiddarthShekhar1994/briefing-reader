"""The follow-up list (spec 3.4, 5.3-5.6): headers read into facts (the 14-day cut before anything is
kept, R9), the deciding message (owner RSVPs and auto-replies never decide, R17), WAITING / OWED and
every reason a thread is not listed (group sends, R48), the participants index, the scanner (SENT
first, R14; the cache by historyId and account identity; partial on a rate limit or any other read
error; a refusal stops the account until its next sign-in, R19; cancel; one thread again, R47) and
followups.json (hide keys and times only). Invented data only; no Google."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from briefing_reader import followups
from briefing_reader.config import ProductivityConfig
from briefing_reader.followups import (
    OWED,
    WAITING,
    FollowupItem,
    HiddenStore,
    MailScanner,
    ThreadFacts,
    classify,
    deciding_message,
    facts_from_metadata,
    hide_key,
    make_view,
    participants_index,
)
from briefing_reader.gmail import MailReadError
from briefing_reader.google_auth import PROBLEM_EXPIRED, PROBLEM_FAILED
from tests.ask_fakes import FakeScanReader, gmail_meta_message, gmail_meta_thread

UTC = timezone.utc
NOW = datetime(2026, 10, 10, 9, 30, tzinfo=UTC)
ME = "me@example.edu"
OWN = frozenset({ME})
SETTINGS = ProductivityConfig(enabled=True)


def ago(*, days: float = 0, hours: float = 0) -> datetime:
    return NOW - timedelta(days=days, hours=hours)


def msg(gmail_id: str, sender: str, to: str, at: datetime, *, labels=("INBOX",), subject: str = "Budget numbers",
        cc: str = "", headers=None) -> dict:
    return gmail_meta_message(gmail_id, sender=sender, to=to, subject=subject, at=at, labels=labels, cc=cc,
                              headers=headers)


def facts(*messages: dict, thread_id: str = "thr0000001", alias: str = "work") -> ThreadFacts:
    found = facts_from_metadata(gmail_meta_thread(thread_id, messages), alias, now=NOW, days=14)
    assert found is not None
    return found


def kind_of(found, *, own=OWN, known=lambda _a: False, own_domains=()) -> str:
    result = classify(found, own=own, own_domains=own_domains, now=NOW, settings=SETTINGS, known=known)
    return result.kind if isinstance(result, FollowupItem) else result


THEM = "Ana Lima <ana@example.org>"


class FactsTests(unittest.TestCase):
    def test_metadata_becomes_facts_and_old_messages_never_get_in(self) -> None:
        old = msg("m0000001", THEM, ME, ago(days=60), subject="Zq7old subject")
        new = msg("m0000002", THEM, ME, ago(days=2), subject="Budget numbers",
                  headers={"Auto-Submitted": "auto-replied", "List-Id": "<lab.example.org>"})
        found = facts(new, old)
        self.assertEqual([message.gmail_id for message in found.messages], ["m0000002"])
        message = found.messages[0]
        self.assertTrue(message.auto and message.listy)
        self.assertEqual((message.sender.email, message.sender.name), ("ana@example.org", "Ana Lima"))
        self.assertEqual(found.subject, "Budget numbers")
        self.assertNotIn("Zq7old", repr(found) + repr(message))
        self.assertNotIn("Budget", repr(found) + repr(message))
        self.assertIsNone(facts_from_metadata({"id": "bad id!"}, "work", now=NOW, days=14))

    def test_a_sensitive_subject_is_never_kept(self) -> None:
        found = facts(msg("m0000001", THEM, ME, ago(days=2), subject="Your verification code is 1234"))
        self.assertTrue(found.sensitive)
        self.assertEqual((found.subject, found.messages[0].subject), ("", ""))
        self.assertEqual(kind_of(found), followups.SENSITIVE)
        listed = facts(msg("m0000002", THEM, ME, ago(days=2), subject="Code review for lab 3"))
        self.assertEqual(kind_of(listed), WAITING)   # the narrow rule: "code review" is listed

    def test_a_60_day_old_message_never_decides_or_counts(self) -> None:
        # The owner's old message is out of the window: only Ana's new one is classified (R9).
        found = facts(msg("m0000001", ME, "ana@example.org", ago(days=60), labels=("SENT",)),
                      msg("m0000002", THEM, ME, ago(days=2)))
        self.assertEqual(kind_of(found), WAITING)
        only_old = facts(msg("m0000001", THEM, ME, ago(days=40)))
        self.assertEqual(kind_of(only_old), followups.TOO_OLD)
        index = participants_index([only_old], OWN)
        self.assertEqual(index, {})


class ClassifyTests(unittest.TestCase):
    def test_waiting_on_you(self) -> None:
        found = facts(msg("m0000001", ME, "ana@example.org", ago(days=4), labels=("SENT",)),
                      msg("m0000002", THEM, ME, ago(days=3)))
        item = classify(found, own=OWN, now=NOW, settings=SETTINGS)
        self.assertIsInstance(item, FollowupItem)
        self.assertEqual((item.kind, item.who, item.who_address, item.last_gmail_id, item.alias),
                         (WAITING, "Ana Lima", "ana@example.org", "m0000002", "work"))
        self.assertEqual(item.reason, "They wrote last, to you directly, 3 days ago")
        self.assertEqual(item.key, hide_key("work", "thr0000001", "m0000002"))
        self.assertNotIn("Ana", repr(item))
        self.assertNotIn("Budget", repr(item))

    def test_owed_to_you_and_auto_replies_never_decide(self) -> None:
        mine = msg("m0000001", ME, "Ana Lima <ana@example.org>", ago(days=5), labels=("SENT",))
        ooo = msg("m0000002", THEM, ME, ago(days=4), headers={"Auto-Submitted": "auto-replied"})
        item = classify(facts(mine, ooo), own=OWN, now=NOW, settings=SETTINGS)
        self.assertEqual((item.kind, item.who, item.last_gmail_id, item.auto_ids),
                         (OWED, "Ana Lima", "m0000001", frozenset({"m0000002"})))
        self.assertEqual(item.reason, "You wrote last, 5 days ago, and nobody answered")

    def test_the_owner_by_sent_label_or_mailbox(self) -> None:
        # No SENT label: the sender is one of the owner's mailboxes (+tag, dots on gmail.com, a send-as alias).
        for sender, own in (("me+lists@example.edu", OWN), ("m.e.owner@gmail.com", frozenset({"meowner@gmail.com"})),
                            ("lab@example.org", frozenset({ME, "lab@example.org"}))):
            with self.subTest(sender=sender):
                found = facts(msg("m0000001", sender, "ana@example.org", ago(days=5), labels=("INBOX",)))
                self.assertEqual(kind_of(found, own=own), OWED)

    def test_owner_rsvps_and_auto_replies_are_never_the_deciding_message(self) -> None:
        theirs = msg("m0000001", THEM, ME, ago(days=3))
        rsvp = msg("m0000002", ME, "ana@example.org", ago(days=2), labels=("SENT",), subject="Accepted: Project sync")
        ooo = msg("m0000003", ME, "ana@example.org", ago(days=1), labels=("SENT",),
                  headers={"Auto-Submitted": "auto-replied"})
        deciding, auto = deciding_message(facts(theirs, rsvp, ooo).messages, OWN, lambda _a: False)
        self.assertEqual((deciding.gmail_id, auto), ("m0000001", frozenset({"m0000002", "m0000003"})))
        self.assertEqual(kind_of(facts(theirs, rsvp, ooo)), WAITING)
        # An RSVP never makes a thread OWED to the organizer.
        invite = msg("m0000004", "Ana Lima <ana@example.org>", ME, ago(days=6), subject="Invitation: Project sync")
        self.assertEqual(kind_of(facts(invite, rsvp)), followups.AUTOMATED)

    def test_every_automated_signal(self) -> None:
        cases = {"list-id": dict(headers={"List-Id": "<news.example.org>"}),
                 "unsubscribe": dict(headers={"List-Unsubscribe": "<mailto:u@example.org>"}),
                 "bulk": dict(headers={"Precedence": "bulk"}),
                 "auto": dict(headers={"Auto-Submitted": "auto-generated"}),
                 "invitation": dict(subject="Updated invitation with note: Sync"),
                 "cancelled": dict(subject="Cancelled event: Sync"),
                 "promotions": dict(labels=("INBOX", "CATEGORY_PROMOTIONS")),
                 "social": dict(labels=("INBOX", "CATEGORY_SOCIAL")),
                 "forums": dict(labels=("INBOX", "CATEGORY_FORUMS"))}
        for name, extra in cases.items():
            with self.subTest(name=name):
                self.assertEqual(kind_of(facts(msg("m0000001", THEM, ME, ago(days=3), **extra))),
                                 followups.AUTOMATED)
        for sender in ("noreply@example.org", "No-Reply+x@example.org", "notifications@example.org",
                       "calendar-notification@google.com", "mailer-daemon@example.org"):
            with self.subTest(sender=sender):
                self.assertEqual(kind_of(facts(msg("m0000001", sender, ME, ago(days=3)))), followups.AUTOMATED)

    def test_updates_dropped_unless_a_known_correspondent(self) -> None:
        updates = facts(msg("m0000001", THEM, ME, ago(days=3), labels=("INBOX", "CATEGORY_UPDATES")))
        self.assertEqual(kind_of(updates), followups.AUTOMATED)
        self.assertEqual(kind_of(updates, known=lambda address: address == "ana@example.org"), WAITING)

    def test_why_a_waiting_thread_is_not_listed(self) -> None:
        self.assertEqual(kind_of(facts(msg("m0000001", THEM, ME, ago(days=3), labels=("CATEGORY_PERSONAL",)))),
                         followups.ARCHIVED)
        self.assertEqual(kind_of(facts(msg("m0000001", THEM, "team@example.org", ago(days=3), cc=ME))),
                         followups.CC_ONLY)
        self.assertEqual(kind_of(facts(msg("m0000001", THEM, "team@example.org", ago(days=3)))), followups.NOT_DIRECT)
        many = ", ".join([ME] + [f"p{n}@example.org" for n in range(5)])
        self.assertEqual(kind_of(facts(msg("m0000001", THEM, many, ago(days=3)))), followups.GROUP)
        self.assertEqual(kind_of(facts(msg("m0000001", THEM, ME, ago(hours=19)))), followups.TOO_NEW)
        self.assertEqual(kind_of(facts(msg("m0000001", THEM, ME, ago(hours=21)))), WAITING)

    def test_why_an_owed_thread_is_not_listed(self) -> None:
        def owed(to: str, at: datetime) -> str:
            return kind_of(facts(msg("m0000001", ME, to, at, labels=("SENT",))))

        self.assertEqual(owed("ana@example.org", ago(days=2)), followups.TOO_NEW)
        self.assertEqual(owed("ana@example.org", ago(days=3, hours=1)), OWED)
        self.assertEqual(owed(ME, ago(days=4)), followups.TO_SELF)
        self.assertEqual(owed("noreply@example.org", ago(days=4)), followups.TO_SELF)
        self.assertEqual(owed(", ".join(f"p{n}@example.org" for n in range(6)), ago(days=4)), followups.GROUP)
        for group in ("team@example.org", "class@example.edu"):
            with self.subTest(group=group):
                self.assertEqual(owed(f"ana@example.org, {group}", ago(days=4)), followups.GROUP)   # R48

    def test_security_senders_and_the_own_domain(self) -> None:
        found = facts(msg("m0000001", "Ana <ana@google.com>", ME, ago(days=3)))
        self.assertEqual(kind_of(found), followups.SENSITIVE)
        self.assertEqual(kind_of(found, own_domains={"google.com"}), WAITING)   # a colleague at a security domain
        self.assertEqual(kind_of(facts(msg("m0000001", "security@example.org", ME, ago(days=3))),
                                 own_domains={"example.org"}), followups.SENSITIVE)

    def test_hidden_rows_and_a_new_deciding_message(self) -> None:
        first = facts(msg("m0000001", THEM, ME, ago(days=3)))
        item = classify(first, own=OWN, now=NOW, settings=SETTINGS)
        hidden = {item.key}
        self.assertEqual(classify(first, own=OWN, now=NOW, settings=SETTINGS, hidden=hidden.__contains__),
                         followups.HIDDEN)
        again = facts(msg("m0000001", THEM, ME, ago(days=3)), msg("m0000002", THEM, ME, ago(days=1)))
        back = classify(again, own=OWN, now=NOW, settings=SETTINGS, hidden=hidden.__contains__)
        self.assertIsInstance(back, FollowupItem)   # something new arrived: another key, the row is back
        self.assertNotEqual(back.key, item.key)

    def test_the_view_orders_oldest_first(self) -> None:
        items = [classify(facts(msg(f"m000000{n}", THEM, ME, ago(days=n + 1)), thread_id=f"thr000000{n}"),
                          own=OWN, now=NOW, settings=SETTINGS) for n in range(1, 4)]
        view = make_view(items)
        self.assertEqual([item.thread_id for item in view.waiting], ["thr0000003", "thr0000002", "thr0000001"])
        self.assertEqual(view.item(items[0].row_id), items[0])
        self.assertNotIn("Ana", repr(view))


class ParticipantsTests(unittest.TestCase):
    def test_the_index_keeps_people_of_listed_threads_newest_first(self) -> None:
        threads = [facts(msg(f"m000000{n}", THEM, ME, ago(days=n)), thread_id=f"thr000000{n}") for n in range(1, 6)]
        threads.append(facts(msg("m0000009", "news@example.org", ME, ago(days=1),
                                 headers={"List-Id": "<n.example.org>"}), thread_id="thr0000009"))
        index = participants_index(threads, OWN)
        refs = index["ana@example.org"]
        self.assertEqual([ref.thread_id for ref in refs], ["thr0000001", "thr0000002", "thr0000003"])
        self.assertNotIn("news@example.org", index)   # automated mail is no participant
        self.assertNotIn(ME, index)
        self.assertEqual(repr(refs[0]), "ThreadRef(work)")


def scan_reader(threads: dict, **kwargs) -> FakeScanReader:
    return FakeScanReader("work", meta=threads, **kwargs)


class ScannerTests(unittest.TestCase):
    def scan(self, scanner: MailScanner, reader, *, stop_key: str = "|0", cancel=None, settings=SETTINGS,
             hidden=lambda _k: False):
        return scanner.scan("work", reader, now=NOW, settings=settings, known=lambda _a: False, hidden=hidden,
                            cancel=cancel or threading.Event(), stop_key=stop_key)

    def threads(self, count: int, *, start: int = 0, label: str = "INBOX") -> dict:
        found = {}
        for n in range(start, start + count):
            thread_id = f"thr{n:07d}"
            sender, to = (THEM, ME) if label == "INBOX" else (ME, "ana@example.org")
            found[thread_id] = gmail_meta_thread(thread_id, [msg(f"m{n:07d}", sender, to, ago(days=3, hours=n % 20),
                                                                 labels=(label,))])
        return found

    def test_sent_first_and_the_cache_by_history(self) -> None:
        threads = {**self.threads(3), **self.threads(2, start=100, label="SENT")}
        reader = scan_reader(threads, own=("lab@example.org",))
        scanner = MailScanner(sleep=lambda _s: None)
        result = self.scan(scanner, reader)
        self.assertEqual([label for label, _d, _m in reader.lists], ["SENT", "INBOX"])
        self.assertEqual(reader.lists[0][2], 50)                 # a third of 150 for SENT
        self.assertEqual(reader.lists[1][2], 148)                # the rest (unused SENT budget too) for INBOX
        self.assertEqual((result.report.listed, result.report.fetched, result.report.cached), (5, 5, 0))
        self.assertEqual((result.report.waiting, result.report.owed), (3, 2))
        again = self.scan(scanner, reader)
        self.assertEqual((again.report.fetched, again.report.cached), (0, 5))   # unchanged: list pages only
        self.assertEqual(len(reader.gets), 5)
        self.assertEqual(reader.own_calls, 2)
        self.assertNotIn("Ana", repr(result.report))

    def test_a_full_inbox_never_pushes_out_the_owed_threads(self) -> None:
        threads = {**self.threads(200), **self.threads(10, start=500, label="SENT")}
        reader = scan_reader(threads)
        result = self.scan(MailScanner(sleep=lambda _s: None), reader)
        self.assertEqual(result.report.owed, 10)                 # R14
        self.assertEqual(result.report.listed, 150)

    def test_identity_change_clears_but_a_token_refresh_does_not(self) -> None:
        reader = scan_reader(self.threads(2))
        scanner = MailScanner(sleep=lambda _s: None)
        self.scan(scanner, reader)
        self.scan(scanner, reader)   # a token refresh changes nothing the scan keys on
        self.assertEqual(len(reader.gets), 2)
        reader.identity = "100000000000000000999|other@example.edu"
        self.scan(scanner, reader)
        self.assertEqual(len(reader.gets), 4)                    # another Google account: read again

    def test_partial_on_a_rate_limit_and_on_any_other_read_error(self) -> None:
        for error, word in ((MailReadError("slow", status=429), "rate_limit"),
                            (MailReadError("down", status=500), "error"),
                            (MailReadError("refresh", problem=PROBLEM_FAILED), "error")):
            with self.subTest(word=word):
                threads = self.threads(4)
                reader = scan_reader(threads)
                scanner = MailScanner(sleep=lambda _s: None)
                self.scan(scanner, reader)
                reader.meta = {thread_id: dict(data, historyId="999") for thread_id, data in threads.items()}
                reader.errors = {"thr0000002": error}
                with self.assertLogs("briefing_reader.followups", level="INFO") as logs:
                    result = self.scan(scanner, reader)
                self.assertTrue(result.report.partial)
                self.assertEqual(result.report.problem, word)
                self.assertEqual(len(result.items), 4)           # earlier facts plus this partial set
                self.assertFalse(scanner.stopped("work"))
                self.assertIn(f"Follow-ups: work stopped ({word}); partial", "\n".join(logs.output))

    def test_a_refusal_stops_the_account_until_the_sign_in_counter_changes(self) -> None:
        reader = scan_reader(self.threads(2), errors={"list:SENT": MailReadError("no", status=401,
                                                                                  problem=PROBLEM_EXPIRED)})
        scanner = MailScanner(sleep=lambda _s: None)
        first = self.scan(scanner, reader, stop_key="sub|0")
        self.assertEqual(first.report.problem, followups.PROBLEM_REFUSED)
        self.assertTrue(scanner.stopped("work"))
        again = self.scan(scanner, reader, stop_key="sub|0")     # a token refresh: same key, not asked again
        self.assertEqual(again.report.problem, followups.PROBLEM_STOPPED)
        self.assertEqual(len(reader.lists), 1)
        after = self.scan(scanner, reader, stop_key="sub|1")     # a new sign-in for the account
        self.assertEqual(after.report.problem, "")
        self.assertEqual(len(after.items), 2)

    def test_a_refused_own_address_read_stops_the_account_too(self) -> None:
        refusal = MailReadError("no", status=403, problem=PROBLEM_FAILED)
        reader = scan_reader(self.threads(2), errors={"own": refusal})
        scanner = MailScanner(sleep=lambda _s: None)
        with self.assertLogs("briefing_reader.followups", level="INFO"):
            first = self.scan(scanner, reader, stop_key="sub|0")
        self.assertEqual((first.report.problem, first.report.partial), (followups.PROBLEM_REFUSED, True))
        self.assertTrue(scanner.stopped("work"))
        self.assertEqual((reader.lists, reader.gets), ([], []))       # nothing listed after the refusal
        self.assertEqual(self.scan(scanner, reader, stop_key="sub|0").report.problem, followups.PROBLEM_STOPPED)
        self.assertEqual(reader.own_calls, 1)                           # a token refresh never asks again
        self.assertEqual(self.scan(scanner, reader, stop_key="sub|1").report.problem, "")

    def test_unconfirmed_and_unreadable_accounts_are_not_scanned(self) -> None:
        scanner = MailScanner(sleep=lambda _s: None)
        unconfirmed = scan_reader(self.threads(1), confirmed="", problem_word="unconfirmed")
        self.assertEqual(self.scan(scanner, unconfirmed).report.problem, "unconfirmed")
        unreadable = scan_reader(self.threads(1), problem=("signed_out", "Not signed in"))
        self.assertEqual(self.scan(scanner, unreadable).report.problem, "unreadable")
        self.assertEqual(unconfirmed.lists + unreadable.lists, [])

    def test_cancel_between_reads(self) -> None:
        reader = scan_reader(self.threads(5))
        cancel = threading.Event()
        calls = []

        def sleep(_seconds: float) -> None:
            calls.append(1)
            if len(calls) == 2:
                cancel.set()

        result = self.scan(MailScanner(sleep=sleep), reader, cancel=cancel)
        self.assertEqual(len(reader.gets), 2)
        self.assertEqual((result.report.partial, result.report.problem), (True, "cancelled"))

    def test_refetch_one_thread(self) -> None:
        threads = self.threads(1)
        reader = scan_reader(threads)
        scanner = MailScanner(sleep=lambda _s: None)
        self.scan(scanner, reader)
        reader.meta["thr0000000"] = gmail_meta_thread("thr0000000", [
            *threads["thr0000000"]["messages"],
            msg("m9000000", ME, "ana@example.org", ago(hours=1), labels=("SENT",))])
        result = scanner.refetch("work", "thr0000000", reader, now=NOW, settings=SETTINGS, known=lambda _a: False,
                                 hidden=lambda _k: False)
        self.assertEqual(result, followups.TOO_NEW)   # the owner replied: an owed thread, too new to list
        self.assertEqual(scanner.refetch("work", "thr9999999", reader, now=NOW, settings=SETTINGS,
                                         known=lambda _a: False, hidden=lambda _k: False), "gone")

    def test_the_cache_holds_at_most_600_threads(self) -> None:
        scanner = MailScanner(sleep=lambda _s: None)
        settings = ProductivityConfig(enabled=True, scan_max_threads=300)
        for alias in ("work", "personal", "lab"):
            reader = FakeScanReader(alias, meta=self.threads(300, start=1000 * len(alias)))
            scanner.scan(alias, reader, now=NOW, settings=settings, known=lambda _a: False, hidden=lambda _k: False,
                         cancel=threading.Event())
        self.assertLessEqual(scanner.cached(), followups.CACHE_MAX)

    def test_a_job_error_that_is_not_gmail_raises(self) -> None:
        reader = scan_reader(self.threads(1), errors={"thr0000000": ValueError("zq7subj")})
        with self.assertRaises(ValueError):
            self.scan(MailScanner(sleep=lambda _s: None), reader)


class HiddenStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / followups.FOLLOWUPS_FILE
        self.now = NOW

    def store(self) -> HiddenStore:
        return HiddenStore(self.path, clock=lambda: self.now)

    def test_hide_for_days_until_new_and_unhide(self) -> None:
        store = self.store()
        days, new = hide_key("work", "thr0000001", "m1"), hide_key("work", "thr0000002", "m2")
        self.assertTrue(store.hide(days, until=NOW + timedelta(days=3)))
        self.assertTrue(store.hide(new, until=None))
        self.assertTrue(store.hidden(days, NOW) and store.hidden(new, NOW + timedelta(days=20)))
        self.assertFalse(store.hidden(days, NOW + timedelta(days=4)))
        data = json.loads(self.path.read_text(encoding="ascii"))
        self.assertEqual(set(data), {"version", "hidden"})
        self.assertEqual(set(data["hidden"]), {days, new})
        self.assertNotIn("thr0000001", self.path.read_text(encoding="ascii"))
        self.assertTrue(self.store().hidden(new, NOW))   # read back from the file
        self.assertTrue(store.unhide(new))
        self.assertFalse(store.hidden(new, NOW))
        self.assertFalse(store.hide("not a key", until=None))

    def test_prune_and_corrupt(self) -> None:
        store = self.store()
        key = hide_key("work", "thr0000001", "m1")
        store.hide(key, until=None)
        self.now = NOW + timedelta(days=31)
        store.hide(hide_key("work", "thr0000002", "m2"), until=None)
        self.assertFalse(store.hidden(key, self.now))            # older than 30 days: pruned
        self.path.write_text("{not json", encoding="ascii")
        with self.assertLogs("briefing_reader.followups", level="WARNING"):
            self.assertEqual(len(self.store()), 0)               # corrupt: empty (rows come back)


class FollowupsCheckTests(unittest.TestCase):
    def test_the_command_line_check_prints_counts_only(self) -> None:
        """--followups-check: one scan per account, counts only (never a subject, a name or an id)."""
        import io
        from types import SimpleNamespace

        from briefing_reader.ask import commands
        from briefing_reader.config import load_config

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.toml").write_text("[ask]\nenabled = true\n", encoding="utf-8")
            config = load_config(root, environ={"NOTION_TOKEN": "", "LOCALAPPDATA": str(root / "local")})
            threads = {"thr0000001": gmail_meta_thread("thr0000001", [
                msg("m0000001", "Zq7 Name <zq7@example.org>", ME, ago(days=3), subject="Zq7subj")])}
            reader = FakeScanReader("work", meta=threads, confirmed=ME)
            unconfirmed = FakeScanReader("personal", meta={}, confirmed="", problem_word="unconfirmed")
            out = io.StringIO()
            code = commands.followups_check(config, out, sources=SimpleNamespace(
                readers={"work": reader, "personal": unconfirmed}), now=NOW)
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("  work: listed 1, read 1 header set(s); 1 waiting on you, 0 owed to you; skipped: none", text)
        self.assertIn("  personal: not scanned: the Google account is not confirmed yet", text)
        for secret in ("zq7", "Zq7", "thr0000001", "m0000001", ME):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
