"""Google sign-in per account: one OAuth client, one saved sign-in per account alias.

Jarvis signs in to each Google account it acts for ("personal", "work": the
``[accounts.<alias>]`` tables of config.toml) separately, with the same OAuth
client (README step 8), and keeps one token file per alias:

    token_path(data_dir, alias)       %LOCALAPPDATA%\\briefing-reader\\google_token_<alias>.json
    migrate_legacy_token(data_dir)    the single google_token.json of older versions becomes the
                                      "personal" account's token (moved once, never copied), so
                                      nobody has to sign in again
    GoogleAccount(alias, ...)         one account's saved sign-in: is_signed_in(), granted_features(),
                                      credentials(), sign_in(), forget(), problem(), bound_email(),
                                      identity_confirmed(), change_problem()
    AccountBindings(path)             which Google account each alias is (accounts.json)

What an account may do comes from its ``features`` (FEATURE_SCOPES):
"calendar", "gmail_send" (send only) and "gmail_read" (read only: Ask Jarvis
reads the threads a request is about; gmail.py's GmailReader, never the
sending path). Desktop apps cannot add a scope to an
existing grant, so ``sign_in()`` always asks for every scope of the account's
features (a new feature means one new, full consent). Google's consent screen
lets you untick a box: the sign-in still succeeds (OAUTHLIB_RELAX_TOKEN_SCOPE
is set just for the flow), the token is saved with the scopes Google actually
granted (so a refresh never asks for more), and a feature whose scopes were not
all granted is unavailable: ``granted_features()`` leaves it out and
``credentials(need=...)`` says so.

Which Google account an alias is: a sign-in of an account with bindings (every
alias the app builds) also asks for "openid" and "userinfo.email"
(IDENTITY_SCOPES, non-sensitive) and reads the address and Google's account id
("sub") from the id_token Google's token endpoint returns with the tokens. The
first such sign-in binds the alias to that account in
%LOCALAPPDATA%\\briefing-reader\\accounts.json (never in config.toml, never
logged); a later sign-in of the alias to another Google account, or to an
account already bound to another alias, is refused and its tokens are dropped.
The token file remembers the account id it was issued for, so
``identity_confirmed()`` (needed before Jarvis sends email) holds only while
the saved sign-in is the bound account's. A token without that (the migrated
google_token.json of older versions) still works for the calendar; sending
needs one new sign-in, which binds.

A new binding is not trusted yet: Google's account chooser makes it easy to
pick the wrong account. Until you confirm it ("Signed in as ana@example.edu
for 'work' - is that right?", ``confirm_binding``), ``pending_confirmation()``
names its address and nothing is sent or changed for the alias (gmail.py,
gcal.py and executor.py check it; reading the calendar goes on). The
confirmation is kept in accounts.json ("confirmed"), so it is asked once per
binding; a binding of an older version, without it, is asked once too.
``disconnect()`` (the dialog's "No, use another account") forgets the sign-in
and the binding, and refuses (AccountError, the binding kept) when the saved
sign-in could not be deleted. An unconfirmed binding never locks the alias: a
new sign-in with another Google account replaces it, and one that takes an
account another alias was bound to (unconfirmed) releases it there (a
confirmed binding refuses both, as above).

``change_problem()`` fails closed: a change (and email) needs a confirmed
binding whose account the saved sign-in was issued for. A saved sign-in that
names a Google account with no binding to read (accounts.json deleted, edited
by hand or unreadable; a disconnect that could not delete the token) is
PROBLEM_IDENTITY: sign in again, which binds and asks. Only a token that names
no account at all (older versions, or Google did not say) keeps changing the
calendar without a binding, as before.

Sign-in problems are told apart (``AccountError.problem``), so a card can say
what happened: no sign-in yet, the sign-in expired or was revoked
(invalid_grant: the token is deleted and the next sign-in starts fresh),
Google blocked it because the account's administrator does not allow this app
(admin_policy_enforced, org_internal), access was denied or cancelled
(access_denied), a box was unticked, or the browser sign-in timed out (Google's
"Access blocked" page does not come back to the app, so a timeout says that
this may be the reason).

``single_send_http(timeout)`` is the HTTP client for the Google services: it
sends a change (anything but GET / HEAD) at most once. httplib2 on its own
sends a request a second time when the first answer was a broken status line;
for a change that first request may already have been carried out, so the
second send is refused (RequestNotResent: an unknown outcome for the caller,
never a silent duplicate).

Everything here is blocking (the browser sign-in waits up to
``open_timeout_s``), so callers use a worker thread. Nothing here talks to
Google except ``sign_in()`` and a token refresh. Qt-free; the Google libraries
are imported lazily. Tokens, the id_token and the client secret are registered
with :func:`config.register_secret` as soon as they are read; the log names an
account only as "work", "personal" or "other", never by its address, and
messages (shown on cards, sometimes logged) never contain an address.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .actions import email_address
from .config import ACCOUNT_FEATURES, register_secret

logger = logging.getLogger(__name__)

CALENDAR_FEATURE = "calendar"
GMAIL_FEATURE = "gmail_send"
GMAIL_READ_FEATURE = "gmail_read"
FEATURE_SCOPES: dict[str, tuple[str, ...]] = {
    CALENDAR_FEATURE: ("https://www.googleapis.com/auth/calendar.events",
                       "https://www.googleapis.com/auth/calendar.settings.readonly"),
    # Send only: this scope can neither read nor change mail.
    GMAIL_FEATURE: ("https://www.googleapis.com/auth/gmail.send",),
    # Read only (a restricted scope): Ask Jarvis reads the threads a request is about. It can
    # neither send nor change mail, and only gmail.GmailReader uses it.
    GMAIL_READ_FEATURE: ("https://www.googleapis.com/auth/gmail.readonly",),
}
# What each feature lets Jarvis do, for messages ("Google did not allow Calendar access ...").
FEATURE_WORDS = {CALENDAR_FEATURE: "Calendar access", GMAIL_FEATURE: "sending email",
                 GMAIL_READ_FEATURE: "reading email"}
# Asked for with every alias's sign-in (non-sensitive): the id_token then says which Google account
# it is, so the alias can be bound to it (accounts.json).
IDENTITY_SCOPES = ("openid", "https://www.googleapis.com/auth/userinfo.email")
# What a token file without a "scopes" entry holds: older versions only ever signed in for Calendar.
LEGACY_SCOPES = FEATURE_SCOPES[CALENDAR_FEATURE]
ACCOUNTS_FILE = "accounts.json"
# The token file key that remembers which Google account (id_token "sub") the token was issued for.
TOKEN_SUB_KEY = "jarvis_account_sub"
# The token file key that remembers which scopes the sign-in asked for: a feature asked for but not
# granted was refused (a box unticked); one never asked for just needs a sign-in that asks for it.
TOKEN_ASKED_KEY = "jarvis_asked_scopes"
LEGACY_TOKEN = "google_token.json"
TOKEN_PATTERN = "google_token_{alias}.json"
LEGACY_ALIAS = "personal"          # the account google_token.json of older versions belongs to
SETUP_HINT = "Set up Google sign-in: README step 8"
SIGN_IN_SUCCESS_MESSAGE = "Jarvis is connected to Google Calendar. You can close this tab."
ALIAS_SUCCESS_MESSAGE = ("Jarvis is connected to Google for the {alias} account. "
                         "You can close this tab.")
NOT_SIGNED_IN_MESSAGE = "Not signed in to Google"
# "select_account": Google shows its account chooser, so the right account is picked for each
# alias. "consent": every time, so Google returns a refresh token (and asks for any new scope).
SIGN_IN_PROMPT = "select_account consent"

# AccountError.problem: why an account cannot be used right now ("" = no problem known).
PROBLEM_FAILED = "failed"         # anything else (network, an unexpected answer)
PROBLEM_SETUP = "setup"           # no usable client secret file or Google packages
PROBLEM_SIGNED_OUT = "signedout"  # no saved sign-in (and the call may not open the browser)
PROBLEM_EXPIRED = "expired"       # Google rejected the saved sign-in (invalid_grant): sign in again
PROBLEM_BLOCKED = "blocked"       # the account's administrator does not allow this app
PROBLEM_DENIED = "denied"         # access_denied: cancelled in the browser, or not allowed
PROBLEM_SCOPE = "scope"           # a box was unticked: a feature's permission is missing
PROBLEM_TIMEOUT = "timeout"       # the browser sign-in was not finished in time
PROBLEM_IDENTITY = "identity"     # another Google account than the one bound to the alias (refused)
PROBLEM_CONFIRM = "confirm"       # bound, but you have not confirmed yet that it is the right account
PROBLEMS = (PROBLEM_FAILED, PROBLEM_SETUP, PROBLEM_SIGNED_OUT, PROBLEM_EXPIRED, PROBLEM_BLOCKED,
            PROBLEM_DENIED, PROBLEM_SCOPE, PROBLEM_TIMEOUT, PROBLEM_IDENTITY, PROBLEM_CONFIRM)

_GOOGLE_MODULES = ("googleapiclient", "google_auth_oauthlib", "google_auth_httplib2",
                   "google.oauth2")
BLOCKED_ERRORS = ("admin_policy_enforced", "org_internal")
_ALIAS_RE = re.compile(r"[a-z][a-z0-9_-]{0,23}")
_LOGGED_ALIASES = frozenset({"personal", "work"})
_RELAX_SCOPE = "OAUTHLIB_RELAX_TOKEN_SCOPE"
_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})
_SUB_RE = re.compile(r"[0-9A-Za-z_-]{1,255}")
_relax_lock = threading.Lock()
_libraries_available: bool | None = None

assert set(FEATURE_SCOPES) == set(ACCOUNT_FEATURES), "config.ACCOUNT_FEATURES must name every feature"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class AccountError(Exception):
    """A sign-in failure whose message is safe to show and log (no tokens, no client secret).

    ``problem`` is one of PROBLEMS; ``status`` an HTTP status when there was one.
    """

    def __init__(self, message: str = "", *, problem: str = PROBLEM_FAILED,
                 status: int | None = None) -> None:
        super().__init__(message)
        self.problem = problem
        self.status = status


class AccountSetupError(AccountError):
    """The OAuth client secret file is missing or invalid, or the Google packages are missing."""

    def __init__(self, message: str = "", *, status: int | None = None) -> None:
        super().__init__(message, problem=PROBLEM_SETUP, status=status)


class AccountAuthError(AccountError):
    """The sign-in was refused, cancelled, blocked, timed out, revoked or lacks a permission."""


class AccountNotSignedIn(AccountAuthError):
    """No usable saved sign-in, and the call may not open the browser to get one."""

    def __init__(self, message: str = NOT_SIGNED_IN_MESSAGE) -> None:
        super().__init__(message, problem=PROBLEM_SIGNED_OUT)


class RequestNotResent(Exception):
    """httplib2 was about to send a change a second time (the answer to the first one was
    lost); refused, because the first one may already have been carried out."""


# --------------------------------------------------------------------------
# Paths and aliases
# --------------------------------------------------------------------------

def valid_alias(alias: str) -> bool:
    """"work", "personal", "lab-2": lowercase letters, digits, - or _ (24 at most)."""
    return isinstance(alias, str) and bool(_ALIAS_RE.fullmatch(alias))


def logged_alias(alias: str) -> str:
    """How an alias is written to the log: "work" / "personal" by name, any other one as "other"."""
    if not alias:
        return "-"
    return alias if alias in _LOGGED_ALIASES else "other"


def scrub_alias(text: str, alias: str) -> str:
    """``text`` (a message shown on a card) as it may be logged: an alias other than "work" /
    "personal" is written as "other", like logged_alias()."""
    logged = logged_alias(alias)
    if not alias or not text or logged == alias:
        return text
    return re.sub(rf"(?<![\w-]){re.escape(alias)}(?![\w-])", logged, text)


def token_path(data_dir: Path, alias: str) -> Path:
    """<data_dir>\\google_token_<alias>.json"""
    if not valid_alias(alias):
        raise ValueError("not an account alias")
    return Path(data_dir) / TOKEN_PATTERN.format(alias=alias)


def account_token_path(data_dir: Path, alias: str) -> Path:
    """Where ``alias``'s sign-in is kept: its own file, except that "personal" keeps using an
    older google_token.json that could not be moved (see migrate_legacy_token)."""
    path = token_path(data_dir, alias)
    legacy = Path(data_dir) / LEGACY_TOKEN
    if alias == LEGACY_ALIAS and not path.exists() and legacy.is_file():
        return legacy
    return path


def migrate_legacy_token(data_dir: Path) -> bool:
    """Move google_token.json to google_token_personal.json once (same folder, never copied).

    Nothing happens when there is no old file or the personal one exists already. When the
    move fails the old file stays where it is and the personal account keeps using it
    (logged without paths). True when a file was moved.
    """
    legacy = Path(data_dir) / LEGACY_TOKEN
    target = token_path(data_dir, LEGACY_ALIAS)
    try:
        if not legacy.is_file() or target.exists():
            return False
        os.replace(legacy, target)
    except OSError as exc:
        logger.warning("Google sign-in: could not move the saved sign-in to the personal account (%s); "
                       "using it where it is", exc.strerror or type(exc).__name__)
        return False
    logger.info("Google sign-in: the saved sign-in is now the personal account's")
    return True


def scopes_for(features: Sequence[str]) -> list[str]:
    """Every scope ``features`` need, in FEATURE_SCOPES order, without repeats."""
    scopes: list[str] = []
    for feature, needed in FEATURE_SCOPES.items():
        if feature in features:
            scopes.extend(scope for scope in needed if scope not in scopes)
    return scopes


def features_granted(scopes: Sequence[str], features: Sequence[str] | None = None) -> frozenset[str]:
    """The features (of ``features``, default all) whose scopes are all in ``scopes``."""
    granted = set(scopes)
    wanted = FEATURE_SCOPES if features is None else [name for name in features if name in FEATURE_SCOPES]
    return frozenset(name for name in wanted if set(FEATURE_SCOPES[name]) <= granted)


def blocked_code(text: str) -> str:
    """"admin_policy_enforced" / "org_internal" when ``text`` names one of them, else ""."""
    return next((code for code in BLOCKED_ERRORS if code in (text or "")), "")


def identity_from_id_token(id_token: Any, client_id: str) -> tuple[str, str] | None:
    """(address, account id) from the id_token Google's token endpoint returned with a sign-in.

    The token is not signature-checked: it came straight from Google over TLS in the same answer
    as the access token. It must be issued by Google for this OAuth client (``aud``), name the
    account ("sub") and a verified address; None when anything is missing or malformed. Never
    logged.
    """
    if not isinstance(id_token, str) or id_token.count(".") != 2 or not client_id:
        return None
    try:
        part = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode("utf-8"))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(claims, dict):
        return None
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    sub, address = claims.get("sub"), claims.get("email")
    if (claims.get("iss") not in _ISSUERS or client_id not in audiences or not isinstance(sub, str)
            or not _SUB_RE.fullmatch(sub) or claims.get("email_verified") in (False, "false")):
        return None
    address = email_address(address) if isinstance(address, str) and "<" not in address else ""
    if not address:
        return None
    return address, sub


@dataclass(frozen=True)
class Binding:
    """The Google account an alias is bound to (address and Google's account id), and whether you
    confirmed that it is the right one (``confirmed``; False for a new binding)."""

    email: str
    sub: str
    bound_at: str = ""
    confirmed: bool = False
    confirmed_at: str = ""


class AccountBindings:
    """Which Google account each alias is: <data_dir>\\accounts.json {alias: {email, sub, bound_at,
    confirmed, confirmed_at}}.

    Written only after a sign-in whose id_token named the account, and when you confirm it; never
    in config.toml and never logged (the log says "bound" / "confirmed", with the alias only). A
    new binding, or a new address of a bound account, starts unconfirmed; an entry of an older
    version without "confirmed" counts as unconfirmed. An unreadable file is treated as empty
    (logged without its content); a failed write keeps the binding in memory for this run. Reads
    and writes are serialised; writes are atomic (temporary file + os.replace).
    """

    def __init__(self, path: Path, clock: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._lock = threading.Lock()
        self._memory: dict[str, Binding] | None = None   # set when a write failed

    def get(self, alias: str) -> Binding | None:
        with self._lock:
            return self._entries().get(alias)

    def alias_of(self, sub: str) -> str:
        """The alias bound to the Google account ``sub`` ("" when none)."""
        with self._lock:
            return next((alias for alias, binding in self._entries().items() if binding.sub == sub), "")

    def bind(self, alias: str, email: str, sub: str, *, release: str = "") -> bool:
        """Bind ``alias`` to the account: a new binding, a new address of the same account, or
        another account in place of a binding you never confirmed (all unconfirmed until
        confirm()). The same account and address again changes nothing; a confirmed binding to
        another account is never replaced (ValueError: GoogleAccount refuses such a sign-in
        first). ``release``: another alias whose unconfirmed binding to this same account is
        given up in the same write (one Google account is one alias). True when it was saved to
        the file."""
        if not valid_alias(alias) or not email or not _SUB_RE.fullmatch(sub or ""):
            raise ValueError("not a binding")
        with self._lock:
            entries = self._entries()
            old = entries.get(alias)
            if old is not None and old.sub == sub and old.email == email:
                return True
            if old is not None and old.sub != sub and old.confirmed:
                raise ValueError("bound to another account you confirmed")
            released = entries.get(release) if release and release != alias else None
            if released is not None:
                if released.sub != sub or released.confirmed:
                    raise ValueError("not an unconfirmed binding of this account")
                del entries[release]
            entries[alias] = Binding(email, sub, self._clock().isoformat(timespec="seconds"))
            saved = self._write(entries)
        if released is not None:
            logger.info("Google (%s): no longer bound to a Google account (another account name was signed in "
                        "with it before you confirmed it here)", logged_alias(release))
        if old is None:
            what = "bound to the account it signed in with"
        elif old.sub == sub:
            what = "account address updated"
        else:
            what = "bound to another Google account (you had not confirmed the earlier one)"
        logger.info("Google (%s): %s", logged_alias(alias), what)
        return saved

    def confirm(self, alias: str, email: str) -> bool:
        """You confirmed that ``alias`` is the Google account with address ``email`` (the address
        the question showed). False, and nothing changes, when the alias is not bound to that
        address (any more). A failed write keeps the confirmation for this run (logged)."""
        with self._lock:
            entries = self._entries()
            old = entries.get(alias)
            if old is None or not isinstance(email, str) or old.email.casefold() != email.strip().casefold():
                return False
            if old.confirmed:
                return True
            entries[alias] = Binding(old.email, old.sub, old.bound_at, True,
                                     self._clock().isoformat(timespec="seconds"))
            self._write(entries)
        logger.info("Google (%s): you confirmed the account it is bound to", logged_alias(alias))
        return True

    def unbind(self, alias: str) -> bool:
        """Forget which account ``alias`` is (the next sign-in binds again). True when one was there."""
        with self._lock:
            entries = self._entries()
            if entries.pop(alias, None) is None:
                return False
            self._write(entries)
        logger.info("Google (%s): no longer bound to a Google account", logged_alias(alias))
        return True

    def _entries(self) -> dict[str, Binding]:
        if self._memory is not None:
            return dict(self._memory)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("Could not read the account bindings (%s); treating them as empty",
                           type(exc).__name__)
            return {}
        if not isinstance(data, dict):
            logger.warning("The account bindings file is not a table; treating it as empty")
            return {}
        entries: dict[str, Binding] = {}
        for alias, value in data.items():
            if not (valid_alias(alias) and isinstance(value, dict)):
                continue
            email, sub = value.get("email"), value.get("sub")
            if isinstance(email, str) and email_address(email) == email and isinstance(sub, str) \
                    and _SUB_RE.fullmatch(sub):
                bound_at, confirmed_at = value.get("bound_at"), value.get("confirmed_at")
                entries[alias] = Binding(email, sub, bound_at if isinstance(bound_at, str) else "",
                                         value.get("confirmed") is True,
                                         confirmed_at if isinstance(confirmed_at, str) else "")
        if len(entries) != len(data):
            logger.warning("Ignored %d malformed account binding(s)", len(data) - len(entries))
        return entries

    def _write(self, entries: dict[str, Binding]) -> bool:
        data = {alias: {"email": item.email, "sub": item.sub, "bound_at": item.bound_at,
                        "confirmed": item.confirmed, "confirmed_at": item.confirmed_at}
                for alias, item in sorted(entries.items())}
        part: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, part = tempfile.mkstemp(prefix=f"{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(part, self.path)
            part = None
            self._memory = None
            return True
        except OSError as exc:
            logger.warning("Could not save the account bindings (%s); kept for this run only",
                           exc.strerror or type(exc).__name__)
            self._memory = dict(entries)
            return False
        finally:
            if part is not None:
                _unlink_quietly(Path(part))


# --------------------------------------------------------------------------
# GoogleAccount
# --------------------------------------------------------------------------

class GoogleAccount:
    """The saved Google sign-in of one account alias. Blocking: use a worker thread.

    ``is_configured``, ``is_signed_in`` and ``granted_features`` read files only (no lock, no
    network), so the UI thread may call them while a sign-in waits in the browser.
    ``credentials`` and ``sign_in`` are serialised by an internal lock. Everything that acts
    for an alias shares one account object, so there is one token per alias.

    ``alias`` "" is the one-account setup of older versions (gcal.GoogleCalendar's own
    constructor): its log lines and messages say "Google Calendar" as they did. ``log``,
    ``refresh`` and ``libraries_available`` default to this module's logger and functions;
    gcal passes its own, so its tests keep patching them there.
    """

    def __init__(self, alias: str, *, client_secret_path: Path, data_dir: Path | None = None,
                 token_path: Path | None = None, features: Sequence[str] = (CALENDAR_FEATURE,),
                 flow_factory: Callable[[Path, list[str]], Any] | None = None,
                 open_timeout_s: int = 300, setup_hint: str = SETUP_HINT,
                 success_message: str | None = None, log: logging.Logger | None = None,
                 refresh: Callable[[Any], None] | None = None,
                 libraries_available: Callable[[], bool] | None = None,
                 bindings: AccountBindings | None = None) -> None:
        if token_path is None:
            if data_dir is None:
                raise ValueError("GoogleAccount needs data_dir or token_path")
            token_path = account_token_path(Path(data_dir), alias)
        self.alias = alias
        self.token_path = Path(token_path)
        self.client_secret_path = Path(client_secret_path)
        self.features = tuple(name for name in features if name in FEATURE_SCOPES)
        self._flow_factory = flow_factory or _installed_app_flow
        self._open_timeout_s = open_timeout_s
        self._setup_hint = setup_hint
        if success_message is None:
            success_message = (ALIAS_SUCCESS_MESSAGE.format(alias=alias) if alias
                               else SIGN_IN_SUCCESS_MESSAGE)
        self._success_message = success_message
        self._log = log or logger
        self._refresh_hook = refresh or (lambda creds: _refresh_credentials(creds))
        self._libraries_hook = libraries_available or (lambda: google_libraries_available())
        # Which Google account the alias is (accounts.json); None for the one-account setup "".
        self._bindings = bindings if alias else None
        self._lock = threading.RLock()
        self._creds: Any = None
        self._granted: list[str] | None = None   # scopes of self._creds (None while not loaded)
        self.generation = 0          # changes whenever the credentials object changes or refreshes
        self._problem = ""
        self._problem_message = ""
        # How the log names this account: "Google Calendar" for the one-account setup (its log
        # lines are kept), else "Google (work)" ("other" for an alias that is not a known word).
        self.log_name = f"Google ({logged_alias(alias)})" if alias else "Google Calendar"

    # ---- state without the network ------------------------------------------------

    def requested_scopes(self) -> list[str]:
        """What sign_in() asks Google for: IDENTITY_SCOPES (an account with bindings) and every
        scope of the account's features."""
        identity = list(IDENTITY_SCOPES) if self._bindings is not None else []
        return identity + [scope for scope in scopes_for(self.features) if scope not in identity]

    def is_configured(self) -> bool:
        """True when the OAuth client secret file exists."""
        return self.client_secret_path.is_file()

    def is_signed_in(self, feature: str | None = None) -> bool:
        """True when a saved sign-in exists that is valid or can be refreshed (and, with
        ``feature``, has that feature's permissions). No network."""
        if not self._libraries_hook():
            return False
        creds = self._read_token(quiet=True)
        if creds is None or not (creds.valid or creds.refresh_token):
            return False
        return feature is None or feature in self.granted_features()

    def granted_features(self) -> frozenset[str]:
        """The configured features the sign-in has every permission for (none when signed out)."""
        granted = self._granted if self._creds is not None else None
        if granted is None:
            info = _token_info(self.token_path)
            if info is None:
                return frozenset()
            granted = _info_scopes(info, LEGACY_SCOPES)
        return features_granted(granted, self.features)

    def refused_features(self) -> frozenset[str]:
        """The configured features whose permissions the saved sign-in asked Google for but did not
        get (a box was unticked). A feature the sign-in never asked for (a token of an older
        version, or a feature added to config.toml since) is not refused: it needs one sign-in
        that asks for it. No network."""
        info = _token_info(self.token_path)
        if info is None:
            return frozenset()
        asked = _asked_scopes(info)
        return frozenset(name for name in features_granted(asked, self.features)
                         if name not in self.granted_features())

    # ---- which Google account (no network) -------------------------------------------------

    def binding(self) -> Binding | None:
        """The Google account this alias is bound to (None before the first sign-in that named it)."""
        return self._bindings.get(self.alias) if self._bindings is not None else None

    def bound_email(self) -> str:
        """The bound account's address ("" when not bound). Shown on cards, never logged."""
        binding = self.binding()
        return binding.email if binding is not None else ""

    def token_sub(self) -> str:
        """The Google account id the saved sign-in was issued for ("" when the file does not say)."""
        info = _token_info(self.token_path)
        sub = info.get(TOKEN_SUB_KEY) if info is not None else None
        return sub if isinstance(sub, str) and _SUB_RE.fullmatch(sub) else ""

    def identity_confirmed(self) -> bool:
        """The saved sign-in is the bound Google account's (what sending email needs)."""
        binding = self.binding()
        return binding is not None and bool(binding.sub) and self.token_sub() == binding.sub

    def pending_confirmation(self) -> str:
        """The bound account's address while you have not confirmed yet that it is the right Google
        account for this alias ("" when confirmed, or not bound). Nothing is sent or changed for
        the alias meanwhile; reading the calendar goes on. Shown in the question, never logged."""
        binding = self.binding()
        return binding.email if binding is not None and not binding.confirmed else ""

    def binding_confirmed(self) -> bool:
        """You confirmed that the bound Google account is the right one for this alias."""
        binding = self.binding()
        return binding is not None and binding.confirmed

    def confirm_binding(self, address: str) -> bool:
        """You answered Yes to "Signed in as ``address`` for this alias - is that right?". False
        (nothing confirmed) when the alias is not bound to that address any more."""
        if self._bindings is None:
            return False
        return self._bindings.confirm(self.alias, address)

    def needs_confirmation(self) -> bool:
        """The alias has no binding you confirmed: its next sign-in leaves one to confirm first
        (so a click that would sign in asks before anything is sent or changed). False for the one-account
        setup "" (no bindings)."""
        return self._bindings is not None and not self.binding_confirmed()

    def change_problem(self) -> tuple[str, str]:
        """Why nothing may be sent or changed for this alias now (PROBLEM_*, a message without
        any address), or ("", "") when it may. Files only, no lock, fails closed:

        - PROBLEM_SIGNED_OUT: there is no saved sign-in (the sign-in that comes first decides);
        - PROBLEM_IDENTITY: the saved sign-in is not the bound account's, or it names a Google
          account but there is no binding to read (accounts.json deleted, edited or unreadable,
          or a disconnect that could not delete the token): sign in again, which binds and asks;
        - PROBLEM_CONFIRM: bound to the saved sign-in's account, but you have not confirmed it.

        A saved sign-in that names no account at all (older versions, or Google did not say)
        without a binding is not held back, as before; the one-account setup "" never is."""
        if self._bindings is None:
            return "", ""
        info = _token_info(self.token_path)
        if info is None:
            return PROBLEM_SIGNED_OUT, (f"Not signed in to the {self.alias} account; nothing was sent or "
                                        "changed")
        sub = info.get(TOKEN_SUB_KEY)
        sub = sub if isinstance(sub, str) and _SUB_RE.fullmatch(sub) else ""
        binding = self.binding()
        if binding is None and not sub:
            return "", ""
        if binding is None or binding.sub != sub:
            return PROBLEM_IDENTITY, self.unknown_account_message()
        if not binding.confirmed:
            return PROBLEM_CONFIRM, (f"You haven't confirmed yet that this is the right Google account for "
                                     f"the {self.alias} account; nothing was sent or changed")
        return "", ""

    def unknown_account_message(self) -> str:
        """The saved sign-in is a Google account Jarvis has no (or another) binding for."""
        return (f"Jarvis doesn't know which Google account the {self.alias} account's sign-in is; sign in "
                "again to confirm it - nothing was sent or changed")

    def problem(self) -> tuple[str, str]:
        """(PROBLEM_*, message) of the last failed sign-in or rejected token; ("", "") when none."""
        return self._problem, self._problem_message

    def _set_problem(self, problem: str = "", message: str = "") -> None:
        self._problem, self._problem_message = problem, message

    # ---- credentials -----------------------------------------------------------------

    def credentials(self, *, interactive: bool, need: str | None = None) -> Any:
        """Usable credentials: the saved sign-in (refreshed when needed), else a new sign-in.

        Without ``interactive`` a missing sign-in raises AccountNotSignedIn instead of opening
        the browser. ``need`` names the feature the caller is about to use; a sign-in without
        its permission raises PROBLEM_SCOPE (after one new sign-in when ``interactive``).
        """
        with self._lock:
            self._require_setup()
            creds = self._creds
            if creds is None:
                creds = self._read_token()
                if creds is None:
                    return self._sign_in_or_raise(interactive, need)
                self._creds = creds
                self._granted = list(getattr(creds, "scopes", None) or LEGACY_SCOPES)
                self.generation += 1
            if need and need not in self.granted_features():
                if interactive:
                    return self._sign_in_or_raise(True, need)
                raise AccountAuthError(self.missing_message(need), problem=PROBLEM_SCOPE)
            if not creds.valid:
                if creds.refresh_token:
                    self._refresh(creds)
                else:
                    self.forget("it cannot be refreshed", problem=PROBLEM_SIGNED_OUT)
                    return self._sign_in_or_raise(interactive, need)
            return self._creds

    def _sign_in_or_raise(self, interactive: bool, need: str | None) -> Any:
        if not interactive:
            raise AccountNotSignedIn()
        self.sign_in()
        if need and need not in self.granted_features():
            raise AccountAuthError(self.missing_message(need), problem=PROBLEM_SCOPE)
        return self._creds

    def missing_message(self, feature: str) -> str:
        what = FEATURE_WORDS.get(feature, feature)
        who = f" for the {self.alias} account" if self.alias else ""
        return f"Google did not allow {what}{who}; sign in again and tick every box"

    def _refresh(self, creds: Any) -> None:
        try:
            self._refresh_hook(creds)
        except Exception as exc:  # noqa: BLE001 - mapped to a safe message
            raise self._refresh_error(exc) from None
        _register_credentials(creds)
        info = _token_info(self.token_path) or {}
        self._save_token(creds, _creds_info(creds, self._granted or list(LEGACY_SCOPES), sub=self.token_sub(),
                                            asked=_asked_scopes(info)))
        self.generation += 1

    def _refresh_error(self, exc: Exception) -> AccountError:
        """A failed token refresh: rejected (token deleted) or the network."""
        from google.auth.exceptions import RefreshError

        if isinstance(exc, RefreshError):
            if exc.retryable:
                return AccountError("Google could not refresh the sign-in just now; try again")
            return self.rejected(str(exc))
        return AccountError(f"Could not reach Google while refreshing the sign-in ({type(exc).__name__})")

    def rejected(self, detail: str = "") -> AccountAuthError:
        """Google rejected the saved sign-in (invalid_grant: expired or revoked; or blocked by an
        administrator): the token is deleted and the error to raise is returned.

        ``detail`` (Google's error text) is only inspected, never logged or shown.
        """
        code = blocked_code(detail)
        if code:
            message = self.blocked_message(code)
            self.forget("Google blocked it", problem=PROBLEM_BLOCKED, message=message)
            return AccountAuthError(message, problem=PROBLEM_BLOCKED)
        if self.alias:
            message = f"Google sign-in for the {self.alias} account expired or was revoked; sign in again"
        else:
            message = "Google Calendar sign-in expired or was revoked. Approve again to sign in."
        self.forget("Google rejected it", problem=PROBLEM_EXPIRED, message=message)
        return AccountAuthError(message, problem=PROBLEM_EXPIRED)

    def blocked_message(self, code: str) -> str:
        whose = f"the {self.alias} account's" if self.alias else "this account's"
        return f"Google blocked the sign-in: {whose} administrator does not allow this app ({code})"

    # ---- sign-in -----------------------------------------------------------------------

    def sign_in(self) -> None:
        """Open the Google sign-in in the browser and save the token.

        Blocks until the user finishes or ``open_timeout_s`` passes. Raises AccountSetupError
        (no usable client secret), AccountAuthError (cancelled, denied, blocked, timed out) or
        AccountError. A partial grant (a box unticked) still signs in; see granted_features().
        """
        with self._lock:
            self._require_setup()
            client = self._read_client_config()
            scopes = self.requested_scopes()
            try:
                flow = self._flow_factory(self.client_secret_path, list(scopes))
            except Exception as exc:  # noqa: BLE001 - any failure here is a setup problem
                raise AccountSetupError(
                    f"{self._setup_hint} (could not use {self.client_secret_path.name}: "
                    f"{type(exc).__name__})") from None
            self._log.info("%s: waiting for sign-in in the browser (up to %d s)", self.log_name,
                           self._open_timeout_s)
            try:
                with _relaxed_scope():
                    creds = flow.run_local_server(
                        port=0, open_browser=True, timeout_seconds=self._open_timeout_s,
                        authorization_prompt_message="", success_message=self._success_message,
                        prompt=SIGN_IN_PROMPT)
            except Exception as exc:  # noqa: BLE001 - mapped to a safe message
                error = self._sign_in_error(exc)
                self._set_problem(error.problem, str(error))
                raise error from None
            _register_credentials(creds)
            identity, release = self._check_identity(creds, client)
            granted = _granted_scopes(creds, scopes)
            # An alias's token also says what was asked for (a box unticked vs a feature added since).
            info = _creds_info(creds, granted, sub=identity[1] if identity else "",
                               asked=scopes if self._bindings is not None else ())
            if granted != list(scopes) and info is not None:
                creds = _credentials_from_info(info) or creds   # refreshes ask only for what was granted
                _register_credentials(creds)
            if identity is not None and self._bindings is not None:
                self._bindings.bind(self.alias, *identity, release=release)
            self._save_token(creds, info)
            self._creds = creds
            self._granted = list(granted)
            self.generation += 1
            missing = [name for name in self.features if name not in features_granted(granted, self.features)]
            if missing:
                self._set_problem(PROBLEM_SCOPE, self.missing_message(missing[0]))
                self._log.warning("%s: signed in without every permission (%s missing)", self.log_name,
                                  ", ".join(missing))
            else:
                self._set_problem()
                self._log.info("%s: signed in", self.log_name)

    def _check_identity(self, creds: Any, client: dict[str, Any]) -> tuple[tuple[str, str] | None, str]:
        """((address, account id), release) of a sign-in that may be kept for this alias; the
        identity is None when Google did not say and the alias is not bound yet (Calendar works;
        sending waits for a sign-in that names the account). ``release``: another alias bound to
        this account that you never confirmed; it gives the account up (one Google account is one
        alias, and the new binding is asked about). Raises AccountAuthError(PROBLEM_IDENTITY),
        dropping the new tokens, for another account than the one you confirmed for this alias,
        an account you confirmed for another alias, or a bound alias whose sign-in does not say
        which account it is. A binding you never confirmed does not refuse another account: the
        new one replaces it, unconfirmed, and is asked about."""
        if self._bindings is None:
            return None, ""
        installed = client.get("installed") if isinstance(client, dict) else None
        client_id = installed.get("client_id", "") if isinstance(installed, dict) else ""
        identity = identity_from_id_token(getattr(creds, "id_token", None), str(client_id))
        binding = self.binding()
        message, release = "", ""
        if identity is None:
            if binding is not None:
                message = (f"Google did not say which account this sign-in is, so it was not kept for "
                           f"the {self.alias} account; sign in again")
        else:
            other = self._bindings.alias_of(identity[1])
            held = self._bindings.get(other) if other and other != self.alias else None
            if held is not None and held.confirmed:
                whose = f"the {other} account" if logged_alias(other) == other else "another account"
                message = (f"That Google account is already set up as {whose} in Jarvis; sign in with "
                           f"the {self.alias} account's own Google account")
            elif binding is not None and binding.sub != identity[1] and binding.confirmed:
                message = (f"This is not the Google account set up as the {self.alias} account; sign in "
                           "with that one (its address is on the card)")
            elif held is not None:
                release = other
        if message:
            self._set_problem(PROBLEM_IDENTITY, message)
            self._log.warning("%s: refused a sign-in to another Google account than the one set up for it",
                              self.log_name)
            raise AccountAuthError(message, problem=PROBLEM_IDENTITY)
        return identity, release

    def _sign_in_error(self, exc: Exception) -> AccountError:
        from google_auth_oauthlib.flow import WSGITimeoutError
        from oauthlib.oauth2.rfc6749.errors import OAuth2Error

        if isinstance(exc, (WSGITimeoutError, TimeoutError)):
            if self.alias:
                # Google's "Access blocked" page (admin_policy_enforced) never comes back to the
                # app, so a block looks like a sign-in that was not finished.
                return AccountAuthError(
                    f"Google sign-in for the {self.alias} account was not finished in time; if Google "
                    "said \"Access blocked\", the account's administrator does not allow this app",
                    problem=PROBLEM_TIMEOUT)
            return AccountAuthError("Google sign-in timed out", problem=PROBLEM_TIMEOUT)
        if isinstance(exc, OAuth2Error):
            code = exc.error or ""
            blocked = blocked_code(f"{code} {getattr(exc, 'description', '') or ''}")
            if blocked:
                return AccountAuthError(self.blocked_message(blocked), problem=PROBLEM_BLOCKED)
            if code == "access_denied":
                who = f" for the {self.alias} account" if self.alias else ""
                return AccountAuthError(f"Google sign-in{who} was cancelled or access was denied",
                                        problem=PROBLEM_DENIED)
            safe = code if re.fullmatch(r"[a-z_]{1,40}", code) else type(exc).__name__
            return AccountAuthError(f"Google sign-in failed ({safe})")
        if isinstance(exc, Warning) and "scope" in str(exc).casefold():
            # oauthlib raises Warning("Scope has changed ...") when a box was unticked and the
            # relaxed mode was not in effect.
            return AccountAuthError("Google sign-in did not grant every permission; "
                                    "approve again and allow all of them", problem=PROBLEM_SCOPE)
        return AccountError(f"Google sign-in failed ({type(exc).__name__})")

    def disconnect(self) -> None:
        """Delete the saved sign-in and forget which Google account the alias is (README:
        "Disconnecting"; the confirmation's "No, use another account"): the next sign-in may pick
        any account and binds it, unconfirmed. When the saved sign-in can't be deleted (the file
        is in use), the binding is kept as it is and AccountError says so: a token left behind
        without its binding would be an account nobody confirmed (change_problem refuses it
        too, but the sign-in that follows must start fresh)."""
        with self._lock:
            if not self.forget("disconnected", problem=PROBLEM_SIGNED_OUT):
                who = f"the {self.alias} account's" if self.alias else "the"
                raise AccountError(f"Could not delete {who} saved Google sign-in (the file is in use); "
                                   "nothing was changed - try again")
            if self._bindings is not None:
                self._bindings.unbind(self.alias)

    def forget(self, reason: str, *, problem: str = PROBLEM_EXPIRED, message: str = "") -> bool:
        """Delete the saved sign-in (the next use signs in again) and remember why. True when no
        saved sign-in is left (deleted, or there was none); False when the file could not be
        deleted (logged)."""
        with self._lock:
            self._creds = None
            self._granted = None
            self.generation += 1
            if problem != PROBLEM_SIGNED_OUT:
                self._set_problem(problem, message)
            try:
                self.token_path.unlink()
            except FileNotFoundError:
                return True
            except OSError as exc:
                self._log.warning("%s: could not delete the saved sign-in (%s)", self.log_name,
                                  exc.strerror or type(exc).__name__)
                return False
            if self.alias:
                self._log.warning("%s: removed the saved sign-in (%s); the next sign-in starts fresh",
                                  self.log_name, reason)
            else:
                self._log.warning("Google Calendar: removed the saved sign-in (%s); "
                                  "the next Approve signs in again", reason)
            return True

    # ---- files ----------------------------------------------------------------------------

    def _require_setup(self) -> None:
        if not self.is_configured():
            raise AccountSetupError(f"{self._setup_hint} ({self.client_secret_path.name} was not found)")
        if not self._libraries_hook():
            raise AccountSetupError(f"{self._setup_hint} (the Google packages are missing: "
                                    "py -3.13 -m pip install -r requirements.txt)")

    def _read_client_config(self) -> dict[str, Any]:
        """Validate the client secret file and register its secret for redaction."""
        name = self.client_secret_path.name
        try:
            data = json.loads(self.client_secret_path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            raise AccountSetupError(f"{self._setup_hint} ({name} was not found)") from None
        except OSError as exc:
            problem = exc.strerror or type(exc).__name__
            raise AccountSetupError(f"{self._setup_hint} (could not read {name}: {problem})") from None
        except ValueError:
            raise AccountSetupError(f"{self._setup_hint} ({name} is not valid JSON)") from None
        installed = data.get("installed") if isinstance(data, dict) else None
        if not isinstance(installed, dict) or not all(
                isinstance(installed.get(key), str) and installed[key].strip()
                for key in ("client_id", "client_secret")):
            raise AccountSetupError(f"{self._setup_hint} ({name} is not an OAuth client of type Desktop app)")
        register_secret(installed["client_secret"])
        return data

    def _read_token(self, *, quiet: bool = False) -> Any | None:
        """Saved credentials, or None when missing or unusable (logged unless quiet; never raised).

        The credentials ask for the scopes the file says were granted, so a refresh never asks
        for a permission that was unticked.
        """
        log = self._log.debug if quiet else self._log.warning
        try:
            text = self.token_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            log("%s: could not read the saved sign-in (%s)", self.log_name, exc.strerror or type(exc).__name__)
            return None
        from google.oauth2.credentials import Credentials

        try:
            info = json.loads(text)
            if not isinstance(info, dict):
                raise ValueError("not a JSON object")
            creds = Credentials.from_authorized_user_info(info, _info_scopes(info, LEGACY_SCOPES))
        except (ValueError, TypeError) as exc:
            if self.alias:
                log("%s: the saved sign-in is unusable (%s); the next sign-in starts fresh", self.log_name,
                    type(exc).__name__)
            else:
                log("Google Calendar: the saved sign-in is unusable (%s); the next Approve signs in again",
                    type(exc).__name__)
            return None
        _register_credentials(creds)
        return creds

    def _save_token(self, creds: Any, info: dict[str, Any] | None = None) -> None:
        """Write the token JSON atomically. A failure is logged, not raised."""
        path = self.token_path
        part = path.with_name(path.name + ".part")
        try:
            text = json.dumps(info) if info is not None else creds.to_json()
            path.parent.mkdir(parents=True, exist_ok=True)
            part.write_text(text, encoding="utf-8")
            os.replace(part, path)
        except OSError as exc:
            _unlink_quietly(part)
            self._log.warning("%s: could not save the sign-in (%s); you will be asked to sign in "
                              "again next time", self.log_name, exc.strerror or type(exc).__name__)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _token_info(path: Path) -> dict[str, Any] | None:
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def _info_scopes(info: dict[str, Any], default: Sequence[str]) -> list[str]:
    """The granted scopes a token file names ("scopes": a list or space-separated text);
    ``default`` for files written before scopes were saved."""
    scopes = info.get("scopes")
    if isinstance(scopes, str):
        scopes = scopes.split()
    if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
        return list(default)
    return [scope for scope in scopes if scope]


def _granted_scopes(creds: Any, requested: Sequence[str]) -> list[str]:
    """The scopes Google granted (the token response's "scope"), requested ones first; the
    requested ones when the credentials do not say."""
    granted = getattr(creds, "granted_scopes", None)
    if isinstance(granted, str):
        granted = granted.split()
    if not isinstance(granted, (list, tuple)) or not granted:
        return list(requested)
    given = {str(scope) for scope in granted}
    ordered = [scope for scope in requested if scope in given]
    return ordered + sorted(given - set(ordered))


def _asked_scopes(info: dict[str, Any]) -> list[str]:
    """The scopes the sign-in that wrote a token file asked for ([] when the file does not say)."""
    asked = info.get(TOKEN_ASKED_KEY)
    if not isinstance(asked, list) or not all(isinstance(scope, str) for scope in asked):
        return []
    return [scope for scope in asked if scope]


def _creds_info(creds: Any, granted: Sequence[str], *, sub: str = "",
                asked: Sequence[str] = ()) -> dict[str, Any] | None:
    """The token file content with "scopes" set to what was granted and, with ``sub``, the Google
    account the token was issued for; with ``asked``, the scopes the sign-in asked for (None if
    unreadable)."""
    try:
        info = json.loads(creds.to_json())
    except (AttributeError, TypeError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    if list(granted) != _info_scopes(info, granted):
        info["scopes"] = list(granted)
    if sub:
        info[TOKEN_SUB_KEY] = sub
    if asked:
        info[TOKEN_ASKED_KEY] = list(asked)
    return info


def _credentials_from_info(info: dict[str, Any]) -> Any | None:
    from google.oauth2.credentials import Credentials

    try:
        return Credentials.from_authorized_user_info(info, _info_scopes(info, ()))
    except (ValueError, TypeError):
        return None


class _relaxed_scope:   # noqa: N801 - used as a context manager
    """OAUTHLIB_RELAX_TOKEN_SCOPE=1 for the duration of one sign-in (restored after)."""

    def __enter__(self) -> None:
        _relax_lock.acquire()
        self._before = os.environ.get(_RELAX_SCOPE)
        os.environ[_RELAX_SCOPE] = "1"

    def __exit__(self, *_exc: Any) -> None:
        try:
            if self._before is None:
                os.environ.pop(_RELAX_SCOPE, None)
            else:
                os.environ[_RELAX_SCOPE] = self._before
        finally:
            _relax_lock.release()


# --------------------------------------------------------------------------
# HTTP: a change is sent at most once
# --------------------------------------------------------------------------

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_single_send_class: type | None = None


def single_send_http(timeout: float) -> Any:
    """An httplib2.Http (``timeout`` seconds per socket operation) that sends anything but a
    GET / HEAD at most once per call: where httplib2 would send it again (the first answer
    was a broken status line), RequestNotResent is raised instead. A 401 answer is still
    re-sent once with a refreshed token by google_auth_httplib2 (each of those is its own
    call); Google did not carry out a request it answered with 401."""
    global _single_send_class
    if _single_send_class is None:
        import httplib2

        class SingleSendHttp(httplib2.Http):
            def _conn_request(self, conn: Any, request_uri: Any, method: Any, body: Any,
                              headers: Any) -> Any:
                if str(method).upper() not in _SAFE_METHODS:
                    conn = _OneRequest(conn)
                return super()._conn_request(conn, request_uri, method, body, headers)

        _single_send_class = SingleSendHttp
    return _single_send_class(timeout=timeout)


class _OneRequest:
    """Wraps one httplib2 connection for one call: ``request`` may be called once."""

    def __init__(self, conn: Any) -> None:
        self._one_request_conn = conn
        self._one_request_sent = False

    def request(self, *args: Any, **kwargs: Any) -> Any:
        if self._one_request_sent:
            try:
                self._one_request_conn.close()
            finally:
                raise RequestNotResent("the change was not sent a second time")
        self._one_request_sent = True
        return self._one_request_conn.request(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._one_request_conn, name)


def _installed_app_flow(client_secret_path: Path, scopes: list[str]) -> Any:
    from google_auth_oauthlib.flow import InstalledAppFlow

    # Same as InstalledAppFlow.from_client_secrets_file, but tolerates a UTF-8 BOM.
    with open(client_secret_path, encoding="utf-8-sig") as fh:
        client_config = json.load(fh)
    return InstalledAppFlow.from_client_config(client_config, scopes)


def _refresh_credentials(creds: Any) -> None:
    from google.auth.transport.requests import Request

    creds.refresh(Request())


def google_libraries_available() -> bool:
    """True when the Google client packages from requirements.txt are installed."""
    global _libraries_available
    if _libraries_available is None:
        try:
            _libraries_available = all(importlib.util.find_spec(name) is not None
                                       for name in _GOOGLE_MODULES)
        except (ImportError, ValueError):
            _libraries_available = False
    return _libraries_available


def _register_credentials(creds: Any) -> None:
    for name in ("token", "refresh_token", "client_secret", "id_token"):
        value = getattr(creds, name, None)
        if isinstance(value, str):
            register_secret(value)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
