"""Tests for briefing_reader.gmail: the message Jarvis sends (headers, threading, injection
attempts, limits, the round-trip check) and GmailSender (one send, error mapping, identity).

Nothing here touches the network: the Gmail service is a fake that records every call, token
refreshes are injected, and every socket connection raises NetworkUsed (a BaseException, so no
broad handler in the code under test can hide it). Addresses, ids and tokens are invented.
"""

from __future__ import annotations

import base64
import email.policy
import http.client
import json
import logging
import socket
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from unittest import mock

from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.errors import HttpError
from httplib2 import Response

from briefing_reader import actions, gmail, google_auth
from briefing_reader.config import RedactingFilter
from briefing_reader.gmail import (
    GmailAuthError,
    GmailError,
    GmailSender,
    GmailSetupError,
    GmailUnknownOutcome,
    MessageRefused,
    OutgoingMail,
    SentMail,
    build_message,
    encode_raw,
    thread_link,
)
from briefing_reader.google_auth import (
    PROBLEM_BLOCKED,
    PROBLEM_CONFIRM,
    PROBLEM_EXPIRED,
    PROBLEM_FAILED,
    PROBLEM_IDENTITY,
    PROBLEM_SCOPE,
    PROBLEM_SETUP,
    PROBLEM_SIGNED_OUT,
    TOKEN_SUB_KEY,
    AccountBindings,
    GoogleAccount,
    RequestNotResent,
)

GMAIL_LOGGER = "briefing_reader.gmail"
CLIENT_ID = "1234567890-fakeclient.apps.googleusercontent.com"
SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email",
          "https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/calendar.settings.readonly",
          "https://www.googleapis.com/auth/gmail.send"]
SUB = "100000000000000000001"
ME = "you@example.edu"
MSGID = "<CAExample0001@mail.example.com>"


class NetworkUsed(BaseException):
    """Raised by the socket guard; a BaseException so broad handlers cannot hide it."""


def _no_network(*args: Any, **kwargs: Any) -> Any:
    raise NetworkUsed("a test tried to open a network connection")


def reply(**changes: Any) -> OutgoingMail:
    fields = dict(account="work", from_addr=ME, to=("ana@example.edu", "ben@example.edu"), cc=(),
                  subject="Re: Thursday noon meeting", body="Hi both,\nNoon works.\nThanks",
                  thread_id="18c0ffee00000001", in_reply_to=MSGID)
    fields.update(changes)
    return OutgoingMail(**fields)


def new_email(**changes: Any) -> OutgoingMail:
    fields = dict(account="personal", from_addr="me@example.com", to=("office@example.edu",),
                  cc=("cy@example.edu",), subject="Question about the lab schedule",
                  body="Hello,\nIs the lab open on Saturday?\nThanks")
    fields.update(changes)
    return OutgoingMail(**fields)


def parse(message: Any) -> Any:
    return BytesParser(policy=email.policy.default).parsebytes(message.as_bytes(policy=email.policy.SMTP))


def http_error(status: int, message: str, reason: str = "", *, rpc_status: str = "",
               rpc_reason: str = "") -> HttpError:
    error: dict[str, Any] = {"code": status, "message": message}
    if reason:
        error["errors"] = [{"reason": reason, "message": message}]
    if rpc_status:
        error["status"] = rpc_status
    if rpc_reason:
        error["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": rpc_reason}]
    return HttpError(Response({"status": str(status)}), json.dumps({"error": error}).encode())


# --------------------------------------------------------------------------
# The message
# --------------------------------------------------------------------------

class BuildMessageTests(unittest.TestCase):
    def test_a_reply_says_exactly_what_was_asked(self) -> None:
        message = build_message(reply(cc=("cy@example.edu",)))
        parsed = parse(message)
        self.assertEqual([name for name in parsed.keys()],
                         ["From", "To", "Cc", "Subject", "In-Reply-To", "References", "Content-Type",
                          "Content-Transfer-Encoding", "MIME-Version"])
        self.assertEqual(str(parsed["From"]), ME)
        self.assertEqual(str(parsed["To"]), "ana@example.edu, ben@example.edu")
        self.assertEqual(str(parsed["Cc"]), "cy@example.edu")
        self.assertEqual(str(parsed["Subject"]), "Re: Thursday noon meeting")
        self.assertEqual((str(parsed["In-Reply-To"]), str(parsed["References"])), (MSGID, MSGID))
        self.assertEqual(parsed.get_content_type(), "text/plain")
        self.assertEqual(parsed.get_content_charset(), "utf-8")
        self.assertFalse(parsed.is_multipart())
        self.assertEqual(parsed.get_content().replace("\r\n", "\n"), "Hi both,\nNoon works.\nThanks\n")
        self.assertNotIn(b"Bcc", message.as_bytes())
        self.assertNotIn(b"Message-ID", message.as_bytes())   # Gmail gives it one

    def test_a_new_email_has_no_thread_headers(self) -> None:
        parsed = parse(build_message(new_email()))
        self.assertIsNone(parsed["In-Reply-To"])
        self.assertIsNone(parsed["References"])
        self.assertEqual(str(parsed["Subject"]), "Question about the lab schedule")

    def test_reply_subjects_keep_or_get_re(self) -> None:
        self.assertEqual(str(parse(build_message(reply(subject="Thursday")))["Subject"]), "Re: Thursday")
        self.assertEqual(str(parse(build_message(reply(subject="RE: Thursday")))["Subject"]), "RE: Thursday")
        self.assertEqual(str(parse(build_message(new_email(subject="Thursday")))["Subject"]), "Thursday")
        self.assertEqual(str(parse(build_message(reply(subject="  Re:   a   b ")))["Subject"]), "Re: a b")
        # A reply may carry the briefing's whole subject= (250 characters) and "Re: " on top.
        self.assertEqual(str(parse(build_message(reply(subject="x" * 250)))["Subject"]), "Re: " + "x" * 250)
        with self.assertRaises(MessageRefused):
            build_message(reply(subject="x" * 251))
        with self.assertRaises(MessageRefused):
            build_message(new_email(subject="x" * 251))
        self.assertEqual((gmail.subject_cap(reply=True), gmail.subject_cap(reply=False)), (254, 250))

    def test_every_reply_subject_the_parser_accepts_can_be_sent(self) -> None:
        for length in (246, 247, 248, 249, 250):
            with self.subTest(length=length):
                line = (f"Reply: acct=work | thread=18c0ffee00000001 | msgid=CAExample0001@mail.example.com "
                        f"| to=ana@example.edu | subject={'s' * length} "
                        "| body=Noon works.")
                action = actions.parse_action_line(line)
                self.assertEqual(action.error, "")
                self.assertEqual(actions.mail_problem(action), "")
                message = build_message(reply(subject=action.title, in_reply_to=""))
                self.assertEqual(str(parse(message)["Subject"]), action.title)

    def test_only_the_thread_id_without_a_message_id(self) -> None:
        parsed = parse(build_message(reply(in_reply_to="")))
        self.assertIsNone(parsed["In-Reply-To"])
        self.assertEqual(str(parsed["Subject"]), "Re: Thursday noon meeting")
        refs = "<a1@example.com> <b2@example.com>"
        parsed = parse(build_message(reply(in_reply_to="<b2@example.com>", references=refs)))
        self.assertEqual(str(parsed["References"]), refs)

    def test_unicode_text_is_kept_exactly(self) -> None:
        body = "Caf\u00e9 at noon \u2014 \U0001F44D\nFamily: \U0001F468\u200d\U0001F469\u200d\U0001F467\n" + "x" * 120
        message = build_message(new_email(subject="Caf\u00e9 \u2014 r\u00e9union", body=body))
        parsed = parse(message)
        self.assertEqual(str(parsed["Subject"]), "Caf\u00e9 \u2014 r\u00e9union")
        self.assertEqual(parsed.get_content().replace("\r\n", "\n"), body + "\n")
        self.assertEqual(parsed["Content-Transfer-Encoding"], "quoted-printable")
        self.assertTrue(message.as_bytes().isascii())
        self.assertEqual(parse(build_message(new_email()))["Content-Transfer-Encoding"], "7bit")

    def test_a_header_look_alike_in_the_body_stays_text(self) -> None:
        body = "Bcc: eve@example.com\nSubject: changed\n\nTo: eve@example.com"
        parsed = parse(build_message(new_email(body=body)))
        self.assertIsNone(parsed["Bcc"])
        self.assertEqual(str(parsed["Subject"]), "Question about the lab schedule")
        self.assertEqual(str(parsed["To"]), "office@example.edu")
        self.assertEqual(parsed.get_content().replace("\r\n", "\n"), body + "\n")

    def test_header_injection_in_the_subject_is_refused(self) -> None:
        encoded_crlf = "=?utf-8?b?" + base64.b64encode(b"hi\r\nBcc: eve@example.com").decode() + "?="
        for subject in ("Hi\r\nBcc: eve@example.com", "Hi\nBcc: eve@example.com", "Hi\rBcc: eve@example.com",
                        "Hi\u2028Bcc: eve@example.com", "Hi\u2029x", "Hi\u0085x", "Hi\x0bx", "Hi\x0cx",
                        "Hi\x00x", "Hi\tthere", "Hi\x1bx", "Hi\x7fx", "abc\u202edef", "a\u200bb", "a\u2066b",
                        "a\U000e0041b", "a\u200db", encoded_crlf, "=?utf-8?q?Hello?=", "Re: price =?ISO-8859-1?Q?a?= x",
                        "=?x?=", "", "   ", "x" * 251, None, 5):
            with self.subTest(subject=repr(subject)[:50]), self.assertRaises(MessageRefused):
                build_message(new_email(subject=subject))
        self.assertEqual(len(str(parse(build_message(new_email(subject="x" * 250)))["Subject"])), 250)
        self.assertEqual(str(parse(build_message(new_email(subject="Is 2 =? 3")))["Subject"]), "Is 2 =? 3")

    def test_recipients_are_plain_valid_and_few(self) -> None:
        five = tuple(f"p{n}@example.edu" for n in range(5))
        self.assertEqual(len(parse(build_message(new_email(to=five[:3], cc=five[3:])))["Cc"].addresses), 2)
        bad = (dict(to=()), dict(to=("Ana <ana@example.edu>",)), dict(to=("ana@example.edu, eve@example.com",)),
               dict(to=("ana@example.edu\r\nBcc: eve@example.com",)), dict(cc=("eve@example.com\n",)),
               dict(to=("ana@Example.EDU",)),   # not normalized: domains are written in lower case
               dict(to=("ana@example.edu",), cc=("ANA@example.edu",)), dict(to=five, cc=("x@example.edu",)),
               dict(to=five + ("x@example.edu",), cc=()), dict(to=("me@example.com",)),
               dict(cc=("Me@Example.com",)), dict(to=(5,)), dict(to=("bob@",)), dict(to=('"a b"@example.edu',)),
               dict(to=("an\u00e1@example.edu",)), dict(from_addr="Me <me@example.com>"),
               dict(from_addr="me@example.com\r\nBcc: eve@example.com"))
        for changes in bad:
            with self.subTest(changes=repr(changes)[:60]), self.assertRaises(MessageRefused):
                build_message(new_email(**changes))
        no_from = parse(build_message(new_email(from_addr="")))
        self.assertIsNone(no_from["From"])

    def test_never_to_the_senders_own_mailbox(self) -> None:
        """Case, a "+tag" and (for gmail.com / googlemail.com) dots do not make it another mailbox."""
        own = (dict(to=("me@example.com",)), dict(to=("ME@example.com",)), dict(to=("me+lab@example.com",)),
               dict(to=("office@example.edu",), cc=("Me+x@example.com",)),
               dict(from_addr="ana.lima@gmail.com", to=("analima@gmail.com",), cc=()),
               dict(from_addr="ana.lima@gmail.com", to=("a.na.li.ma+x@googlemail.com",), cc=()),
               dict(from_addr="analima@googlemail.com", to=("ana.lima@gmail.com",), cc=()))
        for changes in own:
            with self.subTest(changes=changes), self.assertRaises(MessageRefused) as ctx:
                build_message(new_email(**changes))
            self.assertEqual(str(ctx.exception), gmail.OWN_ADDRESS_REFUSED)
        for changes in (dict(to=("me.too@example.com",)), dict(to=("me@example.co",)),
                        dict(from_addr="ana.lima@gmail.com", to=("ana.lima@example.com",), cc=()),
                        dict(from_addr="ana.lima@example.com", to=("analima@example.com",), cc=())):
            with self.subTest(changes=changes):
                build_message(new_email(**changes))   # another mailbox: fine

    def test_thread_headers_must_be_message_ids(self) -> None:
        for changes in (dict(in_reply_to="CAExample0001@mail.example.com"), dict(in_reply_to="<a b@example.com>"),
                        dict(in_reply_to="<a@example.com>\r\nBcc: eve@example.com"), dict(in_reply_to="<a@b><c@d>"),
                        dict(in_reply_to="<a|b@example.com>"), dict(in_reply_to="<a@b@example.com>"),
                        dict(references="<a@example.com>\nBcc: x@example.com"),
                        dict(references="<a@example.com>  <b@example.com>"),
                        dict(references=" ".join(f"<m{n}@example.com>" for n in range(60))),
                        dict(in_reply_to="", references=MSGID), dict(thread_id="bad id"), dict(thread_id="x" * 65),
                        dict(in_reply_to="<" + "a" * 251 + "@example.com>")):
            with self.subTest(changes=repr(changes)[:60]), self.assertRaises(MessageRefused):
                build_message(reply(**changes))

    def test_long_headers_never_make_overlong_lines(self) -> None:
        long_id = "<" + "a" * 250 + "@" + "b" * 250 + ">"
        recipients = tuple(f"{'p' * 60}{n}@{'d' * 60}.example.edu" for n in range(5))
        message = build_message(reply(to=recipients[:3], cc=recipients[3:], in_reply_to=long_id,
                                      subject="Re: " + "word " * 48 + "end"))
        raw = message.as_bytes(policy=email.policy.SMTP)
        self.assertLessEqual(max(len(line) for line in raw.split(b"\r\n")), 998)
        parsed = parse(message)
        self.assertEqual(tuple(a.addr_spec for a in parsed["To"].addresses), recipients[:3])
        self.assertEqual(str(parsed["In-Reply-To"]), long_id)

    def test_bodies(self) -> None:
        self.assertEqual(parse(build_message(new_email(body="a\r\nb\rc")))
                         .get_content().replace("\r\n", "\n"), "a\nb\nc\n")
        self.assertEqual(len(parse(build_message(new_email(body="y" * 5000))).get_content()) >= 5000, True)
        for body in ("", " \n ", "a\x00b", "a\x07b", "a\x1bb", "a\u2028b", "a\u202eb", "a\u200bb",
                     "y" * 5001, None):
            with self.subTest(body=repr(body)[:30]), self.assertRaises(MessageRefused):
                build_message(new_email(body=body))

    def test_the_round_trip_check_refuses_any_difference(self) -> None:
        message = build_message(new_email())
        check = gmail._check_round_trip
        good = dict(from_addr="me@example.com", to=("office@example.edu",), cc=("cy@example.edu",),
                    subject="Question about the lab schedule", in_reply_to="", references="",
                    body="Hello,\nIs the lab open on Saturday?\nThanks")
        check(message, **good)
        for key, value in (("to", ("eve@example.com",)), ("cc", ()), ("subject", "Other"),
                           ("in_reply_to", MSGID), ("body", "Other text"), ("from_addr", "")):
            with self.subTest(key=key), self.assertRaises(MessageRefused):
                check(message, **{**good, key: value})
        message["Reply-To"] = "eve@example.com"
        with self.assertRaises(MessageRefused):
            check(message, **good)

    def test_encode_raw_is_base64url_of_the_message(self) -> None:
        message = build_message(new_email(body="\u00ff" * 300 + "?>>>"))
        raw = encode_raw(message)
        self.assertTrue(raw.isascii())
        self.assertNotIn("+", raw)
        self.assertNotIn("/", raw)
        self.assertEqual(base64.urlsafe_b64decode(raw), message.as_bytes(policy=email.policy.SMTP))

    def test_thread_link(self) -> None:
        link = thread_link("o'neil+x@example.edu", "18c0ffee00000001")
        self.assertEqual(link, "https://mail.google.com/mail/?authuser=o%27neil%2Bx@example.edu#all/18c0ffee00000001")
        self.assertTrue(actions.link_allowed(link))
        self.assertEqual(thread_link("", "18c0ffee00000001"), "https://mail.google.com/mail/#all/18c0ffee00000001")
        self.assertEqual(thread_link("Ana <a@example.edu>", "18c0ffee00000001"),
                         "https://mail.google.com/mail/#all/18c0ffee00000001")
        self.assertEqual(thread_link(ME, "bad id"), "")
        self.assertEqual(thread_link(ME, ""), "")


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------

class FakeRequest:
    def __init__(self, service: FakeGmail, kwargs: dict[str, Any]) -> None:
        self.service = service
        self.kwargs = kwargs

    def execute(self, num_retries: int = 0) -> Any:
        self.service.calls.append((self.kwargs, num_retries))
        outcome = self.service.outcomes.pop(0) if self.service.outcomes else {
            "id": "18c0ffee000000aa", "threadId": self.kwargs["body"].get("threadId", "18c0ffee000000bb"),
            "labelIds": ["SENT"]}
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeGmail:
    """users().messages().send(userId=..., body=...).execute(num_retries=...)"""

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], int]] = []
        self.outcomes: list[Any] = []

    def users(self) -> FakeGmail:
        return self

    def messages(self) -> FakeGmail:
        return self

    def send(self, **kwargs: Any) -> FakeRequest:
        return FakeRequest(self, kwargs)


def _utc_naive(delta: timedelta) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0) + delta


class SenderTestCase(unittest.TestCase):
    def setUp(self) -> None:
        for target, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                             (socket, "create_connection"), (socket, "getaddrinfo")):
            patcher = mock.patch.object(target, name, _no_network)
            patcher.start()
            self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.secret_path = self.root / "google_client_secret.json"
        self.suffix = uuid.uuid4().hex
        self.client_secret = f"GOCSPX-fake-client-secret-{self.suffix}"
        self.access_token = f"ya29.fake-access-token-{self.suffix}"
        self.secret_path.write_text(json.dumps({"installed": {
            "client_id": CLIENT_ID, "client_secret": self.client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"]}}),
            encoding="utf-8")
        self.bindings = AccountBindings(self.data_dir / "accounts.json")
        self.service = FakeGmail()
        self.built: list[Any] = []
        self.flow_results: list[Any] = []

    def write_token(self, *, scopes: list[str] = SCOPES, sub: str | None = SUB, expired: bool = False,
                    asked: list[str] | None = None) -> None:
        info: dict[str, Any] = {
            "token": self.access_token, "refresh_token": f"1//fake-refresh-{self.suffix}",
            "token_uri": "https://oauth2.googleapis.com/token", "client_id": CLIENT_ID,
            "client_secret": self.client_secret, "scopes": scopes,
            "expiry": f"{_utc_naive(timedelta(hours=-2 if expired else 1)).isoformat()}Z"}
        if sub:
            info[TOKEN_SUB_KEY] = sub
        if asked:
            info[google_auth.TOKEN_ASKED_KEY] = asked
        (self.data_dir / "google_token_work.json").write_text(json.dumps(info), encoding="utf-8")

    def bind(self, *, confirmed: bool = True) -> None:
        """Bind "work" to ME, as its first sign-in does, and (by default) confirm it as the dialog's
        Yes does."""
        self.bindings.bind("work", ME, SUB)
        if confirmed:
            self.assertTrue(self.bindings.confirm("work", ME))

    def ready_sender(self, **kwargs: Any) -> GmailSender:
        self.write_token()
        self.bind()
        return self.sender(**kwargs)

    def sender(self, *, refresh: Any = None) -> GmailSender:
        account = GoogleAccount("work", client_secret_path=self.secret_path, data_dir=self.data_dir,
                                features=("calendar", "gmail_send"), bindings=self.bindings,
                                flow_factory=lambda path, scopes: self, refresh=refresh or (lambda creds: None))
        return GmailSender(account, service_factory=self.factory)

    def factory(self, creds: Any) -> FakeGmail:
        self.built.append(creds)
        return self.service

    def run_local_server(self, **kwargs: Any) -> Any:   # the fake OAuth flow
        result = self.flow_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def fails(self, sender: GmailSender, mail: OutgoingMail | None = None,
              kind: type[GmailError] = GmailError) -> GmailError:
        with self.assertRaises(kind) as ctx:
            sender.send(mail or reply())
        return ctx.exception


class SendTests(SenderTestCase):
    def test_one_send_with_the_raw_message_and_the_thread(self) -> None:
        sender = self.ready_sender()
        with self.assertLogs(GMAIL_LOGGER, level="INFO") as logs:
            sent = sender.send(reply())
        self.assertEqual(sent, SentMail("18c0ffee000000aa", "18c0ffee00000001",
                                        f"https://mail.google.com/mail/?authuser={ME}#all/18c0ffee00000001"))
        ((kwargs, retries),) = self.service.calls
        self.assertEqual((kwargs["userId"], retries), ("me", 0))
        self.assertEqual(set(kwargs["body"]), {"raw", "threadId"})
        self.assertEqual(kwargs["body"]["threadId"], "18c0ffee00000001")
        raw = base64.urlsafe_b64decode(kwargs["body"]["raw"])
        self.assertEqual(raw, build_message(reply()).as_bytes(policy=email.policy.SMTP))
        parsed = BytesParser(policy=email.policy.default).parsebytes(raw)
        self.assertEqual((str(parsed["From"]), str(parsed["In-Reply-To"])), (ME, MSGID))
        text = "\n".join(logs.output)
        self.assertIn("Gmail (work): sent message 18c0ffee000000aa (2 recipient(s))", text)
        for private in (ME, "ana@example.edu", "Thursday", "Noon works", "18c0ffee00000001", self.access_token):
            self.assertNotIn(private, text)

    def test_a_new_email_has_no_thread_id(self) -> None:
        self.write_token()
        self.bind()
        sent = self.sender().send(new_email(account="work", from_addr=ME))
        ((kwargs, _),) = self.service.calls
        self.assertEqual(set(kwargs["body"]), {"raw"})
        self.assertEqual(sent.thread_id, "18c0ffee000000bb")

    def test_nothing_is_sent_unless_the_account_is_ready(self) -> None:
        error = self.fails(self.sender(), kind=GmailAuthError)   # no saved sign-in at all
        self.assertEqual(error.problem, PROBLEM_SIGNED_OUT)
        self.assertIn("nothing was sent", str(error))
        self.write_token(scopes=SCOPES[:4], asked=SCOPES)   # sending was not allowed (a box unticked)
        self.bind()
        error = self.fails(self.sender(), kind=GmailAuthError)
        self.assertEqual(error.problem, PROBLEM_SCOPE)
        self.assertIn("nothing was sent", str(error))
        self.assertEqual(self.service.calls, [])

    def test_the_bound_account_must_be_the_signed_in_one(self) -> None:
        self.write_token(sub=None)   # a sign-in that never said which account it is
        self.bind()
        self.assertEqual(self.fails(self.sender(), kind=GmailAuthError).problem, PROBLEM_IDENTITY)
        self.write_token(sub="100000000000000000002")   # another Google account's token
        error = self.fails(self.sender(), kind=GmailAuthError)
        self.assertEqual(error.problem, PROBLEM_IDENTITY)
        self.write_token()
        self.bindings.unbind("work")   # not bound at all
        self.assertEqual(self.fails(self.sender(), kind=GmailAuthError).problem, PROBLEM_IDENTITY)
        self.bind()
        error = self.fails(self.sender(), reply(from_addr="other@example.edu"), kind=GmailAuthError)
        self.assertIn("not the one on the card", str(error))
        self.assertNotIn("other@example.edu", str(error))
        self.fails(self.sender(), reply(from_addr=""), kind=GmailAuthError)
        self.fails(self.sender(), reply(account="personal"))
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.sender().send(reply(from_addr="YOU@example.edu")).message_id, "18c0ffee000000aa")

    def test_a_refused_message_sends_nothing(self) -> None:
        sender = self.ready_sender()
        error = self.fails(sender, reply(subject="Hi\r\nBcc: eve@example.com"), kind=MessageRefused)
        self.assertNotIn("eve@example.com", str(error))
        self.fails(sender, reply(to=(ME,)), kind=MessageRefused)   # its own address
        self.assertEqual(self.service.calls, [])

    def test_never_to_the_sending_account_itself(self) -> None:
        """The account's own mailbox in any spelling, in To or Cc, alone or with others: refused before
        anything goes out (the alias bound to the card's only recipient included)."""
        sender = self.ready_sender()
        for to, cc in (((ME,), ()), (("YOU@example.edu",), ()), (("you+notes@example.edu",), ()),
                       (("ana@example.edu",), ("You+x@example.edu",)), (("ana@example.edu", ME), ())):
            with self.subTest(to=to, cc=cc):
                error = self.fails(sender, reply(to=to, cc=cc), kind=MessageRefused)
                self.assertEqual(str(error), gmail.OWN_ADDRESS_REFUSED)
                self.assertNotIn("@", str(error))
        self.fails(sender, reply(from_addr="you+x@example.edu", to=(ME,)), kind=MessageRefused)
        self.assertEqual(self.fails(sender, reply(from_addr="other@example.edu"), kind=GmailAuthError).problem,
                         PROBLEM_IDENTITY)
        self.assertEqual(self.service.calls, [])
        self.assertEqual(sender.send(reply(to=("you.too@example.edu",))).message_id, "18c0ffee000000aa")

    def test_http_answers(self) -> None:
        cases = (
            (http_error(400, "Invalid To header", "invalidArgument"), GmailError, "", True),
            (http_error(400, "Mail service not enabled", "failedPrecondition"), GmailError, "", True),
            (http_error(404, "Requested entity was not found.", "notFound"), GmailError, "", True),
            (http_error(429, "Too many", "rateLimitExceeded"), GmailError, "", True),
            (http_error(403, "User-rate limit exceeded", "userRateLimitExceeded"), GmailError, "", True),
            (http_error(403, "Gmail API has not been used in project 1 or it is disabled", "accessNotConfigured"),
             GmailSetupError, PROBLEM_SETUP, True),
            (http_error(403, "Access blocked: admin_policy_enforced", "forbidden"), GmailAuthError, PROBLEM_BLOCKED,
             True),
            (http_error(403, "Request had insufficient authentication scopes.", rpc_status="PERMISSION_DENIED",
                        rpc_reason="ACCESS_TOKEN_SCOPE_INSUFFICIENT"), GmailAuthError, PROBLEM_SCOPE, True),
            (http_error(403, "Insufficient Permission", "insufficientPermissions"), GmailAuthError, PROBLEM_SCOPE,
             True),
            (http_error(403, "The domain administrators have disabled Gmail apps.", "domainPolicy"),
             GmailAuthError, PROBLEM_BLOCKED, True),
            (http_error(403, "Forbidden", "forbidden"), GmailAuthError, PROBLEM_FAILED, True),
            (http_error(401, "Invalid Credentials", "authError"), GmailAuthError, PROBLEM_EXPIRED, False),
        )
        for error, kind, problem, token_kept in cases:
            with self.subTest(status=error.resp.status, reason=str(error.reason)[:30]):
                self.service = FakeGmail()
                self.service.outcomes = [error]
                sender = self.ready_sender()
                with self.assertLogs(GMAIL_LOGGER, level="INFO"):
                    failure = self.fails(sender, kind=kind)
                self.assertNotIsInstance(failure, GmailUnknownOutcome)
                self.assertEqual((failure.status, failure.problem), (error.resp.status, problem))
                self.assertIn("nothing was sent", str(failure))
                self.assertEqual(len(self.service.calls), 1)
                self.assertEqual(self.service.calls[0][1], 0)
                self.assertEqual((self.data_dir / "google_token_work.json").exists(), token_kept)

    def test_a_403_never_signs_the_calendar_out(self) -> None:
        """The saved sign-in is shared with the account's calendar: no Gmail 403 deletes it. Sending
        stays off (no second request) until a sign-in through Send; only a 401 forgets the token."""
        cases = (
            (http_error(403, "Insufficient Permission", "insufficientPermissions"), PROBLEM_SCOPE,
             "Google did not allow sending email for the work account"),
            (http_error(403, "The domain administrators have disabled Gmail apps.", "domainPolicy"),
             PROBLEM_BLOCKED, "(domainPolicy)"),
            (http_error(403, "Forbidden", "forbidden"), PROBLEM_FAILED, "Gmail refused access for the work account"),
        )
        for error, problem, text in cases:
            with self.subTest(problem=problem):
                self.service = FakeGmail()
                self.service.outcomes = [error]
                sender = self.ready_sender()
                with self.assertLogs(GMAIL_LOGGER, level="INFO") as logs:
                    failure = self.fails(sender, kind=GmailAuthError)
                self.assertEqual(failure.problem, problem)
                self.assertIn(text, str(failure))
                self.assertTrue(sender.account.is_signed_in("calendar"))
                self.assertTrue((self.data_dir / "google_token_work.json").exists())
                self.assertNotIn("removed the saved sign-in", "\n".join(logs.output))
                self.assertEqual(sender.ready()[0], problem)
                self.assertEqual(self.fails(sender, kind=GmailAuthError).problem, problem)
                self.assertEqual(len(self.service.calls), 1)   # sending stays off: no second request
        # A sign-in through Send turns sending on again.
        self.flow_results = [ReadyAndSignInTests.creds(self, ReadyAndSignInTests.id_token(self))]
        sender.sign_in()
        self.assertEqual(sender.ready(), ("", ""))

    def test_an_administrator_block_turns_sending_off_for_this_run(self) -> None:
        self.service.outcomes = [http_error(403, "admin_policy_enforced", "forbidden")]
        sender = self.ready_sender()
        with self.assertLogs(GMAIL_LOGGER, level="INFO"):
            error = self.fails(sender, kind=GmailAuthError)
        self.assertIn("administrator does not allow this app to send email (admin_policy_enforced)", str(error))
        self.assertEqual(sender.ready()[0], PROBLEM_BLOCKED)
        self.assertTrue(sender.account.is_signed_in("calendar"))   # the calendar keeps working
        self.assertEqual(self.fails(sender, kind=GmailAuthError).problem, PROBLEM_BLOCKED)
        self.assertEqual(len(self.service.calls), 1)

    def test_answers_that_may_mean_it_was_sent_are_unknown_and_never_retried(self) -> None:
        for error in (http_error(500, "Backend Error", "backendError"), http_error(503, "Service Unavailable"),
                      socket.timeout("timed out"), TimeoutError("timed out"), ConnectionResetError("reset"),
                      http.client.IncompleteRead(b""), RequestNotResent("not sent twice"), OSError("broken pipe")):
            with self.subTest(error=type(error).__name__):
                self.service = FakeGmail()
                self.service.outcomes = [error]
                sender = self.ready_sender()
                with self.assertLogs(GMAIL_LOGGER, level="WARNING"):
                    failure = self.fails(sender, kind=GmailUnknownOutcome)
                self.assertIn("check Sent mail before retrying", str(failure))
                self.assertEqual(len(self.service.calls), 1)
                self.assertTrue((self.data_dir / "google_token_work.json").exists())

    def test_failures_before_the_request_left_are_not_unknown(self) -> None:
        class ServerNotFoundError(Exception):
            pass

        for error in (socket.gaierror("no dns"), ConnectionRefusedError("refused"), ServerNotFoundError("dns"),
                      TransportError("refresh failed"), RefreshError("temporarily unavailable", retryable=True)):
            with self.subTest(error=type(error).__name__):
                self.service = FakeGmail()
                self.service.outcomes = [error]
                with self.assertLogs(GMAIL_LOGGER, level="WARNING"):
                    failure = self.fails(self.ready_sender())
                self.assertNotIsInstance(failure, GmailUnknownOutcome)
                self.assertIn("nothing was sent", str(failure))

    def test_a_rejected_refresh_forgets_the_sign_in(self) -> None:
        def refresh(_creds: Any) -> None:
            raise RefreshError("invalid_grant: Token has been expired or revoked.", {"error": "invalid_grant"})

        self.write_token(expired=True)
        self.bind()
        sender = self.sender(refresh=refresh)
        with self.assertLogs("briefing_reader.google_auth", level="WARNING"):
            error = self.fails(sender, kind=GmailAuthError)
        self.assertEqual(error.problem, PROBLEM_EXPIRED)
        self.assertFalse((self.data_dir / "google_token_work.json").exists())
        self.assertEqual(self.service.calls, [])
        self.assertEqual(sender.account.bound_email(), ME)   # the next sign-in must be the same account

    def test_error_messages_and_logs_never_carry_addresses_or_secrets(self) -> None:
        self.service.outcomes = [http_error(400, f"Invalid To header: eve@example.com near {self.access_token}",
                                            "invalidArgument")]
        sender = self.ready_sender()
        with self.assertLogs(level="DEBUG") as logs:
            error = self.fails(sender)
        self.assertIn("<address>", str(error))
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "%s", (str(error),), None)
        RedactingFilter().filter(record)
        for text in (record.getMessage(), "\n".join(logs.output)):
            for private in ("eve@example.com", self.access_token, ME, "ana@example.edu", "Noon works"):
                self.assertNotIn(private, text)

    def test_the_service_is_built_once_per_sign_in(self) -> None:
        sender = self.ready_sender()
        sender.send(reply())
        sender.send(reply())
        self.assertEqual(len(self.built), 1)
        self.assertEqual(len(self.service.calls), 2)   # two approvals, two sends: never more
        self.assertEqual(self.built[0].token, self.access_token)

    def test_the_real_client_never_sends_twice_and_stays_offline(self) -> None:
        self.write_token()
        creds = GoogleAccount("work", client_secret_path=self.secret_path, data_dir=self.data_dir,
                              features=("gmail_send",)).credentials(interactive=False)
        service = gmail._build_service(creds)
        request = service.users().messages().send(userId="me", body={"raw": "eA"})
        self.assertEqual(request.method, "POST")
        self.assertTrue(request.uri.startswith("https://gmail.googleapis.com/gmail/v1/users/me/messages/send"))
        inner = request.http.http
        self.assertIsInstance(inner, type(google_auth.single_send_http(1)))
        self.assertEqual(inner.timeout, gmail.HTTP_TIMEOUT_S)


class ReadyAndSignInTests(SenderTestCase):
    def test_ready_matrix(self) -> None:
        self.assertEqual(self.sender().ready()[0], PROBLEM_SIGNED_OUT)
        self.write_token(scopes=SCOPES[:4], asked=SCOPES)   # asked for sending, a box was unticked
        self.assertEqual(self.sender().ready()[0], PROBLEM_SCOPE)
        self.assertIn("sending email for the work account", self.sender().ready()[1])
        self.bind()
        self.write_token(scopes=SCOPES[:4], asked=SCOPES[:4])   # "gmail_send" was added since
        self.assertEqual(self.sender().ready(),
                         (PROBLEM_SIGNED_OUT, "Sending email needs one more Google sign-in for the work account"))
        self.bindings.unbind("work")
        self.write_token()
        self.assertEqual(self.sender().ready()[0], PROBLEM_IDENTITY)   # not bound yet
        self.bind(confirmed=False)   # bound by a first sign-in, not confirmed yet
        self.assertEqual(self.sender().ready(),
                         (PROBLEM_CONFIRM, "You haven't confirmed yet that this is the right Google account for the "
                                           "work account"))
        self.assertEqual(self.sender().pending_confirmation(), ME)
        self.bind()
        self.assertEqual(self.sender().ready(), ("", ""))
        self.assertEqual(self.sender().pending_confirmation(), "")
        self.assertTrue(self.sender().is_signed_in())
        self.assertEqual(self.sender().from_address(), ME)
        self.secret_path.unlink()
        self.assertEqual(self.sender().ready()[0], PROBLEM_SETUP)

    def test_sign_in_binds_and_allows_sending_once_confirmed(self) -> None:
        token = self.id_token()
        self.flow_results = [self.creds(token)]
        sender = self.sender()
        sender.sign_in()
        # A first sign-in binds, but nothing is sent before you confirm it is the right account.
        self.assertEqual((sender.ready()[0], sender.from_address(), sender.pending_confirmation()),
                         (PROBLEM_CONFIRM, ME, ME))
        error = self.fails(sender, kind=GmailAuthError)
        self.assertEqual(error.problem, PROBLEM_CONFIRM)
        self.assertIn("nothing was sent", str(error))
        self.assertNotIn(ME, str(error))
        self.assertEqual(self.service.calls, [])
        self.assertFalse(sender.confirm_account("someone.else@example.edu"))   # not the address it showed
        self.assertEqual(sender.ready()[0], PROBLEM_CONFIRM)
        self.assertTrue(sender.confirm_account(" YOU@example.edu "))
        self.assertEqual((sender.ready(), sender.pending_confirmation()), (("", ""), ""))
        on_disk = json.loads((self.data_dir / "accounts.json").read_text(encoding="utf-8"))
        self.assertIs(on_disk["work"]["confirmed"], True)
        self.assertTrue(on_disk["work"]["confirmed_at"])
        self.assertEqual(self.sender().ready(), ("", ""))   # the next start: still confirmed
        self.assertEqual(sender.send(reply()).message_id, "18c0ffee000000aa")

    def test_disconnect_forgets_the_sign_in_and_the_binding(self) -> None:
        sender = self.ready_sender()
        with self.assertLogs("briefing_reader.google_auth", level="INFO"):
            sender.disconnect()
        self.assertFalse((self.data_dir / "google_token_work.json").exists())
        self.assertEqual((sender.from_address(), sender.pending_confirmation()), ("", ""))
        self.assertEqual(sender.ready()[0], PROBLEM_SIGNED_OUT)
        self.assertEqual(self.fails(sender, kind=GmailAuthError).problem, PROBLEM_SIGNED_OUT)
        self.flow_results = [self.creds(self.id_token())]
        sender.sign_in()   # the next sign-in binds again, unconfirmed
        self.assertEqual(sender.ready()[0], PROBLEM_CONFIRM)
        self.assertEqual((sender.change_problem()[0], sender.needs_confirmation()), (PROBLEM_CONFIRM, True))
        self.assertEqual(self.service.calls, [])

    def test_a_disconnect_that_cannot_delete_the_sign_in_keeps_the_binding(self) -> None:
        """"No, use another account" while the token file is locked: the binding stays (unconfirmed,
        so nothing is sent) and the caller is told; no sign-in may start over the old one."""
        self.write_token()
        self.bind(confirmed=False)
        sender = self.sender()
        token = self.data_dir / "google_token_work.json"
        real_unlink = Path.unlink

        def locked(path: Path, *args: Any, **kwargs: Any) -> None:
            if path == token:
                raise PermissionError(13, "The process cannot access the file")
            real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", locked),                 self.assertLogs("briefing_reader.google_auth", level="WARNING"),                 self.assertRaises(google_auth.AccountError):
            sender.disconnect()
        self.assertTrue(token.exists())
        self.assertEqual((sender.pending_confirmation(), sender.ready()[0]), (ME, PROBLEM_CONFIRM))
        self.assertEqual(self.fails(sender, kind=GmailAuthError).problem, PROBLEM_CONFIRM)
        self.assertEqual(self.service.calls, [])

    def test_a_token_of_the_calendar_only_version_needs_a_first_send_sign_in(self) -> None:
        """Phase 2 tokens (calendar only, no account id, never asked for sending): the calendar works
        without a new sign-in, and the card asks for the first send sign-in - not "Google did not
        allow sending" (nothing was refused)."""
        from briefing_reader.config import AccountConfig
        from briefing_reader.executor import CalendarBackend, Executor, GmailBackend
        phase2 = SCOPES[2:4]
        info = {"token": self.access_token, "refresh_token": f"1//fake-refresh-{self.suffix}",
                "token_uri": "https://oauth2.googleapis.com/token", "client_id": CLIENT_ID,
                "client_secret": self.client_secret, "scopes": phase2,
                "expiry": f"{_utc_naive(timedelta(hours=1)).isoformat()}Z"}
        (self.data_dir / "google_token_work.json").write_text(json.dumps(info), encoding="utf-8")
        sender = self.sender()
        self.assertTrue(sender.account.is_signed_in("calendar"))
        self.assertEqual(sender.account.refused_features(), frozenset())
        problem, message = sender.ready()
        self.assertEqual(problem, PROBLEM_IDENTITY)
        self.assertNotIn("did not allow", message)
        executor = Executor([CalendarBackend({}), GmailBackend({"work": sender})],
                            {"work": AccountConfig("work", features=("calendar", "gmail_send"))})
        action = actions.parse_action_line("Reply: acct=work | thread=18c0ffee00000001 | msgid=a1@example.com "
                                           "| to=ana@example.edu "
                                           "| subject=Thursday | body=Noon works.")
        status = executor.mail_status(action)
        self.assertEqual(status.from_text, "From: work (account not confirmed yet)")
        self.assertTrue(status.sign_in)
        self.assertFalse(status.hand_off)
        self.assertEqual(status.note, "Jarvis doesn't know yet which Google account work is - click Send to sign "
                                      "in and confirm it")
        self.assertNotIn("did not allow", status.note)

    def test_sign_in_problems(self) -> None:
        self.flow_results = [self.creds(self.id_token(), granted=SCOPES[:4])]
        with self.assertLogs("briefing_reader.google_auth", level="WARNING"), \
                self.assertRaises(GmailAuthError) as ctx:
            self.sender().sign_in()
        self.assertEqual(ctx.exception.problem, PROBLEM_SCOPE)
        self.flow_results = [self.creds(None)]
        with self.assertRaises(GmailAuthError) as ctx:
            self.sender().sign_in()
        self.assertEqual(ctx.exception.problem, PROBLEM_IDENTITY)
        from oauthlib.oauth2.rfc6749.errors import CustomOAuth2Error
        self.flow_results = [CustomOAuth2Error(error="admin_policy_enforced", description="blocked")]
        sender = self.sender()
        with self.assertRaises(GmailAuthError) as ctx:
            sender.sign_in()
        self.assertEqual(ctx.exception.problem, PROBLEM_BLOCKED)
        self.assertEqual(sender.ready()[0], PROBLEM_BLOCKED)

    def id_token(self) -> str:
        def part(data: dict[str, Any]) -> str:
            return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
        claims = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": SUB, "email": ME,
                  "email_verified": True}
        return f"{part({'alg': 'none'})}.{part(claims)}.sig"

    def creds(self, id_token: str | None, granted: list[str] | None = None) -> Any:
        info = {"token": self.access_token, "refresh_token": f"1//fake-refresh-{self.suffix}",
                "token_uri": "https://oauth2.googleapis.com/token", "client_id": CLIENT_ID,
                "client_secret": self.client_secret, "scopes": SCOPES,
                "expiry": f"{_utc_naive(timedelta(hours=1)).isoformat()}Z"}
        creds = mock.Mock()
        creds.token, creds.refresh_token, creds.client_secret = info["token"], info["refresh_token"], self.client_secret
        creds.id_token = id_token
        creds.granted_scopes = granted or SCOPES
        creds.valid = True
        creds.to_json.return_value = json.dumps(info)
        return creds


# --------------------------------------------------------------------------
# Reading (Ask Jarvis): gmail.readonly, never the sending path
# --------------------------------------------------------------------------

READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class FakeReadRequest:
    def __init__(self, service: FakeGmailRead, name: str, kwargs: dict[str, Any]) -> None:
        self.service, self.name, self.kwargs = service, name, kwargs

    def execute(self, num_retries: int = 0) -> Any:
        self.service.calls.append((self.name, self.kwargs, num_retries))
        outcome = self.service.outcomes.pop(0) if self.service.outcomes else {}
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeGmailRead:
    """users().threads().list / get only: it has no send, modify, trash or delete at all."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], int]] = []
        self.outcomes: list[Any] = []

    def users(self) -> FakeGmailRead:
        return self

    def threads(self) -> FakeGmailRead:
        return self

    def list(self, **kwargs: Any) -> FakeReadRequest:
        return FakeReadRequest(self, "threads.list", kwargs)

    def get(self, **kwargs: Any) -> FakeReadRequest:
        return FakeReadRequest(self, "threads.get", kwargs)


class ReaderTests(SenderTestCase):
    def reader(self, features: tuple[str, ...] = ("calendar", "gmail_send", "gmail_read")) -> gmail.GmailReader:
        self.read_service = FakeGmailRead()
        self.read_built: list[Any] = []
        account = GoogleAccount("work", client_secret_path=self.secret_path, data_dir=self.data_dir,
                                features=features, bindings=self.bindings,
                                flow_factory=lambda path, scopes: self, refresh=lambda creds: None)

        def factory(creds: Any) -> FakeGmailRead:
            self.read_built.append(creds)
            return self.read_service

        return gmail.GmailReader(account, service_factory=factory)

    def ready_reader(self) -> gmail.GmailReader:
        self.write_token(scopes=SCOPES + [READ_SCOPE], asked=SCOPES + [READ_SCOPE])
        self.bind()
        return self.reader()

    def test_search_and_thread_are_read_only_calls(self) -> None:
        reader = self.ready_reader()
        self.read_service.outcomes = [{"threads": [{"id": "18c0ffee00000001"}, {"id": "bad id!"},
                                                   {"id": "18c0ffee00000001"}, {"id": "18c0ffee00000002"}]},
                                      {"id": "18c0ffee00000001", "messages": []}]
        with self.assertLogs(GMAIL_LOGGER, level="INFO") as logs:
            found = reader.search("from:ana@example.edu subject:budget")
        self.assertEqual(found, ["18c0ffee00000001", "18c0ffee00000002"])
        self.assertEqual(reader.thread("18c0ffee00000001"), {"id": "18c0ffee00000001", "messages": []})
        (first, query, retries), (second, get, _) = self.read_service.calls
        self.assertEqual((first, query, retries), ("threads.list", {
            "userId": "me", "q": "from:ana@example.edu subject:budget", "maxResults": 3,
            "includeSpamTrash": False, "fields": "threads(id)"}, gmail.READ_RETRIES))
        self.assertEqual((second, get["id"], get["format"]), ("threads.get", "18c0ffee00000001", "full"))
        self.assertEqual(self.flow_results, [])   # never a sign-in
        text = "\n".join(logs.output)
        self.assertIn("Gmail reading (work): a search found 2 thread(s)", text)
        for private in ("ana@example.edu", "budget", "18c0ffee"):
            self.assertNotIn(private, text)
        with self.assertRaises(gmail.MailReadError):
            reader.thread("not a thread id")
        self.assertFalse(hasattr(reader, "send"))

    def test_available_follows_the_sign_in(self) -> None:
        self.assertEqual(self.reader().available()[0], PROBLEM_SIGNED_OUT)
        self.write_token(scopes=SCOPES, asked=SCOPES)   # signed in before "gmail_read" was added
        self.bind()
        problem, message = self.reader().available()
        self.assertEqual((problem, message), (PROBLEM_SIGNED_OUT,
                                              "Reading email needs one more Google sign-in for the work account"))
        self.write_token(scopes=SCOPES, asked=SCOPES + [READ_SCOPE])   # asked, but the box was unticked
        problem, message = self.reader().available()
        self.assertEqual(problem, PROBLEM_SCOPE)
        self.assertIn("did not allow reading email", message)
        self.assertEqual(self.reader(features=("calendar", "gmail_send")).available()[0], PROBLEM_SETUP)
        self.write_token(scopes=SCOPES + [READ_SCOPE], asked=SCOPES + [READ_SCOPE])
        self.assertEqual(self.reader().available(), ("", ""))
        # Sending is not affected by reading either way.
        self.assertEqual(self.sender().ready(), ("", ""))

    def test_reading_never_needs_or_uses_sending(self) -> None:
        self.write_token(scopes=SCOPES[:4] + [READ_SCOPE], asked=SCOPES[:4] + [READ_SCOPE])   # read, no send
        self.bind()
        reader = self.reader()
        self.assertEqual(reader.available(), ("", ""))
        self.assertEqual(self.sender().ready()[0], PROBLEM_SIGNED_OUT)   # sending still needs its own permission
        self.read_service.outcomes = [{"threads": []}]
        reader.search("subject:budget")
        self.assertEqual(self.service.calls, [])   # the sending client was never touched

    def test_refusals_turn_reading_off_but_keep_the_sign_in(self) -> None:
        token = self.data_dir / "google_token_work.json"
        cases = ((http_error(401, "Invalid Credentials"), PROBLEM_EXPIRED),
                 (http_error(403, "Insufficient Permission", "insufficientPermissions"), PROBLEM_SCOPE),
                 (http_error(403, "Denied", "forbidden"), PROBLEM_FAILED),
                 (http_error(403, "Blocked", "domainPolicy"), PROBLEM_BLOCKED))
        for error, problem in cases:
            with self.subTest(problem=problem):
                reader = self.ready_reader()
                self.read_service.outcomes = [error]
                with self.assertLogs(GMAIL_LOGGER, level="INFO"):
                    with self.assertRaises(gmail.MailReadError) as caught:
                        reader.search("subject:budget")
                self.assertEqual(caught.exception.problem, problem)
                self.assertTrue(token.exists())   # the shared sign-in stays for Calendar and sending
                self.assertEqual(reader.available()[0], problem)
                with self.assertRaises(gmail.MailReadError):
                    reader.search("subject:budget")
                self.assertEqual(len(self.read_service.calls), 1)   # not asked again this run

    def test_transient_failures(self) -> None:
        reader = self.ready_reader()
        self.read_service.outcomes = [http_error(404, "Not Found", "notFound"), http_error(429, "Slow down"),
                                      http_error(500, "Backend Error"), TransportError("no route")]
        for expected in ("Gmail has no such thread", "limiting requests", "could not be read just now",
                         "Could not reach Gmail"):
            with self.subTest(expected=expected), self.assertLogs(GMAIL_LOGGER, level="INFO"):
                with self.assertRaises(gmail.MailReadError) as caught:
                    reader.thread("18c0ffee00000001")
                self.assertIn(expected, str(caught.exception))
            if expected == "Could not reach Gmail":
                break
        self.assertEqual(reader.available(), ("", ""))   # none of these turned reading off

    def test_reader_and_sender_never_share_a_client(self) -> None:
        reader = self.ready_reader()
        self.read_service.outcomes = [{"threads": []}]
        reader.search("subject:budget")
        sender = self.sender()
        sender.send(reply())
        self.assertIsNot(self.read_service, self.service)
        self.assertEqual([name for name, _, _ in self.read_service.calls], ["threads.list"])
        self.assertEqual(len(self.service.calls), 1)


class ImportTests(unittest.TestCase):
    def test_qt_free_lazy_and_ascii(self) -> None:
        import subprocess
        import sys

        code = ("import sys, briefing_reader.gmail; "
                "print(sorted({m.split('.')[0] for m in sys.modules "
                "if m.split('.')[0] in ('PySide6', 'googleapiclient', 'google_auth_oauthlib', "
                "'oauthlib', 'httplib2')}))")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parent.parent, timeout=60, check=True)
        self.assertEqual(result.stdout.strip(), "[]")
        self.assertTrue(Path(gmail.__file__).read_bytes().isascii())


class MailViewTests(unittest.TestCase):
    """gmail.mail_view: the message exactly as build_message writes it (the LIVE view's payload)."""

    def test_a_reply_reads_as_built(self) -> None:
        view = gmail.mail_view(reply(subject="Thursday noon meeting", cc=("cy@example.edu", "di@example.edu")))
        self.assertEqual((view.from_addr, view.to, view.cc, view.subject),
                         (ME, "ana@example.edu, ben@example.edu", "cy@example.edu, di@example.edu",
                          "Re: Thursday noon meeting"))
        self.assertEqual((view.in_reply_to, view.references, view.thread_id), (MSGID, MSGID, "18c0ffee00000001"))
        self.assertEqual(view.body, "Hi both,\nNoon works.\nThanks\n")
        parsed = parse(build_message(reply(subject="Thursday noon meeting", cc=("cy@example.edu", "di@example.edu"))))
        self.assertEqual(view.body, parsed.get_content().replace("\r\n", "\n"))
        self.assertEqual(view.subject, str(parsed["Subject"]))

    def test_a_new_email_and_unicode(self) -> None:
        body = "Bonjour \u00e9t\u00e9 \U0001F44B\n" + "long line " * 20
        view = gmail.mail_view(new_email(body=body, cc=()))
        self.assertEqual((view.to, view.cc, view.in_reply_to, view.references, view.thread_id),
                         ("office@example.edu", "", "", "", ""))
        self.assertEqual(view.body, body + "\n")
        self.assertNotIn("Bonjour", repr(view))
        self.assertNotIn("office", repr(view))

    def test_refused_like_build_message(self) -> None:
        for mail in (reply(to=()), reply(subject="=?utf-8?b?SGk=?="), reply(to=(ME,)), reply(body=" ")):
            with self.subTest(mail=mail.subject), self.assertRaises(MessageRefused):
                gmail.mail_view(mail)


# --------------------------------------------------------------------------
# The follow-up list's header scan (B5): metadata only, fixed requests
# --------------------------------------------------------------------------

class FakeGmailScan(FakeGmailRead):
    """threads().list / get plus getProfile and settings().sendAs().list (all read only)."""

    def getProfile(self, **kwargs: Any) -> FakeReadRequest:  # noqa: N802 - the API's name
        return FakeReadRequest(self, "getProfile", kwargs)

    def settings(self) -> FakeGmailScan:
        return self

    def sendAs(self) -> FakeGmailScan:  # noqa: N802 - the API's name
        return self

    def list(self, **kwargs: Any) -> FakeReadRequest:
        name = "sendAs.list" if "labelIds" not in kwargs and "q" not in kwargs else "threads.list"
        return FakeReadRequest(self, name, kwargs)


class ScanReaderTests(SenderTestCase):
    def reader(self) -> gmail.GmailReader:
        self.read_service = FakeGmailScan()
        account = GoogleAccount("work", client_secret_path=self.secret_path, data_dir=self.data_dir,
                                features=("calendar", "gmail_send", "gmail_read"), bindings=self.bindings,
                                flow_factory=lambda path, scopes: self, refresh=lambda creds: None)
        return gmail.GmailReader(account, service_factory=lambda creds: self.read_service)

    def ready(self, *, confirmed: bool = True) -> gmail.GmailReader:
        self.write_token(scopes=SCOPES + [READ_SCOPE], asked=SCOPES + [READ_SCOPE])
        self.bind(confirmed=confirmed)
        return self.reader()

    def test_scan_ids_send_exactly_the_fixed_requests(self) -> None:
        reader = self.ready()
        self.read_service.outcomes = [
            {"threads": [{"id": "18c0ffee00000001", "historyId": "901"}, {"id": "bad id!", "historyId": "1"},
                         {"id": "18c0ffee00000002", "historyId": "x9"}], "nextPageToken": "page2"},
            {"threads": [{"id": "18c0ffee00000003", "historyId": 903}, {"id": "18c0ffee00000001"}]},
            {"threads": [{"id": "18c0ffee00000004", "historyId": "904"}]}]
        with self.assertLogs(GMAIL_LOGGER, level="INFO") as logs:
            sent = reader.scan_ids(label="SENT", days=14, max_threads=50)
            inbox = reader.scan_ids(label="INBOX", days=30, max_threads=100)
        self.assertEqual(sent, [("18c0ffee00000001", "901"), ("18c0ffee00000002", ""), ("18c0ffee00000003", "903")])
        self.assertEqual(inbox, [("18c0ffee00000004", "904")])
        (_n1, first, retries), (_n2, second, _r2), (_n3, third, _r3) = self.read_service.calls
        self.assertEqual(first, {"userId": "me", "labelIds": ["SENT"], "q": "newer_than:14d", "maxResults": 50,
                                 "includeSpamTrash": False, "fields": "threads(id,historyId),nextPageToken"})
        self.assertEqual(retries, gmail.READ_RETRIES)
        self.assertEqual((second["pageToken"], second["maxResults"]), ("page2", 48))
        self.assertEqual(third["q"], "newer_than:14d -category:promotions -category:social -category:forums")
        self.assertEqual(third["labelIds"], ["INBOX"])   # days clamped to 14
        for _name, kwargs, _retries in self.read_service.calls:
            self.assertNotIn("snippet", kwargs["fields"])
        text = "\n".join(logs.output)
        self.assertIn("Gmail reading (work): a scan listed 3 thread(s)", text)
        self.assertNotIn("18c0ffee", text)
        self.assertNotIn("newer_than", text)
        with self.assertRaises(ValueError):
            reader.scan_ids(label="SPAM", days=14, max_threads=10)

    def test_paging_stops_at_the_cap_and_the_page_limit(self) -> None:
        reader = self.ready()
        page = {"threads": [{"id": f"18c0ffee{n:08d}", "historyId": str(n)} for n in range(100)],
                "nextPageToken": "more"}
        self.read_service.outcomes = [page, page, page]
        with self.assertLogs(GMAIL_LOGGER, level="INFO"):
            found = reader.scan_ids(label="INBOX", days=14, max_threads=300)
        self.assertEqual(len(self.read_service.calls), gmail.SCAN_MAX_PAGES)
        self.assertEqual(len(found), 100)   # the same ids twice: kept once
        self.read_service.calls.clear()
        self.read_service.outcomes = [page]
        with self.assertLogs(GMAIL_LOGGER, level="INFO"):
            self.assertEqual(len(reader.scan_ids(label="INBOX", days=14, max_threads=20)), 20)
        self.assertEqual(len(self.read_service.calls), 1)

    def test_thread_metadata_never_asks_for_a_body_or_a_snippet(self) -> None:
        reader = self.ready()
        self.read_service.outcomes = [{"id": "18c0ffee00000001", "historyId": "9", "messages": []}]
        self.assertEqual(reader.thread_metadata("18c0ffee00000001")["historyId"], "9")
        name, kwargs, _retries = self.read_service.calls[0]
        self.assertEqual((name, kwargs["format"], kwargs["metadataHeaders"], kwargs["fields"]),
                         ("threads.get", "metadata", list(gmail.SCAN_HEADERS),
                          "id,historyId,messages(id,labelIds,internalDate,payload/headers)"))
        for word in ("snippet", "sizeEstimate", "body", "parts", "raw"):
            self.assertNotIn(word, kwargs["fields"])
        with self.assertRaises(gmail.MailReadError):
            reader.thread_metadata("not an id")
        self.read_service.outcomes = [http_error(404, "Not Found", "notFound")]
        with self.assertLogs(GMAIL_LOGGER, level="INFO"), self.assertRaises(gmail.MailReadError) as caught:
            reader.thread_metadata("18c0ffee00000002")
        self.assertEqual(caught.exception.status, 404)

    def test_own_addresses_once_per_identity(self) -> None:
        reader = self.ready()
        self.read_service.outcomes = [{"emailAddress": "You@Example.edu"},
                                      {"sendAs": [{"sendAsEmail": "you@example.edu"},
                                                  {"sendAsEmail": "lab@example.org"},
                                                  {"sendAsEmail": "not an address"}]}]
        with self.assertLogs(GMAIL_LOGGER, level="INFO") as logs:
            own = reader.own_addresses()
        self.assertEqual(own, ("you@example.edu", "lab@example.org"))
        (first, profile, _r1), (second, send_as, _r2) = self.read_service.calls
        self.assertEqual((first, profile), ("getProfile", {"userId": "me", "fields": "emailAddress"}))
        self.assertEqual((second, send_as), ("sendAs.list", {"userId": "me", "fields": "sendAs(sendAsEmail)"}))
        self.assertEqual(reader.own_addresses(), own)   # cached for this identity: no second request
        self.assertEqual(len(self.read_service.calls), 2)
        text = "\n".join(logs.output)
        self.assertIn("Gmail reading (work): 2 own address(es)", text)
        self.assertNotIn("example", text)
        other = self.ready()
        self.read_service.outcomes = [http_error(500, "Backend Error")]
        with self.assertLogs(GMAIL_LOGGER, level="INFO"):
            self.assertEqual(other.own_addresses(), ())

    def test_confirmed_address_scan_problem_and_identity(self) -> None:
        unbound = self.reader()
        self.assertEqual((unbound.confirmed_address(), unbound.scan_problem(), unbound.identity_key()),
                         ("", gmail.SCAN_IDENTITY, ""))
        reader = self.ready(confirmed=False)
        self.assertEqual((reader.confirmed_address(), reader.scan_problem()), ("", gmail.SCAN_UNCONFIRMED))
        self.assertTrue(self.bindings.confirm("work", ME))
        self.assertEqual((reader.confirmed_address(), reader.scan_problem()), (ME, ""))
        self.assertEqual(reader.identity_key(), f"{SUB}|{ME}")
        self.write_token(scopes=SCOPES + [READ_SCOPE], sub="100000000000000000999")   # another Google account
        self.assertEqual((reader.confirmed_address(), reader.scan_problem()), ("", gmail.SCAN_IDENTITY))
        self.assertEqual(self.read_service.calls, [])   # files only

    def test_is_rate_limited(self) -> None:
        self.assertTrue(gmail.is_rate_limited(gmail.MailReadError("slow", status=429)))
        self.assertTrue(gmail.is_rate_limited(gmail.MailReadError("slow", status=403)))
        self.assertFalse(gmail.is_rate_limited(gmail.MailReadError("no", status=403, problem=PROBLEM_FAILED)))
        self.assertFalse(gmail.is_rate_limited(gmail.MailReadError("no", status=401, problem=PROBLEM_EXPIRED)))
        self.assertFalse(gmail.is_rate_limited(gmail.MailReadError("down", status=500)))
        self.assertFalse(gmail.is_rate_limited(ValueError("x")))
        reader = self.ready()
        self.read_service.outcomes = [http_error(403, "Rate", "userRateLimitExceeded")]
        with self.assertLogs(GMAIL_LOGGER, level="INFO"), self.assertRaises(gmail.MailReadError) as caught:
            reader.scan_ids(label="INBOX", days=14, max_threads=10)
        self.assertTrue(gmail.is_rate_limited(caught.exception))
        self.assertEqual(reader.available(), ("", ""))   # a rate limit never turns reading off

    def test_a_refused_own_address_read_stops_the_scan_until_the_next_sign_in(self) -> None:
        from briefing_reader.config import ProductivityConfig
        from briefing_reader.followups import PROBLEM_REFUSED, PROBLEM_STOPPED, MailScanner

        reader = self.ready()
        scanner = MailScanner(sleep=lambda _s: None)
        settings = ProductivityConfig(enabled=True)

        def scan(counter: int) -> Any:
            return scanner.scan("work", reader, now=datetime.now(timezone.utc), settings=settings,
                                known=lambda _a: False, hidden=lambda _k: False, cancel=threading.Event(),
                                stop_key=f"{reader.identity_key()}|{counter}")

        self.read_service.outcomes = [http_error(403, "Forbidden")]     # e.g. a Workspace admin's block
        with self.assertLogs(level="INFO"):
            first = scan(0)
        self.assertEqual((first.report.problem, first.report.partial), (PROBLEM_REFUSED, True))
        self.assertTrue(scanner.stopped("work"))
        self.assertEqual([name for name, _kwargs, _r in self.read_service.calls], ["getProfile"])
        for _refresh in range(2):
            reader._account.generation += 1                               # a token refresh (hourly)
            self.assertEqual(scan(0).report.problem, PROBLEM_STOPPED)
        self.assertEqual(len(self.read_service.calls), 1)                # the refused read is never asked again
        self.read_service.outcomes = [{"emailAddress": ME}, {"sendAs": []}, {"threads": []}, {"threads": []}]
        with self.assertLogs(level="INFO"):
            after = scan(1)                                               # a new sign-in for the account
        self.assertEqual((after.report.problem, after.report.partial), ("", False))
        self.assertFalse(scanner.stopped("work"))
        other = self.ready()
        self.read_service.outcomes = [http_error(403, "Rate", "userRateLimitExceeded")]
        with self.assertLogs(GMAIL_LOGGER, level="INFO"):
            self.assertEqual(other.own_addresses(), ())                   # a rate limit is not a refusal


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
