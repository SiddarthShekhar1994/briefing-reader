"""One case of the page snapshot camera (snapshot_engine.PageCamera), run in a process of its own
by tests/test_snapshot_engine.py so QtWebEngine never starts inside the shared unittest process.

    py -3.13 tests/snapshot_probe.py <case>      prints one JSON line (the case's findings)

Cases: guard, render, hooks, hang, disk, teardown, closing, gate, resolver, inherited, control,
webauthn. Qt runs offscreen. LOCAL HTML ONLY: no case loads a real page. Three nets keep every
request on this PC: on the offscreen platform the camera refuses every network URL and blocks every
network request of a local page, and Chromium is started with a proxy that is not there
(snapshots.OFFLINE_FLAGS, set here before anything as the inherited value; the camera merges
Jarvis's own flags in when the engine starts). The hooks page's image addresses are local, private or reserved (.invalid)
names but one public name (www.example.org) that only the offscreen rule blocks. The hooks, gate,
control and webauthn pages also try the channels the request interceptor never sees (WebRTC STUN /
TURN with the ICE candidates it gathers, WebTransport from the page and from a worker, a
pre-connection) against listeners of this process on 127.0.0.1, and ask whether Windows Hello is
reachable.

The gate case runs the PRODUCTION network settings instead (the camera's production request rules,
Chromium through the network gate, snapshot_gate.ConnectGate): its gate gets a fake resolver and a
fake connector, so the one name it may "reach" leads to a listener of this process and every other
name or address is refused - nothing leaves this PC there either. Its page also names TURN servers
by name ("localhost", "turn.localhost": names Chromium would answer itself, never asking this PC's
resolver even without Jarvis's rules). The resolver case is the gate case with Chromium's error
lines left on (no --log-level): its stderr then shows that Chromium's own lookup of those names
failed by Jarvis's resolver rule (a "localhost" lookup fails no other way). The inherited case sets
inherited settings Jarvis must refuse (a quoted switch, a bare "--", a TLS key log, automatic
Windows sign-in) and shoots after each - none may start Chromium - then a plain one.

The control case runs Chromium WITHOUT Jarvis's WebRTC policy and with Windows Hello reachable
(CONTROL_FLAGS): the same page's STUN / TURN packets then do reach the listeners, which shows that
the zero counts of the other cases are the flags' doing. The webauthn case (Jarvis's flags; run by
the tests only after the control showed Windows Hello reachable without them) asks for a passkey
and watches that no "Windows Security" dialog comes up. Invented content only.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

APP_NAME = f"jarvis-snapshot-probe-{os.getpid()}"
MARKER = "PROBE-CONSOLE-MARKER-7f3a"
PAGE_URL = "https://www.example.org/probe-page"
OK, BAD, WAIT = "#00c000", "#c00000", "#808080"

RENDER_HTML = """<!doctype html><html><head><style>
html,body{margin:0;height:100%;background:#1e6091}
#a{position:absolute;left:5%;top:10%;width:30%;height:30%;background:#e63946}
#b{position:absolute;left:60%;top:55%;width:30%;height:30%;background:#2a9d8f}
</style></head><body><div id=a></div><div id=b></div></body></html>"""

_BOXES = ("win", "notify", "geo", "dialogs")
HOOKS_HTML = """<!doctype html><html><head><style>
html,body{margin:0;height:100%;background:#202020}
.box{position:absolute;top:30%;width:20%;height:40%;background:""" + WAIT + """}
#win{left:2%} #notify{left:26%} #geo{left:50%} #dialogs{left:74%}
</style></head><body>
<div class=box id=win></div><div class=box id=notify></div><div class=box id=geo></div><div class=box id=dialogs></div>
<img src="http://example.invalid/http.png"><img src="https://localhost:9/local.png">
<img src="https://10.0.0.5/private.png"><img src="https://printer/single.png">
<img src="https://127.0.0.1:9/loop.png"><img src="https://www.example.org/public.png">
<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">
<script>
function mark(id, good){document.getElementById(id).style.background = good ? '""" + OK + """' : '""" + BAD + """';}
console.log('""" + MARKER + """'); console.error('""" + MARKER + """-error');
try { mark('win', window.open('https://www.example.org/popup') === null); } catch (e) { mark('win', true); }
try { Notification.requestPermission().then(function(p){ mark('notify', p === 'denied'); },
                                           function(){ mark('notify', true); }); } catch (e) { mark('notify', true); }
try { navigator.geolocation.getCurrentPosition(function(){ mark('geo', false); },
                                               function(err){ mark('geo', err.code === 1); }); } catch (e) { mark('geo', true); }
try { alert('probe'); var c = confirm('probe'); var p = prompt('probe', 'x');
      mark('dialogs', c === false && (p === null || p === '')); } catch (e) { mark('dialogs', false); }
try { var a = document.createElement('a'); a.href = 'data:text/plain,probe'; a.download = 'probe.txt';
      document.body.appendChild(a); a.click(); } catch (e) {}
try { navigator.clipboard.readText().catch(function(){}); } catch (e) {}
</script></body></html>"""

# A label only: the gate case's fake connector sends it to a listener of this process.
PUBLIC_LABEL = "93.184.215.14"
REPORT = "https://report.invalid/"


def channels_html(ports: dict[str, int], extra: str = "", credentials: bool = False) -> str:
    """Script that tries the channels the request interceptor never sees, each against a listener
    of this process on 127.0.0.1, and reports back through image requests to REPORT (blocked):
    WebRTC (STUN, TURN over UDP and TCP; the ICE candidates it gathered, counted by type only),
    WebTransport from the page and from a worker, a pre-connection, and whether Windows Hello is
    reachable (isUserVerifyingPlatformAuthenticatorAvailable). ``credentials``: also ask for a
    passkey (navigator.credentials.get / create) - only once Windows Hello answered "not
    reachable", and only in a case whose control showed that Jarvis's flags are what makes it so
    (see the webauthn case)."""
    asks = "true" if credentials else "false"
    return f"""<link rel="preconnect" href="https://127.0.0.1:{ports['preconnect']}">
<script>
function report(name, value) {{ new Image().src = '{REPORT}' + name + '?' + encodeURIComponent(value); }}
var ice = {{host: 0, srflx: 0, prflx: 0, relay: 0, other: 0}};
function iceCounts(state) {{
  return 'host=' + ice.host + ',srflx=' + ice.srflx + ',prflx=' + ice.prflx + ',relay=' + ice.relay +
         ',other=' + ice.other + ',state=' + state;
}}
try {{
  var pc = new RTCPeerConnection({{iceServers: [
    {{urls: 'stun:127.0.0.1:{ports['stun']}'}},
    {{urls: 'turn:127.0.0.1:{ports['turn_udp']}?transport=udp', username: 'probe', credential: 'probe'}},
    {{urls: 'turn:127.0.0.1:{ports['turn_tcp']}?transport=tcp', username: 'probe', credential: 'probe'}}]}});
  pc.onicecandidate = function(event) {{
    if (!event.candidate) {{ report('ice_done', iceCounts('complete')); return; }}
    var found = event.candidate.type || ((/ typ (\\w+)/.exec(event.candidate.candidate) || [])[1]) || 'other';
    ice[found in ice ? found : 'other'] += 1;
  }};
  pc.createDataChannel('probe');
  pc.createOffer().then(function(o) {{ return pc.setLocalDescription(o); }}).catch(function() {{}});
  setTimeout(function() {{ report('ice', iceCounts(pc.iceGatheringState)); }}, 2200);
  report('rtc', 'made');
}} catch (e) {{ report('rtc', 'threw'); }}
try {{
  var wt = new WebTransport('https://127.0.0.1:{ports['webtransport']}/');
  wt.ready.catch(function() {{}}); wt.closed.catch(function() {{}});
  report('webtransport', 'made');
}} catch (e) {{ report('webtransport', 'threw'); }}
try {{
  var source = "try {{ var w = new WebTransport('https://127.0.0.1:{ports['wt_worker']}/'); " +
               "w.ready.catch(function() {{}}); w.closed.catch(function() {{}}); postMessage('made'); }} " +
               "catch (e) {{ postMessage('threw'); }}";
  var worker = new Worker(URL.createObjectURL(new Blob([source], {{type: 'text/javascript'}})));
  worker.onmessage = function(message) {{ report('wt_worker', message.data); }};
}} catch (e) {{ report('wt_worker', 'no worker'); }}
function askForPasskeys() {{
  // A sign-in with a passkey, called off after 4 s, then - one request at a time - a new passkey,
  // called off after 2 s. Each says how it ended (no Windows dialog may come up meanwhile).
  var challenge = new Uint8Array(32);
  report('cred_asked', 'yes');
  function create() {{
    try {{
      var stop = new AbortController();
      setTimeout(function() {{ stop.abort(); }}, 2000);
      navigator.credentials.create({{signal: stop.signal, publicKey: {{challenge: challenge, timeout: 3000,
        rp: {{name: 'Probe'}}, user: {{id: new Uint8Array(8), name: 'probe', displayName: 'Probe'}},
        pubKeyCredParams: [{{type: 'public-key', alg: -7}}]}}}})
        .then(function() {{ report('cred_create', 'resolved'); }},
              function(e) {{ report('cred_create', 'rejected ' + e.name); }});
    }} catch (e) {{ report('cred_create', 'threw'); }}
  }}
  try {{
    var later = new AbortController();
    setTimeout(function() {{ later.abort(); }}, 4000);
    navigator.credentials.get({{signal: later.signal,
                               publicKey: {{challenge: challenge, timeout: 3000, userVerification: 'discouraged'}}}})
      .then(function() {{ report('cred_get', 'resolved'); create(); }},
            function(e) {{ report('cred_get', 'rejected ' + e.name); create(); }});
  }} catch (e) {{ report('cred_get', 'threw'); create(); }}
}}
try {{
  PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable().then(
    function(v) {{ report('uvpaa', String(v)); if (v === false && {asks}) {{ askForPasskeys(); }} }},
    function() {{ report('uvpaa', 'error'); }});
}} catch (e) {{ report('uvpaa', 'threw'); }}
{extra}
</script>"""


class Listeners:
    """TCP and UDP listeners on 127.0.0.1 that record what reaches them (counts and first bytes)."""

    def __init__(self, tcp: tuple[str, ...], udp: tuple[str, ...]) -> None:
        self.hits: dict[str, list[str]] = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.ports = {name: self._tcp(name) for name in tcp}
        self.ports.update({name: self._udp(name) for name in udp})

    def _record(self, name: str, text: str) -> None:
        with self.lock:
            self.hits.setdefault(name, []).append(text)

    def _tcp(self, name: str) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        sock.settimeout(0.2)

        def run() -> None:
            while not self.stop.is_set():
                try:
                    conn, _address = sock.accept()
                except OSError:
                    continue
                data = b""
                try:
                    conn.settimeout(1.0)
                    data = conn.recv(64)
                except OSError:
                    pass
                finally:
                    conn.close()
                self._record(name, f"tcp {data[:2].hex()}")
            sock.close()

        threading.Thread(target=run, daemon=True).start()
        return sock.getsockname()[1]

    def _udp(self, name: str) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.2)

        def run() -> None:
            while not self.stop.is_set():
                try:
                    data, _address = sock.recvfrom(4096)
                except OSError:
                    continue
                self._record(name, f"udp {len(data)}")
            sock.close()

        threading.Thread(target=run, daemon=True).start()
        return sock.getsockname()[1]

    def counts(self) -> dict[str, int]:
        with self.lock:
            return {name: len(self.hits.get(name, [])) for name in self.ports}

    def first(self, name: str) -> list[str]:
        with self.lock:
            return list(self.hits.get(name, []))


CHANNEL_TCP = ("preconnect", "turn_tcp")
CHANNEL_UDP = ("stun", "turn_udp", "webtransport", "wt_worker")
# The control case: Chromium as it would run WITHOUT Jarvis's WebRTC policy and with Windows Hello
# reachable - it shows that the channels above really reach this PC's listeners when nothing stops
# them (so the other cases' zero counts mean something). WebRtcHideLocalIpsWithMdns off: no mDNS
# name is announced on the network for the control's local candidates; the dead proxy stays.
CONTROL_FLAGS = ("--disable-features=AudioServiceOutOfProcess,WebRtcHideLocalIpsWithMdns", "--disable-gpu",
                 "--disable-logging", "--mute-audio")

HANG_HTML = ("<html><body style='background:#2a9d8f'><h1>stuck</h1>"
             "<script>while(true){}</script></body></html>")

STORAGE_HTML = """<!doctype html><html><body style="margin:0;height:100%;background:#334455">
<div id=s style="position:absolute;left:10%;top:10%;width:40%;height:40%;background:""" + WAIT + """"></div>
<div id=w style="position:absolute;left:55%;top:10%;width:40%;height:40%;background:""" + WAIT + """"></div>
<script>
var seen = null; try { seen = localStorage.getItem('probe'); } catch (e) {}
document.getElementById('s').style.background = seen === null ? '""" + OK + """' : '""" + BAD + """';
var back = null; try { localStorage.setItem('probe', 'kept?'); back = localStorage.getItem('probe'); } catch (e) {}
document.getElementById('w').style.background = back === 'kept?' ? '""" + OK + """' : '""" + BAD + """';
document.cookie = 'probe=1; max-age=86400';
try { indexedDB.open('probe-db', 1); } catch (e) {}
try { caches.open('probe-cache').catch(function(){}); } catch (e) {}
</script></body></html>"""


def children(name: str = "QtWebEngineProcess.exe") -> list[int]:
    """The process ids of this process's ``name`` children (Windows; [] elsewhere)."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
    kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(Entry))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    snapshot = kernel.CreateToolhelp32Snapshot(0x2, 0)
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return []
    found: list[int] = []
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32ParentProcessID == os.getpid() and entry.szExeFile.casefold() == name.casefold():
                found.append(int(entry.th32ProcessID))
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return found


def windows_security_shown() -> bool:
    """A visible top-level window titled "Windows Security" (Windows's own passkey / PIN dialog) is
    on this desktop (Windows only; False elsewhere). Only that yes / no is kept, never a title."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    user = ctypes.WinDLL("user32", use_last_error=True)
    found = [False]
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _param) -> bool:
        if user.IsWindowVisible(hwnd):
            buffer = ctypes.create_unicode_buffer(64)
            user.GetWindowTextW(hwnd, buffer, 64)
            if buffer.value == "Windows Security":
                found[0] = True
        return True

    user.EnumWindows(proto(visit), 0)
    return found[0]


def main(case: str) -> dict:
    from briefing_reader import snapshot_engine, snapshot_gate, snapshots

    if case == "control":
        snapshots.CHROMIUM_FLAGS = CONTROL_FLAGS   # read by chromium_flags at the engine's start
    elif case == "resolver":   # Chromium's error lines on stderr: they show its own lookups failing
        snapshots.CHROMIUM_FLAGS = tuple(flag for flag in snapshots.CHROMIUM_FLAGS
                                         if not flag.startswith("--log-level"))
    # The dead proxy before anything (a last net, also when this probe runs on its own): the camera
    # judges it as the inherited value and merges Jarvis's own flags in when the engine starts.
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(snapshots.OFFLINE_FLAGS)
    snapshot_engine.prepare_application()
    from PySide6.QtCore import QEventLoop, QStandardPaths, QTimer, qInstallMessageHandler
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication

    app = QApplication([sys.argv[0], "-platform", "offscreen"])
    app.setApplicationName(APP_NAME)
    messages: list[tuple[str, str]] = []
    qInstallMessageHandler(lambda _mode, context, text: messages.append(
        (str(getattr(context, "category", "") or ""), str(text))))
    results: list = []

    def wait_until(predicate, timeout_s: float) -> bool:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if predicate():
                return True
            loop = QEventLoop()
            QTimer.singleShot(20, loop.quit)
            loop.exec()
        return bool(predicate())

    def pause(seconds: float) -> None:
        wait_until(lambda: False, seconds)

    def make(**kwargs):
        camera = snapshot_engine.PageCamera(app, **kwargs)
        camera.finished.connect(results.append)
        return camera

    peak = {"children": 0}

    def shoot(camera, url: str, html: str | None = None, timeout_s: float = 40.0):
        results.clear()
        camera.shoot(url, html)
        early = list(results)   # finished() is never emitted inside shoot()

        def done() -> bool:
            peak["children"] = max(peak["children"], len(children()))
            return bool(results)

        wait_until(done, timeout_s)
        return (results[0] if results else None), bool(early)

    def describe(result) -> dict:
        if result is None:
            return {"none": True}
        return {"ok": result.ok, "reason": result.reason, "kind": snapshots.reason_kind(result.reason),
                "bytes": len(result.image), "format": result.image_format, "width": result.width,
                "height": result.height, "final_url": result.final_url, "still_loading": result.still_loading,
                "blocked": result.blocked, "took_ms": result.took_ms, "jpeg": result.image[:2] == b"\xff\xd8",
                "repr_has_url": PAGE_URL in repr(result)}

    def colours(result, points) -> list[str]:
        image = QImage.fromData(result.image) if result is not None and result.image else QImage()
        if image.isNull():
            return []
        return [image.pixelColor(int(x * (image.width() - 1)), int(y * (image.height() - 1))).name()
                for x, y in points]

    def no_children(timeout_s: float) -> bool:
        return wait_until(lambda: not children(), timeout_s)

    def marker_in_messages() -> bool:
        return any(MARKER in text for _category, text in messages)

    reports: dict[str, str] = {}

    def recording(url, *, main_frame, local=False):
        """snapshots.request_problem, also noting the page's reports (blocked like any .invalid name)."""
        if isinstance(url, str) and url.startswith(REPORT):
            name, _sep, value = url[len(REPORT):].partition("?")
            reports[name] = unquote(value)
        return snapshots.request_problem(url, main_frame=main_frame, local=local)

    report: dict = {"case": case, "platform": app.platformName()}
    if case == "guard":
        camera = make()
        report["started_before"] = camera.started()
        result, early = shoot(camera, "https://www.example.org/")
        report.update(result=describe(result), emitted_inside_shoot=early, started_after=camera.started(),
                      has_view=camera.has_view(), children=len(children()))
    elif case == "render":
        camera = make()
        result, early = shoot(camera, PAGE_URL, RENDER_HTML)
        report.update(result=describe(result), emitted_inside_shoot=early, started=camera.started(),
                      colours=colours(result, ((0.2, 0.25), (0.75, 0.7), (0.5, 0.03), (0.97, 0.97))))
        report["children_while_open"] = len(children())
        camera.close_view()
        report["children_after_close"] = 0 if no_children(10) else len(children())
        report["has_view"] = camera.has_view()
    elif case == "hooks":
        seen: list[tuple[bool, str]] = []

        def decide(url, *, main_frame, local=False):
            seen.append((bool(main_frame), url.split(":", 1)[0]))
            return snapshots.request_problem(url, main_frame=main_frame, local=local)

        listeners = Listeners(CHANNEL_TCP, CHANNEL_UDP)
        camera = make(decide=lambda url, **kw: (recording(url, **kw), decide(url, **kw))[1], settle_ms=3000)
        page = HOOKS_HTML.replace("</body>", channels_html(listeners.ports) + "</body>")
        download_dirs: list[str] = []
        refuse = camera._refuse

        def refuse_recording(name, request, method):   # where Chromium meant to put the refused download
            if name == "downloads":
                try:
                    download_dirs.append(request.downloadDirectory())
                except Exception:  # noqa: BLE001
                    download_dirs.append("?")
            refuse(name, request, method)

        camera._refuse = refuse_recording
        result, _early = shoot(camera, PAGE_URL, page)
        pause(0.5)
        folder = snapshot_engine.download_folder()
        report.update(download_dirs=download_dirs, download_folder=folder,
                      download_folder_entries=sorted(os.listdir(folder)) if os.path.isdir(folder) else None)
        pause(0.5)
        listeners.stop.set()
        report.update(channel_hits=listeners.counts(), reports=dict(reports))
        report.update(result=describe(result), hooks=camera.hook_counts(),
                      boxes=dict(zip(_BOXES, colours(result, ((0.12, 0.5), (0.36, 0.5), (0.6, 0.5), (0.84, 0.5))))),
                      decided_main_frames=sorted({scheme for main, scheme in seen if main}),
                      decided_subresources=sorted({scheme for main, scheme in seen if not main}),
                      top_levels=len(QApplication.topLevelWidgets()))
        camera.close_view()
        no_children(10)
        pause(0.5)
        report["console_marker_in_qt_messages"] = marker_in_messages()
        report["qt_categories"] = sorted({category for category, _text in messages})
    elif case == "control":
        # Without Jarvis's WebRTC policy and with Windows Hello reachable (CONTROL_FLAGS; the dead
        # proxy and the request checks stay): what the same page reaches when nothing stops it.
        listeners = Listeners(CHANNEL_TCP, CHANNEL_UDP)
        camera = make(decide=recording, settle_ms=3000)
        result, _early = shoot(camera, PAGE_URL, RENDER_HTML.replace(
            "</body>", channels_html(listeners.ports) + "</body>"))
        pause(0.5)
        listeners.stop.set()
        camera.close_view()
        no_children(10)
        report.update(result=describe(result), channel_hits=listeners.counts(), reports=dict(reports),
                      flags=os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split())
    elif case == "webauthn":
        # Jarvis's flags (as in the hooks case), and the page asks for a passkey once Windows Hello
        # answered "not reachable". Run only after the control case showed, on this PC, that Windows
        # Hello IS reachable without Jarvis's flags (tests/test_snapshot_engine.py): so a "no" here
        # is the flags' doing, and no Windows dialog can come up on the owner's screen.
        listeners = Listeners(CHANNEL_TCP, CHANNEL_UDP)
        camera = make(decide=recording, settle_ms=8000, timeout_s=25)
        shown_before = windows_security_shown()
        dialog = {"seen": False}
        results.clear()
        camera.shoot(PAGE_URL, RENDER_HTML.replace(
            "</body>", channels_html(listeners.ports, credentials=True) + "</body>"))

        def finished_watching() -> bool:
            dialog["seen"] = dialog["seen"] or windows_security_shown()
            return bool(results)

        wait_until(finished_watching, 40)
        pause(0.5)
        dialog["seen"] = dialog["seen"] or windows_security_shown()
        listeners.stop.set()
        camera.close_view()
        no_children(10)
        report.update(result=describe(results[0] if results else None), channel_hits=listeners.counts(),
                      reports=dict(reports), hooks=camera.hook_counts(),
                      windows_security_before=shown_before, windows_security_during=dialog["seen"])
    elif case == "hang":
        camera = make(timeout_s=2.0)
        result, _early = shoot(camera, PAGE_URL, HANG_HTML, timeout_s=30)
        report["result"] = describe(result)
        started = time.monotonic()
        camera.close_view()
        pause(2.0)
        report["children_2s_after_close"] = len(children())
        report["close_and_wait_s"] = round(time.monotonic() - started, 2)
    elif case == "disk":
        roots = {name: os.environ.get(name, "") for name in ("LOCALAPPDATA", "APPDATA", "TEMP", "TMP")}
        before = {name: sorted(os.listdir(path)) if path and os.path.isdir(path) else None
                  for name, path in roots.items()}
        locations = [QStandardPaths.writableLocation(kind) for kind in (
            QStandardPaths.StandardLocation.AppLocalDataLocation, QStandardPaths.StandardLocation.AppDataLocation,
            QStandardPaths.StandardLocation.CacheLocation, QStandardPaths.StandardLocation.GenericCacheLocation)]
        camera = make()
        result, _early = shoot(camera, PAGE_URL, STORAGE_HTML)
        camera.close_view()
        no_children(10)
        pause(1.0)
        after = {name: sorted(os.listdir(path)) if path and os.path.isdir(path) else None
                 for name, path in roots.items()}
        folder = snapshot_engine.download_folder()
        report.update(result=describe(result),
                      new_in_env_folders={name: sorted(set(after[name] or []) - set(before[name] or []))
                                          for name in roots},
                      download_folder=folder,
                      download_folder_entries=sorted(os.listdir(folder)) if os.path.isdir(folder) else None,
                      app_folders_made=[bool(path) and APP_NAME in path and os.path.exists(path)
                                        for path in locations[:3]],
                      app_named_folder_in_generic_cache=os.path.exists(os.path.join(locations[3], APP_NAME)))
    elif case == "teardown":
        from PySide6.QtWebEngineCore import QWebEnginePage

        camera = make()
        first, _early = shoot(camera, PAGE_URL, STORAGE_HTML)
        pause(0.5)
        view = camera._view   # the probe looks inside: no default page between shots
        report["pages_on_view_between_shots"] = len(view.findChildren(QWebEnginePage)) if view is not None else -1
        second, _early = shoot(camera, PAGE_URL, STORAGE_HTML)
        report.update(first=describe(first), second=describe(second),
                      first_storage=colours(first, ((0.3, 0.3), (0.75, 0.3))),
                      second_storage=colours(second, ((0.3, 0.3), (0.75, 0.3))),
                      children_while_open=len(children()))
        camera.close_view()
        report["children_after_close"] = 0 if no_children(10) else len(children())
        pause(1.0)
        report["children_1s_later"] = len(children())
        report["has_view"] = camera.has_view()
        report["busy"] = camera.busy()
    elif case == "closing":
        # Jarvis closing mid-shot: AppController.shutdown runs after app.exec() returned (no event
        # loop), so close_view must delete page, profile and view itself.
        camera = make(settle_ms=30_000)
        results.clear()
        camera.shoot(PAGE_URL, RENDER_HTML)
        wait_until(lambda: len(children()) > 0, 20)
        report["children_before_close"] = len(children())
        camera.close_view()   # at the top level: no event loop runs here
        report["views_right_after_close"] = sum(type(widget).__name__ == "QWebEngineView"
                                                for widget in QApplication.topLevelWidgets())
        report["busy"] = camera.busy()
        report["children_after_close"] = 0 if no_children(10) else len(children())
        wait_until(lambda: results, 5)
        report["result"] = describe(results[0] if results else None)
        report["profile_warning"] = any("still not deleted" in text for _category, text in messages)
    elif case == "inherited":
        # Inherited settings that would undo the private window (judged before Chromium starts:
        # each shot must be refused without starting it), then a plain inherited value: the page's
        # pre-connection to a listener of this process still reaches nothing.
        listeners = Listeners(("preconnect",), ())
        key_log = os.path.join(os.environ.get("TEMP", "") or str(PROJECT_ROOT), f"probe-keys-{os.getpid()}.log")
        page = RENDER_HTML.replace(
            "</body>", f'<link rel="preconnect" href="https://127.0.0.1:{listeners.ports["preconnect"]}"></body>')
        dead = " ".join(snapshots.OFFLINE_FLAGS)
        bad = {"quoted_no_sandbox": {"QTWEBENGINE_CHROMIUM_FLAGS": f'"--no-sandbox" {dead}'},
               "quoted_no_proxy": {"QTWEBENGINE_CHROMIUM_FLAGS": f'"--no-proxy-server" {dead}'},
               "quoted_debugging": {"QTWEBENGINE_CHROMIUM_FLAGS": f'"--remote-debugging-port=9222" {dead}'},
               "quoted_certificates": {"QTWEBENGINE_CHROMIUM_FLAGS": f'"--ignore-certificate-errors" {dead}'},
               "terminator": {"QTWEBENGINE_CHROMIUM_FLAGS": f"--disable-gpu -- {dead}"},
               "key_log_flag": {"QTWEBENGINE_CHROMIUM_FLAGS": f"--ssl-key-log-file={key_log} {dead}"},
               "key_log_env": {"QTWEBENGINE_CHROMIUM_FLAGS": dead, "SSLKEYLOGFILE": key_log},
               "windows_sign_in": {"QTWEBENGINE_CHROMIUM_FLAGS": f"--auth-server-allowlist=* {dead}"}}
        camera = make(settle_ms=1500)
        refused: dict[str, str] = {}
        for label, env in bad.items():
            os.environ.update(env)
            result, _early = shoot(camera, PAGE_URL, page)
            refused[label] = snapshots.reason_kind(result.reason) if result is not None else "none"
            os.environ.pop("SSLKEYLOGFILE", None)
            os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = dead
        report.update(refused=refused, started_after_refusals=camera.started(),
                      children_after_refusals=len(children()))
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"--disable-gpu {dead}"
        result, _early = shoot(camera, PAGE_URL, page)
        pause(0.5)
        listeners.stop.set()
        camera.close_view()
        no_children(10)
        report.update(result=describe(result), started=camera.started(), channel_hits=listeners.counts(),
                      key_log_made=os.path.exists(key_log),
                      flags=snapshots.flag_tokens(os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")))
    elif case in ("gate", "resolver"):
        # Production network settings (see the module doc): the camera's production request rules
        # and Chromium through the network gate, whose resolver and connector are fakes.
        listeners = Listeners(CHANNEL_TCP + ("tunnel",), CHANNEL_UDP)
        answers = {"tunnel.example.org": [PUBLIC_LABEL], "rebind.example.org": ["127.0.0.1"]}
        asked: list[str] = []

        def resolve(host: str, _port: int) -> list[str]:
            asked.append(host)
            if host not in answers:
                raise OSError("the probe resolves no real name")
            return list(answers[host])

        def connect(address: str, _port: int, timeout: float) -> socket.socket:
            if address != PUBLIC_LABEL:
                raise OSError("the probe reaches only its own listener")
            return socket.create_connection(("127.0.0.1", listeners.ports["tunnel"]), timeout=timeout)

        gate_hosts: set[str] = set()
        judge = snapshots.tunnel_target

        def noting(authority):
            host, problem = judge(authority)
            gate_hosts.add(host)
            return host, problem

        snapshots.tunnel_target = noting   # the gate reads it from the module: what it was asked for
        snapshot_engine._gate_factory = lambda: snapshot_gate.ConnectGate(resolve=resolve, connect=connect)
        snapshot_engine._test_platform = lambda: False
        extra = ("fetch('https://rebind.example.org/x').catch(function() {});\n"
                 "fetch('https://tunnel.example.org/x').catch(function() {});\n"
                 # TURN servers by NAME: Chromium looks such a name up itself (no request, no proxy)
                 "try { var named = new RTCPeerConnection({iceServers: ["
                 "{urls: 'turn:localhost:443?transport=tcp', username: 'probe', credential: 'probe'},"
                 "{urls: 'turn:turn.localhost:443?transport=tcp', username: 'probe', credential: 'probe'}]});\n"
                 "  var namedErrors = 0; named.onicecandidateerror = function() { namedErrors += 1; };\n"
                 "  named.createDataChannel('named');\n"
                 "  named.createOffer().then(function(o) { return named.setLocalDescription(o); })"
                 ".catch(function() {});\n"
                 "  setTimeout(function() { report('named_turn_errors', String(namedErrors)); }, 2500);\n"
                 "} catch (e) { report('named_turn_errors', 'threw'); }")
        camera = make(decide=recording, settle_ms=4000, timeout_s=20)
        result, _early = shoot(camera, PAGE_URL, RENDER_HTML.replace(
            "</body>", channels_html(listeners.ports, extra) + "</body>"))
        camera.close_view()
        no_children(10)
        pause(0.5)
        listeners.stop.set()
        flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
        gate = snapshot_engine._ENGINE.gate
        report.update(result=describe(result), channel_hits=listeners.counts(), reports=dict(reports),
                      tunnel=listeners.first("tunnel"), gate_counts=snapshot_engine.gate_counts(),
                      gate_port=gate.port() if gate is not None else 0, gate_hosts=sorted(gate_hosts),
                      resolved=sorted(set(asked)), flags=snapshots.flag_tokens(flags))
    else:
        report["error"] = f"unknown case {case!r}"
    report["children_peak_during_shots"] = peak["children"]
    report["offline_flags"] = all(flag in os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
                                  for flag in snapshots.OFFLINE_FLAGS)
    qInstallMessageHandler(None)
    return report


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1] if len(sys.argv) > 1 else "")), flush=True)
