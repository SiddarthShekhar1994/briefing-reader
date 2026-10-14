"""Sending the replies and emails you approve, through the Gmail API (gmail.send only), and reading
the threads an Ask is about (gmail.readonly only; GmailReader, a separate client).

    OutgoingMail(...)            one message, exactly as it will be sent
    build_message(mail)          the RFC 5322 message (an email.message.EmailMessage), checked
    mail_view(mail)              MailView: that message's headers and text as they read (the LIVE
                                 view shows exactly what will be sent)
    encode_raw(message)          base64url text for users.messages.send
    thread_link(address, id)     the sent message's thread in Gmail (the card's Open link)
    GmailSender(account)         one account's sending: ready(), sign_in(), send(mail) -> SentMail
    GmailReader(account)         Ask Jarvis's reading: available(), search(query), thread(id);
                                 never signs in, never sends, never changes or deletes mail. The
                                 productivity bundle's follow-up list reads headers only through
                                 scan_ids(label=, days=, max_threads=), thread_metadata(id) and
                                 own_addresses() (field masks without a snippet or a body), and asks
                                 confirmed_address() / scan_problem() / identity_key() (files only)

What build_message refuses (GmailError, before anything goes anywhere):

- recipients: To and Cc together name 1 to MAX_RECIPIENTS addresses, at least one in To, each a
  plain address as actions.email_address writes it (no display name: a name never decides who
  an address is), no repeats, never the sender's own mailbox (recipients.same_mailbox: case,
  spaces, a "+tag" or Gmail's dots aside); there is never a Bcc header and never an attachment
  (one text/plain part).
- headers: a line break of any kind (CR, LF, U+0085, U+2028, U+2029, VT, FF), a control or
  invisible format character, an RFC 2047 encoded word ("=?...?=") in the subject (Python and
  mail apps decode it, so recipients would see other text than the card shows), a subject over
  250 characters (254 for a reply, whose "Re: " comes on top, as the briefing's subject= allows);
  In-Reply-To / References must be Message-IDs "<left@right>" of printable ASCII without spaces.
- the finished message is parsed back and must say exactly what was asked: the same From, To,
  Cc, Subject, In-Reply-To and References, no other header that names people (Bcc, Reply-To,
  Sender, Resent-*), one text/plain part whose text is the body.

A reply (``thread_id``) keeps a "Re: " subject, so Gmail files it in the thread, and carries
In-Reply-To / References when the line gave the Message-ID.

``GmailSender.send`` makes one users.messages.send call (num_retries=0) through
google_auth.single_send_http, so one approval sends at most one message: a 5xx answer, or no
answer after the request went out, is GmailUnknownOutcome (it may have been sent; never
retried). It never opens the browser and refuses unless the saved sign-in is the Google account
bound to the alias (GoogleAccount.identity_confirmed), you confirmed that binding
(GoogleAccount.pending_confirmation is "": PROBLEM_CONFIRM otherwise), ``from_addr`` is that
account's address and no recipient is that account's own mailbox. Logs carry the alias
("work", "personal", else "other"), Gmail's message id, recipient counts, HTTP statuses and
Google's reason codes only; never addresses, subjects or text, and error messages never quote
an address.

A Gmail answer never signs the account out of its other features: the saved sign-in is shared
with the alias's calendar, so only a 401 (or a refresh Google rejects: invalid_grant) deletes it.
A 403 that says sending is not allowed, an administrator block or any other 403 turns sending off
for this alias until its next sign-in through Send (this run only); the calendar keeps working.

Qt-free; the Google libraries are imported lazily.
"""

from __future__ import annotations

import base64
import email.policy
import json
import logging
import re
import socket
import ssl
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Any
from urllib.parse import quote

from .actions import MAX_RECIPIENTS, email_address
from .config import redact
from .google_auth import (
    GMAIL_FEATURE,
    GMAIL_READ_FEATURE,
    PROBLEM_BLOCKED,
    PROBLEM_CONFIRM,
    PROBLEM_DENIED,
    PROBLEM_EXPIRED,
    PROBLEM_FAILED,
    PROBLEM_IDENTITY,
    PROBLEM_SCOPE,
    PROBLEM_SETUP,
    PROBLEM_SIGNED_OUT,
    PROBLEM_TIMEOUT,
    AccountAuthError,
    AccountError,
    AccountNotSignedIn,
    AccountSetupError,
    GoogleAccount,
    blocked_code,
    google_libraries_available,
    logged_alias,
    single_send_http,
)
from .google_auth import _register_credentials as _register_credentials  # noqa: PLC0414
from .recipients import own_recipients

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_S = 30        # per request; a send that gets no answer in time is an unknown outcome
SUBJECT_CAP = 250
BODY_CAP = 5000
SEND_SETUP_HINT = "Set up email sending: README step 8b"
NOTHING_SENT = "nothing was sent"
UNKNOWN_SENT = ("the message may or may not have been sent - check Sent mail before retrying")
OWN_ADDRESS_REFUSED = "The message would go to the sending account's own address"
GMAIL_THREAD_LINK = "https://mail.google.com/mail/?authuser={user}#all/{thread}"
GMAIL_DEFAULT_LINK = "https://mail.google.com/mail/#all/{thread}"

_POLICY = email.policy.SMTP           # CRLF line ends, folded at 78 characters
_READ_POLICY = email.policy.default
_GMAIL_ID_RE = re.compile(r"[0-9A-Za-z_-]{6,64}")
_MSGID_PART = r"[\x21-\x3b\x3d\x3f\x41-\x7b\x7d\x7e]{1,250}"   # printable ASCII but < > @ |
_MSGID_RE = re.compile(rf"<{_MSGID_PART}@{_MSGID_PART}>")
_REFERENCES_CAP = 900
# Any line break or control character (a header is one line; tabs are not used in ours either).
_HEADER_BAD_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")
# In the body: control characters but line feed and tab, and the Unicode line separators.
_BODY_BAD_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")
_ZERO_WIDTH_JOINER = "\u200d"       # kept inside combined emoji
# Headers that name people: only From / To / Cc may be in what Jarvis sends.
_PEOPLE_HEADERS = frozenset({"bcc", "reply-to", "sender", "resent-from", "resent-to", "resent-cc",
                             "resent-bcc", "resent-sender", "disposition-notification-to", "return-path"})
_EXPECTED_HEADERS = frozenset({"from", "to", "cc", "subject", "in-reply-to", "references", "content-type",
                               "content-transfer-encoding", "mime-version"})
_REPLY_PREFIX = "Re: "
_MAX_MESSAGE_LEN = 200
_ADDRESS_LIKE_RE = re.compile(r"[^\s<>()\[\]\"',;:]+@[^\s<>()\[\]\"',;:]+")
_RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded",
                                 "dailyLimitExceeded", "RATE_LIMIT_EXCEEDED", "RESOURCE_EXHAUSTED"})
_SETUP_REASONS = frozenset({"accessNotConfigured", "SERVICE_DISABLED"})
_SCOPE_REASONS = frozenset({"ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficientPermissions"})
# 403 reasons that mean the account's administrator turned Gmail off for apps like this one.
_BLOCKED_REASONS = ("domainPolicy",)
# Failures that certainly happened before the request reached Google (nothing was sent).
_NOT_SENT_ERRORS: tuple[type[BaseException], ...] = (socket.gaierror, ConnectionRefusedError,
                                                     ssl.SSLCertVerificationError)
_NOT_SENT_NAMES = frozenset({"ServerNotFoundError", "TransportError"})


# --------------------------------------------------------------------------
# Errors and data
# --------------------------------------------------------------------------

class GmailError(Exception):
    """Why a message was not sent; the message is safe to show and log (no address, subject,
    text or token). ``problem`` is google_auth's PROBLEM_* of a sign-in or setup problem."""

    def __init__(self, message: str = "", *, status: int | None = None, problem: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.problem = problem


class MessageRefused(GmailError):
    """build_message would not send this message as it stands (nothing was sent)."""


class GmailSetupError(GmailError):
    """No usable OAuth client file or Google packages, or the Gmail API is off for the project."""


class GmailAuthError(GmailError):
    """The account cannot send now: not signed in, sending not allowed, blocked by its
    administrator, expired, or the sign-in is not the account bound to the alias."""


class GmailUnknownOutcome(GmailError):
    """The send went out but no clear answer came back (5xx, timeout, connection lost): it may or
    may not have been sent. Never retried by itself."""


@dataclass(frozen=True)
class OutgoingMail:
    """One message exactly as it will be sent. Addresses are plain and normalized
    (actions.email_address); ``body`` is the text with "\\n" line breaks."""

    account: str                       # the alias it is sent from ("work")
    from_addr: str                     # the account's own address ("" = no From header; Gmail fills it)
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    body: str
    thread_id: str = ""                # Gmail's thread id for a reply ("" for a new email)
    in_reply_to: str = ""              # "<left@right>" of the message answered ("" when unknown)
    references: str = ""               # "" = the same as in_reply_to

    @property
    def recipient_count(self) -> int:
        return len(self.to) + len(self.cc)


@dataclass(frozen=True)
class SentMail:
    message_id: str                    # Gmail's id of the sent message
    thread_id: str
    link: str                          # the thread in Gmail ("" when Gmail's answer had no usable id)


# --------------------------------------------------------------------------
# The message
# --------------------------------------------------------------------------

def build_message(mail: OutgoingMail) -> EmailMessage:
    """The message ``mail`` describes, checked (see the module docs); MessageRefused otherwise."""
    to, cc = _recipients(mail)
    from_addr = mail.from_addr
    if from_addr and email_address(from_addr) != from_addr:
        raise MessageRefused("The sender is not a plain email address")
    if from_addr and own_recipients(to + cc, from_addr):
        raise MessageRefused(OWN_ADDRESS_REFUSED)
    thread_id = mail.thread_id
    if thread_id and not _GMAIL_ID_RE.fullmatch(thread_id):
        raise MessageRefused("The Gmail thread id is not valid")
    subject = _subject(mail.subject, reply=bool(thread_id))
    in_reply_to, references = _threading_headers(mail.in_reply_to, mail.references)
    body = _body(mail.body)

    message = EmailMessage(policy=_POLICY)
    try:
        if from_addr:
            message["From"] = from_addr
        message["To"] = ", ".join(to)
        if cc:
            message["Cc"] = ", ".join(cc)
        message["Subject"] = subject
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            message["References"] = references
        lines = body.split("\n")
        cte = "7bit" if body.isascii() and max(len(line) for line in lines) <= 78 else "quoted-printable"
        message.set_content(body, subtype="plain", charset="utf-8", cte=cte)
    except (ValueError, TypeError) as exc:   # the email package refused a value we did not catch
        raise MessageRefused(f"The message could not be put together ({type(exc).__name__})") from None
    _check_round_trip(message, from_addr=from_addr, to=to, cc=cc, subject=subject,
                      in_reply_to=in_reply_to, references=references, body=body)
    return message


@dataclass(frozen=True)
class MailView:
    """One message exactly as build_message writes it, read back from the built message (the LIVE
    view's "Will send exactly this"): the headers as they read (the subject with its "Re: ", To and
    Cc joined) and the text part's content. Shown on screen only; never logged."""

    from_addr: str
    to: str
    cc: str
    subject: str
    in_reply_to: str
    references: str
    thread_id: str                     # Gmail's thread (the send request's threadId, not a header)
    body: str

    def __repr__(self) -> str:   # names nobody and quotes nothing
        return f"<MailView: {len(self.body)} chars>"


def mail_view(mail: OutgoingMail) -> MailView:
    """``mail`` as build_message writes it (pure; MessageRefused like build_message)."""
    message = build_message(mail)

    def header(name: str) -> str:
        value = message.get(name)
        return " ".join(str(value).split()) if value is not None else ""

    return MailView(from_addr=header("From"), to=header("To"), cc=header("Cc"), subject=header("Subject"),
                    in_reply_to=header("In-Reply-To"), references=header("References"), thread_id=mail.thread_id,
                    body=message.get_content().replace("\r\n", "\n"))


def encode_raw(message: EmailMessage) -> str:
    """The message as users.messages.send takes it ("raw"): base64url of its RFC 5322 bytes."""
    return base64.urlsafe_b64encode(message.as_bytes(policy=_POLICY)).decode("ascii")


def thread_link(address: str, thread_id: str) -> str:
    """The thread in Gmail, opened as ``address`` when that is a plain address ("" without a
    usable thread id)."""
    if not isinstance(thread_id, str) or not _GMAIL_ID_RE.fullmatch(thread_id):
        return ""
    if address and email_address(address) == address:
        return GMAIL_THREAD_LINK.format(user=quote(address, safe="@"), thread=thread_id)
    return GMAIL_DEFAULT_LINK.format(thread=thread_id)


def _recipients(mail: OutgoingMail) -> tuple[tuple[str, ...], tuple[str, ...]]:
    to, cc = tuple(mail.to), tuple(mail.cc)
    if not to:
        raise MessageRefused("The message has nobody in To")
    count = len(to) + len(cc)
    if count > MAX_RECIPIENTS:
        raise MessageRefused(f"The message names {count} recipients (at most {MAX_RECIPIENTS})")
    seen: set[str] = set()
    for address in to + cc:
        if not isinstance(address, str) or email_address(address) != address:
            raise MessageRefused("A recipient is not a plain email address")
        if address.casefold() in seen:
            raise MessageRefused("A recipient is named twice")
        seen.add(address.casefold())
    return to, cc


def _subject(subject: Any, *, reply: bool) -> str:
    if not isinstance(subject, str):
        raise MessageRefused("The subject must be text")
    if _HEADER_BAD_RE.search(subject):
        raise MessageRefused("The subject has a line break or a control character")
    if _has_format_character(subject, allow_joiner=False):
        raise MessageRefused("The subject has an invisible format character")
    start = subject.find("=?")
    if start >= 0 and subject.find("?=", start + 2) >= 0:
        raise MessageRefused('The subject contains "=?...?=" (an encoded word), which mail apps show '
                             "as other text")
    subject = " ".join(subject.split())
    if reply and not subject.casefold().startswith("re:"):
        subject = _REPLY_PREFIX + subject
    if not subject:
        raise MessageRefused("The subject is empty")
    cap = subject_cap(reply=reply)
    if len(subject) > cap:
        raise MessageRefused(f"The subject is too long ({len(subject)} characters, at most {cap})")
    return subject


def subject_cap(*, reply: bool) -> int:
    """The longest subject Jarvis sends: SUBJECT_CAP, plus the "Re: " a reply's subject gets."""
    return SUBJECT_CAP + len(_REPLY_PREFIX) if reply else SUBJECT_CAP


def _threading_headers(in_reply_to: Any, references: Any) -> tuple[str, str]:
    if not in_reply_to:
        if references:
            raise MessageRefused("References without In-Reply-To")
        return "", ""
    if not isinstance(in_reply_to, str) or not _MSGID_RE.fullmatch(in_reply_to):
        raise MessageRefused("In-Reply-To is not a Message-ID")
    references = references or in_reply_to
    if (not isinstance(references, str) or len(references) > _REFERENCES_CAP
            or not all(_MSGID_RE.fullmatch(part) for part in references.split(" "))):
        raise MessageRefused("References is not a list of Message-IDs")
    return in_reply_to, references


def _body(body: Any) -> str:
    if not isinstance(body, str):
        raise MessageRefused("The message must be text")
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    if _BODY_BAD_RE.search(body):
        raise MessageRefused("The message has a control character")
    if _has_format_character(body, allow_joiner=True):
        raise MessageRefused("The message has an invisible format character")
    if not body.strip():
        raise MessageRefused("The message is empty")
    if len(body) > BODY_CAP:
        raise MessageRefused(f"The message is too long ({len(body)} characters, at most {BODY_CAP})")
    return body


def _has_format_character(text: str, *, allow_joiner: bool) -> bool:
    """Unicode format characters (direction overrides, zero-width spaces, tags) change how a text
    reads without showing; a zero-width joiner inside a combined emoji is allowed in a body."""
    if text.isascii():
        return False
    return any(unicodedata.category(char) == "Cf" and not (allow_joiner and char == _ZERO_WIDTH_JOINER)
               for char in text)


def _check_round_trip(message: EmailMessage, *, from_addr: str, to: tuple[str, ...], cc: tuple[str, ...],
                      subject: str, in_reply_to: str, references: str, body: str) -> None:
    """Parse the finished bytes back: they must say exactly what was asked, nothing more."""
    try:
        parsed = BytesParser(policy=_READ_POLICY).parsebytes(message.as_bytes(policy=_POLICY))
        names = [name.casefold() for name in parsed.keys()]
        if len(names) != len(set(names)) or not set(names) <= _EXPECTED_HEADERS or set(names) & _PEOPLE_HEADERS:
            raise MessageRefused("The message would carry other headers than the card shows")
        if _addresses(parsed, "From") != ((from_addr,) if from_addr else ()) \
                or _addresses(parsed, "To") != to or _addresses(parsed, "Cc") != cc:
            raise MessageRefused("The message would go to other people than the card shows")
        if str(parsed.get("Subject", "")) != subject:
            raise MessageRefused("The message would carry another subject than the card shows")
        if str(parsed.get("In-Reply-To", "")).strip() != in_reply_to \
                or str(parsed.get("References", "")).strip() != references:
            raise MessageRefused("The message would carry other thread headers than the card shows")
        if parsed.is_multipart() or parsed.get_content_type() != "text/plain":
            raise MessageRefused("The message would not be one plain-text part")
        text = parsed.get_content().replace("\r\n", "\n")
        if text != (body if body.endswith("\n") else body + "\n"):
            raise MessageRefused("The message would carry other text than the card shows")
    except MessageRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - anything odd here means: do not send
        raise MessageRefused(f"The message could not be checked ({type(exc).__name__})") from None


def _addresses(parsed: Any, name: str) -> tuple[str, ...]:
    header = parsed.get(name)
    if header is None:
        return ()
    found = []
    for address in getattr(header, "addresses", ()):
        if address.display_name:
            raise MessageRefused("A recipient would carry a display name")
        found.append(address.addr_spec)
    return tuple(found)


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------

class GmailSender:
    """Sending for one account alias (one GoogleAccount, shared with its calendar). Blocking: use
    a worker thread. ``ready``, ``is_signed_in`` and ``from_address`` read files only (no network,
    no lock), so the UI thread may call them; ``sign_in`` and ``send`` are serialised."""

    def __init__(self, account: GoogleAccount, *,
                 service_factory: Callable[[Any], Any] | None = None) -> None:
        self._account = account
        self._service_factory = service_factory or _build_service
        self._lock = threading.RLock()
        self._service: Any = None
        self._service_creds: Any = None
        self._service_generation = -1
        self._blocked = ""                 # an administrator block Gmail answered with (this run)
        # (PROBLEM_*, message) of another 403 Gmail answered (this run; until the next sign-in here).
        self._refused: tuple[str, str] = ("", "")
        self.log_name = f"Gmail ({logged_alias(account.alias)})"

    @property
    def account(self) -> GoogleAccount:
        return self._account

    @property
    def alias(self) -> str:
        return self._account.alias

    # ---- state without the network -------------------------------------------------------

    def is_configured(self) -> bool:
        return self._account.is_configured()

    def is_signed_in(self) -> bool:
        """A usable saved sign-in that allows sending and is the bound Google account."""
        return self._account.is_signed_in(GMAIL_FEATURE) and self._account.identity_confirmed()

    def from_address(self) -> str:
        """The address mail from this alias goes out from: the bound account's ("" while Jarvis
        does not know which Google account the alias is). Shown on cards, never logged."""
        return self._account.bound_email()

    def sign_in_problem(self) -> tuple[str, str]:
        if self._blocked:
            return PROBLEM_BLOCKED, self.blocked_message()
        return self._account.problem()

    def blocked_message(self) -> str:
        code = self._blocked or "admin_policy_enforced"
        return (f"The {self.alias} account's administrator does not allow this app to send email ({code}); "
                f"{NOTHING_SENT}")

    def ready(self) -> tuple[str, str]:
        """("", "") when this alias can send now; else (google_auth PROBLEM_*, why). No network.

        PROBLEM_SETUP: no client file or packages. PROBLEM_BLOCKED: the administrator blocks the app
        (or its Gmail access). PROBLEM_SIGNED_OUT / EXPIRED / DENIED / TIMEOUT / FAILED: no usable
        sign-in (PROBLEM_SIGNED_OUT also while the sign-in never asked for sending: a token of an
        older version, or "gmail_send" added since). PROBLEM_SCOPE: signed in, but sending was not
        allowed (a box was unticked, or Gmail answered so). PROBLEM_IDENTITY: Jarvis does not know
        (or a sign-in picked another) Google account. PROBLEM_CONFIRM: signed in and bound, but
        you have not confirmed yet that it is the right Google account for this alias.
        PROBLEM_FAILED: Gmail refused access (403).
        """
        if not self.is_configured():
            return PROBLEM_SETUP, f"{SEND_SETUP_HINT} (the OAuth client file was not found)"
        if not google_libraries_available():
            return PROBLEM_SETUP, (f"{SEND_SETUP_HINT} (the Google packages are missing: "
                                   "py -3.13 -m pip install -r requirements.txt)")
        if self._blocked:
            return PROBLEM_BLOCKED, self.blocked_message()
        problem, message = self._account.problem()
        if not self._account.is_signed_in():
            if problem in (PROBLEM_EXPIRED, PROBLEM_BLOCKED, PROBLEM_DENIED, PROBLEM_TIMEOUT,
                           PROBLEM_IDENTITY, PROBLEM_FAILED) and message:
                return problem, message
            return PROBLEM_SIGNED_OUT, f"Not signed in to the {self.alias} account"
        if self._refused[0]:
            return self._refused
        if GMAIL_FEATURE not in self._account.granted_features():
            if problem in (PROBLEM_BLOCKED, PROBLEM_DENIED, PROBLEM_TIMEOUT, PROBLEM_IDENTITY) and message:
                return problem, message   # the sign-in that would have added sending failed
            if GMAIL_FEATURE in self._account.refused_features():
                return PROBLEM_SCOPE, self._account.missing_message(GMAIL_FEATURE)
            if not self._account.identity_confirmed():   # e.g. the token of an older version
                return PROBLEM_IDENTITY, self._unconfirmed_message()
            return PROBLEM_SIGNED_OUT, (f"Sending email needs one more Google sign-in for the {self.alias} "
                                        "account")
        if not self._account.identity_confirmed():
            if problem == PROBLEM_IDENTITY and message:
                return problem, message
            return PROBLEM_IDENTITY, self._unconfirmed_message()
        if self._account.pending_confirmation():
            return PROBLEM_CONFIRM, self.confirm_message()
        return "", ""

    def _unconfirmed_message(self) -> str:
        return (f"Jarvis doesn't know yet which Google account the {self.alias} account is - sign in again "
                "to confirm it")

    def confirm_message(self) -> str:
        """Why nothing is sent while the binding is not confirmed (no address in it)."""
        return (f"You haven't confirmed yet that this is the right Google account for the {self.alias} "
                "account")

    # ---- the first sign-in's question: is this the right Google account? ----------------

    def pending_confirmation(self) -> str:
        """The bound account's address while you have not confirmed it for this alias ("" when
        confirmed or not bound). Nothing is sent meanwhile. No network, no lock."""
        return self._account.pending_confirmation()

    def confirm_account(self, address: str) -> bool:
        """You answered Yes: ``address`` is this alias's Google account (kept in accounts.json).
        False when the alias is not bound to that address (any more)."""
        return self._account.confirm_binding(address)

    def change_problem(self) -> tuple[str, str]:
        """google_auth.GoogleAccount.change_problem (fails closed; no network, no lock)."""
        return self._account.change_problem()

    def needs_confirmation(self) -> bool:
        """The alias has no binding you confirmed: its next sign-in leaves one to confirm first."""
        return self._account.needs_confirmation()

    def disconnect(self) -> None:
        """You answered "No, use another account": forget the saved sign-in and the binding (the
        next sign-in may pick any account and asks again). Raises google_auth.AccountError, the
        binding kept, when the saved sign-in could not be deleted."""
        with self._lock:
            try:
                self._account.disconnect()
            finally:
                self._service = None
                self._blocked = ""
                self._refused = ("", "")

    # ---- the browser ---------------------------------------------------------------------

    def sign_in(self) -> None:
        """Google's sign-in in the browser for this alias (every feature's scopes and the
        account's identity). Raises GmailSetupError / GmailAuthError / GmailError; afterwards
        sending must be allowed and the account known, else GmailAuthError says why."""
        with self._lock:
            if not google_libraries_available():
                raise GmailSetupError(self.ready()[1], problem=PROBLEM_SETUP)
            with _account_errors():
                self._account.sign_in()
            self._service = None
            self._blocked = ""
            self._refused = ("", "")
            if GMAIL_FEATURE not in self._account.granted_features():
                problem, message = self._account.problem()
                raise GmailAuthError(message or self._account.missing_message(GMAIL_FEATURE),
                                     problem=problem or PROBLEM_SCOPE)
            if not self._account.identity_confirmed():
                raise GmailAuthError(f"Google did not say which account the {self.alias} account is; "
                                     "sign in again", problem=PROBLEM_IDENTITY)

    # ---- sending -------------------------------------------------------------------------

    def send(self, mail: OutgoingMail) -> SentMail:
        """Send ``mail`` once (see the module docs). Raises MessageRefused (nothing sent: the
        message as it stands), GmailSetupError / GmailAuthError / GmailError (nothing sent) or
        GmailUnknownOutcome (it may have been sent)."""
        with self._lock:
            if mail.account != self.alias:
                raise GmailError(f"This message belongs to another account; {NOTHING_SENT}")
            raw = encode_raw(build_message(mail))
            problem, message = self.ready()
            if problem:
                error = GmailSetupError if problem == PROBLEM_SETUP else GmailAuthError
                raise error(f"{message}; {NOTHING_SENT}" if NOTHING_SENT not in message else message,
                            problem=problem)
            bound = self._account.bound_email()
            if not mail.from_addr or mail.from_addr.casefold() != bound.casefold():
                raise GmailAuthError(f"The {self.alias} account's address is not the one on the card; "
                                     f"{NOTHING_SENT}", problem=PROBLEM_IDENTITY)
            if own_recipients(mail.to + mail.cc, bound):   # build_message checked from_addr: and the account?
                raise MessageRefused(OWN_ADDRESS_REFUSED)
            service = self._get_service()
            body: dict[str, Any] = {"raw": raw}
            if mail.thread_id:
                body["threadId"] = mail.thread_id
            try:
                result = self._execute(service.users().messages().send(userId="me", body=body))
            except GmailError as exc:
                logger.warning("%s: sending failed (%s%s)", self.log_name, type(exc).__name__,
                               f", HTTP {exc.status}" if exc.status else "")
                raise
            result = result if isinstance(result, dict) else {}
            message_id = str(result.get("id") or "")
            thread_id = str(result.get("threadId") or "")
            if not _GMAIL_ID_RE.fullmatch(message_id):
                message_id = ""
            if not _GMAIL_ID_RE.fullmatch(thread_id):
                thread_id = ""
            logger.info("%s: sent message %s (%d recipient(s))", self.log_name, message_id or "(no id returned)",
                        mail.recipient_count)
            return SentMail(message_id=message_id, thread_id=thread_id,
                            link=thread_link(mail.from_addr, thread_id or mail.thread_id))

    def _get_service(self) -> Any:
        with _account_errors():
            creds = self._account.credentials(interactive=False, need=GMAIL_FEATURE)
        if self._service is None or creds is not self._service_creds \
                or self._service_generation != self._account.generation:
            try:
                self._service = self._service_factory(creds)
            except Exception as exc:  # noqa: BLE001 - mapped to a safe message
                raise GmailError(f"Could not start the Gmail client ({type(exc).__name__}); {NOTHING_SENT}") from None
            self._service_creds = creds
            self._service_generation = self._account.generation
        return self._service

    def _execute(self, request: Any) -> Any:
        """The one send: never retried; every failure becomes a GmailError (see _http_error)."""
        from googleapiclient.errors import HttpError

        try:
            result = request.execute(num_retries=0)
        except HttpError as exc:
            raise self._http_error(exc) from None
        except Exception as exc:  # noqa: BLE001 - socket/ssl/httplib2 errors share no base
            raise self._transport_error(exc) from None
        if self._service_creds is not None:
            _register_credentials(self._service_creds)   # the library may have refreshed the token
        return result

    def _http_error(self, exc: Any) -> GmailError:
        status = _status_of(exc)
        message = _safe_message(exc)
        detail = f"{status}: {message}" if message else str(status)
        reasons = _error_reasons(exc)
        logger.info("%s: Gmail answered HTTP %d (%s)", self.log_name, status,
                    ", ".join(sorted(reason for reason in reasons if re.fullmatch(r"[A-Za-z_]{1,40}", reason)))
                    or "no reason code")
        if status == 401:
            text = f"Gmail refused the {self.alias} account's sign-in ({detail}); {NOTHING_SENT} - sign in again"
            self._account.forget("Gmail answered 401", problem=PROBLEM_EXPIRED, message=text)
            return GmailAuthError(text, status=status, problem=PROBLEM_EXPIRED)
        if status == 403:
            if reasons & _SETUP_REASONS:
                return GmailSetupError(f"{SEND_SETUP_HINT}: the Gmail API is not turned on for the OAuth "
                                       f"client's project ({detail}); {NOTHING_SENT}", status=status,
                                       problem=PROBLEM_SETUP)
            code = blocked_code(f"{message} {' '.join(sorted(reasons))}")
            code = code or next((reason for reason in _BLOCKED_REASONS if reason in reasons), "")
            if code:
                self._blocked = code
                return GmailAuthError(self.blocked_message(), status=status, problem=PROBLEM_BLOCKED)
            if reasons & _RATE_LIMIT_REASONS:
                return GmailError(f"Gmail is limiting requests right now ({detail}); {NOTHING_SENT} - "
                                  "try again later", status=status)
            # The saved sign-in is shared with the calendar: keep it, and turn sending off until
            # the next sign-in through Send.
            if reasons & _SCOPE_REASONS:
                self._refused = (PROBLEM_SCOPE, self._account.missing_message(GMAIL_FEATURE))
                logger.warning("%s: Gmail does not allow sending with this sign-in; sending is off until "
                               "the next sign-in (the sign-in is kept)", self.log_name)
                return GmailAuthError(f"{self._refused[1]}; {NOTHING_SENT}", status=status, problem=PROBLEM_SCOPE)
            self._refused = (PROBLEM_FAILED, f"Gmail refused access for the {self.alias} account ({detail})")
            logger.warning("%s: Gmail refused access; sending is off until the next sign-in (the sign-in is "
                           "kept)", self.log_name)
            return GmailAuthError(f"{self._refused[1]}; {NOTHING_SENT} - sign in again", status=status,
                                  problem=PROBLEM_FAILED)
        if status == 429:
            return GmailError(f"Gmail is limiting requests right now ({detail}); {NOTHING_SENT} - try again later",
                              status=status)
        if status >= 500:
            return GmailUnknownOutcome(f"Gmail answered {detail} while sending; {UNKNOWN_SENT}", status=status)
        return GmailError(f"Gmail refused the message ({detail}); {NOTHING_SENT}", status=status)

    def _transport_error(self, exc: Exception) -> GmailError:
        """A non-HTTP failure: a token refresh Google rejected (before sending), or the network."""
        from google.auth.exceptions import RefreshError

        if isinstance(exc, RefreshError):
            if exc.retryable:
                return GmailError(f"Google could not refresh the sign-in just now; {NOTHING_SENT} - try again")
            error = self._account.rejected(str(exc))
            return GmailAuthError(f"{error}; {NOTHING_SENT}", problem=error.problem)
        name = type(exc).__name__
        if isinstance(exc, _NOT_SENT_ERRORS) or name in _NOT_SENT_NAMES:
            return GmailError(f"Could not reach Gmail ({name}); {NOTHING_SENT}")
        return GmailUnknownOutcome(f"No answer from Gmail while sending ({name}); {UNKNOWN_SENT}")


# --------------------------------------------------------------------------
# Reading (Ask Jarvis): gmail.readonly only, never the sending path
# --------------------------------------------------------------------------

READ_RETRIES = 2              # reads are safe to retry
MAX_SEARCH_THREADS = 3
# The follow-up list's header scan (the productivity bundle): the labels it lists, the categories the
# inbox leaves out (Updates stay: a known correspondent's mail there is listed), the only headers it
# reads and the field masks of its requests: never a snippet, a size or a body.
SCAN_LABELS = ("INBOX", "SENT")
SCAN_INBOX_EXCLUDE = "-category:promotions -category:social -category:forums"
SCAN_HEADERS = ("From", "To", "Cc", "Reply-To", "Subject", "List-Id", "List-Unsubscribe", "Precedence",
                "Auto-Submitted")
SCAN_PAGE = 100
SCAN_MAX_PAGES = 2            # per label
SCAN_DAYS_MAX = 14
SCAN_THREADS_MAX = 300
SCAN_LIST_FIELDS = "threads(id,historyId),nextPageToken"
SCAN_GET_FIELDS = "id,historyId,messages(id,labelIds,internalDate,payload/headers)"
OWN_PROFILE_FIELDS = "emailAddress"
OWN_SEND_AS_FIELDS = "sendAs(sendAsEmail)"
SCAN_UNCONFIRMED = "unconfirmed"   # the binding is not confirmed yet: AppController._ask_account fixes it
SCAN_IDENTITY = "identity"         # the saved sign-in names no (or another) Google account: sign in again
_HISTORY_RE = re.compile(r"\d{1,30}")


def is_rate_limited(error: BaseException) -> bool:
    """Gmail asked Jarvis to slow down: a MailReadError with status 429, or a 403 without a sign-in
    problem (the rate-limit path of GmailReader._http_error)."""
    if not isinstance(error, MailReadError):
        return False
    return error.status == 429 or (error.status == 403 and not error.problem)


class MailReadError(GmailError):
    """Reading for Ask failed; nothing was changed (the message names no address or text)."""


class GmailReader:
    """Reading the threads an Ask is about, for one account alias (the "gmail_read" feature:
    gmail.readonly). It shares the alias's GoogleAccount (one sign-in) but never its Gmail client:
    GmailSender sends with its own, and nothing here can send, change or delete mail.

    ``available`` reads files only (no network, no lock); ``search`` and ``thread`` never sign in
    (a missing sign-in or permission is MailReadError with the google_auth PROBLEM_*) and never
    delete the shared sign-in: a 401 or 403 turns reading off for this alias until the next
    sign-in. Logs carry the alias, counts and HTTP statuses only: never a query, a subject, an
    address or a word of a message.
    """

    def __init__(self, account: GoogleAccount, *, service_factory: Callable[[Any], Any] | None = None) -> None:
        self._account = account
        self._service_factory = service_factory or _build_service
        self._lock = threading.RLock()
        self._service: Any = None
        self._service_creds: Any = None
        self._service_generation = -1
        self._refused: tuple[str, str] = ("", "")
        self._refused_generation = -1
        self._own_cache: tuple[str, tuple[str, ...]] | None = None   # (identity_key, own addresses)
        self.log_name = f"Gmail reading ({logged_alias(account.alias)})"

    @property
    def alias(self) -> str:
        return self._account.alias

    def available(self) -> tuple[str, str]:
        """("", "") when this alias's mail can be read now; else (PROBLEM_*, why). No network."""
        alias = self.alias
        if GMAIL_READ_FEATURE not in self._account.features:
            return PROBLEM_SETUP, f'The {alias} account is not set up for reading mail (config.toml features "gmail_read")'
        if not self._account.is_configured() or not google_libraries_available():
            return PROBLEM_SETUP, f"{SEND_SETUP_HINT} (Google sign-in is not set up)"
        if not self._account.is_signed_in():
            return PROBLEM_SIGNED_OUT, f"Not signed in to the {alias} account"
        if self._refused[0] and self._refused_generation == self._account.generation:
            return self._refused
        if GMAIL_READ_FEATURE not in self._account.granted_features():
            if GMAIL_READ_FEATURE in self._account.refused_features():
                return PROBLEM_SCOPE, (f"Google did not allow reading email for the {alias} account; sign in again "
                                       "and tick that box")
            return PROBLEM_SIGNED_OUT, f"Reading email needs one more Google sign-in for the {alias} account"
        return "", ""

    def search(self, query: str, *, max_threads: int = MAX_SEARCH_THREADS) -> list[str]:
        """The ids of at most ``max_threads`` threads matching ``query`` (already checked by
        ask.mail.check_query), newest first; never spam or trash."""
        with self._lock:
            service = self._get_service()
            request = service.users().threads().list(userId="me", q=query, maxResults=max(1, min(max_threads, 10)),
                                                     includeSpamTrash=False, fields="threads(id)")
            answer = self._execute(request, "searching")
        found = []
        for item in (answer.get("threads") if isinstance(answer, dict) else None) or ():
            thread_id = item.get("id") if isinstance(item, dict) else None
            if isinstance(thread_id, str) and _GMAIL_ID_RE.fullmatch(thread_id) and thread_id not in found:
                found.append(thread_id)
        logger.info("%s: a search found %d thread(s)", self.log_name, len(found[:max_threads]))
        return found[:max_threads]

    def thread(self, thread_id: str) -> dict[str, Any]:
        """users.threads.get (format=full) of one thread: its messages' headers and bodies, as
        ask.mail.thread_from_api reads them. MailReadError (404) when there is no such thread."""
        if not isinstance(thread_id, str) or not _GMAIL_ID_RE.fullmatch(thread_id):
            raise MailReadError("That is not a Gmail thread id")
        with self._lock:
            service = self._get_service()
            request = service.users().threads().get(
                userId="me", id=thread_id, format="full",
                fields="id,messages(id,threadId,labelIds,internalDate,payload)")
            answer = self._execute(request, "reading a thread")
        return answer if isinstance(answer, dict) else {}

    # ---- the follow-up list's header scan (metadata only) ----------------------------------

    def scan_ids(self, *, label: str, days: int, max_threads: int) -> list[tuple[str, str]]:
        """(thread id, historyId) of the threads with ``label`` (SCAN_LABELS) in the last ``days``
        days (1..14), newest first, at most ``max_threads`` (1..300) and SCAN_MAX_PAGES pages; the
        inbox leaves out promotions, social and forums. Ids and historyIds only (the field mask);
        never spam or trash. No free text reaches this request."""
        if label not in SCAN_LABELS:
            raise ValueError("not a label the scan lists")
        days = max(1, min(SCAN_DAYS_MAX, int(days)))
        cap = max(1, min(SCAN_THREADS_MAX, int(max_threads)))
        query = f"newer_than:{days}d" + (f" {SCAN_INBOX_EXCLUDE}" if label == "INBOX" else "")
        found: list[tuple[str, str]] = []
        seen: set[str] = set()
        token = ""
        with self._lock:
            service = self._get_service()
            for _page in range(SCAN_MAX_PAGES):
                kwargs: dict[str, Any] = {"userId": "me", "labelIds": [label], "q": query,
                                          "maxResults": min(SCAN_PAGE, cap - len(found)), "includeSpamTrash": False,
                                          "fields": SCAN_LIST_FIELDS}
                if token:
                    kwargs["pageToken"] = token
                answer = self._execute(service.users().threads().list(**kwargs), "listing threads")
                answer = answer if isinstance(answer, dict) else {}
                for item in answer.get("threads") or ():
                    thread_id = item.get("id") if isinstance(item, dict) else None
                    if not isinstance(thread_id, str) or not _GMAIL_ID_RE.fullmatch(thread_id) or thread_id in seen:
                        continue
                    history = item.get("historyId")
                    history = str(history) if isinstance(history, (str, int)) and not isinstance(history, bool) else ""
                    seen.add(thread_id)
                    found.append((thread_id, history if _HISTORY_RE.fullmatch(history) else ""))
                    if len(found) >= cap:
                        break
                token = answer.get("nextPageToken") if isinstance(answer.get("nextPageToken"), str) else ""
                if not token or len(found) >= cap:
                    break
        logger.info("%s: a scan listed %d thread(s)", self.log_name, len(found))
        return found

    def thread_metadata(self, thread_id: str) -> dict[str, Any]:
        """users.threads.get(format="metadata") of one thread: its messages' ids, labels, dates and
        the SCAN_HEADERS only (the field mask: never a snippet, a size or a body). MailReadError
        (404) when there is no such thread."""
        if not isinstance(thread_id, str) or not _GMAIL_ID_RE.fullmatch(thread_id):
            raise MailReadError("That is not a Gmail thread id")
        with self._lock:
            service = self._get_service()
            request = service.users().threads().get(userId="me", id=thread_id, format="metadata",
                                                    metadataHeaders=list(SCAN_HEADERS), fields=SCAN_GET_FIELDS)
            answer = self._execute(request, "reading a thread's headers")
        return answer if isinstance(answer, dict) else {}

    def own_addresses(self) -> tuple[str, ...]:
        """The owner's own addresses on this account: the profile's address and every "send as"
        address (gmail.readonly), casefolded and once each; () on any error except a refusal (a 401,
        or a 403 that is not a rate limit: that MailReadError is raised, so the follow-up scan stops
        the account, R19). Kept per account identity (identity_key): a token refresh never asks
        again. Logs a count only."""
        key = self.identity_key()
        cached = self._own_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        found: list[str] = []
        try:
            with self._lock:
                service = self._get_service()
                profile = self._execute(service.users().getProfile(userId="me", fields=OWN_PROFILE_FIELDS),
                                        "reading the profile")
                send_as = self._execute(service.users().settings().sendAs().list(userId="me",
                                                                                 fields=OWN_SEND_AS_FIELDS),
                                        "reading the send-as addresses")
        except Exception as exc:  # noqa: BLE001 - only the bound address is known then
            logger.info("%s: own addresses not read (%s)", self.log_name, type(exc).__name__)
            if isinstance(exc, MailReadError) and exc.status in (401, 403) and not is_rate_limited(exc):
                raise
            return ()
        values = [profile.get("emailAddress") if isinstance(profile, dict) else None]
        for item in (send_as.get("sendAs") if isinstance(send_as, dict) else None) or ():
            values.append(item.get("sendAsEmail") if isinstance(item, dict) else None)
        for value in values:
            plain = email_address(value) if isinstance(value, str) else ""
            if plain and plain.casefold() not in found:
                found.append(plain.casefold())
        result = tuple(found)
        self._own_cache = (key, result)
        logger.info("%s: %d own address(es)", self.log_name, len(result))
        return result

    def confirmed_address(self) -> str:
        """The bound address when you confirmed the binding and the saved sign-in is that Google
        account; else "" (the follow-up scan reads only such an account). Files only."""
        account = self._account
        try:
            if account.binding_confirmed() and account.identity_confirmed():
                return account.bound_email()
        except Exception:  # noqa: BLE001 - unknown: not scanned
            return ""
        return ""

    def scan_problem(self) -> str:
        """"" | SCAN_UNCONFIRMED (bound, but you have not confirmed it: Confirm) | SCAN_IDENTITY (the saved
        sign-in names no or another Google account: sign in again). Files only."""
        account = self._account
        try:
            if not account.bound_email() or not account.identity_confirmed():
                return SCAN_IDENTITY
            if not account.binding_confirmed():
                return SCAN_UNCONFIRMED
        except Exception:  # noqa: BLE001
            return SCAN_IDENTITY
        return ""

    def identity_key(self) -> str:
        """The account identity the scan's cache and the own addresses are kept for: the saved
        sign-in's Google id and the bound address ("" when both are unknown). Never
        GoogleAccount.generation, which changes on every token refresh."""
        account = self._account
        try:
            sub, bound = account.token_sub(), account.bound_email().casefold()
        except Exception:  # noqa: BLE001
            return ""
        return f"{sub}|{bound}" if sub or bound else ""

    def _get_service(self) -> Any:
        problem, message = self.available()
        if problem:
            raise MailReadError(message, problem=problem)
        with _account_errors():
            creds = self._account.credentials(interactive=False, need=GMAIL_READ_FEATURE)
        if self._service is None or creds is not self._service_creds \
                or self._service_generation != self._account.generation:
            try:
                self._service = self._service_factory(creds)
            except Exception as exc:  # noqa: BLE001 - mapped to a safe message
                raise MailReadError(f"Could not start the Gmail client ({type(exc).__name__})") from None
            self._service_creds = creds
            self._service_generation = self._account.generation
        return self._service

    def _execute(self, request: Any, what: str) -> Any:
        from googleapiclient.errors import HttpError

        try:
            result = request.execute(num_retries=READ_RETRIES)
        except HttpError as exc:
            raise self._http_error(exc, what) from None
        except GmailError:
            raise
        except Exception as exc:  # noqa: BLE001 - socket/ssl/httplib2/refresh errors share no base
            from google.auth.exceptions import RefreshError

            logger.info("%s: %s failed (%s)", self.log_name, what, type(exc).__name__)
            if isinstance(exc, RefreshError):
                raise MailReadError(f"Google could not refresh the {self.alias} account's sign-in just now",
                                    problem=PROBLEM_FAILED) from None
            raise MailReadError(f"Could not reach Gmail while {what} ({type(exc).__name__})") from None
        if self._service_creds is not None:
            _register_credentials(self._service_creds)
        return result

    def _http_error(self, exc: Any, what: str) -> MailReadError:
        status = _status_of(exc)
        reasons = _error_reasons(exc)
        logger.info("%s: Gmail answered HTTP %d while %s (%s)", self.log_name, status, what,
                    ", ".join(sorted(reason for reason in reasons if re.fullmatch(r"[A-Za-z_]{1,40}", reason)))
                    or "no reason code")
        alias = self.alias
        if status == 404:
            return MailReadError("Gmail has no such thread", status=status)
        if status == 401:
            self._refuse(PROBLEM_EXPIRED, f"Gmail refused the {alias} account's sign-in; sign in again")
            return MailReadError(self._refused[1], status=status, problem=PROBLEM_EXPIRED)
        if status == 403:
            if reasons & _SETUP_REASONS:
                return MailReadError(f"{SEND_SETUP_HINT}: the Gmail API is not turned on for the OAuth client's "
                                     "project", status=status, problem=PROBLEM_SETUP)
            if reasons & _RATE_LIMIT_REASONS:
                return MailReadError("Gmail is limiting requests right now; try again later", status=status)
            code = blocked_code(" ".join(sorted(reasons))) or next(
                (reason for reason in _BLOCKED_REASONS if reason in reasons), "")
            if code:
                self._refuse(PROBLEM_BLOCKED, f"The {alias} account's administrator does not allow this app to read "
                                              f"email ({code})")
            elif reasons & _SCOPE_REASONS:
                self._refuse(PROBLEM_SCOPE, f"Google did not allow reading email for the {alias} account; sign in "
                                            "again and tick that box")
            else:
                self._refuse(PROBLEM_FAILED, f"Gmail refused reading for the {alias} account (403)")
            return MailReadError(self._refused[1], status=status, problem=self._refused[0])
        if status == 429:
            return MailReadError("Gmail is limiting requests right now; try again later", status=status)
        return MailReadError(f"Gmail could not be read just now ({status})", status=status)

    def _refuse(self, problem: str, message: str) -> None:
        self._refused = (problem, message)
        self._refused_generation = self._account.generation
        logger.warning("%s: reading is off until the next sign-in (the sign-in is kept)", self.log_name)


class _account_errors:   # noqa: N801 - used as a context manager
    """google_auth's AccountErrors as the matching GmailErrors (same message and problem)."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, kind: Any, exc: Any, _tb: Any) -> bool:
        if exc is None or not isinstance(exc, AccountError):
            return False
        if isinstance(exc, AccountSetupError):
            raise GmailSetupError(str(exc), status=exc.status, problem=PROBLEM_SETUP) from None
        if isinstance(exc, AccountNotSignedIn):
            raise GmailAuthError(str(exc), problem=PROBLEM_SIGNED_OUT) from None
        if isinstance(exc, AccountAuthError):
            raise GmailAuthError(str(exc), status=exc.status, problem=exc.problem) from None
        raise GmailError(str(exc), status=exc.status, problem=exc.problem or PROBLEM_FAILED) from None


# --------------------------------------------------------------------------
# Google library glue (lazy imports; injected in tests)
# --------------------------------------------------------------------------

def _build_service(creds: Any) -> Any:
    import google_auth_httplib2
    from googleapiclient.discovery import build

    # An explicit timeout (no answer in time is an unknown outcome, never retried) and a client
    # that never sends a change twice by itself (single_send_http). static_discovery uses the API
    # description bundled with the library: no extra request.
    http = google_auth_httplib2.AuthorizedHttp(creds, http=single_send_http(HTTP_TIMEOUT_S))
    return build("gmail", "v1", http=http, cache_discovery=False, static_discovery=True)


def _status_of(exc: Any) -> int:
    status = getattr(getattr(exc, "resp", None), "status", 0)
    try:
        return int(status)
    except (TypeError, ValueError):
        return 0


def _safe_message(exc: Any) -> str:
    """Google's error text, redacted, without anything that looks like an address, shortened."""
    text = " ".join(str(getattr(exc, "reason", "") or "").split())
    text = _ADDRESS_LIKE_RE.sub("<address>", redact(text))
    if len(text) > _MAX_MESSAGE_LEN:
        text = text[:_MAX_MESSAGE_LEN - 3] + "..."
    return text


def _error_reasons(exc: Any) -> set[str]:
    """The machine-readable reasons in a Google error body (both error formats)."""
    try:
        error = json.loads(getattr(exc, "content", b"").decode("utf-8"))["error"]
    except (ValueError, KeyError, TypeError, AttributeError):
        return set()
    if not isinstance(error, dict):
        return set()
    reasons: set[str] = set()
    if isinstance(error.get("status"), str):
        reasons.add(error["status"])
    for key in ("errors", "details"):
        for item in error.get(key) or ():
            if isinstance(item, dict) and isinstance(item.get("reason"), str):
                reasons.add(item["reason"])
    return reasons
