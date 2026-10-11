"""Page snapshots, the Qt half: a hidden private window that opens a page a web research read, or a
checked source its answer cites, and takes a picture of its first screen, for the LIVE view
(pictures.page_ready).

    webengine_available()             PySide6 has QtWebEngine 6.8+ (checked without importing it)
    prepare_application()             Qt's attributes the web engine needs, set before QApplication
    PageCamera(parent, ...)           one page at a time on the GUI thread: shoot(url) -> finished(ShotResult)
    PageSnapshotter(parent, ...)      the LIVE side: request(step, url, task_id=) from any thread (a
                                      PENDING picture at once), the shots one at a time on the GUI
                                      thread, READY / "Picture unavailable" on the step; end_run,
                                      cancel_task, drop_cleared (LIVE's Clear), shutdown

How a shot runs (PageCamera, GUI thread only): QtWebEngine is imported on first use (never at
import: this module imports QtCore and QtGui only), with the Software Qt Quick API and the Chromium
flags of snapshots.CHROMIUM_FLAGS (no Windows Hello for pages, no WebRTC UDP that bypasses the
proxy, no name looked up by Chromium itself, no error lines of Chromium's on stderr). Every
connection Chromium makes goes through Jarvis's network gate (snapshot_gate.ConnectGate, started
first, loopback included): it tunnels only to public IPv4 addresses on port 443, checked on the
address a name actually resolves to, so pre-connections, redirect targets and WebRTC's TCP reach
nothing on this PC or its network either. Every shot gets a FRESH off-the-record profile and page
(no cookies, cache, storage or permission carries from one page to the next and nothing is written
to disk) shown in one hidden 1280x800 view per research run (WA_DontShowOnScreen: never on screen,
no focus, no mouse). Every request of the page also passes snapshots.request_problem (public https
only, by name); downloads are cancelled, permissions denied, pop-ups, dialogs, file pickers, full
screen, sign-in prompts and client certificates refused, bad certificates rejected, sound muted,
the page's console text dropped. Inherited settings that would undo this (no sandbox, remote
debugging, ignored certificate errors, a TLS key log, a quoted or "--"-cut flags value ...) refuse
every shot: judged on the environment as Jarvis inherited it (_judged_environ), never on his own
merged flags. A shot ends with a picture (scaled to
at most 960 px wide, JPEG) or a reason (snapshots.R_*): not loaded, a 15 s limit, a blank or hung
page, a crashed renderer. Then its page and profile are deleted; close_view() deletes the view and
the renderer process exits (at once when no event loop runs any more: Jarvis closing). On the
offscreen platform (tests, harnesses) no network page is ever opened: only local HTML, every
network request of it is blocked, and Chromium runs with a proxy that is not there
(snapshots.OFFLINE_FLAGS, instead of the gate) as a last net.

Logging: counts, kinds and times only ("Page snapshot: taken in 2.3 s (1 of 2 for this research,
3 requests blocked)"); never a URL, a host, a title or anything of the page.
"""

from __future__ import annotations

import atexit
import importlib.util
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping
from typing import Any

from PySide6.QtCore import QBuffer, QByteArray, QCoreApplication, QIODevice, QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QGuiApplication, QImage, QImageWriter

from . import pictures, snapshot_gate, snapshots
from .snapshots import ShotResult

logger = logging.getLogger(__name__)

MIN_QT_VERSION = (6, 8)          # QWebEnginePermission
HTTP_CACHE_BYTES = 32 * 1024 * 1024
TEST_PLATFORMS = frozenset({"offscreen", "minimal"})   # no network page is ever opened on these
GRAB_MIN_PROGRESS = 30           # at the time limit a page this far along is pictured anyway
_UNIFORM_SAMPLE = (48, 30)       # a grab is "blank" when this smooth thumbnail of it is one colour
_LOCAL_SCHEMES = frozenset({"data", "blob", "about"})
_FLAGS_ENV = "QTWEBENGINE_CHROMIUM_FLAGS"
_SETTINGS_OFF = (
    "JavascriptCanOpenWindows", "JavascriptCanAccessClipboard", "JavascriptCanPaste", "PluginsEnabled",
    "PdfViewerEnabled", "WebGLEnabled", "Accelerated2dCanvasEnabled", "LocalContentCanAccessRemoteUrls",
    "LocalContentCanAccessFileUrls", "ScreenCaptureEnabled", "AllowRunningInsecureContent",
    "AllowGeolocationOnInsecureOrigins", "HyperlinkAuditingEnabled", "FullScreenSupportEnabled",
    "DnsPrefetchEnabled", "NavigateOnDropEnabled", "FocusOnNavigationEnabled",
    "AllowWindowActivationFromJavaScript", "ErrorPageEnabled", "ShowScrollBars", "AutoLoadIconsForPage",
    "TouchIconsEnabled", "SpatialNavigationEnabled", "ScrollAnimatorEnabled", "BackForwardCacheEnabled",
)
_SETTINGS_ON = ("JavascriptEnabled", "WebRTCPublicInterfacesOnly", "PlaybackRequiresUserGesture",
                "LocalStorageEnabled")


# --------------------------------------------------------------------------
# The web engine (process-wide, GUI thread)
# --------------------------------------------------------------------------

class _EngineState:
    """Process-wide: QtWebEngine is imported once, Chromium starts once (at the first profile)."""

    def __init__(self) -> None:
        self.imported = False
        self.started = False          # Chromium initialised in this process (the first profile made)
        self.failed = ""              # a reason that will not change (R_NO_ENGINE)
        self.import_at = 0.0
        self.classes: Any = None
        self.gate: Any = None         # the network gate Chromium's proxy points at (not on a test platform)
        self.inherited_flags = ""     # QTWEBENGINE_CHROMIUM_FLAGS as inherited, before Jarvis merged his in
        self.written_flags: str | None = None   # what Jarvis wrote there (None: nothing yet)


_ENGINE = _EngineState()
# Makes the network gate (a seam for the engine probe: its gate never reaches the network).
_gate_factory: Callable[[], Any] = snapshot_gate.ConnectGate


def webengine_available() -> bool:
    """True when PySide6's QtWebEngine (6.8 or newer: QWebEnginePermission) is installed. Never
    imports QtWebEngine (only looks for it) and never raises."""
    try:
        core = sys.modules.get("PySide6.QtWebEngineCore")
        if core is not None:
            return hasattr(core, "QWebEnginePermission") and (
                "PySide6.QtWebEngineWidgets" in sys.modules
                or importlib.util.find_spec("PySide6.QtWebEngineWidgets") is not None)
        import PySide6

        version = tuple(getattr(PySide6, "__version_info__", (0, 0))[:2])
        if version < MIN_QT_VERSION:
            return False
        return all(importlib.util.find_spec(name) is not None
                   for name in ("PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQuick"))
    except Exception:  # noqa: BLE001 - "not available" is the safe answer
        return False


_DOWNLOAD_FOLDER: list[str] = []   # this process's private download folder, made on first use


def download_folder() -> str:
    """The pictured pages' download folder: a private temporary folder of this process, never the
    owner's Downloads (Chromium prepares even a download it is told to cancel in the profile's
    download folder). Every download is cancelled, so it stays empty; it is deleted when Jarvis
    exits. Raises OSError when it cannot be made (the shot then fails: no fallback folder)."""
    if not _DOWNLOAD_FOLDER:
        path = tempfile.mkdtemp(prefix="jarvis-snapshots-")
        _DOWNLOAD_FOLDER.append(path)
        atexit.register(shutil.rmtree, path, True)
    return _DOWNLOAD_FOLDER[0]


def prepare_application() -> None:
    """Qt's application attributes the web engine needs, set before the QApplication exists:
    AA_ShareOpenGLContexts (Qt's rule for a web engine started after QApplication) and
    AA_DisableShaderDiskCache (no pipeline cache folder under %LOCALAPPDATA%). QtCore only,
    idempotent; does nothing once a QCoreApplication exists; never raises."""
    try:
        if QCoreApplication.instance() is not None:
            return
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_DisableShaderDiskCache, True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Page snapshots: application attributes not set (%s)", type(exc).__name__)


def _test_platform() -> bool:
    try:
        return QGuiApplication.platformName().casefold() in TEST_PLATFORMS
    except Exception:  # noqa: BLE001 - unknown: treat as a test run (no network page)
        return True


def _classes() -> Any:
    """The QtWebEngine classes and Jarvis's subclasses of them, made on first use (cached)."""
    if _ENGINE.classes is not None:
        return _ENGINE.classes
    from types import SimpleNamespace

    from PySide6.QtWebEngineCore import (
        QWebEnginePage,
        QWebEngineProfile,
        QWebEngineSettings,
        QWebEngineUrlRequestInfo,
        QWebEngineUrlRequestInterceptor,
    )
    from PySide6.QtWebEngineWidgets import QWebEngineView

    class Interceptor(QWebEngineUrlRequestInterceptor):
        """Every request of the page: ``judge(info)`` True blocks it (any error blocks it too)."""

        def __init__(self, judge: Callable[[Any], bool], parent: QObject) -> None:
            super().__init__(parent)
            self._judge = judge

        def interceptRequest(self, info: Any) -> None:  # noqa: N802 - Qt's name
            try:
                block = bool(self._judge(info))
            except Exception:  # noqa: BLE001 - never raise into Chromium: block
                block = True
            if block:
                try:
                    info.block(True)
                except Exception:  # noqa: BLE001
                    pass

    class Page(QWebEnginePage):
        """A page that opens no window, shows no dialog or file picker and keeps no console text."""

        def createWindow(self, _type: Any) -> Any:  # noqa: N802
            return None

        def javaScriptConsoleMessage(self, *_args: Any) -> None:  # noqa: N802 - the text is dropped
            return None

        def javaScriptAlert(self, *_args: Any) -> None:  # noqa: N802
            return None

        def javaScriptConfirm(self, *_args: Any) -> bool:  # noqa: N802
            return False

        def javaScriptPrompt(self, *_args: Any) -> tuple[bool, str]:  # noqa: N802
            return False, ""

        def chooseFiles(self, *_args: Any) -> list[str]:  # noqa: N802
            return []

    info = QWebEngineUrlRequestInfo.ResourceType
    main_types = {info.ResourceTypeMainFrame}
    preload = getattr(info, "ResourceTypeNavigationPreloadMainFrame", None)
    if preload is not None:
        main_types.add(preload)
    _ENGINE.classes = SimpleNamespace(
        QWebEnginePage=QWebEnginePage, QWebEngineProfile=QWebEngineProfile, QWebEngineSettings=QWebEngineSettings,
        QWebEngineView=QWebEngineView, Interceptor=Interceptor, Page=Page, main_types=frozenset(main_types),
        websocket=getattr(info, "ResourceTypeWebSocket", None))
    return _ENGINE.classes


def _start_gate() -> int:
    """The network gate's port, starting it once (0: it could not start)."""
    if _ENGINE.gate is None:
        _ENGINE.gate = _gate_factory()
    return int(_ENGINE.gate.start() or 0)


def gate_counts() -> dict[str, int]:
    """The network gate's counts (tunnels / refused / failed; {} before it started)."""
    try:
        return dict(_ENGINE.gate.counts()) if _ENGINE.gate is not None else {}
    except Exception:  # noqa: BLE001
        return {}


def _judged_environ() -> Mapping[str, str]:
    """The environment the inherited-settings checks judge (snapshots.sandbox_problem /
    engine_flags_problem): os.environ, but with QTWEBENGINE_CHROMIUM_FLAGS as Jarvis inherited it
    once he wrote his own flags there - his resolver rules and proxy are never what is judged. A
    value changed since he wrote it is judged as it is now."""
    written = _ENGINE.written_flags
    if written is None or os.environ.get(_FLAGS_ENV) != written:
        return os.environ
    environ = dict(os.environ)
    environ[_FLAGS_ENV] = _ENGINE.inherited_flags
    return environ


def _start_engine() -> str:
    """Import QtWebEngine once (the network gate, Chromium flags and the Software Qt Quick API
    first): "" when ready, else a reason (R_NO_ENGINE / R_ENGINE_FAILED). Chromium never starts
    without the gate (or, on a test platform, the dead proxy)."""
    if _ENGINE.imported:
        return ""
    if _ENGINE.failed:
        return _ENGINE.failed
    if not webengine_available():
        _ENGINE.failed = snapshots.R_NO_ENGINE
        return _ENGINE.failed
    try:
        _ENGINE.import_at = time.monotonic()
        offline = _test_platform()
        gate_port = 0 if offline else _start_gate()
        if not offline and not gate_port:
            logger.warning("Page snapshots: the network gate could not be started")
            return snapshots.R_ENGINE_FAILED
        if _ENGINE.written_flags is None or os.environ.get(_FLAGS_ENV) != _ENGINE.written_flags:
            _ENGINE.inherited_flags = os.environ.get(_FLAGS_ENV, "")
        merged = snapshots.chromium_flags(_ENGINE.inherited_flags, offline=offline, gate_port=gate_port)
        os.environ[_FLAGS_ENV] = merged
        _ENGINE.written_flags = merged
        from PySide6.QtQuick import QQuickWindow, QSGRendererInterface

        QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.Software)
        _classes()
    except ImportError:
        _ENGINE.failed = snapshots.R_NO_ENGINE
        return _ENGINE.failed
    except Exception as exc:  # noqa: BLE001
        logger.warning("Page snapshots: the web engine could not be loaded (%s)", type(exc).__name__)
        return snapshots.R_ENGINE_FAILED
    _ENGINE.imported = True
    return ""


def _quietly(step: Callable[[], Any]) -> None:
    """Run one teardown step; a failure (an object already gone) never stops the next one."""
    try:
        step()
    except Exception:  # noqa: BLE001
        pass


def _alive(item: Any) -> bool:
    try:
        import shiboken6

        return bool(shiboken6.isValid(item))
    except Exception:  # noqa: BLE001
        return False


def _delete_now(item: Any) -> None:
    import shiboken6

    if shiboken6.isValid(item):
        shiboken6.delete(item)


def _event_loop_running() -> bool:
    try:
        return QThread.currentThread().loopLevel() > 0
    except Exception:  # noqa: BLE001
        return True


def _encoded(url: QUrl) -> str:
    return bytes(url.toEncoded().data()).decode("ascii", "replace")


def _uniform(image: QImage) -> bool:
    """True when the grab shows one colour (nothing to picture: a blank or hung page)."""
    if image.isNull() or image.width() < 2 or image.height() < 2:
        return True
    small = image.scaled(_UNIFORM_SAMPLE[0], _UNIFORM_SAMPLE[1], Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation).convertToFormat(QImage.Format.Format_RGB32)
    first = small.pixel(0, 0)
    return all(small.pixel(x, y) == first for y in range(small.height()) for x in range(small.width()))


def _write(image: QImage, fmt: str, quality: int) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        return b""
    try:
        if not image.save(buffer, fmt, quality):
            return b""
    finally:
        buffer.close()
    return bytes(data.data())


def _jpeg_supported() -> bool:
    try:
        return any(bytes(name.data()).lower() in (b"jpg", b"jpeg") for name in QImageWriter.supportedImageFormats())
    except Exception:  # noqa: BLE001
        return False


def encode_picture(image: QImage, max_bytes: int) -> tuple[bytes, str]:
    """``image`` as JPEG (quality JPEG_QUALITY, then JPEG_QUALITY_SMALL when over ``max_bytes``) or
    PNG when JPEG cannot be written here; (b"", fmt) when it does not fit ``max_bytes``."""
    image = image.convertToFormat(QImage.Format.Format_RGB32)
    if _jpeg_supported():
        for quality in (snapshots.JPEG_QUALITY, snapshots.JPEG_QUALITY_SMALL):
            data = _write(image, "JPG", quality)
            if data and len(data) <= max_bytes:
                return data, "JPG"
        return b"", "JPG"
    data = _write(image, "PNG", -1)
    return (data, "PNG") if data and len(data) <= max_bytes else (b"", "PNG")


# --------------------------------------------------------------------------
# The camera: one page at a time
# --------------------------------------------------------------------------

class _Shot:
    """One shot's state (GUI thread)."""

    __slots__ = ("serial", "url", "html", "final_hint", "local", "offline", "started", "progress", "loaded",
                 "starts", "fail_starts", "tries", "blocked", "done", "profile", "page", "interceptor", "on_view")

    def __init__(self, serial: int, url: str, html: str | None, final_hint: str) -> None:
        self.serial = serial
        self.url = url
        self.html = html
        self.final_hint = final_hint
        self.local = html is not None
        self.offline = True
        self.started = time.monotonic()
        self.progress = 0
        self.loaded = False
        self.starts = 0
        self.fail_starts = -1
        self.tries = 0
        self.blocked = 0
        self.done = False
        self.profile: Any = None
        self.page: Any = None
        self.interceptor: Any = None
        self.on_view = False

    def __repr__(self) -> str:
        return f"_Shot(serial={self.serial}, loaded={self.loaded}, done={self.done})"


class PageCamera(QObject):
    """Takes a picture of one page at a time in a hidden private window (GUI thread only).

    shoot(url) / shoot(url, html) (local HTML instead of the URL: tests and harnesses) ->
    ``finished(ShotResult)``, always delivered from the event loop (never inside shoot()).
    abort() ends the current shot (R_CANCELLED); close_view() deletes the hidden view (the
    renderer process exits); started() says whether the web engine runs in this process.
    """

    finished = Signal(object)
    _deliver = Signal(object)

    def __init__(self, parent: QObject | None = None, *,
                 decide: Callable[..., str] = snapshots.request_problem, timeout_s: float = snapshots.TIMEOUT_S,
                 settle_ms: int = snapshots.SETTLE_MS, max_bytes: int = 1_500_000,
                 blank_retry_ms: int = snapshots.BLANK_RETRY_MS) -> None:
        super().__init__(parent)
        self._decide = decide
        self._timeout_ms = max(100, int(float(timeout_s) * 1000))
        self._settle_ms = max(0, int(settle_ms))
        self._retry_ms = max(0, int(blank_retry_ms))
        self._max_bytes = max(1, int(max_bytes))
        self._view: Any = None
        self._shot: _Shot | None = None
        self._serial = 0
        self._hooks: Counter[str] = Counter()
        self._retired: list[Any] = []    # interceptors of ended shots until Qt deletes them
        self._doomed: list[Any] = []     # pages and profiles (then the view) waiting for their deleteLater
        self._timeout = self._timer(self._on_timeout)
        self._settle = self._timer(self._on_settle)
        self._fail_check = self._timer(self._on_fail_check)
        self._deliver.connect(self._emit_finished, Qt.ConnectionType.QueuedConnection)

    def _timer(self, slot: Callable[[], None]) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(slot)
        return timer

    def __repr__(self) -> str:
        return f"PageCamera(busy={self.busy()}, view={self._view is not None})"

    # ---- state -------------------------------------------------------------------------------

    def busy(self) -> bool:
        return self._shot is not None

    def started(self) -> bool:
        """The web engine (Chromium) was started in this process."""
        return _ENGINE.started

    def has_view(self) -> bool:
        return self._view is not None

    def hook_counts(self) -> dict[str, int]:
        """How often each refusal hook fired (permissions, downloads, windows ...): counts only."""
        return dict(self._hooks)

    # ---- the shot ----------------------------------------------------------------------------

    def shoot(self, url: str, html: str | None = None, *, final_url: str = "") -> None:
        """Picture the page at ``url`` (or the local ``html``, reported as ``url``; ``final_url``
        the address to report for it). A call while a shot runs is ignored. Never raises."""
        if self._shot is not None:
            logger.debug("Page snapshots: a shot was asked for while one runs (ignored)")
            return
        self._prune()
        self._serial += 1
        shot = self._shot = _Shot(self._serial, url if isinstance(url, str) else "",
                                  html if isinstance(html, str) else None,
                                  final_url if isinstance(final_url, str) else "")
        try:
            shot.offline = _test_platform()
            if shot.offline and not shot.local:
                self._end(shot, snapshots.R_TEST_RUN)   # never a network page in a test or harness
                return
            environ = _judged_environ()   # as inherited: never Jarvis's own merged flags
            problem = snapshots.sandbox_problem(environ) or snapshots.engine_flags_problem(environ)
            if problem:
                self._end(shot, problem)
                return
            if QCoreApplication.instance() is None:
                self._end(shot, snapshots.R_ENGINE_FAILED)
                return
            reason = _start_engine()
            if reason:
                self._end(shot, reason)
                return
            reason = self._open(shot, _classes())
            if reason:
                self._end(shot, reason)
        except Exception as exc:  # noqa: BLE001 - a shot never raises
            logger.debug("Page snapshots: the shot failed to start (%s)", type(exc).__name__)
            self._end(shot, snapshots.R_ENGINE_FAILED)

    def _open(self, shot: _Shot, classes: Any) -> str:
        first = not _ENGINE.started
        profile = classes.QWebEngineProfile(self)   # no storage name: off the record
        shot.profile = profile
        if not profile.isOffTheRecord():
            return snapshots.R_ENGINE_FAILED
        if first:
            _ENGINE.started = True
            logger.info("Page snapshots: web engine started in %.1f s", time.monotonic() - _ENGINE.import_at)
        self._configure(profile, classes, shot)
        view = self._view
        if view is None:
            view = classes.QWebEngineView()
            view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            view.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            view.setAccessibleName("")
            view.resize(*snapshots.VIEWPORT)
            self._view = view
        page = classes.Page(profile, view)
        shot.page = page
        self._connect(page, shot)
        page.setAudioMuted(True)
        view.setPage(page)
        shot.on_view = True
        if not view.isVisible():
            view.show()
        self._timeout.start(self._timeout_ms)
        if shot.local:
            page.setHtml(shot.html or "", QUrl(snapshots.LOCAL_BASE_URL))
            return ""
        try:
            target = QUrl.fromEncoded(QByteArray(shot.url.encode("ascii")))
        except UnicodeEncodeError:
            target = QUrl(shot.url)
        if not target.isValid() or self._decide(_encoded(target), main_frame=True, local=False):
            shot.blocked += 1
            return snapshots.R_LOAD_FAILED
        page.load(target)
        return ""

    def _configure(self, profile: Any, classes: Any, shot: _Shot) -> None:
        kinds = classes.QWebEngineProfile
        profile.setHttpCacheType(kinds.HttpCacheType.MemoryHttpCache)
        profile.setHttpCacheMaximumSize(HTTP_CACHE_BYTES)
        profile.setPersistentCookiesPolicy(kinds.PersistentCookiesPolicy.NoPersistentCookies)
        if hasattr(profile, "setPersistentPermissionsPolicy"):
            profile.setPersistentPermissionsPolicy(kinds.PersistentPermissionsPolicy.AskEveryTime)
        profile.setSpellCheckEnabled(False)
        if hasattr(profile, "setPushServiceEnabled"):
            profile.setPushServiceEnabled(False)
        # The shot holds the interceptor: Qt only points at it, and an unreferenced Python interceptor
        # is collected and then checks nothing.
        shot.interceptor = classes.Interceptor(lambda info: self._judge(shot, info), profile)
        profile.setUrlRequestInterceptor(shot.interceptor)
        profile.setDownloadPath(download_folder())   # never the owner's Downloads, even for a cancelled one
        profile.downloadRequested.connect(lambda download: self._refuse("downloads", download, "cancel"))
        settings = profile.settings()
        attribute = classes.QWebEngineSettings.WebAttribute
        for names, value in ((_SETTINGS_OFF, False), (_SETTINGS_ON, True)):
            for name in names:
                item = getattr(attribute, name, None)
                if item is not None:
                    settings.setAttribute(item, value)
        policy = classes.QWebEngineSettings
        settings.setUnknownUrlSchemePolicy(policy.UnknownUrlSchemePolicy.DisallowUnknownUrlSchemes)
        settings.setImageAnimationPolicy(policy.ImageAnimationPolicy.AnimateOnce)

    def _connect(self, page: Any, shot: _Shot) -> None:
        page.loadStarted.connect(lambda: self._on_started(shot))
        page.loadProgress.connect(lambda value: self._on_progress(shot, value))
        page.loadFinished.connect(lambda ok: self._on_loaded(shot, ok))
        page.navigationRequested.connect(lambda request: self._on_navigation(shot, request))
        page.renderProcessTerminated.connect(lambda *_args: self._on_crash(shot))
        if hasattr(page, "permissionRequested"):
            page.permissionRequested.connect(lambda permission: self._refuse("permissions", permission, "deny"))
        else:   # before 6.8 (webengine_available() says no; kept for safety)
            page.featurePermissionRequested.connect(lambda origin, feature: self._deny_feature(page, origin, feature))
        refusals = (("fileSystemAccessRequested", "reject"), ("desktopMediaRequested", "cancel"),
                    ("webAuthUxRequested", "cancel"), ("registerProtocolHandlerRequested", "reject"),
                    ("fullScreenRequested", "reject"), ("certificateError", "rejectCertificate"),
                    ("selectClientCertificate", "selectNone"))
        for signal_name, method in refusals:
            signal = getattr(page, signal_name, None)
            if signal is not None:
                signal.connect(lambda request, name=signal_name, how=method: self._refuse(name, request, how))
        # Sign-in prompts: the authenticator is left empty, so the request is refused.
        page.authenticationRequired.connect(lambda *_args: self._count("authenticationRequired"))
        page.proxyAuthenticationRequired.connect(lambda *_args: self._count("proxyAuthenticationRequired"))
        page.newWindowRequested.connect(lambda _request: self._count("windows"))

    # ---- hooks (GUI thread; pure, fast, never raise) -----------------------------------------

    def _count(self, name: str) -> None:
        self._hooks[name] += 1

    def _refuse(self, name: str, request: Any, method: str) -> None:
        self._hooks[name] += 1
        try:
            getattr(request, method)()
        except Exception:  # noqa: BLE001
            pass

    def _deny_feature(self, page: Any, origin: Any, feature: Any) -> None:
        self._hooks["permissions"] += 1
        try:
            page.setFeaturePermission(origin, feature, type(page).PermissionPolicy.PermissionDeniedByUser)
        except Exception:  # noqa: BLE001
            pass

    def _problem(self, shot: _Shot, url: QUrl, main_frame: bool) -> str:
        """Why the page may not load ``url`` ("" = it may)."""
        scheme = url.scheme().casefold()
        text = f"{scheme}:" if scheme in ("data", "blob") else _encoded(url)
        problem = self._decide(text, main_frame=main_frame, local=shot.local)
        if not problem and shot.offline and scheme not in _LOCAL_SCHEMES:
            problem = "blocked: no network in a test run"
        return problem

    def _judge(self, shot: _Shot, info: Any) -> bool:
        """The interceptor: True blocks the request (counted)."""
        if shot.done:
            return True
        classes = _ENGINE.classes
        kind = info.resourceType()
        if info.isDownload() or (classes.websocket is not None and kind == classes.websocket):
            shot.blocked += 1
            return True
        if self._problem(shot, info.requestUrl(), kind in classes.main_types):
            shot.blocked += 1
            return True
        return False

    def _on_navigation(self, shot: _Shot, request: Any) -> None:
        try:
            if shot.done or self._problem(shot, request.url(), bool(request.isMainFrame())):
                if not shot.done:
                    shot.blocked += 1
                request.reject()
        except Exception:  # noqa: BLE001 - unsure: refuse
            try:
                request.reject()
            except Exception:  # noqa: BLE001
                pass

    def _on_started(self, shot: _Shot) -> None:
        shot.starts += 1

    def _on_progress(self, shot: _Shot, value: int) -> None:
        shot.progress = int(value)

    def _on_loaded(self, shot: _Shot, ok: bool) -> None:
        if shot is not self._shot or shot.done:
            return
        if ok:
            shot.loaded = True
            self._fail_check.stop()
            if shot.tries == 0:
                self._settle.start(self._settle_ms)
            return
        if shot.loaded:
            return   # a later navigation failed; the page that loaded is still there
        shot.fail_starts = shot.starts
        self._fail_check.start(snapshots.LOAD_FAIL_GRACE_MS)

    def _on_fail_check(self) -> None:
        shot = self._shot
        if shot is None or shot.done or shot.loaded:
            return
        page = shot.page
        loading = False
        try:
            loading = bool(page.isLoading()) if page is not None and hasattr(page, "isLoading") else False
        except Exception:  # noqa: BLE001
            loading = False
        if shot.starts > shot.fail_starts or loading:
            return   # a new load began (a script's redirect): the time limit still applies
        self._end(shot, snapshots.R_LOAD_FAILED)

    def _on_crash(self, shot: _Shot) -> None:
        if shot is self._shot and not shot.done:
            self._end(shot, snapshots.R_CRASHED)

    def _on_settle(self) -> None:
        shot = self._shot
        if shot is not None and not shot.done:
            self._grab(shot, final=False)

    def _on_timeout(self) -> None:
        shot = self._shot
        if shot is None or shot.done:
            return
        if shot.loaded:
            self._grab(shot, final=True)
        elif shot.progress >= GRAB_MIN_PROGRESS:
            self._grab(shot, final=True, still_loading=True)
        else:
            self._end(shot, snapshots.R_TIMEOUT)

    def _grab(self, shot: _Shot, *, final: bool, still_loading: bool = False) -> None:
        try:
            view = self._view
            image = view.grab().toImage() if view is not None else QImage()
            if _uniform(image):
                if final:
                    self._end(shot, snapshots.R_BLANK if shot.loaded else snapshots.R_TIMEOUT)
                elif shot.tries < snapshots.BLANK_RETRIES:
                    shot.tries += 1
                    self._settle.start(self._retry_ms)
                else:
                    self._end(shot, snapshots.R_BLANK)
                return
            scaled = image.scaledToWidth(min(snapshots.MAX_WIDTH, image.width()),
                                         Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(1.0)
            data, fmt = encode_picture(scaled, self._max_bytes)
            if not data:
                self._end(shot, snapshots.R_TOO_LARGE)
                return
            final_url = shot.final_hint or shot.url
            if not shot.local and shot.page is not None:
                final_url = _encoded(shot.page.url()) or shot.url
            self._end(shot, "", image=data, image_format=fmt, width=scaled.width(), height=scaled.height(),
                      final_url=final_url, still_loading=still_loading)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Page snapshots: the picture could not be made (%s)", type(exc).__name__)
            self._end(shot, snapshots.R_BLANK)

    # ---- the end of a shot -------------------------------------------------------------------

    def _end(self, shot: _Shot, reason: str, **data: Any) -> None:
        """Every path ends here once: timers stopped, page and profile deleted, finished() queued."""
        if shot.done:
            return
        shot.done = True
        for timer in (self._timeout, self._settle, self._fail_check):
            timer.stop()
        took = int((time.monotonic() - shot.started) * 1000)
        if reason:
            result = ShotResult(False, reason, took_ms=took, blocked=shot.blocked)
        else:
            result = ShotResult(True, took_ms=took, blocked=shot.blocked, **data)
        try:
            self._teardown(shot)
        except Exception as exc:  # noqa: BLE001 - the shot still ends
            logger.debug("Page snapshots: teardown failed (%s)", type(exc).__name__)
        if self._shot is shot:
            self._shot = None
        try:
            self._deliver.emit(result)
        except Exception:  # noqa: BLE001 - the camera is going away
            pass

    def _teardown(self, shot: _Shot) -> None:
        page, profile, interceptor = shot.page, shot.profile, shot.interceptor
        shot.page = shot.profile = shot.interceptor = None
        if interceptor is not None:
            # Kept until Qt deleted it with its profile (a request still in flight is blocked: shot.done).
            self._retired.append(interceptor)
        if page is not None:
            _quietly(lambda: page.triggerAction(type(page).WebAction.Stop))
            if shot.on_view and self._view is not None:
                _quietly(lambda: self._view.setPage(None))
            _quietly(page.deleteLater)
            self._doomed.append(page)
        if profile is not None:
            _quietly(profile.deleteLater)   # after the page (deferred deletes run in order); its interceptor with it
            self._doomed.append(profile)
        self._prune()

    def _prune(self) -> None:
        """Let go of the interceptors, pages and profiles Qt has deleted."""
        self._retired = [item for item in self._retired if _alive(item)]
        self._doomed = [item for item in self._doomed if _alive(item)]

    def _emit_finished(self, result: ShotResult) -> None:
        self.finished.emit(result)

    def abort(self) -> None:
        """Stop the current shot: finished(R_CANCELLED)."""
        shot = self._shot
        if shot is not None:
            self._end(shot, snapshots.R_CANCELLED)

    def close_view(self) -> None:
        """The run is over: a running shot is cancelled and the hidden view deleted (no renderer
        process is left). The next shot makes a new view."""
        self.abort()
        view, self._view = self._view, None
        if view is not None:
            try:
                view.hide()
            except Exception:  # noqa: BLE001 - already gone
                pass
            self._doomed.append(view)
        if _event_loop_running():
            for item in self._doomed:
                if item is view:
                    _quietly(item.deleteLater)
        else:
            # No event loop (Jarvis is closing: shutdown() runs after app.exec() returned), so a
            # deleteLater would never run: page, profile and view are deleted now, in that order.
            for item in self._doomed:
                _quietly(lambda item=item: _delete_now(item))
        self._prune()


# --------------------------------------------------------------------------
# The LIVE side
# --------------------------------------------------------------------------

class ShotJob:
    """One page to picture for one step of a research run (``waited``: it already waited
    START_DELAY_MS for the web engine's first start)."""

    __slots__ = ("task_id", "step", "url", "number", "waited")

    def __init__(self, task_id: int, step: Any, url: str, number: int) -> None:
        self.task_id = task_id
        self.step = step
        self.url = url
        self.number = number
        self.waited = False

    def __repr__(self) -> str:   # never the URL
        return f"ShotJob(task={self.task_id}, number={self.number})"


def _shown(step: Any) -> bool:
    """The step is still in LIVE (not Cleared, its task not gone)."""
    try:
        return step.view() is not None
    except Exception:  # noqa: BLE001
        return False


def _camera_started(camera: Any) -> bool:
    try:
        started = getattr(camera, "started", None)
        return bool(started()) if callable(started) else True
    except Exception:  # noqa: BLE001
        return True


class PageSnapshotter(QObject):
    """The page pictures of web research runs (one object per app; lives on the GUI thread).

    request(step, url, task_id=) is called from the research's thread: the step shows "Opening..."
    (PENDING) at once, or "Picture unavailable: <why>" for a page that is not pictured; the shots
    run one at a time on the GUI thread and the picture (or the reason) replaces the PENDING one.
    end_run(task_id) says the run asks for no more; the hidden view is closed when every run that
    asked has ended and nothing is queued (else after IDLE_STOP_S without a request).
    cancel_task(task_id) drops the run's queued pages and stops its running shot; drop_cleared()
    (LIVE's Clear) does the same for the steps that went; shutdown() stops everything for good.
    Never raises, never blocks.
    """

    _wake = Signal()
    _run_ended = Signal(int)
    _cancelled = Signal(int)

    def __init__(self, parent: QObject | None = None, *, html_for: Callable[[str], Any] | None = None,
                 camera_factory: Callable[[QObject], Any] | None = None, max_per_run: int = snapshots.MAX_PER_RUN,
                 keep_text: bool = True) -> None:
        super().__init__(parent)
        self._html_for = html_for
        self._camera_factory = camera_factory
        try:
            cap = int(max_per_run)
        except (TypeError, ValueError):
            cap = snapshots.MAX_PER_RUN
        self._max_per_run = max(0, min(cap, snapshots.MAX_PER_RUN))
        self._keep_text = bool(keep_text)
        self._lock = threading.Lock()
        self._jobs: deque[ShotJob] = deque()
        self._asked: set[int] = set()
        self._budget = snapshots.RunBudget()
        self._counts = {"requested": 0, "taken": 0, "unavailable": 0, "refused": 0}
        self._camera: Any = None
        self._current: ShotJob | None = None
        self._view_open = False
        self._shut = False
        self._start_timer = QTimer(self)
        self._start_timer.setSingleShot(True)
        self._start_timer.timeout.connect(self._pump)
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.setInterval(int(snapshots.IDLE_STOP_S * 1000))
        self._idle_timer.timeout.connect(self._idle_stop)
        self._wake.connect(self._pump, Qt.ConnectionType.QueuedConnection)
        self._run_ended.connect(self._on_run_ended, Qt.ConnectionType.QueuedConnection)
        self._cancelled.connect(self._on_cancel, Qt.ConnectionType.QueuedConnection)

    def __repr__(self) -> str:
        return f"PageSnapshotter(counts={self.counts()})"

    # ---- any thread --------------------------------------------------------------------------

    def _add(self, name: str) -> None:
        with self._lock:
            self._counts[name] += 1

    def _refuse(self, step: Any, url: str, reason: str) -> None:
        self._add("refused")
        step.picture(pictures.page_unavailable(url, reason))
        logger.info("Page snapshot: unavailable (%s)", snapshots.reason_kind(reason))

    def request(self, step: Any, url: str, *, task_id: int) -> None:
        """Picture the page ``url`` a research run (LIVE task ``task_id``) read, or a checked source
        its answer cites, on ``step``.
        Refused (UNAVAILABLE at once, no page opened): after shutdown, [live] text = false, a page
        that is not public https or a search results page, past MAX_PER_RUN, the same page again.
        Never raises, never blocks (any thread)."""
        try:
            text = url if isinstance(url, str) else ""
            if step is None or step.view() is None:
                return   # no LIVE step to show it on: no page is opened for nothing
            self._add("requested")
            with self._lock:
                shut = self._shut
            if shut:
                self._refuse(step, text, snapshots.R_SHUTDOWN)
                return
            if not self._keep_text:
                self._refuse(step, text, snapshots.R_TEXT_OFF)
                return
            reason = snapshots.snapshot_url_problem(text) or self._budget.admit(task_id, text, self._max_per_run)
            if reason:
                self._refuse(step, text, reason)
                return
            step.picture(pictures.page_pending(text))
            with self._lock:
                if self._shut:
                    shut = True
                else:
                    self._jobs.append(ShotJob(task_id, step, text, self._budget.taken(task_id)))
                    self._asked.add(task_id)
            if shut:
                step.picture(pictures.page_unavailable(text, snapshots.R_SHUTDOWN))
                return
            self._wake.emit()
        except Exception as exc:  # noqa: BLE001 - never changes what the research does
            logger.debug("Page snapshots: request failed (%s)", type(exc).__name__)

    def end_run(self, task_id: int) -> None:
        """The run asks for no more pictures (any thread)."""
        try:
            self._budget.end(task_id)
            self._run_ended.emit(int(task_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Page snapshots: end_run failed (%s)", type(exc).__name__)

    def cancel_task(self, task_id: int) -> None:
        """The owner cancelled the run: its queued pages get no picture, its running shot stops."""
        try:
            self._budget.cancel(task_id)
            if QThread.currentThread() is self.thread():
                self._on_cancel(int(task_id))
            else:
                self._cancelled.emit(int(task_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Page snapshots: cancel_task failed (%s)", type(exc).__name__)

    def counts(self) -> dict[str, int]:
        """requested / taken / unavailable / refused (tests, logs)."""
        with self._lock:
            return dict(self._counts)

    # ---- GUI thread --------------------------------------------------------------------------

    def drop_cleared(self) -> None:
        """LIVE was cleared: the queued pages of steps that went are dropped, and a running shot
        whose step went stops (nobody would see its picture; no page is opened for nothing)."""
        try:
            with self._lock:
                kept = deque(job for job in self._jobs if _shown(job.step))
                self._jobs = kept
            current = self._current
            if current is not None and not _shown(current.step) and self._camera is not None:
                self._camera.abort()   # finished(R_CANCELLED) -> _on_shot -> the next job
            elif current is None:
                self._pump()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Page snapshots: drop_cleared failed (%s)", type(exc).__name__)

    def idle(self) -> bool:
        """No job queued, no shot running, no hidden view, no timer waiting."""
        with self._lock:
            queued = bool(self._jobs)
        camera_busy = False
        if self._camera is not None:
            try:
                camera_busy = bool(self._camera.busy())
            except Exception:  # noqa: BLE001
                camera_busy = False
        return (not queued and self._current is None and not self._view_open and not camera_busy
                and not self._start_timer.isActive() and not self._idle_timer.isActive())

    def _ensure_camera(self) -> Any:
        if self._camera is None:
            camera = self._camera_factory(self) if self._camera_factory is not None else PageCamera(self)
            camera.finished.connect(self._on_shot)
            self._camera = camera
        return self._camera

    def _local_page(self, url: str) -> tuple[str | None, str]:
        if self._html_for is None:
            return None, ""
        try:
            page = self._html_for(url)
        except Exception:  # noqa: BLE001
            return None, ""
        if isinstance(page, tuple) and len(page) == 2 and isinstance(page[0], str):
            return page[0], page[1] if isinstance(page[1], str) else ""
        return (page, "") if isinstance(page, str) else (None, "")

    def _next_job(self) -> ShotJob | None:
        while True:
            with self._lock:
                if not self._jobs:
                    return None
                job = self._jobs.popleft()
            if job.step.view() is None:
                continue   # Cleared (or the task is gone): nobody would see it
            if self._budget.cancelled(job.task_id):
                self._unavailable(job, snapshots.R_CANCELLED)
                continue
            return job

    def _unavailable(self, job: ShotJob, reason: str) -> None:
        self._add("unavailable")
        job.step.picture(pictures.page_unavailable(job.url, reason))
        logger.info("Page snapshot: unavailable (%s)", snapshots.reason_kind(reason))

    def _pump(self) -> None:
        try:
            if self._shut or self._current is not None or self._start_timer.isActive():
                return
            job = self._next_job()
            if job is None:
                self._after_queue()
                return
            self._idle_timer.stop()
            try:
                camera = self._ensure_camera()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Page snapshots: no camera (%s)", type(exc).__name__)
                self._unavailable(job, snapshots.R_ENGINE_FAILED)
                self._wake.emit()
                return
            if not job.waited and not _camera_started(camera):
                # Before the engine's first start (a shot that freezes the GUI ~1 s): let "Opening..."
                # be drawn first. Per job, so a first job dropped while it waits takes nothing from the next.
                job.waited = True
                with self._lock:
                    self._jobs.appendleft(job)
                self._start_timer.start(snapshots.START_DELAY_MS)
                return
            self._current = job
            self._view_open = True
            html, final_url = self._local_page(job.url)
            try:
                if html is None:
                    camera.shoot(job.url)
                elif final_url:
                    camera.shoot(job.url, html, final_url=final_url)
                else:
                    camera.shoot(job.url, html)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Page snapshots: shoot failed (%s)", type(exc).__name__)
                if self._current is job:
                    self._on_shot(ShotResult(False, snapshots.R_ENGINE_FAILED))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Page snapshots: pump failed (%s)", type(exc).__name__)

    def _on_shot(self, result: Any) -> None:
        job, self._current = self._current, None
        if job is None or self._shut:
            return
        try:
            if isinstance(result, ShotResult) and result.ok:
                self._add("taken")
                job.step.picture(pictures.page_ready(
                    job.url, image=result.image, image_format=result.image_format, width=result.width,
                    height=result.height, final_url=result.final_url, took_ms=result.took_ms,
                    still_loading=result.still_loading, blocked=result.blocked))
                logger.info("Page snapshot: taken in %.1f s (%d of %d for this research, %d requests blocked)",
                            result.took_ms / 1000, job.number, self._budget.taken(job.task_id), result.blocked)
            else:
                reason = getattr(result, "reason", "") or snapshots.R_ENGINE_FAILED
                self._unavailable(job, reason)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Page snapshots: result not shown (%s)", type(exc).__name__)
        finally:
            if not self._shut:
                self._wake.emit()   # the next job, or close the view when none is left

    def _after_queue(self) -> None:
        """Nothing queued and no shot running: close the view when every run that asked has ended,
        else wait IDLE_STOP_S for another request."""
        if not self._view_open:
            return
        with self._lock:
            asked = set(self._asked)
        if all(self._budget.ended(task_id) for task_id in asked):
            self._close_view()
        elif not self._idle_timer.isActive():
            self._idle_timer.start()

    def _on_run_ended(self, _task_id: int) -> None:
        self._pump()

    def _on_cancel(self, task_id: int) -> None:
        try:
            with self._lock:
                dropped = [job for job in self._jobs if job.task_id == task_id]
                self._jobs = deque(job for job in self._jobs if job.task_id != task_id)
            for job in dropped:
                self._unavailable(job, snapshots.R_CANCELLED)
            current = self._current
            if current is not None and current.task_id == task_id and self._camera is not None:
                self._camera.abort()   # finished(R_CANCELLED) -> _on_shot
            elif current is None:
                self._pump()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Page snapshots: cancel failed (%s)", type(exc).__name__)

    def _idle_stop(self) -> None:
        with self._lock:
            queued = bool(self._jobs)
        if queued:
            self._pump()
        elif self._current is None:
            self._close_view()

    def _close_view(self) -> None:
        self._idle_timer.stop()
        was_open, self._view_open = self._view_open, False
        if self._camera is not None:
            try:
                self._camera.close_view()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Page snapshots: close_view failed (%s)", type(exc).__name__)
        with self._lock:
            done = [task_id for task_id in self._asked if self._budget.ended(task_id)]
            for task_id in done:
                self._asked.discard(task_id)
        for task_id in done:
            if not self._budget.cancelled(task_id):   # a cancelled run's late requests stay refused
                self._budget.forget(task_id)
        if was_open:
            logger.info("Page snapshots: engine stopped")

    def shutdown(self) -> None:
        """Jarvis closes (GUI thread, before live.close()): timers stopped, the running shot
        aborted, the view closed; later requests are refused."""
        try:
            with self._lock:
                if self._shut:
                    return
                self._shut = True
                jobs = list(self._jobs)
                self._jobs.clear()
            self._start_timer.stop()
            self._idle_timer.stop()
            current, self._current = self._current, None
            for job in jobs + ([current] if current is not None else []):
                try:
                    job.step.picture(pictures.page_unavailable(job.url, snapshots.R_SHUTDOWN))
                except Exception:  # noqa: BLE001
                    pass
            was_open, self._view_open = self._view_open, False
            if self._camera is not None:
                self._camera.close_view()
            if was_open:
                logger.info("Page snapshots: engine stopped")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Page snapshots: shutdown failed (%s)", type(exc).__name__)
