"""The Qt half of the command line: activation messages and Qt's own log output.

A later launch finds the single-instance lock taken (see
:mod:`briefing_reader.__main__`) and sends the running app a one-line JSON
message such as ``{"cmd": "activate", "run": null, "now": false, "open": true}``
(``--open``, ``--now``; ``--read`` sends ``"read": true``, a scheduled ``--run PM``
the run alone, ``--ask`` adds ``"ask": true`` and ``--hud`` - like the hotkey agent's HUD key -
``"desktop": true``) over a QLocalServer (a named pipe on Windows);
:class:`ActivationServer` receives it and hands it to
``AppController.handle_activation``.

Kept apart from ``__main__`` so a launch that ends early (``--catch-up`` with
nothing due, ``--hotkey-agent``) never imports PySide6.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, QtMsgType
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from . import APP_NAME

logger = logging.getLogger(__name__)

ACTIVATION_TIMEOUT_S = 5.0
_MAX_MESSAGE_BYTES = 64 * 1024


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------

def activation_message(run: str | None, *, launch: str | None = None, now: bool = False,
                       ask: bool = False, desktop: bool = False) -> dict[str, object]:
    """The one-line message a later launch sends. With ``launch`` ("open", "read", "scheduled"):
    ``{"cmd": "activate", "run": run, "now": false}`` plus ``"open": true`` or ``"read": true``
    (a scheduled launch sends the run alone). Without it, the older form with ``"now"`` as given.
    ``ask`` (``--ask``) adds ``"ask": true`` and ``desktop`` (``--hud``, as the HUD key's) ``"desktop": true``.
    An older app ignores the keys it does not know."""
    message: dict[str, object] = {"cmd": "activate", "run": run, "now": bool(now) if launch is None else False}
    if launch == "open":
        message["open"] = True
    elif launch == "read":
        message["read"] = True
    if ask:
        message["ask"] = True
    if desktop:
        message["desktop"] = True
    return message


def forward_to_running_instance(name: str, run: str | None, now: bool = False, *, launch: str | None = None,
                                ask: bool = False, desktop: bool = False) -> int:
    """Ask the running instance to come forward (retrying while it starts); the exit code.
    The message is :func:`activation_message`'s (``launch`` says how; without it the older
    ``now`` form, which test launchers still send)."""
    app = QCoreApplication.instance() or QCoreApplication([sys.argv[0] if sys.argv else APP_NAME])
    message = activation_message(run, launch=launch, now=now, ask=ask, desktop=desktop)
    payload = (json.dumps(message) + "\n").encode("utf-8")
    deadline = time.monotonic() + ACTIVATION_TIMEOUT_S
    while not try_send(name, payload):
        if time.monotonic() >= deadline:
            logger.warning("another instance is running but did not answer within %.0f s",
                           ACTIVATION_TIMEOUT_S)
            return 1
        time.sleep(0.25)   # the first instance may still be starting
    logger.info("another instance is running; asked it to come forward")
    del app
    return 0


def try_send(name: str, payload: bytes) -> bool:
    """Connect to the running instance and send ``payload``; False if nobody is listening."""
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(500):
        socket.abort()
        return False
    if payload:
        socket.write(payload)
        socket.flush()
        socket.waitForBytesWritten(1000)
    sent = socket.bytesToWrite() == 0
    socket.disconnectFromServer()
    if socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
        socket.waitForDisconnected(1000)
    return sent


# --------------------------------------------------------------------------
# Receiving
# --------------------------------------------------------------------------

class ActivationServer(QObject):
    """Receives one-line JSON "come forward" requests from later launches."""

    def __init__(self, name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._name = name
        self._handler: Callable[[dict], None] | None = None
        self._buffers: dict[int, bytes] = {}
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._on_new_connection)

    def set_handler(self, handler: Callable[[dict], None]) -> None:
        self._handler = handler

    def listen(self) -> bool:
        QLocalServer.removeServer(self._name)
        if self._server.listen(self._name):
            logger.info("Listening for activation requests on %s", self._name)
            return True
        logger.warning("Could not listen for activation requests: %s", self._server.errorString())
        return False

    def close(self) -> None:
        self._server.close()

    def _on_new_connection(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                return
            self._buffers[id(socket)] = b""
            socket.readyRead.connect(lambda s=socket: self._read(s))
            socket.disconnected.connect(lambda s=socket: self._finish(s))
            self._read(socket)

    def _read(self, socket: QLocalSocket) -> None:
        key = id(socket)
        if key not in self._buffers:
            return
        data = self._buffers[key] + socket.readAll().data()
        *lines, rest = data.split(b"\n")
        if len(rest) > _MAX_MESSAGE_BYTES:
            logger.warning("Dropping an oversized activation message")
            rest = b""
        self._buffers[key] = rest
        for line in lines:
            self._dispatch(line)

    def _finish(self, socket: QLocalSocket) -> None:
        self._read(socket)
        rest = self._buffers.pop(id(socket), b"")
        if rest.strip():
            self._dispatch(rest)
        socket.deleteLater()

    def _dispatch(self, line: bytes) -> None:
        if not line.strip():
            return
        message = parse_activation(line)
        if message is None:
            logger.warning("Ignoring a malformed activation message")
        elif self._handler is None:
            logger.info("Activation request arrived before the app was ready; ignored")
        else:
            self._handler(message)


def parse_activation(line: bytes) -> dict | None:
    try:
        message = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(message, dict) or message.get("cmd") != "activate":
        return None
    return message


# --------------------------------------------------------------------------
# Qt's own messages
# --------------------------------------------------------------------------

QT_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}
_FFMPEG_CHATTER_RE = re.compile(r"^\[\w+ @ (?:0x)?[0-9a-fA-F]+\]|ffmpeg", re.IGNORECASE)
_qt_logger = logging.getLogger("qt")
# A web page's console ("js") and the web engine's own messages ("qt.webengine...") may carry a
# page's text or addresses: only the category is logged (page snapshots, snapshot_engine.py).
_PAGE_CATEGORIES = ("js",)
_PAGE_CATEGORY_PREFIX = "qt.webengine"


def forward_qt_message(mode: QtMsgType, context: Any, message: str) -> None:
    """Qt message handler: route Qt's own warnings into the log file (a web page's messages as
    their category only, at DEBUG: the text is never logged)."""
    try:
        category = str(getattr(context, "category", "") or "")
        if category in _PAGE_CATEGORIES or category.startswith(_PAGE_CATEGORY_PREFIX):
            _qt_logger.debug("[%s] a web page message (not logged)", category)
            return
        text = str(message or "")
        if category.startswith("qt.multimedia.ffmpeg") or _FFMPEG_CHATTER_RE.search(text):
            level = logging.DEBUG
        else:
            level = QT_LEVELS.get(mode, logging.WARNING)
        prefix = f"[{category}] " if category and category != "default" else ""
        _qt_logger.log(level, "%s%s", prefix, text)
    except Exception:  # noqa: BLE001 - a message handler must never raise into Qt
        pass
