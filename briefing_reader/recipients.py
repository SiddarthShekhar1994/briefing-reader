"""Who a Reply or Email goes to: the own-address refusal and the NEW RECIPIENT check.

Jarvis never sends a message to the account it sends from. An address is that account's own
mailbox (OWN) also when it is written another way: other upper / lower case, spaces around it,
a "+tag" after the name (Gmail delivers "ana+notes@example.edu" to "ana@example.edu") or, for
gmail.com / googlemail.com, dots in the name. A card that names its own account in To or Cc is
refused as it stands ("This would send to the work account (ana@example.edu) itself - edit the
recipients"): such a card is a slip of the briefing, or its alias is bound to the wrong Google
account, so nothing is quietly left out and nothing goes out until the address is removed in
Edit.

Sending never reads a thread to prove that an address belongs in it. Instead an address needs no
extra confirmation only when it is

- in a domain of ``[actions] trusted_domains``, or a subdomain of one (empty by default), or
- an address Jarvis sent to before (RecipientHistory: recipients.json).

Every other address is NEW: its card shows a red NEW RECIPIENT chip and Send waits until it is
ticked on its card ("Send to <address>"; ProposedAction.confirmed, memory only).
Addresses are compared as plain, casefolded addresses; a display name never counts. A recipient
that Ask Jarvis proposed and you did not type (ProposedAction.unverified) skips the trusted
domains: it is NEW unless Jarvis sent to it before.

    mailbox_key(address)      the mailbox an address delivers to (only for comparing addresses)
    same_mailbox(a, b)        both are the same mailbox (case, spaces, +tag, Gmail dots aside)
    own_recipients(...)       the recipients that are the sending account itself
    RecipientHistory(path)    %LOCALAPPDATA%\\briefing-reader\\recipients.json: the sha256 of each
                              address Jarvis sent to and when (never the address itself)
    classify(addresses, ...)  address -> OWN / TRUSTED / KNOWN / NEW
    review(to, cc, ...)       what a card shows and Send needs: the recipients, which of them are
                              the account itself (Send refuses), which are new, which of those
                              are still unconfirmed

Qt-free. Logs carry counts only, never an address or a hash.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

RECIPIENTS_FILE = "recipients.json"
OWN = "own"            # the sending account itself: Send refuses until it is removed
TRUSTED = "trusted"    # in [actions] trusted_domains
KNOWN = "known"        # Jarvis sent to it before
NEW = "new"            # needs a tick on the card before Send
MAX_REMEMBERED = 5000  # the most recent addresses kept in recipients.json
NO_TO_PROBLEM = "The message has nobody in To - edit the recipients"
# The card's refusal (shown on the card only: it names the address, so it is never logged or saved).
OWN_RECIPIENT_NOTE = "This would send to {who} itself - edit the recipients"
# The same refusal without the address: what a failed send saves (actions.json) and may log.
OWN_RECIPIENT_MESSAGE = ("This would send to the {alias} account's own address; nothing was sent - "
                         "edit the recipients")
_KEY_RE = re.compile(r"[0-9a-f]{64}")
# Mail domains whose names ignore dots ("a.na@gmail.com" is "ana@gmail.com").
_DOTLESS_DOMAINS = {"gmail.com": "gmail.com", "googlemail.com": "gmail.com"}


def mailbox_key(address: str) -> str:
    """The mailbox ``address`` delivers to, for comparing it with the sending account's address.

    Spaces around it and upper / lower case do not count, nor a "+tag" after the name
    (sub-addressing: Gmail and Google Workspace deliver it to the same mailbox), nor dots in a
    gmail.com / googlemail.com name. "" for an empty text. Only ever compared, never sent or shown.
    """
    if not isinstance(address, str):
        return ""
    text = address.strip().casefold()
    local, at, domain = text.rpartition("@")
    if not at:
        return text
    local, domain = local.strip(), domain.strip().rstrip(".")
    plain = local.split("+", 1)[0]
    if plain:
        local = plain
    if domain in _DOTLESS_DOMAINS:
        domain = _DOTLESS_DOMAINS[domain]
        local = local.replace(".", "") or local
    return f"{local}@{domain}"


def same_mailbox(first: str, second: str) -> bool:
    """``first`` and ``second`` are the same mailbox (see mailbox_key); False when either one is
    not an address."""
    key = mailbox_key(first)
    return "@" in key and key == mailbox_key(second)


def own_recipients(addresses: Iterable[str], own: str) -> tuple[str, ...]:
    """The addresses that are the sending account ``own`` itself (none while ``own`` is unknown)."""
    if not isinstance(own, str) or "@" not in own:
        return ()
    return tuple(address for address in addresses if same_mailbox(address, own))


def own_recipient_note(account: str, address: str) -> str:
    """The card's refusal: "This would send to the work account (ana@example.edu) itself - edit
    the recipients". For the card only (it names the address)."""
    if account and address:
        who = f"the {account} account ({address})"
    elif account:
        who = f"the {account} account"
    else:
        who = address or "the sending account"
    return OWN_RECIPIENT_NOTE.format(who=who)


def address_key(address: str) -> str:
    """How recipients.json names an address: sha256 of the casefolded address (hex)."""
    return hashlib.sha256(address.strip().casefold().encode("utf-8")).hexdigest()


def in_domains(address: str, domains: Iterable[str]) -> bool:
    """``address`` is in one of ``domains`` or a subdomain of one ("lab.example.edu" counts for
    "example.edu"; "badexample.edu" does not)."""
    _, at, domain = address.rpartition("@")
    if not at:
        return False
    domain = domain.casefold().rstrip(".")
    for trusted in domains:
        trusted = trusted.casefold().strip().lstrip("@").rstrip(".")
        if trusted and (domain == trusted or domain.endswith("." + trusted)):
            return True
    return False


class RecipientHistory:
    """The addresses Jarvis sent to: {sha256(casefold(address)): last sent (ISO)}, at most
    MAX_REMEMBERED (the oldest are dropped). Read once, then kept in memory; writes are atomic.
    An unreadable file counts as empty (logged without its content); a failed write keeps the
    addresses for this run."""

    def __init__(self, path: Path, clock: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._lock = threading.Lock()
        self._entries: dict[str, str] | None = None

    def knows(self, address: str) -> bool:
        with self._lock:
            return address_key(address) in self._load()

    def __len__(self) -> int:
        with self._lock:
            return len(self._load())

    def add(self, addresses: Iterable[str]) -> bool:
        """Remember ``addresses`` as sent to now. True when it was saved to the file."""
        keys = {address_key(address) for address in addresses if address and address.strip()}
        if not keys:
            return True
        now = self._clock().isoformat(timespec="seconds")
        with self._lock:
            entries = self._load()
            for key in keys:
                entries[key] = now
            if len(entries) > MAX_REMEMBERED:
                newest = sorted(entries.items(), key=lambda item: item[1], reverse=True)[:MAX_REMEMBERED]
                entries.clear()
                entries.update(newest)
            saved = self._write(entries)
        logger.info("Remembered %d recipient(s) as sent to through Jarvis", len(keys))
        return saved

    def _load(self) -> dict[str, str]:
        if self._entries is not None:
            return self._entries
        entries: dict[str, str] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as exc:
            logger.warning("Could not read the list of earlier recipients (%s); every recipient counts "
                           "as new until the next send", type(exc).__name__)
            data = {}
        if not isinstance(data, dict):
            logger.warning("The list of earlier recipients is not a table; every recipient counts as new")
            data = {}
        for key, value in data.items():
            if isinstance(key, str) and _KEY_RE.fullmatch(key) and isinstance(value, str):
                entries[key] = value
        if len(entries) != len(data):
            logger.warning("Ignored %d malformed entr(ies) in the list of earlier recipients",
                           len(data) - len(entries))
        self._entries = entries
        return entries

    def _write(self, entries: dict[str, str]) -> bool:
        text = json.dumps(dict(sorted(entries.items())), indent=1) + "\n"
        part: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, part = tempfile.mkstemp(prefix=f"{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(part, self.path)
            part = None
            return True
        except OSError as exc:
            logger.warning("Could not save the list of earlier recipients (%s); kept for this run only",
                           exc.strerror or type(exc).__name__)
            return False
        finally:
            if part is not None:
                try:
                    os.unlink(part)
                except OSError:
                    pass


def classify(addresses: Iterable[str], *, own: str, trusted_domains: Sequence[str],
             history: RecipientHistory | None, unverified: Collection[str] = ()) -> dict[str, str]:
    """address -> OWN (the sending account ``own`` itself, written any way same_mailbox allows),
    TRUSTED, KNOWN or NEW (first match wins, in that order). An ``unverified`` address (an Ask
    proposal's recipient you did not type) skips TRUSTED: KNOWN only when Jarvis sent to it before,
    else NEW."""
    kinds: dict[str, str] = {}
    unchecked = {address.strip().casefold() for address in unverified if isinstance(address, str)}
    for address in addresses:
        if own and same_mailbox(address, own):
            kinds[address] = OWN
        elif address.strip().casefold() in unchecked:
            kinds[address] = KNOWN if history is not None and history.knows(address) else NEW
        elif in_domains(address, trusted_domains):
            kinds[address] = TRUSTED
        elif history is not None and history.knows(address):
            kinds[address] = KNOWN
        else:
            kinds[address] = NEW
    return kinds


@dataclass(frozen=True)
class RecipientReview:
    """What a Reply / Email card shows about its recipients and what Send needs.

    ``to`` / ``cc`` are the card's recipients as it names them (nothing is left out). ``kinds``
    names each of them OWN / TRUSTED / KNOWN / NEW, in To-then-Cc order. ``own`` lists the ones
    that are the sending account itself: then ``problem`` says so and Send refuses until they are
    removed in Edit.
    """

    to: tuple[str, ...]
    cc: tuple[str, ...]
    kinds: tuple[tuple[str, str], ...]
    own: tuple[str, ...]            # the sending account itself: the card is refused as it stands
    new: tuple[str, ...]            # the red NEW RECIPIENT chips
    unconfirmed: tuple[str, ...]    # new and not ticked yet: Send waits (disabled) for the tick
    problem: str = ""               # why nothing can be sent as it stands ("" = nothing)

    @property
    def recipients(self) -> tuple[str, ...]:
        return self.to + self.cc

    @property
    def ready(self) -> bool:
        """Send may go ahead as far as the recipients go."""
        return not self.problem and not self.own and not self.unconfirmed

    def kind_of(self, address: str) -> str:
        key = address.casefold()
        return next((kind for item, kind in self.kinds if item.casefold() == key), "")


def review(to: Sequence[str], cc: Sequence[str], *, own: str, account: str = "", confirmed: Collection[str] = (),
           trusted_domains: Sequence[str] = (), history: RecipientHistory | None = None,
           unverified: Collection[str] = ()) -> RecipientReview:
    """The recipients of a Reply / Email as a card shows them and Send needs them (no network).

    ``own`` is the sending account's address ("" while Jarvis does not know it; Send then signs
    in first anyway); ``account`` its alias, for the refusal note. Any recipient that is the
    account itself (same_mailbox) refuses the card: "This would send to the work account
    (ana@example.edu) itself - edit the recipients". ``unverified``: recipients an Ask proposed
    that you did not type (ProposedAction.unverified); each is NEW, even in a trusted domain,
    unless Jarvis sent to it before, so Send waits for its tick."""
    to, cc = tuple(to), tuple(cc)
    kinds = classify(to + cc, own=own, trusted_domains=trusted_domains, history=history, unverified=unverified)
    mine = tuple(address for address in to + cc if kinds[address] == OWN)
    ticked = {address.casefold() for address in confirmed}
    new = tuple(address for address in to + cc if kinds[address] == NEW)
    unconfirmed = tuple(address for address in new if address.casefold() not in ticked)
    problem = ""
    if mine:
        problem = own_recipient_note(account, own.strip())
    elif not to:
        problem = NO_TO_PROBLEM
    return RecipientReview(to=to, cc=cc, kinds=tuple(kinds.items()), own=mine, new=new,
                           unconfirmed=unconfirmed, problem=problem)
