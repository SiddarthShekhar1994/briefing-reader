"""Tests for briefing_reader.snapshot_engine's PageCamera: the hidden private window that takes a
page's picture. Each case runs in its own process (tests/snapshot_probe.py), so QtWebEngine never
starts inside the shared unittest process; the cases run side by side to save time.

LOCAL HTML ONLY: no case loads a real page. The probes run offscreen (the camera refuses every
network URL and blocks every network request there) with LOCALAPPDATA / APPDATA / TEMP in a
temporary folder and Chromium pointed at a proxy that is not there (snapshots.OFFLINE_FLAGS); the
gate case runs the production network settings through the network gate, whose resolver and
connector are fakes that reach only a listener of the probe (see tests/snapshot_probe.py).

Chromium never looks a name up itself (a TURN server's name fails by Jarvis's resolver rule: the
resolver case) and prints none of its own error lines (--log-level=3); inherited settings that
would undo the window (a quoted switch, a bare "--", a TLS key log, automatic Windows sign-in) are
refused before Chromium starts (the inherited case).

WebRTC, WebTransport, pre-connections and Windows Hello are proved closed against a control: the
control case runs the same page without Jarvis's WebRTC policy and Windows Hello flag, and its
STUN / TURN packets do reach the probe's listeners on 127.0.0.1 (and Windows Hello answers when this
PC has it), so the zero counts with Jarvis's flags are the flags' doing. The webauthn case (a page
asking for a passkey) runs only once the control showed Windows Hello reachable without the flags
and the hooks case showed it unreachable with them, so no Windows dialog can come up on this PC's
screen. Skipped when this PySide6 has no QtWebEngine 6.8+.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from briefing_reader import snapshot_engine, snapshots

PROBE = Path(__file__).with_name("snapshot_probe.py")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CASES = ("guard", "render", "hooks", "hang", "disk", "teardown", "closing", "gate", "resolver", "inherited",
         "control")
SILENT_CHANNELS = {"preconnect": 0, "turn_tcp": 0, "stun": 0, "turn_udp": 0, "webtransport": 0, "wt_worker": 0}
NO_CANDIDATES = {"host": "0", "srflx": "0", "prflx": "0", "relay": "0", "other": "0"}
PAGE_URL = "https://www.example.org/probe-page"
GREEN = "#00c000"
REPORTS: dict[str, dict] = {}
FAILURES: dict[str, str] = {}
# Chromium's own log lines on stderr ("[pid:tid:date/time:ERROR:file.cc(12)] ...") and its
# "Failed to resolve address for <name>., errorcode: -105" (the probes' names are invented ones).
CHROMIUM_LINE = re.compile(r"^\[\d+:\d+:\d+/\d+\.\d+:(?:ERROR|WARNING|INFO|VERBOSE\d*):", re.MULTILINE)
RESOLVE_FAILED = re.compile(r"Failed to resolve address for (\S+?)\.?, errorcode: -105")


def _run(case: str) -> tuple[str, dict | None, str]:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        folders = {name: os.path.join(tmp, name.lower()) for name in ("LOCALAPPDATA", "APPDATA", "TEMP")}
        for folder in folders.values():
            os.makedirs(folder)
        env = {key: value for key, value in os.environ.items()
               if key not in ("QTWEBENGINE_DISABLE_SANDBOX", "QTWEBENGINE_CHROMIUM_FLAGS")}
        env.update(folders)
        env["TMP"] = folders["TEMP"]
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(snapshots.OFFLINE_FLAGS)
        try:
            # sys.executable is the interpreter itself (not the py launcher, which reads LOCALAPPDATA).
            done = subprocess.run([sys.executable, str(PROBE), case], cwd=PROJECT_ROOT, env=env,
                                  capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            return case, None, "the probe did not finish within 90 s"
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    if done.returncode != 0 or not lines:
        tail = "\n".join(line for line in done.stderr.splitlines() if "gpu_channel_manager" not in line)[-2000:]
        return case, None, f"rc={done.returncode}\n{tail}"
    report = json.loads(lines[-1])
    report["chromium_lines"] = len(CHROMIUM_LINE.findall(done.stderr))
    report["resolve_failures"] = sorted(set(RESOLVE_FAILED.findall(done.stderr)))
    return case, report, ""


def _keep(case: str, report: dict | None, failure: str) -> None:
    if report is not None:
        REPORTS[case] = report
    else:
        FAILURES[case] = failure


def setUpModule() -> None:
    if not snapshot_engine.webengine_available():
        return
    with ThreadPoolExecutor(max_workers=len(CASES)) as pool:
        for case, report, failure in pool.map(_run, CASES):
            _keep(case, report, failure)
    # A page asking for a passkey only where the control showed that, without Jarvis's flags,
    # Windows Hello answers on this PC - and the hooks case that with them it does not: then the
    # flags are what keeps Windows's own dialog away, and none can come up during the run.
    if (REPORTS.get("control", {}).get("reports", {}).get("uvpaa") == "true"
            and REPORTS.get("hooks", {}).get("reports", {}).get("uvpaa") == "false"):
        _keep(*_run("webauthn"))


def _candidates(report: dict) -> dict[str, str]:
    """The ICE candidates the page gathered, by type (from "host=0,srflx=0,...,state=complete")."""
    text = report["reports"].get("ice_done") or report["reports"].get("ice") or ""
    return dict(part.split("=", 1) for part in text.split(",") if "=" in part)


def _close(colour: str, expected: str, tolerance: int = 40) -> bool:
    try:
        return all(abs(int(colour[i:i + 2], 16) - int(expected[i:i + 2], 16)) <= tolerance for i in (1, 3, 5))
    except (TypeError, ValueError, IndexError):
        return False


@unittest.skipUnless(snapshot_engine.webengine_available(), "this PySide6 has no QtWebEngine 6.8+")
class PageCameraProbeTests(unittest.TestCase):
    def report(self, case: str) -> dict:
        if case in FAILURES:
            self.fail(f"probe {case} failed: {FAILURES[case]}")
        self.assertIn(case, REPORTS)
        report = REPORTS[case]
        self.assertEqual(report["platform"], "offscreen")
        if case not in ("gate", "resolver"):
            self.assertTrue(report["offline_flags"], "Chromium must run with the dead proxy in tests")
        return report

    def test_guard_refuses_a_network_page_offscreen_without_starting_the_engine(self) -> None:
        report = self.report("guard")
        self.assertFalse(report["result"]["ok"])
        self.assertEqual(report["result"]["reason"], snapshots.R_TEST_RUN)
        self.assertFalse(report["emitted_inside_shoot"])
        self.assertFalse(report["started_before"])
        self.assertFalse(report["started_after"])
        self.assertFalse(report["has_view"])
        self.assertEqual(report["children"], 0)
        self.assertEqual(report["children_peak_during_shots"], 0)

    def test_render_gives_a_jpeg_of_the_first_screen_at_most_960_wide(self) -> None:
        report = self.report("render")
        result = report["result"]
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["jpeg"])
        self.assertEqual(result["format"], "JPG")
        self.assertEqual((result["width"], result["height"]), (960, 600))
        self.assertLessEqual(result["bytes"], 1_500_000)
        self.assertEqual(result["final_url"], PAGE_URL)   # local HTML: reported as the address asked
        self.assertFalse(result["still_loading"])
        self.assertFalse(result["repr_has_url"])
        self.assertFalse(report["emitted_inside_shoot"])
        self.assertTrue(report["started"])
        red, green, top, corner = report["colours"]
        self.assertTrue(_close(red, "#e63946"), red)
        self.assertTrue(_close(green, "#2a9d8f"), green)
        self.assertTrue(_close(top, "#1e6091"), top)
        self.assertTrue(_close(corner, "#1e6091"), corner)
        self.assertGreaterEqual(report["children_peak_during_shots"], 1)   # a renderer while the page was open
        self.assertEqual(report["children_after_close"], 0)
        self.assertFalse(report["has_view"])

    def test_hooks_block_local_requests_and_refuse_permissions_windows_dialogs_and_downloads(self) -> None:
        report = self.report("hooks")
        result = report["result"]
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["still_loading"])
        # Six images: http, localhost, a private IP, a single-label name, loopback, and a public name
        # that only the offscreen rule blocks - every one reached the interceptor and was blocked.
        self.assertGreaterEqual(result["blocked"], 6)
        self.assertIn("https", report["decided_subresources"])
        self.assertEqual(report["decided_main_frames"], ["data"])
        self.assertGreaterEqual(report["hooks"].get("permissions", 0), 2)   # geolocation + notifications
        # The page tried four downloads (<a download> of data: and blob:, a download frame, a form):
        # the window's sandbox turned each down before Chromium prepared it, so none reached
        # downloadRequested and nothing appeared in this PC's real Downloads folder, even briefly.
        # (Something else saving into Downloads during the run would also fail this; rerun it.)
        self.assertEqual(report["hooks"].get("downloads", 0), 0)
        self.assertEqual(report["download_dirs"], [])
        self.assertTrue(report["downloads_watched"])
        self.assertEqual(report["downloads_new_entries"], [])
        self.assertFalse(report["downloads_mtime_changed"])
        # The profile's private download folder (a last net) stayed empty and went with the process.
        folder = Path(report["download_folder"])
        self.assertTrue(folder.name.startswith("jarvis-snapshots-"), folder)
        self.assertNotIn((Path.home() / "Downloads").resolve(), folder.resolve().parents)
        self.assertEqual(report["download_folder_entries"], [])
        self.assertFalse(folder.exists())
        for box, colour in report["boxes"].items():
            with self.subTest(box=box):
                self.assertTrue(_close(colour, GREEN), f"{box}: {colour}")
        self.assertEqual(report["top_levels"], 1)   # the hidden view only: window.open made nothing
        self.assertFalse(report["console_marker_in_qt_messages"])
        self.assertNotIn("js", report["qt_categories"])
        # WebRTC (STUN, TURN over UDP and TCP), WebTransport (also from a worker) and a
        # pre-connection to this PC: the interceptor never sees them; the flags and the dead proxy
        # stop every one (the control case shows the same page reaching the listeners without them).
        self.assertEqual(report["reports"].get("rtc"), "made")
        self.assertEqual(report["reports"].get("wt_worker"), "made")
        self.assertEqual(report["channel_hits"], SILENT_CHANNELS)
        candidates = _candidates(report)
        self.assertEqual({key: candidates.get(key) for key in NO_CANDIDATES}, NO_CANDIDATES)   # no UDP at all
        self.assertEqual(report["reports"].get("uvpaa"), "false")   # no Windows Hello for a page

    def test_production_settings_send_every_connection_through_the_gate(self) -> None:
        report = self.report("gate")
        self.assertTrue(report["result"]["ok"], report["result"])
        port = report["gate_port"]
        self.assertGreater(port, 0)
        self.assertIn(f"--proxy-server=127.0.0.1:{port}", report["flags"])
        self.assertIn("--proxy-bypass-list=<-loopback>", report["flags"])
        self.assertNotIn("--proxy-server=127.0.0.1:9", report["flags"])
        self.assertIn("--force-webrtc-ip-handling-policy=disable_non_proxied_udp", report["flags"])
        self.assertIn(snapshots.RESOLVER_RULES, report["flags"])   # one token: QtWebEngine removed the quotes
        self.assertIn("--log-level=3", report["flags"])
        self.assertEqual(report["chromium_lines"], 0)   # none of Chromium's own lines (a page's TURN name ...)
        # TURN servers by name: Chromium's own lookup fails by the resolver rule (the resolver case
        # shows it), its connection reaches the gate by name and is refused there.
        self.assertNotIn(report["reports"].get("named_turn_errors"), (None, "threw", "0"))
        self.assertTrue({"localhost", "turn.localhost"} <= set(report["gate_hosts"]), report["gate_hosts"])
        # Nothing on this PC is reached: not by WebRTC, WebTransport or a pre-connection to
        # 127.0.0.1, not by a public name that resolves to 127.0.0.1 (refused by the gate) ...
        self.assertEqual(report["reports"].get("rtc"), "made")
        self.assertEqual(report["reports"].get("webtransport"), "made")
        self.assertEqual(report["reports"].get("wt_worker"), "made")
        self.assertEqual({name: count for name, count in report["channel_hits"].items() if name != "tunnel"},
                         SILENT_CHANNELS)
        candidates = _candidates(report)
        self.assertEqual({key: candidates.get(key) for key in NO_CANDIDATES}, NO_CANDIDATES)
        self.assertEqual(report["resolved"], ["rebind.example.org", "tunnel.example.org"])
        # refused: the pre-connection, TURN over TCP and the rebinding name
        self.assertGreaterEqual(report["gate_counts"]["refused"], 3)
        # ... while a public name is tunnelled: the page's TLS reached the (local stand-in) server.
        self.assertGreaterEqual(report["channel_hits"]["tunnel"], 1)
        self.assertTrue(all(hit == "tcp 1603" for hit in report["tunnel"]), report["tunnel"])
        self.assertGreaterEqual(report["gate_counts"]["tunnels"], 1)
        self.assertEqual(report["reports"].get("uvpaa"), "false")

    def test_chromium_looks_no_name_up_itself(self) -> None:
        # The gate case with Chromium's own error lines on: its lookup of each TURN server name the
        # page gave failed with "name not resolved" - for "localhost", which Chromium otherwise
        # answers itself, only Jarvis's resolver rule does that - while the gate, at 127.0.0.1, still
        # tunnels a public name (the rule leaves the proxy reachable).
        report = self.report("resolver")
        self.assertTrue(report["result"]["ok"], report["result"])
        self.assertIn(snapshots.RESOLVER_RULES, report["flags"])
        self.assertNotIn("--log-level=3", report["flags"])
        self.assertTrue({"localhost", "turn.localhost"} <= set(report["resolve_failures"]),
                        report["resolve_failures"])
        self.assertEqual(report["resolved"], ["rebind.example.org", "tunnel.example.org"])
        self.assertGreaterEqual(report["gate_counts"]["tunnels"], 1)
        self.assertEqual({name: count for name, count in report["channel_hits"].items() if name != "tunnel"},
                         SILENT_CHANNELS)

    def test_inherited_settings_that_undo_the_window_never_start_chromium(self) -> None:
        # QtWebEngine removes double quotes ('"--no-proxy-server"' is that switch) and Chromium reads
        # no switch after a bare "--" (Jarvis's proxy and WebRTC policy would be plain words): such a
        # value, a TLS key log or automatic Windows sign-in refuses every shot before Chromium starts.
        report = self.report("inherited")
        self.assertEqual(report["refused"], {
            "quoted_no_sandbox": "sandbox", "quoted_no_proxy": "unsafe_flags", "quoted_debugging": "unsafe_flags",
            "quoted_certificates": "unsafe_flags", "terminator": "unsafe_flags", "key_log_flag": "unsafe_flags",
            "key_log_env": "unsafe_flags", "windows_sign_in": "unsafe_flags"})
        self.assertFalse(report["started_after_refusals"])
        self.assertEqual(report["children_after_refusals"], 0)
        self.assertFalse(report["key_log_made"])
        # A plain inherited value: the shot is taken, and the page's pre-connection reaches nothing.
        self.assertTrue(report["result"]["ok"], report["result"])
        self.assertTrue(report["started"])
        self.assertEqual(report["channel_hits"], {"preconnect": 0})
        self.assertIn("--disable-gpu", report["flags"])
        self.assertIn(snapshots.RESOLVER_RULES, report["flags"])
        self.assertEqual(report["chromium_lines"], 0)

    def test_the_control_reaches_this_pcs_listeners_without_jarvis_flags(self) -> None:
        # The same page and listeners as the hooks case, Chromium without Jarvis's WebRTC policy and
        # Windows Hello flag (the dead proxy stays): WebRTC's UDP does reach 127.0.0.1, so the zero
        # counts of the other cases are the flags' doing, not a page or listener that never worked.
        report = self.report("control")
        self.assertTrue(report["result"]["ok"], report["result"])
        self.assertNotIn("--force-webrtc-ip-handling-policy=disable_non_proxied_udp", report["flags"])
        self.assertFalse(any("WebAuthenticationUseNativeWinApi" in flag for flag in report["flags"]))
        self.assertEqual(report["reports"].get("rtc"), "made")
        hits = report["channel_hits"]
        self.assertGreater(hits["stun"] + hits["turn_udp"], 0, hits)
        self.assertEqual(hits["turn_tcp"] + hits["preconnect"], 0, hits)   # TCP: the dead proxy
        self.assertIn(report["reports"].get("uvpaa"), ("true", "false"))   # "true" where this PC has Windows Hello

    def test_a_page_asking_for_a_passkey_brings_up_no_windows_dialog(self) -> None:
        if "webauthn" not in REPORTS and "webauthn" not in FAILURES:
            self.skipTest("Windows Hello does not answer on this PC even without Jarvis's flags (control case)")
        report = self.report("webauthn")
        self.assertTrue(report["result"]["ok"], report["result"])
        reports = report["reports"]
        self.assertEqual(reports.get("uvpaa"), "false")
        self.assertEqual(reports.get("cred_asked"), "yes")
        # Both requests reached Chromium and waited (no authenticator, no prompt) until called off.
        self.assertEqual(reports.get("cred_get"), "rejected AbortError")
        self.assertEqual(reports.get("cred_create"), "rejected AbortError")
        self.assertFalse(report["windows_security_before"])
        self.assertFalse(report["windows_security_during"])   # no "Windows Security" passkey dialog
        self.assertEqual(report["channel_hits"], SILENT_CHANNELS)

    def test_a_hung_page_gives_nothing_and_leaves_no_renderer(self) -> None:
        report = self.report("hang")
        self.assertFalse(report["result"]["ok"])
        self.assertIn(report["result"]["reason"], (snapshots.R_BLANK, snapshots.R_TIMEOUT))
        self.assertGreaterEqual(report["children_peak_during_shots"], 1)
        self.assertEqual(report["children_2s_after_close"], 0)

    def test_nothing_is_written_to_disk(self) -> None:
        report = self.report("disk")
        self.assertTrue(report["result"]["ok"], report["result"])
        # Only the private download folder (empty; deleted when the process exits) is new.
        folder = Path(report["download_folder"])
        self.assertEqual(report["download_folder_entries"], [])
        self.assertFalse(folder.exists())
        new = {name: [entry for entry in entries if entry != folder.name]
               for name, entries in report["new_in_env_folders"].items()}
        self.assertEqual(new, {"LOCALAPPDATA": [], "APPDATA": [], "TEMP": [], "TMP": []})
        self.assertEqual(report["app_folders_made"], [False, False, False])
        self.assertFalse(report["app_named_folder_in_generic_cache"])

    def test_every_shot_gets_a_fresh_private_profile_and_teardown_leaves_nothing(self) -> None:
        report = self.report("teardown")
        self.assertTrue(report["first"]["ok"], report["first"])
        self.assertTrue(report["second"]["ok"], report["second"])
        first_unseen, first_written = report["first_storage"]
        second_unseen, second_written = report["second_storage"]
        self.assertTrue(_close(first_written, GREEN), first_written)    # the page could store ...
        self.assertTrue(_close(first_unseen, GREEN), first_unseen)
        self.assertTrue(_close(second_written, GREEN), second_written)
        self.assertTrue(_close(second_unseen, GREEN), second_unseen)    # ... and the next page never saw it
        self.assertEqual(report["pages_on_view_between_shots"], 0)
        self.assertEqual(report["children_after_close"], 0)
        self.assertEqual(report["children_1s_later"], 0)
        self.assertFalse(report["has_view"])
        self.assertFalse(report["busy"])


    def test_closing_mid_shot_without_an_event_loop_deletes_everything_at_once(self) -> None:
        report = self.report("closing")
        self.assertGreaterEqual(report["children_before_close"], 1)
        self.assertEqual(report["views_right_after_close"], 0)
        self.assertFalse(report["busy"])
        self.assertEqual(report["children_after_close"], 0)
        self.assertEqual(report["result"]["reason"], snapshots.R_CANCELLED)
        self.assertFalse(report["profile_warning"])


class EngineHelperTests(unittest.TestCase):
    """The module's Qt-light helpers (no QtWebEngine started)."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.qt = QApplication.instance() or QApplication(["test_snapshot_engine", "-platform", "offscreen"])

    def test_importing_the_module_does_not_import_the_web_engine(self) -> None:
        code = ("import sys; import briefing_reader.snapshot_engine as engine; "
                "print(sorted(m for m in sys.modules if m.startswith(('PySide6.QtWebEngine', 'PySide6.QtQuick'))))")
        done = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertEqual(done.stdout.strip(), "[]")

    def test_webengine_available_only_looks(self) -> None:
        code = ("import sys; import briefing_reader.snapshot_engine as engine; ok = engine.webengine_available(); "
                "print(ok, any(m.startswith('PySide6.QtWebEngine') for m in sys.modules))")
        done = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        available, imported = done.stdout.split()
        self.assertIn(available, ("True", "False"))
        self.assertEqual(imported, "False")

    def test_prepare_application_sets_the_attributes_before_qapplication_only(self) -> None:
        code = ("from PySide6.QtCore import QCoreApplication, Qt\n"
                "import briefing_reader.snapshot_engine as engine\n"
                "engine.prepare_application(); engine.prepare_application()\n"
                "A = Qt.ApplicationAttribute\n"
                "print(QCoreApplication.testAttribute(A.AA_ShareOpenGLContexts),"
                " QCoreApplication.testAttribute(A.AA_DisableShaderDiskCache))\n"
                "app = QCoreApplication([])\n"
                "engine.prepare_application()\n"
                "print('after')\n")
        done = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertEqual(done.stdout.split(), ["True", "True", "after"])
        self.assertNotIn("must be set before", done.stderr)

    def test_encode_picture_falls_to_the_smaller_quality_then_refuses(self) -> None:
        from PySide6.QtGui import QColor, QImage

        image = QImage(320, 200, QImage.Format.Format_RGB32)
        for y in range(200):
            for x in range(320):
                image.setPixelColor(x, y, QColor((x * 7 + y * 13) % 256, (x * 3) % 256, (y * 5) % 256))
        data, fmt = snapshot_engine.encode_picture(image, 1_500_000)
        self.assertEqual(fmt, "JPG")
        self.assertEqual(data[:2], b"\xff\xd8")
        best = len(data)
        small, _fmt = snapshot_engine.encode_picture(image, best - 1)   # only quality 60 fits
        self.assertTrue(small)
        self.assertLess(len(small), best)
        none, _fmt = snapshot_engine.encode_picture(image, 10)
        self.assertEqual(none, b"")

    def test_a_uniform_grab_is_blank_and_a_small_mark_is_not(self) -> None:
        from PySide6.QtGui import QColor, QImage

        image = QImage(1280, 800, QImage.Format.Format_RGB32)
        image.fill(QColor("#ffffff"))
        self.assertTrue(snapshot_engine._uniform(image))
        for y in range(20, 32):
            for x in range(30, 120):
                image.setPixelColor(x, y, QColor("#202020"))   # a short line of "text" in a corner
        self.assertFalse(snapshot_engine._uniform(image))
        self.assertTrue(snapshot_engine._uniform(QImage()))


if __name__ == "__main__":
    unittest.main()
