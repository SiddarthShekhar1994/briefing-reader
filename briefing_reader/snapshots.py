"""Page snapshots of a web research, the rules (Qt-free): which pages may be pictured, what the
hidden private window may load, how many pictures a research gets, and the words the LIVE view shows.

    snapshot_url_problem(url)         why a page a research READ may not be pictured ("" = it may):
                                      only public https pages (actions.web_link_problem), never a
                                      search results page
    request_problem(url, main_frame=, local=)
                                      the request interceptor's and the navigation check's decision
                                      for one request of the page ("" = allow): https to public hosts
                                      only (a main frame on port 443, by name), data: / blob:
                                      subresources, about:blank; everything else is blocked
    final_host_note(asked, final)     "redirected to www.example.net" ("" = the page stayed on its host)
    chromium_flags(existing, offline=, gate_port=)
                                      QTWEBENGINE_CHROMIUM_FLAGS with Jarvis's flags merged in (and the
                                      dead proxy of a test run, or the network gate's proxy)
    flag_tokens(value)                QTWEBENGINE_CHROMIUM_FLAGS split as QtWebEngine splits it (two
                                      double quotes group text and are removed)
    sandbox_problem(environ)          R_SANDBOX_OFF when the web engine's renderer sandbox is turned off
    engine_flags_problem(environ)     R_UNSAFE_FLAGS when inherited web engine settings could undo the
                                      private window's guarantees (remote debugging, a TLS key log,
                                      a quoted value, a bare "--", or any flags switch not on the
                                      short list of tolerated ones)
    tunnel_target(authority)          the network gate's decision for one CONNECT "host:port" (port 443,
                                      a public IPv4 address or a public name)
    resolved_problem(addresses)       the gate's decision for the addresses a name resolved to (every
                                      one a public IPv4 address, else refused: DNS rebinding)
    reason_kind(reason)               the one word a log line may carry for a reason ("timeout")
    over_cap_reason(cap)              the refusal past a research's picture cap, with that cap's number
    RunBudget                         at most MAX_PER_RUN pictures per research, one per page (thread-safe)
    ShotResult                        what one shot gave: the JPEG bytes and sizes, or a reason

Rules: nothing here touches the network, Qt, a file or the log; every function is pure, fast and
never raises (the interceptor and the navigation check call request_problem on the GUI thread for
every request of a page). request_problem checks names only; where a name resolves is checked by
the network gate (snapshot_gate: every connection of the private window goes through it, and it
connects only to the public address it checked). The gate connects over IPv4 only: a global IPv6
address may be this PC's own or a device's on its network (and a NAT64 one may carry a private IPv4
address), which a public-address test cannot tell from an internet host. Chromium never looks a
name up itself (RESOLVER_RULES). repr() of every object here names counts and sizes only, never a
URL.
"""

from __future__ import annotations

import ipaddress
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from .actions import link_host, public_host_problem, web_link_problem
from .pictures import NOTE_OPENED, NOTE_OPENING

__all__ = [
    "MAX_PER_RUN", "VIEWPORT", "MAX_WIDTH", "TIMEOUT_S", "SETTLE_MS", "BLANK_RETRY_MS", "BLANK_RETRIES",
    "JPEG_QUALITY", "JPEG_QUALITY_SMALL", "START_DELAY_MS", "IDLE_STOP_S", "LOCAL_BASE_URL", "CHROMIUM_FLAGS",
    "OFFLINE_FLAGS",
    "NOTE_OPENING", "NOTE_OPENED", "snapshot_url_problem", "request_problem", "final_host_note",
    "chromium_flags", "flag_tokens", "sandbox_problem", "engine_flags_problem", "tunnel_target",
    "resolved_problem", "reason_kind", "over_cap_reason", "RunBudget", "ShotResult", "GATE_FLAGS", "GATE_PORT",
    "RESOLVER_RULES",
]

MAX_PER_RUN = 4                 # pictures per research run (the fetch cap); min(4, [research] max_fetches)
VIEWPORT = (1280, 800)          # logical px of the hidden view
MAX_WIDTH = 960                 # stored image width at most
TIMEOUT_S = 15.0                # one snapshot, from load start
SETTLE_MS = 600                 # after loadFinished(True), before the grab
BLANK_RETRY_MS = 500            # a uniform grab is retried after this ...
BLANK_RETRIES = 2               # ... this many times, then "showed nothing"
JPEG_QUALITY = 80
JPEG_QUALITY_SMALL = 60         # when the first encoding is over the picture cap
START_DELAY_MS = 250            # the first shot starts after this, so "Opening..." is drawn first
IDLE_STOP_S = 10.0              # no request and no ended run within this -> the engine stops anyway
LOAD_FAIL_GRACE_MS = 300        # a failed load followed by a new one (a script's redirect) within this goes on
LOCAL_BASE_URL = "https://snapshot.invalid/"   # base URL of local HTML (tests / harness)
# Chromium never looks a name up itself: every page name is resolved (and checked) by the network
# gate, and the proxy is an IP address. Without this a page could make this PC ask its resolver - the
# network, LLMNR / NetBIOS broadcasts, mDNS - for names of its choosing (WebRTC looks a TURN server's
# name up before anything reaches the proxy). "^NOTFOUND" is the rule's "no such name"; the proxy's
# 127.0.0.1 stays reachable. It holds spaces: chromium_flags writes it in double quotes, which
# QtWebEngine removes.
RESOLVER_RULES = "--host-resolver-rules=MAP * ^NOTFOUND,EXCLUDE 127.0.0.1"
# WebAuthenticationUseNativeWinApi off: a page cannot reach Windows Hello (its own "Windows Security"
# dialog would show on the owner's screen). WebRTC sends no UDP that bypasses the proxy (STUN / TURN
# to this PC or its network); its TCP goes through the proxy like everything else. --log-level=3:
# Chromium's own error lines (some name a host a page chose) are not printed to a console start's
# stderr; --disable-logging alone does not stop them.
CHROMIUM_FLAGS = ("--disable-features=AudioServiceOutOfProcess,WebAuthenticationUseNativeWinApi",
                  "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-gpu", "--disable-logging",
                  "--log-level=3", "--mute-audio", RESOLVER_RULES)
# On the offscreen platform (tests, harnesses) every connection also goes to a proxy that is not there
# (loopback included): a last net under the request checks, so no test can reach the network.
OFFLINE_FLAGS = ("--proxy-server=127.0.0.1:9", "--proxy-bypass-list=<-loopback>")
# Elsewhere every connection goes through Jarvis's network gate (snapshot_gate.ConnectGate) on this
# port of 127.0.0.1, loopback included; with a proxy Chromium also sends no QUIC (WebTransport).
GATE_FLAGS = ("--proxy-server=127.0.0.1:{port}", "--proxy-bypass-list=<-loopback>")
GATE_PORT = 443                 # the one port the gate connects to

# Reasons ("Picture unavailable: <reason>"), shown in LIVE; a log line carries reason_kind() only.
R_NOT_PUBLIC_HTTPS = "Jarvis opens only public https pages"   # + the rule's own words in brackets
R_SEARCH_PAGE = "a search results page is not opened"
R_OVER_CAP = "at most 4 page pictures per research"   # over_cap_reason(MAX_PER_RUN); a lower cap says its own
R_NO_PICTURES = "no page pictures for a research that reads no pages ([research] max_fetches = 0)"
R_SAME_PAGE = "the same page as an earlier picture"
R_TIMEOUT = "the page did not load within 15 s"
R_LOAD_FAILED = "the page did not load on this PC"
R_TOO_LARGE = "the picture was too large to keep in memory"
R_BLANK = "the page showed nothing to picture"
R_CRASHED = "the page stopped working on this PC"
R_NO_ENGINE = "this PC's PySide6 has no web engine (QtWebEngine)"
R_ENGINE_FAILED = "the private window could not be started"
R_SANDBOX_OFF = "the web engine's sandbox is turned off on this PC"
R_UNSAFE_FLAGS = "the web engine's safety settings are changed on this PC"
R_CANCELLED = "the request was cancelled"
R_TEST_RUN = "no page is opened in a test run"
R_TEXT_OFF = "not kept ([live] text = false)"
R_SHUTDOWN = "Jarvis closed"

_REASON_KINDS = {
    R_TIMEOUT: "timeout", R_LOAD_FAILED: "load_failed", R_TOO_LARGE: "too_large", R_BLANK: "blank",
    R_CRASHED: "crashed", R_NO_ENGINE: "no_engine", R_ENGINE_FAILED: "engine_failed", R_SANDBOX_OFF: "sandbox",
    R_UNSAFE_FLAGS: "unsafe_flags", R_CANCELLED: "cancelled", R_TEST_RUN: "test_run", R_SHUTDOWN: "shutdown",
}

# request_problem's answers (truthy = blocked; never a URL in them)
BLOCKED = "blocked"
B_SCHEME = "blocked: not https"
B_LOCAL = "blocked: a local or private address"
B_MAIN_FRAME = "blocked: not a page Jarvis opens"
B_CREDENTIALS = "blocked: a user name or password in the address"

# tunnel_target / resolved_problem's answers (the gate never logs them with a host)
G_TARGET = "refused: not a host and port"
G_PORT = "refused: not the https port"
G_LOCAL = "refused: a local or private address"
G_IPV6 = "refused: an IPv6 address (the gate connects over IPv4 only)"
G_UNRESOLVED = "refused: the name did not resolve"

_LOCAL_SCHEMES = frozenset({"data", "blob"})
_ABOUT_PAGES = frozenset({"blank", "srcdoc"})
_MAIN_FRAME_PORTS = (None, 443)
_SANDBOX_ENV = "QTWEBENGINE_DISABLE_SANDBOX"
_DEBUG_ENV = "QTWEBENGINE_REMOTE_DEBUGGING"
_FLAGS_ENV = "QTWEBENGINE_CHROMIUM_FLAGS"
_KEY_LOG_ENV = "SSLKEYLOGFILE"   # Chromium (like Python's ssl) appends every TLS session's secrets there
_NO_SANDBOX_FLAGS = frozenset({"no-sandbox", "single-process"})
_SWITCH_TERMINATOR = "--"       # Chromium reads every token after it as a plain argument, not a switch
# Proxy switches: replaced by the test run's dead proxy or the gate's (never left to win).
_PROXY_SWITCHES = frozenset({"proxy-server", "proxy-bypass-list", "proxy-pac-url", "no-proxy-server",
                             "proxy-auto-detect", "winhttp-proxy-resolver"})
_FEATURES_OFF = "--disable-features"
# The inherited QTWEBENGINE_CHROMIUM_FLAGS switches Jarvis tolerates (names without their "--", "-"
# or "/": Chromium on Windows reads all three): his own (chromium_flags adds them once or replaces
# them), the proxy switches (replaced) and a few that only change how pages are drawn. Every other
# switch, and every plain word, is refused (R_UNSAFE_FLAGS), never quietly dropped: Chromium has more
# switches that write files (logs, traces, TLS key logs), open a debugging port, sign in with this
# PC's Windows account or weaken the sandbox than a list of refused ones could keep up with. An
# inherited --disable-features may only name features Jarvis turns off himself.
_TOLERATED_SWITCHES = frozenset({
    "disable-gpu", "disable-logging", "log-level", "mute-audio", "force-webrtc-ip-handling-policy",
    "lang", "force-device-scale-factor", "disable-gpu-compositing", "disable-direct-composition",
    "disable-smooth-scrolling", "disable-lcd-text", "force-color-profile"}) | _PROXY_SWITCHES
_OWN_FEATURES_OFF = frozenset(part for flag in CHROMIUM_FLAGS if flag.startswith(_FEATURES_OFF + "=")
                              for part in flag.partition("=")[2].split(",") if part)
_MAX_RUNS = 64                  # RunBudget forgets the oldest runs beyond this many


# --------------------------------------------------------------------------
# Which pages may be pictured
# --------------------------------------------------------------------------

def _labels_before_suffix(host: str, name: str) -> bool:
    """``host`` is ``name`` under a public suffix ("google.com", "www.google.co.uk",
    "news.google.de"): a label equal to ``name`` followed by one or two short labels."""
    labels = host.split(".")
    for index, label in enumerate(labels):
        rest = labels[index + 1:]
        if label == name and 1 <= len(rest) <= 2 and all(part.isalpha() and len(part) <= 4 for part in rest):
            return True
    return False


def _under(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _path_is(path: str, *prefixes: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _search_page(host: str, path: str, query: str) -> bool:
    """A search engine's results page (it lists other pages; a picture of it shows nothing read)."""
    host = host.casefold().rstrip(".")
    path = unquote(path or "/").casefold().rstrip("/") or "/"
    if _labels_before_suffix(host, "google") and _path_is(path, "/search"):
        return True
    if _under(host, "bing.com") and _path_is(path, "/search"):
        return True
    if _under(host, "duckduckgo.com") and any(key == "q" for key, _value in parse_qsl(query, keep_blank_values=True)):
        return True
    if _under(host, "search.yahoo.com") or host == "search.brave.com":
        return True
    if _labels_before_suffix(host, "yandex") and _path_is(path, "/search"):
        return True
    if _under(host, "baidu.com") and path == "/s":
        return True
    if _under(host, "ecosia.org") and _path_is(path, "/search"):
        return True
    if _under(host, "startpage.com") and _path_is(path, "/do/search", "/sp/search"):
        return True
    return False


def snapshot_url_problem(url: Any) -> str:
    """Why a page a research read or cites may not be pictured ("" = it may): it must pass
    actions.web_link_problem (https, a public host by name, no IP address, no user:pass, port none
    or 443, no dot segments, at most 2,048 characters, none of | < > " `), and it is not a search
    results page (Google, Bing, DuckDuckGo, Yahoo, Brave, Yandex, Baidu, Ecosia, Startpage).
    International (xn--) hosts are allowed: the picture's note carries the warning."""
    try:
        problem = web_link_problem(url)
        if problem:
            return f"{R_NOT_PUBLIC_HTTPS} ({problem})"
        parts = urlsplit(url)
        if _search_page(parts.hostname or "", parts.path, parts.query):
            return R_SEARCH_PAGE
        return ""
    except Exception:  # noqa: BLE001 - a rule never raises
        return R_NOT_PUBLIC_HTTPS


# --------------------------------------------------------------------------
# What the hidden private window may load
# --------------------------------------------------------------------------

def _ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """A public IPv4 address: globally reachable (not loopback, private, link-local, shared,
    reserved ...). Never an IPv6 address: a global one may be this PC's own or a device's on its
    network (a router at <prefix>::1), and a NAT64 one (64:ff9b::/96) may carry a private IPv4
    address - nothing here can tell those from an internet host. The gate connects over IPv4 only."""
    return isinstance(address, ipaddress.IPv4Address) and address.is_global


def request_problem(url: Any, *, main_frame: bool, local: bool = False) -> str:
    """The decision for one request of a pictured page ("" = allow; anything else = block). ``url``
    is the request's URL fully encoded (an ASCII host). Never raises (any failure blocks).

    - https: no user name or password; an IP-address host is blocked unless it is a public IPv4
      address (a subresource only: a main frame is never an IP address), a name is blocked when
      actions.public_host_problem refuses it (localhost, a name without a dot, .local / .lan /
      .internal / .home.arpa ..., LOCAL_DOMAINS, names that spell a private IPv4 address); a main
      frame also only on port 443; a subresource on any port.
    - data:, blob: subresources; a data: main frame only for local HTML (``local``: tests, harness).
    - about:blank (and about:srcdoc, an inline frame's own text).
    - every other scheme (http, file, ftp, ws, wss, chrome, qrc, javascript, filesystem,
      view-source, mailto ...) is blocked.
    """
    try:
        if not isinstance(url, str) or not url or ":" not in url:
            return BLOCKED
        scheme = url.split(":", 1)[0].casefold()
        if scheme == "about":
            page = url.split(":", 1)[1].split("#", 1)[0].split("?", 1)[0].casefold()
            return "" if page in _ABOUT_PAGES else B_SCHEME
        if scheme in _LOCAL_SCHEMES:
            if not main_frame or (scheme == "data" and local):
                return ""
            return B_MAIN_FRAME
        if scheme != "https":
            return B_SCHEME
        if not url.isascii() or any(char.isspace() for char in url):
            return BLOCKED
        parts = urlsplit(url)
        if parts.username is not None or parts.password is not None:
            return B_CREDENTIALS
        port = parts.port   # ValueError for a bad port: blocked below
        host = (parts.hostname or "").rstrip(".")
        if not host:
            return BLOCKED
        address = _ip_literal(host)
        if address is not None:
            if main_frame or not _public_address(address):
                return B_LOCAL if not _public_address(address) else B_MAIN_FRAME
            return ""
        if public_host_problem(host):
            return B_LOCAL
        if main_frame and port not in _MAIN_FRAME_PORTS:
            return B_MAIN_FRAME
        return ""
    except Exception:  # noqa: BLE001 - the interceptor never raises: block
        return BLOCKED


def final_host_note(asked_url: Any, final_url: Any) -> str:
    """"redirected to www.example.net" when the page ended on another host ("" otherwise)."""
    try:
        asked = link_host(asked_url) if isinstance(asked_url, str) else ""
        final = link_host(final_url) if isinstance(final_url, str) else ""
        if final and final.casefold() != asked.casefold():
            return f"redirected to {final}"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _switch(token: str) -> str:
    """A command-line token's switch name: lower case, without its value and its "--", "-" or "/"."""
    return token.partition("=")[0].casefold().lstrip("-/")


def flag_tokens(value: Any) -> list[str]:
    """QTWEBENGINE_CHROMIUM_FLAGS split the way QtWebEngine splits it before Chromium reads it: at
    white space, except between two double quotes, which group the text and are removed
    ('"--a=b c"' and '--a="b c"' are both the ONE switch --a=b c; '"--no-sandbox"' is --no-sandbox)."""
    tokens: list[str] = []
    current: list[str] = []
    quoted = started = False
    for char in str(value or ""):
        if char == '"':
            quoted, started = not quoted, True
        elif char.isspace() and not quoted:
            if started:
                tokens.append("".join(current))
                current, started = [], False
        else:
            current.append(char)
            started = True
    if started:
        tokens.append("".join(current))
    return tokens


def _flags_text(tokens: list[str]) -> str:
    """Tokens back into one QTWEBENGINE_CHROMIUM_FLAGS value (flag_tokens gives them back): a token
    with white space in it (RESOLVER_RULES) in double quotes."""
    return " ".join(f'"{token}"' if not token or any(char.isspace() for char in token) else token
                    for token in tokens)


def chromium_flags(existing: Any, *, offline: bool = False, gate_port: int = 0) -> str:
    """``existing`` QTWEBENGINE_CHROMIUM_FLAGS (read as QtWebEngine reads it: flag_tokens) with
    Jarvis's CHROMIUM_FLAGS in: a --disable-features list is merged into the one already there, a
    switch with a value replaces one already there, a plain switch is added once. ``offline`` (a
    test platform): OFFLINE_FLAGS too; else ``gate_port`` (the network gate's port): GATE_FLAGS.
    Either replaces every proxy switch already there. Jarvis's flags always go before a bare "--"
    (after it Chromium reads no switch; engine_flags_problem refuses such a value anyway)."""
    tokens = flag_tokens(existing)
    rest: list[str] = []
    if _SWITCH_TERMINATOR in tokens:
        cut = tokens.index(_SWITCH_TERMINATOR)
        tokens, rest = tokens[:cut], tokens[cut:]
    proxy: tuple[str, ...] = ()
    if offline:
        proxy = OFFLINE_FLAGS
    elif isinstance(gate_port, int) and 0 < gate_port < 65536:
        proxy = tuple(flag.format(port=gate_port) for flag in GATE_FLAGS)
    if proxy:
        tokens = [token for token in tokens if _switch(token) not in _PROXY_SWITCHES] + list(proxy)
    features_off = _switch(_FEATURES_OFF)
    for flag in CHROMIUM_FLAGS:
        name, sep, value = flag.partition("=")
        if name == _FEATURES_OFF:
            # Every --disable-features already there goes into ONE (Chromium reads only the last of
            # a repeated switch: a second inherited one would silently drop Jarvis's features).
            found = [i for i, token in enumerate(tokens) if _switch(token) == features_off]
            current: list[str] = []
            for part in [part for i in found for part in tokens[i].partition("=")[2].split(",")] + value.split(","):
                if part and part not in current:
                    current.append(part)
            merged = f"{_FEATURES_OFF}={','.join(current)}"
            if not found:
                tokens.append(merged)
                continue
            tokens = [token for i, token in enumerate(tokens) if i not in found[1:]]
            tokens[found[0]] = merged
        elif sep:
            if flag not in tokens:
                tokens = [token for token in tokens if _switch(token) != _switch(name)] + [flag]
        elif not any(_switch(token) == _switch(name) for token in tokens):
            tokens.append(flag)
    return _flags_text(tokens + rest)


def sandbox_problem(environ: Mapping[str, str]) -> str:
    """R_SANDBOX_OFF when the web engine's renderer would run without its sandbox:
    QTWEBENGINE_DISABLE_SANDBOX set (Qt reads any value as off) or --no-sandbox /
    --single-process in QTWEBENGINE_CHROMIUM_FLAGS (read as QtWebEngine reads it: a quoted
    "--no-sandbox" too); "" otherwise."""
    try:
        if str(environ.get(_SANDBOX_ENV, "") or "").strip():
            return R_SANDBOX_OFF
        flags = {_switch(token) for token in flag_tokens(environ.get(_FLAGS_ENV, ""))}
        return R_SANDBOX_OFF if flags & _NO_SANDBOX_FLAGS else ""
    except Exception:  # noqa: BLE001 - unknown: refuse
        return R_SANDBOX_OFF


def _tolerated(token: str) -> bool:
    """One inherited QTWEBENGINE_CHROMIUM_FLAGS token Jarvis accepts (_TOLERATED_SWITCHES)."""
    if not token.startswith(("-", "/")):
        return False                          # a plain word (Chromium reads it as an argument)
    name = _switch(token)
    if name == _switch(_FEATURES_OFF):
        return all(part in _OWN_FEATURES_OFF for part in token.partition("=")[2].split(",") if part)
    return name in _TOLERATED_SWITCHES


def engine_flags_problem(environ: Mapping[str, str]) -> str:
    """R_UNSAFE_FLAGS when the inherited environment could undo the private window's guarantees:
    QTWEBENGINE_REMOTE_DEBUGGING set (any program on this PC could read and drive the hidden page),
    SSLKEYLOGFILE set (Chromium would append every pictured page's TLS secrets to that file), or in
    QTWEBENGINE_CHROMIUM_FLAGS a double quote (QtWebEngine removes it: '"--no-proxy-server"' is a
    switch a plain split does not see), a bare "--" (every switch after it, Jarvis's own proxy and
    WebRTC policy included, would be read as a plain argument), or any switch or word that is not
    in _TOLERATED_SWITCHES (remote debugging, ignored certificate errors, web security or site
    isolation off, host rules, a profile folder, Chromium's own log, network log, trace or TLS key
    log, Windows sign-in to servers, a weaker sandbox ... all refused); "" otherwise. Judge the
    value Jarvis INHERITED (the camera does: snapshot_engine), never the one with his own flags
    merged in."""
    try:
        if str(environ.get(_DEBUG_ENV, "") or "").strip() or str(environ.get(_KEY_LOG_ENV, "") or "").strip():
            return R_UNSAFE_FLAGS
        value = str(environ.get(_FLAGS_ENV, "") or "")
        if '"' in value:
            return R_UNSAFE_FLAGS
        for token in flag_tokens(value):
            if token == _SWITCH_TERMINATOR or not _tolerated(token):
                return R_UNSAFE_FLAGS
        return ""
    except Exception:  # noqa: BLE001 - unknown: refuse
        return R_UNSAFE_FLAGS


# --------------------------------------------------------------------------
# The network gate's rules (snapshot_gate.ConnectGate)
# --------------------------------------------------------------------------

def tunnel_target(authority: Any) -> tuple[str, str]:
    """(host, problem) for one CONNECT request's "host:port" ("" problem = the host may be looked
    up): the port must be GATE_PORT; an IP address must be a public IPv4 address; a name must pass
    actions.public_host_problem. The host comes back in lower case without a final dot or IPv6
    brackets. Never raises (anything odd: G_TARGET)."""
    try:
        if not isinstance(authority, str):
            return "", G_TARGET
        text = authority.strip()
        if text.startswith("["):
            host, sep, rest = text[1:].partition("]")
            if not sep or not rest.startswith(":"):
                return "", G_TARGET
            port_text = rest[1:]
        else:
            host, sep, port_text = text.rpartition(":")
            if not sep or ":" in host:
                return "", G_TARGET
        host = host.casefold().rstrip(".")
        if not host or not host.isascii() or any(char.isspace() or char in "/@\\%" for char in host):
            return "", G_TARGET
        if not port_text.isascii() or not port_text.isdigit() or int(port_text) != GATE_PORT:
            return host, G_PORT
        address = _ip_literal(host)
        if address is not None:
            if address.version == 6:
                return host, G_IPV6
            return host, ("" if _public_address(address) else G_LOCAL)
        return host, (G_LOCAL if public_host_problem(host) else "")
    except Exception:  # noqa: BLE001 - a rule never raises
        return "", G_TARGET


def resolved_problem(addresses: Any) -> str:
    """"" when the gate may connect: at least one address, and every address a name resolved to is
    a public IPv4 address. One loopback, private, link-local, reserved or IPv6 address refuses the
    whole name (a public name that leads to this PC or its network: DNS rebinding). Never raises."""
    try:
        found = [_ip_literal(str(item)) for item in addresses]
        if not found:
            return G_UNRESOLVED
        if any(address is None or not _public_address(address) for address in found):
            return G_LOCAL
        return ""
    except Exception:  # noqa: BLE001
        return G_LOCAL


def over_cap_reason(cap: Any) -> str:
    """Why a page past a research's picture cap gets none, naming that cap (it is min(4, [research]
    max_fetches)): R_OVER_CAP at 4, "at most 2 page pictures per research", R_NO_PICTURES at 0."""
    try:
        count = max(0, int(cap))
    except (TypeError, ValueError, OverflowError):
        count = MAX_PER_RUN
    if count == MAX_PER_RUN:
        return R_OVER_CAP
    if count == 0:
        return R_NO_PICTURES
    return f"at most {count} page picture{'s' if count != 1 else ''} per research"


def reason_kind(reason: Any) -> str:
    """The one word a log line may carry for a reason: timeout / load_failed / too_large / blank /
    crashed / no_engine / engine_failed / sandbox / cancelled / test_run / shutdown, else refused
    ("" for no reason)."""
    if not isinstance(reason, str):
        return "refused"
    return _REASON_KINDS.get(reason, "refused") if reason else ""


# --------------------------------------------------------------------------
# Pictures per research
# --------------------------------------------------------------------------

def _page_key(url: str) -> str:
    """The page an address names, for "the same page": scheme and host in lower case, the path
    ("/" when empty) and the query; the fragment ignored."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold().rstrip(".")
        return f"{parts.scheme.casefold()}://{host}{parts.path or '/'}?{parts.query}"
    except ValueError:
        return url


@dataclass
class _Run:
    pages: set[str] = field(default_factory=set)
    count: int = 0
    ended: bool = False
    cancelled: bool = False


class RunBudget:
    """How many pictures each research run (keyed by its LIVE task id) got, and which pages:
    at most ``cap`` per run, one per page. Thread-safe; memory only (page keys, never logged)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[int, _Run] = {}

    def __repr__(self) -> str:
        with self._lock:
            return f"RunBudget(runs={len(self._runs)})"

    def _run(self, task_id: int) -> _Run:
        run = self._runs.get(task_id)
        if run is None:
            run = self._runs[task_id] = _Run()
            if len(self._runs) > _MAX_RUNS:   # the oldest ended runs go first, then the oldest
                stale = [key for key, value in self._runs.items() if value.ended and key != task_id]
                for key in (stale or [key for key in self._runs if key != task_id])[:len(self._runs) - _MAX_RUNS]:
                    del self._runs[key]
        return run

    def admit(self, task_id: int, url: str, cap: int = MAX_PER_RUN) -> str:
        """"" and the page counted, or why it gets no picture (R_SAME_PAGE, over_cap_reason(cap),
        R_CANCELLED for a cancelled run)."""
        key = _page_key(url) if isinstance(url, str) else ""
        with self._lock:
            run = self._run(task_id)
            if run.cancelled:
                return R_CANCELLED
            if key in run.pages:
                return R_SAME_PAGE
            if run.count >= max(0, int(cap)):
                return over_cap_reason(cap)
            run.pages.add(key)
            run.count += 1
            return ""

    def taken(self, task_id: int) -> int:
        """Pictures admitted so far for the run."""
        with self._lock:
            run = self._runs.get(task_id)
            return run.count if run is not None else 0

    def end(self, task_id: int) -> None:
        with self._lock:
            self._run(task_id).ended = True

    def ended(self, task_id: int) -> bool:
        with self._lock:
            run = self._runs.get(task_id)
            return run is not None and run.ended

    def cancel(self, task_id: int) -> None:
        with self._lock:
            run = self._run(task_id)
            run.cancelled = True
            run.ended = True

    def cancelled(self, task_id: int) -> bool:
        with self._lock:
            run = self._runs.get(task_id)
            return run is not None and run.cancelled

    def forget(self, task_id: int) -> None:
        with self._lock:
            self._runs.pop(task_id, None)


# --------------------------------------------------------------------------
# One shot's result
# --------------------------------------------------------------------------

@dataclass(frozen=True, repr=False)
class ShotResult:
    """What one shot gave: ``ok`` with the image (JPEG, PNG when JPEG cannot be written), its
    pixel size, the page's final URL, the time it took, whether it still loaded at the time limit
    and how many of its requests were blocked; else ``reason`` (an R_* text)."""

    ok: bool
    reason: str = ""
    image: bytes = b""
    image_format: str = "JPG"
    width: int = 0
    height: int = 0
    final_url: str = ""
    took_ms: int = 0
    still_loading: bool = False
    blocked: int = 0

    def __repr__(self) -> str:   # never the URL or the image
        if not self.ok:
            return f"ShotResult(ok=False, reason={reason_kind(self.reason)!r}, blocked={self.blocked})"
        return (f"ShotResult(ok=True, {self.image_format} {self.width}x{self.height}, {len(self.image)} bytes, "
                f"took_ms={self.took_ms}, still_loading={self.still_loading}, blocked={self.blocked})")
