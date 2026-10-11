"""Tests for briefing_reader.snapshots: which pages a research's snapshot may open, what the hidden
private window may load, the Chromium flags, the sandbox and unsafe-settings checks, the network
gate's rules, the per-run budget and ShotResult.

Qt-free and pure: nothing is opened, resolved or loaded. Invented addresses only.
"""

from __future__ import annotations

import threading
import unittest

from briefing_reader import actions, snapshots
from briefing_reader.snapshots import (
    R_CANCELLED,
    R_NOT_PUBLIC_HTTPS,
    R_OVER_CAP,
    R_SAME_PAGE,
    R_SANDBOX_OFF,
    R_SEARCH_PAGE,
    R_UNSAFE_FLAGS,
    RunBudget,
    ShotResult,
    chromium_flags,
    engine_flags_problem,
    final_host_note,
    flag_tokens,
    reason_kind,
    request_problem,
    resolved_problem,
    sandbox_problem,
    snapshot_url_problem,
    tunnel_target,
)


class SnapshotUrlTests(unittest.TestCase):
    def test_public_https_pages_may_be_pictured(self) -> None:
        for url in ("https://www.example.org/", "https://docs.example.com/guide/start?x=1#part",
                    "https://www.example.org:443/a", "https://www.google.com/maps/place/x",
                    "https://www.bing.com/maps", "https://duckduckgo.com/about", "https://news.example.net/s",
                    "https://xn--bcher-kva.example/"):
            with self.subTest(url=url):
                self.assertEqual(snapshot_url_problem(url), "")

    def test_refused_pages_say_why_without_the_address(self) -> None:
        cases = ("http://www.example.org/", "https://93.184.216.34/", "https://[2001:db8::1]/", "https://localhost/",
                 "https://nas.local/", "https://printer/", "https://ana:secret@www.example.org/",
                 "https://www.example.org:8443/", "https://www.example.org/" + "a" * 2_030, "ftp://www.example.org/",
                 "https://www.example.org/a|b", "javascript:alert(1)", "", None, 42)
        for url in cases:
            with self.subTest(url=str(url)[:60]):
                problem = snapshot_url_problem(url)
                self.assertTrue(problem.startswith(R_NOT_PUBLIC_HTTPS), problem)
                self.assertNotIn("example", problem)

    def test_the_rules_own_words_come_in_brackets(self) -> None:
        self.assertEqual(snapshot_url_problem("https://nas.local/"), f"{R_NOT_PUBLIC_HTTPS} ({actions.LOCAL_ADDRESS})")

    def test_a_2048_character_address_is_the_limit(self) -> None:
        base = "https://www.example.org/"
        self.assertEqual(snapshot_url_problem(base + "a" * (2_048 - len(base))), "")
        self.assertTrue(snapshot_url_problem(base + "a" * (2_049 - len(base))))

    def test_search_results_pages_are_not_opened(self) -> None:
        for url in ("https://www.google.com/search?q=budget", "https://google.co.uk/search?q=x",
                    "https://www.google.de/search/about-this-result?q=x", "https://www.bing.com/search?q=x",
                    "https://duckduckgo.com/?q=x", "https://html.duckduckgo.com/html/?q=x",
                    "https://search.yahoo.com/search?p=x", "https://uk.search.yahoo.com/search?p=x",
                    "https://search.brave.com/search?q=x", "https://yandex.ru/search/?text=x",
                    "https://www.baidu.com/s?wd=x", "https://www.ecosia.org/search?q=x",
                    "https://www.startpage.com/do/search?q=x", "https://www.google.com/%73earch?q=x"):
            with self.subTest(url=url):
                self.assertEqual(snapshot_url_problem(url), R_SEARCH_PAGE)

    def test_a_search_engines_other_pages_are_allowed(self) -> None:
        for url in ("https://www.google.com/", "https://www.google.com/searchconsole", "https://duckduckgo.com/",
                    "https://www.bing.com/", "https://www.baidu.com/sa", "https://www.example.org/search?q=x"):
            with self.subTest(url=url):
                self.assertEqual(snapshot_url_problem(url), "")


class RequestProblemTests(unittest.TestCase):
    def blocked(self, url: object, *, main_frame: bool, local: bool = False) -> bool:
        return bool(request_problem(url, main_frame=main_frame, local=local))

    def test_public_https(self) -> None:
        for main in (True, False):
            with self.subTest(main_frame=main):
                self.assertFalse(self.blocked("https://www.example.org/page", main_frame=main))
                self.assertFalse(self.blocked("https://cdn.example.net:443/a.png", main_frame=main))

    def test_ports_only_for_subresources(self) -> None:
        self.assertTrue(self.blocked("https://www.example.org:8443/", main_frame=True))
        self.assertFalse(self.blocked("https://cdn.example.net:8443/a.js", main_frame=False))

    def test_local_schemes(self) -> None:
        self.assertFalse(self.blocked("data:image/png;base64,AAAA", main_frame=False))
        self.assertFalse(self.blocked("blob:https://www.example.org/1234", main_frame=False))
        self.assertTrue(self.blocked("data:text/html,hi", main_frame=True))
        self.assertFalse(self.blocked("data:text/html,hi", main_frame=True, local=True))
        self.assertTrue(self.blocked("blob:https://www.example.org/1234", main_frame=True))
        self.assertTrue(self.blocked("blob:https://www.example.org/1234", main_frame=True, local=True))
        for main in (True, False):
            self.assertFalse(self.blocked("about:blank", main_frame=main))
            self.assertFalse(self.blocked("about:srcdoc", main_frame=main))
            self.assertTrue(self.blocked("about:settings", main_frame=main))

    def test_every_other_scheme_is_blocked(self) -> None:
        for url in ("file:///C:/Windows/win.ini", "http://www.example.org/", "ftp://www.example.org/",
                    "ws://www.example.org/s", "wss://www.example.org/s", "chrome://settings", "qrc:/x",
                    "javascript:alert(1)", "filesystem:https://www.example.org/temporary/x",
                    "view-source:https://www.example.org/", "mailto:ana@example.com", "chrome-extension://abc/x"):
            for main in (True, False):
                with self.subTest(url=url, main_frame=main):
                    self.assertTrue(self.blocked(url, main_frame=main))
                    self.assertTrue(self.blocked(url, main_frame=main, local=True))

    def test_local_and_private_addresses_are_blocked(self) -> None:
        for host in ("127.0.0.1", "10.0.0.5", "172.16.0.1", "192.168.1.1", "169.254.1.1", "100.64.0.1", "0.0.0.0",
                     "[::1]", "[fe80::1]", "[::ffff:192.168.1.1]", "[::ffff:7f00:1]", "[fc00::1]",
                     "[2002:c0a8:101::1]", "localhost", "foo.localhost", "printer", "nas.local", "router.internal",
                     "box.lan", "home.home.arpa", "snapshot.invalid", "192-168-1-1.example.net",
                     "10.0.0.5.example.net", "nip.io", "192.168.1.1.nip.io", "routerlogin.net"):
            for main in (True, False):
                with self.subTest(host=host, main_frame=main):
                    self.assertTrue(self.blocked(f"https://{host}/x", main_frame=main))
                    self.assertTrue(self.blocked(f"https://{host}:8443/x", main_frame=main))

    def test_a_public_ip_literal_only_as_a_subresource(self) -> None:
        self.assertFalse(self.blocked("https://93.184.216.34/a.png", main_frame=False))
        self.assertFalse(self.blocked("https://93.184.216.34:8443/a.png", main_frame=False))
        self.assertTrue(self.blocked("https://93.184.216.34/", main_frame=True))
        # never an IPv6 address: a global one may be this PC's own or a device's on its network, a
        # NAT64 one may carry a private IPv4 address (the gate connects over IPv4 only)
        for host in ("[2606:4700::1]", "[2a01:4f8:1:2::1]", "[64:ff9b::a00:1]", "[64:ff9b::c0a8:101]"):
            for main in (True, False):
                with self.subTest(host=host, main_frame=main):
                    self.assertTrue(self.blocked(f"https://{host}/a.png", main_frame=main))

    def test_credentials_and_garbage_are_blocked(self) -> None:
        for url in ("https://ana:pw@www.example.org/", "https://ana@www.example.org/", "https://www.example.org:99999/",
                    "https:", "https://", "https:///x", "nonsense", "", None, 42, b"https://www.example.org/",
                    "https://www.exa mple.org/", "https://www.ex\u00e4mple.org/"):
            for main in (True, False):
                with self.subTest(url=repr(url)[:50], main_frame=main):
                    self.assertTrue(self.blocked(url, main_frame=main))

    def test_never_raises(self) -> None:
        class Weird(str):
            def split(self, *args: object, **kwargs: object) -> list[str]:
                raise RuntimeError("boom")

        self.assertTrue(self.blocked(Weird("https://www.example.org/"), main_frame=True))


class HelperTests(unittest.TestCase):
    def test_final_host_note(self) -> None:
        self.assertEqual(final_host_note("https://example.org/a", "https://www.example.net/b"),
                         "redirected to www.example.net")
        self.assertEqual(final_host_note("https://Example.org/a", "https://example.ORG/b"), "")
        self.assertEqual(final_host_note("https://example.org/a", ""), "")
        self.assertEqual(final_host_note("https://example.org/a", None), "")

    def test_chromium_flags_merge_without_duplicates(self) -> None:
        self.assertEqual(flag_tokens(chromium_flags("")), list(snapshots.CHROMIUM_FLAGS))
        merged = chromium_flags("--foo --disable-gpu --disable-features=Bar")
        tokens = flag_tokens(merged)
        self.assertEqual(tokens.count("--disable-gpu"), 1)
        self.assertIn("--foo", tokens)
        self.assertIn("--disable-features=Bar,AudioServiceOutOfProcess,WebAuthenticationUseNativeWinApi", tokens)
        self.assertEqual(sum(token.startswith("--disable-features") for token in tokens), 1)
        self.assertEqual(chromium_flags(merged), merged)
        self.assertIn("--mute-audio", tokens)
        self.assertIn("--disable-logging", tokens)
        self.assertIn("--log-level=3", tokens)   # none of Chromium's own error lines on stderr
        self.assertIn("--force-webrtc-ip-handling-policy=disable_non_proxied_udp", tokens)
        self.assertEqual(flag_tokens(chromium_flags("--log-level=0")).count("--log-level=3"), 1)
        self.assertNotIn("--log-level=0", flag_tokens(chromium_flags("--log-level=0")))

    def test_chromium_looks_no_name_up_itself(self) -> None:
        # One switch with spaces in it: written in double quotes, which QtWebEngine removes.
        self.assertEqual(snapshots.RESOLVER_RULES, "--host-resolver-rules=MAP * ^NOTFOUND,EXCLUDE 127.0.0.1")
        for kwargs in ({}, {"offline": True}, {"gate_port": 50123}):
            with self.subTest(**kwargs):
                text = chromium_flags("--foo", **kwargs)
                self.assertIn(f'"{snapshots.RESOLVER_RULES}"', text)
                self.assertEqual(flag_tokens(text).count(snapshots.RESOLVER_RULES), 1)
                self.assertEqual(sorted(flag_tokens(chromium_flags(text, **kwargs))),   # merged again: the same
                                 sorted(flag_tokens(text)))
        # the proxies it must leave reachable are on 127.0.0.1
        self.assertTrue(all("127.0.0.1:" in flag for flag in snapshots.OFFLINE_FLAGS + snapshots.GATE_FLAGS
                            if flag.startswith("--proxy-server")))

    def test_flag_tokens_read_the_value_as_qtwebengine_does(self) -> None:
        self.assertEqual(flag_tokens(""), [])
        self.assertEqual(flag_tokens(None), [])
        self.assertEqual(flag_tokens("  --a   --b=c\t--d "), ["--a", "--b=c", "--d"])
        self.assertEqual(flag_tokens('"--no-sandbox"'), ["--no-sandbox"])          # the quotes are removed
        self.assertEqual(flag_tokens('"--a=b c" --d'), ["--a=b c", "--d"])         # and group white space
        self.assertEqual(flag_tokens('--a="b c"d --e'), ["--a=b cd", "--e"])
        self.assertEqual(flag_tokens('"--x --no-proxy-server"'), ["--x --no-proxy-server"])   # ONE argument
        self.assertEqual(flag_tokens('--a ""'), ["--a", ""])

    def test_jarvis_flags_go_before_a_bare_switch_terminator(self) -> None:
        tokens = flag_tokens(chromium_flags("--disable-gpu -- --no-proxy-server", gate_port=50123))
        cut = tokens.index("--")
        self.assertEqual(tokens[cut:], ["--", "--no-proxy-server"])   # left as Chromium would read them ...
        for flag in ("--proxy-server=127.0.0.1:50123", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                     snapshots.RESOLVER_RULES):
            self.assertLess(tokens.index(flag), cut)                   # ... and Jarvis's are switches

    def test_windows_hello_and_webrtc_udp_are_off_and_an_inherited_policy_is_replaced(self) -> None:
        tokens = flag_tokens(chromium_flags(""))
        features = next(token for token in tokens if token.startswith("--disable-features="))
        self.assertIn("WebAuthenticationUseNativeWinApi", features.split("=", 1)[1].split(","))
        self.assertIn("AudioServiceOutOfProcess", features.split("=", 1)[1].split(","))
        inherited = flag_tokens(chromium_flags("--force-webrtc-ip-handling-policy=default --Disable-Features=Foo"))
        self.assertNotIn("--force-webrtc-ip-handling-policy=default", inherited)
        self.assertEqual([token for token in inherited if "webrtc" in token],
                         ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
        self.assertIn("--disable-features=Foo,AudioServiceOutOfProcess,WebAuthenticationUseNativeWinApi", inherited)
        # Chromium reads only the last of a repeated switch: two inherited lists become one with ours in it.
        twice_text = chromium_flags("--disable-features=Foo --bar /disable-features=Baz,Foo")
        twice = flag_tokens(twice_text)
        self.assertEqual([token for token in twice if "disable-features" in token],
                         ["--disable-features=Foo,Baz,AudioServiceOutOfProcess,WebAuthenticationUseNativeWinApi"])
        self.assertIn("--bar", twice)
        self.assertEqual(flag_tokens(chromium_flags(twice_text)), twice)

    def test_offline_flags_replace_any_proxy(self) -> None:
        flags = flag_tokens(chromium_flags("--proxy-server=http://proxy.example.com:8080 --foo", offline=True))
        for flag in snapshots.OFFLINE_FLAGS:
            self.assertIn(flag, flags)
        self.assertNotIn("--proxy-server=http://proxy.example.com:8080", flags)
        self.assertIn("--foo", flags)
        self.assertFalse(any(flag.startswith("--proxy") for flag in flag_tokens(chromium_flags(""))))

    def test_the_gate_replaces_every_inherited_proxy_switch(self) -> None:
        inherited = ("--no-proxy-server --proxy-pac-url=https://wpad.example.com/p.pac -proxy-auto-detect "
                     "--proxy-server=http://proxy.example.com:8080 --proxy-bypass-list=* --foo")
        text = chromium_flags(inherited, gate_port=50123)
        flags = flag_tokens(text)
        self.assertEqual([flag for flag in flags if "proxy" in flag],
                         ["--proxy-server=127.0.0.1:50123", "--proxy-bypass-list=<-loopback>"])
        self.assertIn("--foo", flags)
        self.assertEqual(sorted(flag_tokens(chromium_flags(text, gate_port=50123))), sorted(flags))
        # a test platform keeps the dead proxy even when a gate port is given
        offline = flag_tokens(chromium_flags(inherited, offline=True, gate_port=50123))
        self.assertEqual([flag for flag in offline if "proxy" in flag], list(snapshots.OFFLINE_FLAGS))
        for bad in (0, -1, 70000, "50123", None):
            with self.subTest(port=bad):
                self.assertIn("--no-proxy-server", flag_tokens(chromium_flags("--no-proxy-server", gate_port=bad)))

    def test_sandbox_problem(self) -> None:
        self.assertEqual(sandbox_problem({}), "")
        self.assertEqual(sandbox_problem({"QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu"}), "")
        self.assertEqual(sandbox_problem({"QTWEBENGINE_DISABLE_SANDBOX": "1"}), R_SANDBOX_OFF)
        self.assertEqual(sandbox_problem({"QTWEBENGINE_DISABLE_SANDBOX": "0"}), R_SANDBOX_OFF)   # Qt: set = off
        self.assertEqual(sandbox_problem({"QTWEBENGINE_DISABLE_SANDBOX": ""}), "")
        self.assertEqual(sandbox_problem({"QTWEBENGINE_CHROMIUM_FLAGS": "--foo --no-sandbox"}), R_SANDBOX_OFF)
        self.assertEqual(sandbox_problem({"QTWEBENGINE_CHROMIUM_FLAGS": "--single-process"}), R_SANDBOX_OFF)
        self.assertEqual(sandbox_problem(None), R_SANDBOX_OFF)   # type: ignore[arg-type]
        # Chromium on Windows reads "-", "/" and any case too
        for flags in ("-no-sandbox", "/no-sandbox", "--No-Sandbox", "--single-process=1"):
            with self.subTest(flags=flags):
                self.assertEqual(sandbox_problem({"QTWEBENGINE_CHROMIUM_FLAGS": flags}), R_SANDBOX_OFF)
        # read as QtWebEngine reads the value: it removes double quotes
        for flags in ('"--no-sandbox"', '--disable-gpu "--single-process"', '--no-"sandbox"'):
            with self.subTest(flags=flags):
                self.assertEqual(sandbox_problem({"QTWEBENGINE_CHROMIUM_FLAGS": flags}), R_SANDBOX_OFF)

    def test_inherited_settings_that_undo_the_private_window_refuse_every_shot(self) -> None:
        self.assertEqual(engine_flags_problem({}), "")
        self.assertEqual(engine_flags_problem({"QTWEBENGINE_REMOTE_DEBUGGING": "9222"}), R_UNSAFE_FLAGS)
        self.assertEqual(engine_flags_problem({"QTWEBENGINE_REMOTE_DEBUGGING": "127.0.0.1:9222"}), R_UNSAFE_FLAGS)
        self.assertEqual(engine_flags_problem({"QTWEBENGINE_REMOTE_DEBUGGING": " "}), "")
        for flag in ("--remote-debugging-port=9222", "--remote-debugging-pipe", "-remote-debugging-address=0.0.0.0",
                     "--remote-allow-origins=*", "--ignore-certificate-errors",
                     "--ignore-certificate-errors-spki-list=abc", "/ignore-ssl-errors", "--disable-web-security",
                     "--allow-running-insecure-content", "--disable-site-isolation-trials",
                     "--host-resolver-rules=MAP * 127.0.0.1", "--host-rules=MAP * 10.0.0.1",
                     "--user-data-dir=C:/example", "--unsafely-treat-insecure-origin-as-secure=http://a.example",
                     "--enable-logging=stderr", "--log-file=C:/example/log.txt", "--log-net-log=C:/example/n.json",
                     "--net-log-capture-mode=Everything", "--v=1", "--vmodule=*=2", "--Remote-Debugging-Port=1",
                     # a TLS key log (every pictured page's secrets in a file), automatic Windows sign-in
                     "--ssl-key-log-file=C:/example/keys.log", "--auth-server-allowlist=*",
                     "--auth-server-whitelist=*", "--auth-negotiate-delegate-allowlist=*",
                     # QtWebEngine removes double quotes: a quoted switch a plain split would not see
                     '"--no-sandbox"', '"--no-proxy-server"', '"--remote-debugging-port=9222"',
                     '"--ignore-certificate-errors"', '"--user-data-dir=C:\\My Folder"', '--lang="en-US"',
                     # after a bare "--" Chromium reads no switch: Jarvis's proxy and WebRTC policy
                     # would be plain words
                     "--", "-- --foo",
                     # not on the tolerated list: startup tracing (a file with the page's address and
                     # title), a weaker GPU sandbox, switches Jarvis does not know, plain words, a
                     # --disable-features naming a feature Jarvis does not turn off himself
                     "--trace-startup", "--trace-startup=navigation,loading", "--trace-startup-file=C:/example/t.json",
                     "--enable-tracing", "--trace-to-console", "--trace-config-file=C:/example/c.json",
                     "--disable-gpu-sandbox", "--video-capture-use-gpu-memory-buffer", "--x-y=a--b", "-",
                     "example.org", "--enable-features=Anything", "--disable-features=NetworkServiceSandbox",
                     "--disable-features=AudioServiceOutOfProcess,IsolateOrigins"):
            with self.subTest(flag=flag):
                self.assertEqual(engine_flags_problem({"QTWEBENGINE_CHROMIUM_FLAGS": f"--disable-gpu {flag}"}),
                                 R_UNSAFE_FLAGS)
        self.assertEqual(engine_flags_problem({"QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu --"}), R_UNSAFE_FLAGS)
        self.assertEqual(engine_flags_problem({"SSLKEYLOGFILE": "C:/example/keys.log"}), R_UNSAFE_FLAGS)
        self.assertEqual(engine_flags_problem({"SSLKEYLOGFILE": " "}), "")
        jarvis_own = [flag for flag in snapshots.CHROMIUM_FLAGS if flag != snapshots.RESOLVER_RULES]
        for fine in (" ".join(jarvis_own), "--proxy-server=127.0.0.1:9 --proxy-bypass-list=<-loopback>",
                     "--lang=en-US", "--disable-gpu", "--log-level=0", "/disable-gpu",
                     "--force-device-scale-factor=1.5",
                     "--disable-features=WebAuthenticationUseNativeWinApi", "--disable-features=", "--no-proxy-server",
                     "--force-webrtc-ip-handling-policy=default", ""):
            with self.subTest(fine=fine):
                self.assertEqual(engine_flags_problem({"QTWEBENGINE_CHROMIUM_FLAGS": fine}), "")
        # Jarvis's own merged flags (his resolver rules, in quotes) would be refused: the camera judges
        # the value Jarvis INHERITED, never the one he wrote (snapshot_engine._judged_environ).
        self.assertEqual(engine_flags_problem({"QTWEBENGINE_CHROMIUM_FLAGS": chromium_flags("", gate_port=50123)}),
                         R_UNSAFE_FLAGS)
        self.assertEqual(engine_flags_problem(None), R_UNSAFE_FLAGS)   # type: ignore[arg-type]
        self.assertEqual(reason_kind(R_UNSAFE_FLAGS), "unsafe_flags")

    def test_reason_kinds_are_single_words(self) -> None:
        self.assertEqual(reason_kind(snapshots.R_TIMEOUT), "timeout")
        self.assertEqual(reason_kind(snapshots.R_LOAD_FAILED), "load_failed")
        self.assertEqual(reason_kind(snapshots.R_BLANK), "blank")
        self.assertEqual(reason_kind(snapshots.R_CRASHED), "crashed")
        self.assertEqual(reason_kind(snapshots.R_NO_ENGINE), "no_engine")
        self.assertEqual(reason_kind(snapshots.R_ENGINE_FAILED), "engine_failed")
        self.assertEqual(reason_kind(snapshots.R_SANDBOX_OFF), "sandbox")
        self.assertEqual(reason_kind(snapshots.R_CANCELLED), "cancelled")
        self.assertEqual(reason_kind(snapshots.R_TEST_RUN), "test_run")
        for refused in (R_SEARCH_PAGE, R_OVER_CAP, R_SAME_PAGE, snapshots.R_TEXT_OFF,
                        snapshot_url_problem("https://nas.local/"), "anything else", None):
            self.assertEqual(reason_kind(refused), "refused")
        self.assertEqual(reason_kind(""), "")

    def test_notes_are_the_pictures_words(self) -> None:
        from briefing_reader import pictures

        self.assertEqual(snapshots.NOTE_OPENING, pictures.NOTE_OPENING)
        self.assertEqual(snapshots.NOTE_OPENED, pictures.NOTE_OPENED)

    def test_constants(self) -> None:
        self.assertEqual(snapshots.MAX_PER_RUN, 4)
        self.assertEqual(snapshots.VIEWPORT, (1280, 800))
        self.assertEqual(snapshots.MAX_WIDTH, 960)
        self.assertEqual(snapshots.TIMEOUT_S, 15.0)
        self.assertTrue(snapshots.LOCAL_BASE_URL.startswith("https://"))
        self.assertTrue(request_problem(snapshots.LOCAL_BASE_URL, main_frame=False))   # never a real host
        self.assertIn("4", R_OVER_CAP)

    def test_module_is_ascii_and_qt_free(self) -> None:
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        self.assertTrue((root / "briefing_reader" / "snapshots.py").read_bytes().isascii())
        self.assertTrue((root / "briefing_reader" / "snapshot_engine.py").read_bytes().isascii())
        code = ("import sys; import briefing_reader.snapshots; "
                "print(any(m.startswith('PySide6') for m in sys.modules))")
        done = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertEqual(done.stdout.strip(), "False")


class GateRuleTests(unittest.TestCase):
    """The network gate's rules: port 443 only, public addresses only, checked after the lookup."""

    def test_public_names_and_addresses_on_443_may_be_looked_up(self) -> None:
        for authority, host in (("www.example.org:443", "www.example.org"), ("WWW.Example.ORG.:443", "www.example.org"),
                                ("1.1.1.1:443", "1.1.1.1"),
                                ("xn--bcher-kva.example:443", "xn--bcher-kva.example")):
            with self.subTest(authority=authority):
                self.assertEqual(tunnel_target(authority), (host, ""))

    def test_other_ports_and_local_or_odd_targets_are_refused(self) -> None:
        for authority, problem in (
                ("www.example.org:80", snapshots.G_PORT), ("www.example.org:8443", snapshots.G_PORT),
                ("www.example.org:0443x", snapshots.G_PORT), ("www.example.org:", snapshots.G_PORT),
                ("127.0.0.1:443", snapshots.G_LOCAL), ("10.0.0.5:443", snapshots.G_LOCAL),
                ("192.168.1.1:443", snapshots.G_LOCAL), ("169.254.169.254:443", snapshots.G_LOCAL),
                ("[::1]:443", snapshots.G_IPV6), ("[fe80::1]:443", snapshots.G_IPV6),
                ("[::ffff:127.0.0.1]:443", snapshots.G_IPV6), ("localhost:443", snapshots.G_LOCAL),
                # never IPv6: a global address may be this PC's own or a device's on its network,
                # a NAT64 one may carry a private IPv4 address
                ("[2606:4700::1111]:443", snapshots.G_IPV6), ("[2a01:4f8:1:2::1]:443", snapshots.G_IPV6),
                ("[64:ff9b::a00:1]:443", snapshots.G_IPV6), ("[64:ff9b::c0a8:101]:443", snapshots.G_IPV6),
                ("printer:443", snapshots.G_LOCAL), ("nas.local:443", snapshots.G_LOCAL),
                ("router.home.arpa:443", snapshots.G_LOCAL), ("127.0.0.1.nip.io:443", snapshots.G_LOCAL),
                ("routerlogin.net:443", snapshots.G_LOCAL), ("127.1:443", snapshots.G_LOCAL),
                ("www.example.org", snapshots.G_TARGET), ("", snapshots.G_TARGET), ("::1:443", snapshots.G_TARGET),
                ("[::1]443", snapshots.G_TARGET), ("user@www.example.org:443", snapshots.G_TARGET),
                ("a b:443", snapshots.G_TARGET), ("[fe80::1%12]:443", snapshots.G_TARGET),
                ("b\u00fccher.example:443", snapshots.G_TARGET)):
            with self.subTest(authority=authority):
                self.assertEqual(tunnel_target(authority)[1], problem)
        self.assertEqual(tunnel_target(None), ("", snapshots.G_TARGET))   # type: ignore[arg-type]

    def test_every_resolved_address_must_be_public(self) -> None:
        self.assertEqual(resolved_problem(["93.184.215.14"]), "")
        self.assertEqual(resolved_problem(("93.184.215.14", "93.184.215.15")), "")
        for addresses in (["127.0.0.1"], ["93.184.215.14", "127.0.0.1"], ["10.1.2.3"], ["::1"],
                          ["::ffff:192.168.0.1"], ["2002:c0a8:0101::1"], ["169.254.169.254"], ["0.0.0.0"],
                          ["not an address"], ["2a01:4f8:1:2::1"], ["64:ff9b::c0a8:101"],
                          ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"]):
            with self.subTest(addresses=addresses):
                self.assertEqual(resolved_problem(addresses), snapshots.G_LOCAL)
        self.assertEqual(resolved_problem([]), snapshots.G_UNRESOLVED)
        self.assertEqual(resolved_problem(None), snapshots.G_LOCAL)   # type: ignore[arg-type]


class RunBudgetTests(unittest.TestCase):
    def test_four_per_run_and_one_per_page(self) -> None:
        budget = RunBudget()
        for index in range(4):
            self.assertEqual(budget.admit(7, f"https://www.example.org/{index}"), "")
        self.assertEqual(budget.admit(7, "https://www.example.org/4"), R_OVER_CAP)
        self.assertEqual(budget.admit(7, "https://www.example.org/0"), R_SAME_PAGE)
        self.assertEqual(budget.taken(7), 4)
        self.assertEqual(budget.admit(8, "https://www.example.org/0"), "")   # another run
        self.assertEqual(budget.taken(8), 1)

    def test_the_same_page_ignores_case_of_the_host_and_the_fragment(self) -> None:
        budget = RunBudget()
        self.assertEqual(budget.admit(1, "https://www.example.org/a?x=1#top"), "")
        self.assertEqual(budget.admit(1, "https://WWW.Example.org/a?x=1#end"), R_SAME_PAGE)
        self.assertEqual(budget.admit(1, "https://www.example.org/a?x=2"), "")
        self.assertEqual(budget.admit(1, "https://www.example.org"), "")
        self.assertEqual(budget.admit(1, "https://www.example.org/"), R_SAME_PAGE)

    def test_a_smaller_cap(self) -> None:
        budget = RunBudget()
        self.assertEqual(budget.admit(1, "https://www.example.org/a", cap=1), "")
        self.assertEqual(budget.admit(1, "https://www.example.org/b", cap=1), "at most 1 page picture per research")
        self.assertEqual(budget.admit(2, "https://www.example.org/a", cap=0), snapshots.R_NO_PICTURES)
        for index in range(2):
            budget.admit(3, f"https://www.example.org/{index}", cap=2)
        self.assertEqual(budget.admit(3, "https://www.example.org/c", cap=2), "at most 2 page pictures per research")

    def test_the_refusal_names_the_cap_it_was_refused_at(self) -> None:
        # The cap is min(4, [research] max_fetches): a lower one never reads "at most 4".
        self.assertEqual(snapshots.over_cap_reason(4), R_OVER_CAP)
        self.assertEqual(snapshots.over_cap_reason(snapshots.MAX_PER_RUN), R_OVER_CAP)
        self.assertEqual(snapshots.over_cap_reason(3), "at most 3 page pictures per research")
        self.assertEqual(snapshots.over_cap_reason(1), "at most 1 page picture per research")
        self.assertEqual(snapshots.over_cap_reason(0), snapshots.R_NO_PICTURES)
        self.assertEqual(snapshots.over_cap_reason(-2), snapshots.R_NO_PICTURES)
        self.assertEqual(snapshots.over_cap_reason("x"), R_OVER_CAP)
        self.assertEqual(snapshots.reason_kind(snapshots.over_cap_reason(2)), "refused")

    def test_end_cancel_forget(self) -> None:
        budget = RunBudget()
        self.assertFalse(budget.ended(3))
        budget.admit(3, "https://www.example.org/")
        budget.end(3)
        self.assertTrue(budget.ended(3))
        self.assertFalse(budget.cancelled(3))
        budget.cancel(4)
        self.assertTrue(budget.cancelled(4))
        self.assertTrue(budget.ended(4))
        self.assertEqual(budget.admit(4, "https://www.example.org/"), R_CANCELLED)
        budget.forget(3)
        self.assertFalse(budget.ended(3))
        self.assertEqual(budget.taken(3), 0)
        self.assertEqual(budget.admit(3, "https://www.example.org/"), "")

    def test_old_runs_are_forgotten_beyond_a_bound(self) -> None:
        budget = RunBudget()
        for task in range(200):
            budget.admit(task, "https://www.example.org/")
            budget.end(task)
        self.assertLessEqual(len(budget._runs), 65)
        self.assertNotIn("example", repr(budget))

    def test_thread_safety(self) -> None:
        budget = RunBudget()
        admitted: list[str] = []
        lock = threading.Lock()

        def worker(start: int) -> None:
            for index in range(50):
                result = budget.admit(9, f"https://www.example.org/{(start + index) % 30}")
                if result == "":
                    with lock:
                        admitted.append(f"{start}-{index}")

        threads = [threading.Thread(target=worker, args=(n * 7,)) for n in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(admitted), 4)
        self.assertEqual(budget.taken(9), 4)


class ShotResultTests(unittest.TestCase):
    def test_repr_names_no_address_or_bytes(self) -> None:
        good = ShotResult(True, image=b"\xff\xd8SECRET-BYTES", width=960, height=600,
                          final_url="https://secret.example.org/page", took_ms=1200, blocked=3)
        bad = ShotResult(False, snapshots.R_TIMEOUT, final_url="https://secret.example.org/page")
        for result in (good, bad):
            text = repr(result)
            self.assertNotIn("secret", text)
            self.assertNotIn("SECRET", text)
        self.assertIn("960x600", repr(good))
        self.assertIn("timeout", repr(bad))


if __name__ == "__main__":
    unittest.main()
