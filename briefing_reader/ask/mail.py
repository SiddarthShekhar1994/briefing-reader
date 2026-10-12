"""Gmail thread text for Ask Jarvis: the searches the planner may ask for, and what it may read.

    check_query(query, grounding)  the planner's Gmail search, normalized, or QueryRefused: plain
                                   words, quoted phrases and a short list of operators only; never
                                   spam / trash, never passwords, codes, sign-in or security mail,
                                   banking or account recovery, never an account-security sender,
                                   never a URL; something of 3+ characters must narrow it. With
                                   ``grounding`` (the planner always passes it) every word, phrase
                                   and from: / to: / cc: value must come from the owner: the words
                                   typed in the command, an address typed there, or a person Jarvis
                                   supplied from the calendar or the briefing whose name the command
                                   names. Email text never grounds a search.
    is_security_sender(address, own_domains=)   an account-security or payment sender (the check
                                   check_query applies to from: / to: / cc:; a meeting prep applies
                                   it to every guest too, with no own domains)
    thread_from_api(data, account) a users.threads.get answer (format=full) -> MailThread
    message_text(payload)          one message's readable text: text/plain (else the HTML made
                                   plain), without quoted history, signatures, attachments or
                                   invisible characters
    trim_threads(threads)          at most MAX_THREADS threads and MAX_MAIL_CHARS characters: the
                                   newest message of each thread in full (up to NEWEST_CHARS), older
                                   ones cut to OLDER_CHARS, the oldest left out first
    briefing_threads(command, ...) the briefing's Reply cards a request clearly names (their threads
                                   are read without a search)

Everything here is pure and works on data already fetched (gmail.GmailReader does the read-only
API calls). The text lives in memory for one Ask only: nothing here logs or saves it.
"""

from __future__ import annotations

import base64
import binascii
import html
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from email.utils import getaddresses
from typing import Any

from ..actions import REPLY, ProposedAction, email_address

MAX_THREADS = 3
MAX_MAIL_CHARS = 16000
NEWEST_CHARS = 6000
OLDER_CHARS = 600
MAX_MESSAGES = 6               # per thread: the newest ones
MAX_PEOPLE = 10                # per To / Cc header ("+N more" beyond); Reply-To keeps MAX_REPLY_TO
MAX_REPLY_TO = 3
HEADER_CAP = 20_000            # characters of one address header that are parsed
HTML_TEXT_CAP = 60_000         # characters of HTML-made text kept before the quote / signature cut
MAX_QUERY_CHARS = 200
MAX_QUERY_TOKENS = 24
PART_CAP = 200_000             # bytes of one decoded body part that are looked at
CUT_MARK = " [...]"

_OPERATORS = {
    "from": "who", "to": "who", "cc": "who", "subject": "text", "after": "date", "before": "date",
    "older_than": "age", "newer_than": "age", "in": "in", "is": "is", "has": "has",
    "category": "category", "rfc822msgid": "msgid",
}
# Never "anywhere" (it includes spam and trash) and no label: (a label can be spam / trash too).
_IN_VALUES = frozenset({"inbox", "sent", "starred", "important", "snoozed"})
_IS_VALUES = frozenset({"unread", "read", "starred", "important", "snoozed"})
_HAS_VALUES = frozenset({"attachment"})
_CATEGORIES = frozenset({"primary", "social", "promotions", "updates", "forums", "purchases", "reservations"})
_DATE_RE = re.compile(r"\d{4}[/-]\d{1,2}[/-]\d{1,2}")
_AGE_RE = re.compile(r"\d{1,3}[dmy]")
_MSGID_RE = re.compile(r"<?[\x21-\x3b\x3d\x3f-\x7e]{1,250}@[\x21-\x3b\x3d\x3f-\x7e]{1,250}>?")
_WHO_RE = re.compile(r"[\w.+@'-]{1,100}")
_TOKEN_RE = re.compile(r'"[^"]*"|[(){}]|[^\s(){}"]+')
_PUNCTUATION = frozenset(".,'&!?#$%+-_@/:")
# A search for these is refused: they are what an injected instruction would fish for.
_SENSITIVE_RE = re.compile(
    r"\b(?:passwords?|passcodes?|passwd|2fa|mfa|otp|one[- ]time|verif(?:y|ied|ies|ication)\w*|"
    r"security|secure|log[- ]?ins?|sign[- ]?ins?|logon|authenticat\w*|auth|2[- ]?step|two[- ]?(?:step|factor)|"
    r"codes?|pins?|recovery|recover|reset|magic link|confirm(?:ation)? (?:code|link)|unusual activity|"
    r"suspicious|new device|bank(?:ing)?|routing number|account numbers?|iban|swift|ssn|social security|"
    r"tax id|credit cards?|debit cards?|card numbers?|cvv|statements?|api ?keys?|private keys?|seed phrase|"
    r"secret|keys?|tokens?|credentials?|wallet|crypto)\b", re.IGNORECASE)
# Senders whose mail is about the owner's accounts themselves (sign-ins, codes, money).
_SECURITY_DOMAINS = ("accounts.google.com", "google.com", "account.microsoft.com", "accountprotection.microsoft.com",
                     "microsoft.com", "id.apple.com", "appleid.apple.com", "apple.com", "okta.com", "auth0.com",
                     "duosecurity.com", "duo.com", "login.gov", "paypal.com", "venmo.com", "stripe.com", "coinbase.com",
                     "plaid.com", "1password.com", "lastpass.com", "bitwarden.com", "github.com", "anthropic.com",
                     "claude.ai")
_SECURITY_LOCALS = frozenset({"security", "verify", "verification", "password", "passwords", "2fa", "mfa", "otp",
                              "signin", "login", "logins", "account-security", "accountprotection", "auth", "alerts"})
# Words a search may use without the owner having typed them (they narrow nothing).
_FREE_WORDS = frozenset("the and for from with about into this that re fw fwd aw or not".split())
# Parts of an address that say nothing about who it is (never enough to tie it to the command).
_ADDRESS_NOISE = frozenset("""com org net edu gov mil int io co uk us ca au de fr in me info biz ai app dev
mail email mails gmail googlemail outlook hotmail live yahoo icloud proton protonmail
no noreply reply do not donotreply notifications notification notify news newsletter team support help
hello hi contact admin office inbox www""".split())
_URL_RE = re.compile(r"https?|://|www\.", re.IGNORECASE)

_CF_RE = re.compile("[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb]")
_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")
_ON_WROTE_RE = re.compile(r"^On .{4,200}\bwrote:\s*$", re.IGNORECASE)
_ON_START_RE = re.compile(r"^On (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|\d)", re.IGNORECASE)
_ORIGINAL_RE = re.compile(r"^-{2,}\s*Original Message\s*-{2,}$|^_{10,}$", re.IGNORECASE)
_FROM_LINE_RE = re.compile(r"^\*?From:\*?\s", re.IGNORECASE)
_SENT_LINE_RE = re.compile(r"^\*?(?:Sent|Date):\*?\s", re.IGNORECASE)
_TAG_NAME_RE = re.compile(r"[a-z][a-z0-9]{0,15}")
_DROP_BLOCKS = frozenset({"script", "style", "head", "title"})
_BREAK_TAGS = frozenset({"p", "div", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol"})
_HTML_QUOTE_ATTR_RE = re.compile(r"class=[\"']?gmail_quote|id=[\"']?divrplyfwdmsg|id=[\"']?appendonsend")
_GMAIL_ID_RE = re.compile(r"[0-9A-Za-z_-]{6,64}")
_STOPWORDS = frozenset("""about after again also and any are back been before being but can could did does
email emails for from get had has have her here him his how into its just let mail message more most
need not now off our out please reply said says send sent she should some still than that the their
them then there these they thing this those thread through today tomorrow was were what when where
which while who will with would write wrote yes yet you your""".split())
_MAIL_WORDS_RE = re.compile(r"\b(?:e-?mails?|mails?|repl(?:y|ied|ies)|respond|answer|thread|messages?|wrote|"
                            r"write back|get back|inbox)\b", re.IGNORECASE)
_NAME_RE = re.compile(r"([^<>,;|=]{2,80}?)\s*<([^<>]+)>")
_FIELD_RE = re.compile(r"(?:^|\|)\s*(to|cc)\s*=([^|]*)", re.IGNORECASE)


class QueryRefused(ValueError):
    """Why Jarvis will not run a search the planner asked for (its own words; safe to show)."""


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MailPerson:
    email: str
    name: str = ""


@dataclass(frozen=True)
class MailMessage:
    gmail_id: str
    message_id: str = ""                   # "left@right" (no angle brackets); "" when none
    sender: MailPerson | None = None
    to: tuple[MailPerson, ...] = ()
    cc: tuple[MailPerson, ...] = ()
    reply_to: tuple[MailPerson, ...] = ()
    sent_at: datetime | None = None
    subject: str = ""
    text: str = ""
    cut: bool = False                      # text shortened (CUT_MARK at its end)
    to_more: int = 0                       # To addresses beyond MAX_PEOPLE (never shown or indexed)
    cc_more: int = 0

    def people(self) -> tuple[MailPerson, ...]:
        found = ((self.sender,) if self.sender else ()) + self.reply_to + self.to + self.cc
        return found


@dataclass(frozen=True)
class MailThread:
    account: str
    thread_id: str
    subject: str
    messages: tuple[MailMessage, ...]      # oldest first
    left_out: int = 0                      # older messages not shown

    @property
    def chars(self) -> int:
        return sum(len(message.text) for message in self.messages)


@dataclass(frozen=True)
class MailReport:
    """What one Ask read (counts only: safe to log and show)."""

    accounts: tuple[str, ...] = ()
    threads: int = 0
    messages: int = 0
    chars: int = 0


def report(threads: Sequence[MailThread]) -> MailReport:
    return MailReport(tuple(sorted({thread.account for thread in threads})), len(threads),
                      sum(len(thread.messages) for thread in threads), sum(thread.chars for thread in threads))


# --------------------------------------------------------------------------
# Searches the planner asks for
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SearchGrounding:
    """What a planner's search may name (context.ContextIndex.search_grounding builds it): the
    words of the owner's command, the addresses typed there, the people Jarvis supplied from the
    calendar or the briefing (address -> the words of their name and address) and the
    Message-IDs it supplied. Nothing that only email text supplied is here."""

    words: frozenset[str] = frozenset()
    addresses: frozenset[str] = frozenset()
    people: Mapping[str, frozenset[str]] = field(default_factory=dict, hash=False)
    msgids: frozenset[str] = frozenset()

    def word(self, word: str) -> bool:
        """``word`` (casefolded) is one the owner typed (or the same word with another ending)."""
        return any(same_word(word, typed) for typed in self.words)

    def named_people(self) -> dict[str, frozenset[str]]:
        """The supplied people the command names (a name or address word of theirs was typed)."""
        return {address: words for address, words in self.people.items()
                if any(len(word) >= 3 and word not in _ADDRESS_NOISE and self.word(word) for word in words)}


def search_words(text: str) -> set[str]:
    """The words (letters and digits) of ``text``, casefolded."""
    return {word for word in re.findall(r"[^\W_]+", text.casefold()) if word}


def address_words(address: str) -> set[str]:
    """The words of an address or a domain, without the ones that say nothing about who it is."""
    return {word for word in search_words(address) if word not in _ADDRESS_NOISE}


def same_word(word: str, typed: str) -> bool:
    """``word`` and ``typed`` (casefolded) are the same word, or the same word with another
    ending ("budget" / "budgets", "confirmation" / "confirmed"): a shared start of four letters
    when one starts the other, else of six."""
    if word == typed:
        return True
    short, long_ = sorted((word, typed), key=len)
    if len(short) >= 4 and long_.startswith(short):
        return True
    common = 0
    for left, right in zip(word, typed):
        if left != right:
            break
        common += 1
    return common >= 6


def check_query(query: Any, grounding: SearchGrounding | None = None) -> str:
    """``query`` with its spaces tidied, when Jarvis may run it; else QueryRefused."""
    if not isinstance(query, str):
        raise QueryRefused("The search is not text")
    text = " ".join(query.split())
    if not text:
        raise QueryRefused("The search is empty")
    if len(text) > MAX_QUERY_CHARS:
        raise QueryRefused(f"The search is too long ({len(text)} characters, at most {MAX_QUERY_CHARS})")
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in query) or _CF_RE.search(query) \
            or any(char in "<>|\\`;[]*=" for char in text):
        raise QueryRefused("The search has characters Jarvis does not allow")
    if _URL_RE.search(text):
        raise QueryRefused("The search names a web address")
    if _SENSITIVE_RE.search(text):
        raise QueryRefused("Jarvis does not search mail for passwords, codes, sign-ins, security, banking or "
                           "account recovery")
    if text.count('"') % 2:
        raise QueryRefused("The search's quotes do not match")
    tokens = _TOKEN_RE.findall(text)
    if len(tokens) > MAX_QUERY_TOKENS:
        raise QueryRefused("The search has too many parts")
    named = grounding.named_people() if grounding is not None else {}
    narrowing = False
    depth = 0
    for token in tokens:
        if token in ("(", "{"):
            depth += 1
            continue
        if token in (")", "}"):
            depth -= 1
            if depth < 0:
                raise QueryRefused("The search's brackets do not match")
            continue
        if token.startswith('"'):
            if len(token) < 3 or not _plain(token[1:-1]):
                raise QueryRefused("A quoted phrase in the search is empty or has odd characters")
            narrowing = _owned_text(token[1:-1], grounding, named) or narrowing
            continue
        if token in ("OR", "AND", "-"):
            continue
        negated = token.startswith("-")
        word = token[1:] if negated else token
        name, colon, value = word.partition(":")
        if colon and name.casefold() in _OPERATORS:
            operator = name.casefold()
            if not value:   # "subject:" before a quoted phrase or a bracket group, checked on its own
                if _OPERATORS[operator] not in ("who", "text"):
                    raise QueryRefused(f"{operator}: needs a value")
                continue
            _check_value(operator, value)
            kind = _OPERATORS[operator]
            if kind == "who":
                narrowing = _owned_who(value, grounding, named, negated) or narrowing
            elif kind == "text" and not negated:
                narrowing = _owned_text(value, grounding, named) or narrowing
            elif kind == "msgid":
                if grounding is not None and value.strip("<>").casefold() not in grounding.msgids:
                    raise QueryRefused("The search names an email Jarvis didn't show the planner")
                narrowing = narrowing or not negated
            continue
        if colon and name:
            raise QueryRefused(f"Jarvis does not run searches with {_short(name)}:")
        if not _plain(word):
            raise QueryRefused("A word in the search has odd characters")
        if negated:
            continue   # leaving mail out never reads more of it
        narrowing = _owned_text(word, grounding, named) or narrowing
    if depth != 0:
        raise QueryRefused("The search's brackets do not match")
    if not narrowing:
        raise QueryRefused("The search is too broad (it needs a word, a name or a subject of 3 or more letters)")
    return text


def _owned_text(text: str, grounding: SearchGrounding | None, named: Mapping[str, frozenset[str]]) -> bool:
    """A word, phrase or subject: each word of 3+ letters must be the owner's (typed, or a word of a
    person the command names); True when one of them narrows the search."""
    narrows = False
    for word in search_words(text):
        if len(word) < 3 or word in _FREE_WORDS:
            continue
        if grounding is not None and not grounding.word(word) \
                and not any(word in words for words in named.values()):
            raise QueryRefused("The search uses words you didn't type; Jarvis searches only for what you asked")
        narrows = True
    return narrows


def _in_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def is_security_sender(address: str, *, own_domains: Collection[str] = ()) -> bool:
    """``address`` (or a bare domain) belongs to an account-security or payment sender: its domain
    is one of _SECURITY_DOMAINS or a subdomain of one, unless it is one of ``own_domains`` (the
    owner's own work domain: only the follow-up list passes them), or its local part is one of
    _SECURITY_LOCALS (never exempt). A search Jarvis builds (the Ask's, a meeting prep's) asks with
    no ``own_domains``."""
    folded = str(address or "").strip().casefold().strip("<>")
    local, at, domain = folded.rpartition("@")
    host = domain if at else folded
    owned = {str(item).strip().casefold().strip(".") for item in own_domains if str(item).strip()}
    if any(_in_domain(host, blocked) for blocked in _SECURITY_DOMAINS) \
            and not any(_in_domain(host, mine) for mine in owned):
        return True
    return bool(at) and local in _SECURITY_LOCALS


def _owned_who(value: str, grounding: SearchGrounding | None, named: Mapping[str, frozenset[str]],
               negated: bool) -> bool:
    """A from: / to: / cc: value: never an account-security sender; with ``grounding``, an address
    typed in the command, a person the command names, or words the command has."""
    folded = value.casefold().strip("<>")
    if folded == "me":
        return False
    at = "@" in folded
    if is_security_sender(folded):
        raise QueryRefused("Jarvis does not search mail from account-security or payment senders")
    words = {word for word in address_words(folded) if len(word) >= 3}
    if not words:
        raise QueryRefused("The search is too broad (it needs a name or an address of 3 or more letters)")
    if negated or grounding is None:
        return not negated
    if at and (folded in grounding.addresses or folded in named):
        return True
    if all(grounding.word(word) or any(word in person for person in named.values()) for word in words):
        return True
    raise QueryRefused("The search names someone you didn't mention; Jarvis searches only for what you asked")


def _short(text: str) -> str:
    return text if len(text) <= 20 and text.replace("_", "").isalnum() else "that operator"


def _plain(text: str) -> bool:
    """Letters, digits, spaces and a little punctuation (names with accents included)."""
    return (any(char.isalnum() for char in text)
            and all(char.isalnum() or char.isspace() or char in _PUNCTUATION for char in text))


def _check_value(operator: str, value: str) -> None:
    kind = _OPERATORS[operator]
    value_folded = value.casefold()
    ok = {
        "who": lambda: bool(_WHO_RE.fullmatch(value)),
        "text": lambda: _plain(value),
        "date": lambda: bool(_DATE_RE.fullmatch(value)),
        "age": lambda: bool(_AGE_RE.fullmatch(value_folded)),
        "in": lambda: value_folded in _IN_VALUES,
        "is": lambda: value_folded in _IS_VALUES,
        "has": lambda: value_folded in _HAS_VALUES,
        "category": lambda: value_folded in _CATEGORIES,
        "msgid": lambda: bool(_MSGID_RE.fullmatch(value)),
    }[kind]()
    if not ok:
        if operator == "in":
            raise QueryRefused("Jarvis searches only the inbox, sent, starred, important or snoozed mail (never "
                               "spam or trash)")
        raise QueryRefused(f"{operator}: has a value Jarvis does not allow")


# --------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------

def _header(headers: Any, name: str) -> str:
    for entry in headers if isinstance(headers, list) else ():
        if isinstance(entry, dict) and str(entry.get("name", "")).casefold() == name:
            value = entry.get("value")
            return value if isinstance(value, str) else ""
    return ""


def _people(value: str, cap: int = MAX_PEOPLE) -> tuple[tuple[MailPerson, ...], int]:
    """The first ``cap`` distinct addresses of a header, and how many more it names."""
    found: list[MailPerson] = []
    seen: set[str] = set()
    more = 0
    try:
        pairs = getaddresses([value[:HEADER_CAP]]) if value else []
    except (TypeError, ValueError, IndexError):
        pairs = []
    for name, address in pairs:
        plain = email_address(address)
        if plain and plain.casefold() not in seen:
            seen.add(plain.casefold())
            if len(found) < cap:
                found.append(MailPerson(plain, clean_line(name, 60)))
            else:
                more += 1
    if value and len(value) > HEADER_CAP:   # past the part parsed: about one more per "@"
        more = max(more, value.count("@") - len(found))
    return tuple(found), more


def _decode(data: Any, charset: str) -> str:
    if not isinstance(data, str) or not data:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return ""
    raw = raw[:PART_CAP]
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _charset(headers: Any) -> str:
    match = re.search(r"charset\s*=\s*\"?([A-Za-z0-9_.:-]{1,40})", _header(headers, "content-type"))
    return match.group(1) if match else "utf-8"


def _parts(payload: Any, depth: int = 0) -> Iterable[dict]:
    if not isinstance(payload, dict) or depth > 12:
        return
    yield payload
    for part in payload.get("parts") or ():
        yield from _parts(part, depth + 1)


def _is_attachment(part: Mapping[str, Any]) -> bool:
    disposition = _header(part.get("headers"), "content-disposition").casefold()
    return bool(part.get("filename")) or disposition.startswith("attachment")


def message_text(payload: Any) -> str:
    """The readable text of one message payload: its first text/plain part (never an attachment),
    else its first text/html part made plain; quoted history and signatures removed."""
    plain = html_part = None
    for part in _parts(payload):
        mime = str(part.get("mimeType") or "").casefold()
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        if _is_attachment(part) or not body.get("data"):
            continue
        if mime == "text/plain" and plain is None:
            plain = _decode(body.get("data"), _charset(part.get("headers")))
        elif mime == "text/html" and html_part is None:
            html_part = _decode(body.get("data"), _charset(part.get("headers")))
    if plain and plain.strip():
        return strip_quoted(plain)
    if html_part:
        return strip_quoted(html_to_text(html_part))
    return ""


def html_to_text(text: str) -> str:
    """HTML as plain text: no scripts, styles, comments or tags; the quoted history (a blockquote,
    Gmail's gmail_quote, Outlook's reply header) and everything after it dropped. One pass with
    str.find (linear in the input, whatever it holds: an unclosed comment or script ends the text)."""
    text = text[:PART_CAP]
    lower = text.lower()
    out: list[str] = []
    pos, size = 0, len(text)
    while pos < size:
        start = text.find("<", pos)
        if start < 0:
            out.append(text[pos:])
            break
        out.append(text[pos:start])
        if lower.startswith("<!--", start):
            close = lower.find("-->", start + 4)
            if close < 0:
                break
            out.append(" ")
            pos = close + 3
            continue
        end = text.find(">", start + 1)
        if end < 0:
            out.append(text[start:])
            break
        tag = lower[start + 1:end]
        closing = tag.startswith("/")
        match = _TAG_NAME_RE.match(tag[1:] if closing else tag)
        name = match.group(0) if match else ""
        if not closing and name in _DROP_BLOCKS:
            close = lower.find("</" + name, end + 1)
            if close < 0:
                break
            after = lower.find(">", close)
            out.append(" ")
            pos = size if after < 0 else after + 1
            continue
        if (not closing and name == "blockquote") or _HTML_QUOTE_ATTR_RE.search(tag):
            break   # the quoted history starts here
        if not closing and name == "li":
            out.append("\n- ")
        elif name == "br" or (closing and name in _BREAK_TAGS):
            out.append("\n")
        else:
            out.append(" ")
        pos = end + 1
    text = html.unescape("".join(out)[:HTML_TEXT_CAP]).replace("\u00a0", " ")
    return "\n".join(" ".join(line.split()) for line in text.splitlines())


def strip_quoted(text: str) -> str:
    """``text`` up to the quoted history ("On <date>, <name> wrote:", "-----Original Message-----",
    an Outlook "From: ... Sent: ..." block) or the signature ("-- "), without ">" lines, invisible
    characters or runs of blank lines."""
    lines = _CF_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n")).replace("\t", " ").split("\n")
    kept: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        joined = f"{stripped} {lines[index + 1].strip()}" if index + 1 < len(lines) else stripped
        if _ON_WROTE_RE.match(stripped) or (_ON_START_RE.match(stripped) and _ON_WROTE_RE.match(joined)):
            break
        if _ORIGINAL_RE.match(stripped):
            break
        following = lines[index + 1:index + 4]
        if _FROM_LINE_RE.match(stripped) and any(_SENT_LINE_RE.match(after.strip()) for after in following):
            break
        if line.rstrip() in ("--", "-- "):
            break
        if stripped.startswith(">"):
            continue
        kept.append(_CONTROL_RE.sub("", line.rstrip()))
    out: list[str] = []
    for line in kept:
        if not line.strip() and (not out or not out[-1].strip()):
            continue
        out.append(line)
    return "\n".join(out).strip()


def clean_line(text: Any, cap: int) -> str:
    """One line of header text: invisible and control characters removed, spaces collapsed, cut."""
    if not isinstance(text, str):
        return ""
    text = " ".join(_CONTROL_RE.sub(" ", _CF_RE.sub("", text)).split())
    return text if len(text) <= cap else text[:cap - 3] + "..."


def thread_from_api(data: Any, account: str) -> MailThread | None:
    """A users.threads.get answer (format=full) -> MailThread (messages oldest first), None when it
    is not one. Only From, To, Cc, Reply-To, Subject and Message-ID headers are kept."""
    if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not _GMAIL_ID_RE.fullmatch(data["id"]):
        return None
    items: list[tuple[datetime | None, dict]] = []
    for item in data.get("messages") or ():
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not _GMAIL_ID_RE.fullmatch(item["id"]):
            continue
        labels = item.get("labelIds") if isinstance(item.get("labelIds"), list) else []
        if "DRAFT" in labels or "SPAM" in labels or "TRASH" in labels:
            continue
        sent_at = None
        try:
            sent_at = datetime.fromtimestamp(int(item.get("internalDate")) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
        items.append((sent_at, item))
    if not items:
        return None
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    items.sort(key=lambda pair: pair[0] or oldest)
    left_out = max(0, len(items) - MAX_MESSAGES)   # only the newest are read at all
    messages: list[MailMessage] = []
    for sent_at, item in items[left_out:]:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        headers = payload.get("headers")
        msgid = _header(headers, "message-id").strip()
        match = _MSGID_RE.fullmatch(msgid) if msgid else None
        senders, _ = _people(_header(headers, "from"), 1)
        to, to_more = _people(_header(headers, "to"))
        cc, cc_more = _people(_header(headers, "cc"))
        reply_to, _ = _people(_header(headers, "reply-to"), MAX_REPLY_TO)
        messages.append(MailMessage(
            gmail_id=item["id"], message_id=msgid.strip("<>") if match else "",
            sender=senders[0] if senders else None, to=to, cc=cc, reply_to=reply_to, sent_at=sent_at,
            subject=clean_line(_header(headers, "subject"), 200), text=message_text(payload),
            to_more=to_more, cc_more=cc_more))
    subject = next((message.subject for message in reversed(messages) if message.subject), "")
    return MailThread(account, data["id"], subject, tuple(messages), left_out=left_out)


def _cut(text: str, cap: int) -> tuple[str, bool]:
    if len(text) <= cap:
        return text, False
    keep = max(0, cap - len(CUT_MARK))
    return text[:keep].rstrip() + CUT_MARK, True


def trim_threads(threads: Sequence[MailThread], *, max_threads: int = MAX_THREADS, max_chars: int = MAX_MAIL_CHARS,
                 newest_chars: int = NEWEST_CHARS, older_chars: int = OLDER_CHARS,
                 max_messages: int = MAX_MESSAGES) -> list[MailThread]:
    """At most ``max_threads`` threads within ``max_chars`` characters of text in all: each thread
    gets an equal share; its newest message keeps up to ``newest_chars`` of it, each older one up to
    ``older_chars``, newest first; what does not fit is left out (counted in ``left_out``)."""
    chosen = list(threads[:max_threads])
    if not chosen:
        return []
    share = max_chars // len(chosen)
    trimmed: list[MailThread] = []
    for thread in chosen:
        newest_first = list(reversed(thread.messages))
        kept: list[MailMessage] = []
        left = share
        left_out = thread.left_out + max(0, len(newest_first) - max_messages)
        for index, message in enumerate(newest_first[:max_messages]):
            cap = min(newest_chars if index == 0 else older_chars, left)
            if index > 0 and cap < min(120, len(message.text) or 120):
                left_out += 1
                continue
            text, cut = _cut(message.text, max(cap, 0))
            kept.append(replace(message, text=text, cut=cut or message.cut))
            left -= len(text)
        trimmed.append(replace(thread, messages=tuple(reversed(kept)), left_out=left_out))
    return trimmed


# --------------------------------------------------------------------------
# Briefing threads a request clearly names
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ThreadRef:
    account: str
    thread_id: str
    score: int


def _words(text: str) -> set[str]:
    return {word for word in re.findall(r"[^\W_]{3,}", text.casefold())}


def _named(raw: str) -> list[tuple[str, str]]:
    """(name, address) pairs written in a line's to= / cc= ("Ana Lima <ana@example.edu>")."""
    pairs: list[tuple[str, str]] = []
    for match in _FIELD_RE.finditer(raw or ""):
        for name, address in _NAME_RE.findall(match.group(2)):
            pairs.append((" ".join(name.split()).strip("\"' "), address.strip()))
    return pairs


def briefing_threads(command: str, pending: Sequence[ProposedAction], readable: Collection[str],
                     *, limit: int = MAX_THREADS) -> list[ThreadRef]:
    """The briefing Reply cards (on accounts in ``readable``) that ``command`` clearly names, best
    first: it names a recipient's address or first name, or several words of the subject, and says
    it is about mail (or names the card even more clearly)."""
    words = _words(command)
    folded = command.casefold()
    mail_words = bool(_MAIL_WORDS_RE.search(command))
    found: list[ThreadRef] = []
    seen: set[tuple[str, str]] = set()
    for action in pending:
        if action.kind != REPLY or action.error or not action.structured or action.account not in readable:
            continue
        thread = action.field("thread")
        if not thread or (action.account, thread) in seen:
            continue
        score = 0
        if any(address.casefold() in folded for address in action.mail_recipients()):
            score += 3
        names = {name.split()[0].casefold() for name, _ in _named(action.raw) if name and len(name.split()[0]) >= 3}
        if names & words:
            score += 2
        subject = _words(re.sub(r"^\s*(?:(?:re|fwd?|aw)\s*:\s*)+", "", action.title, flags=re.IGNORECASE))
        overlap = {word for word in subject & words if len(word) >= 4 and word not in _STOPWORDS}
        score += len(overlap)
        if score >= 3 or (score >= 2 and mail_words):
            seen.add((action.account, thread))
            found.append(ThreadRef(action.account, thread, score))
    found.sort(key=lambda ref: -ref.score)
    return found[:limit]


def thread_ids_seen(threads: Iterable[MailThread]) -> set[tuple[str, str]]:
    return {(thread.account, thread.thread_id) for thread in threads}
