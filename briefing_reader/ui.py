"""PySide6 user interface: the HUD prompt and reading screens and the app controller.

The module is organised in three parts:

* theme and helpers: the Jarvis HUD theme (widgets and tokens live in
  :mod:`briefing_reader.hud`), the app icon (drawn at runtime) and small Win32
  helpers for focus;
* views: ``PromptView`` ("Hear it now?" beside the status orb),
  ``ReadingView`` (STATUS and the TODAY / DEADLINES agenda on the left; orb,
  current line, Ask Jarvis's command bar when [ask] is on, controls and the
  JARVIS / BRIEFING / LIVE tabs over the centre panel, with a SECTIONS popover,
  in the middle; approvals and activity on the right) and ``BriefingWindow``,
  the frameless HUD window that stacks the two; ``LiveWindow``, the LIVE view
  popped out into a window of its own (not on top, its own taskbar button;
  closing it docks the view back);
* the LIVE view's plumbing: ``_LiveFeed`` drains ``AppController.live`` on the
  GUI thread (the stream's listener only emits a queued signal; at most about
  ten drains a second) and hands what changed to the LIVE tab's ``hud.LiveLog``
  and the pop-out's; ``LiveSummary`` drives the tab's dot, the strip's tag (its
  tooltip) and the pop-out's toolbar. ``[live] auto_open`` brings up LIVE (or
  the pop-out: also with "tab" when the LIVE tab would be too short to show a
  step and there is another screen) once per Ask or approved card; the view
  only shows, it never acts;
* ``AppController``: the state machine ("prompt", "snoozed", "reading",
  "quitting") that owns the window, tray icon, fetch thread, TTS worker,
  player and the action worker (approved proposals, the agenda, the cards'
  event checks and the Google sign-in of each account), and with [ask] on the
  Ask controller (:mod:`briefing_reader.ask_ui`, its own "ask" thread) whose
  proposals join the cards under ASK. A web research's answer (a "web:"
  request, or one the planner hands over) is one JARVIS entry with its
  numbered sources, spoken without the [n] marks; each source is an ASK card
  labelled with its site whose "Open page" opens it in the browser only on that
  click, after the web rule (any public https site, never a local or private
  address) is checked again. ``AppController.live`` is the LIVE
  view's event stream (:mod:`briefing_reader.live`, ``[live]``): every Ask, every
  approved card (from the click through the undo countdown to Google's answer)
  and every briefing fetch, as it happens; on screen only.

Threading: Qt objects are only touched on the GUI thread. The fetch thread,
the TTS worker, the action worker and the ask thread only emit bridge signals,
which are queued to the GUI thread where all state lives. Every worker is a daemon
thread, so a stuck network call or a browser sign-in can never block exit
(only a change already on its way to Google is waited for, at most 5 s). The
action worker runs one call at a time, so reading the agenda never overlaps a
sign-in; while an approval, its undo countdown or a sign-in runs, the other
cards' Deny / Approve are locked.
"""

from __future__ import annotations

import ctypes
import dataclasses
import hashlib
import logging
import math
import os
import queue
import random
import re
import shutil
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import shiboken6
from PySide6.QtCore import (
    QCoreApplication,
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
    QTextBlock,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QSizePolicy,
    QSystemTrayIcon,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import hud
from . import live as _live
from . import pictures as _pictures
from .actions import (
    CALENDAR,
    CANCEL,
    DONE_TEXT,
    EMAIL,
    MAX_RECIPIENTS,
    MOVE,
    REPLY,
    RETRY_TEXT,
    RSVP,
    SEND_TEXT,
    SOURCE_ASK,
    SOURCE_BRIEFING,
    STATUS_CREATED,
    STATUS_DENIED,
    STATUS_DONE,
    STATUS_EXISTS,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SENT,
    STATUS_UNKNOWN,
    TODO,
    ActionStore,
    CardView,
    EditInvalid,
    ProposedAction,
    body_links,
    body_needs_review,
    card_view,
    copied_text,
    copy_text,
    due_words,
    email_address,
    extract_actions,
    kind_label,
    link_allowed,
    link_host,
    parse_time_range,
    result_text,
    web_link_allowed,
    when_text,
    with_error,
)
from .agenda import (
    Deadline,
    agenda_window,
    deadlines_from_events,
    due_label,
    event_rows,
    events_in_window,
    extract_deadlines,
    merge_deadlines,
)
from .ask.validate import ALREADY_LISTED
from .config import DEFAULT_ACCOUNT, AgendaConfig, Config
from .executor import (
    DISCONNECT_FAILED_MESSAGE,
    STAGE_SIGNED_IN,
    STAGE_SIGNIN,
    STAGE_WORKING,
    ActionEdit,
    EditError,
    EventCheck,
    ExecError,
    ExecResult,
    Executor,
    MailStatus,
    account_of,
    apply_edit,
    build_accounts,
    build_calendars,
    build_executor,
    build_senders,
    check_event,
    check_failure,
    hand_off_tools,
    mail_note,
    recipient_history,
    run_action,
    sign_in_note,
)
from .gcal import (
    CalendarAuthError,
    CalendarError,
    CalendarEvent,
    CalendarNotSignedIn,
    CalendarSetupError,
    GoogleCalendar,
)
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
    logged_alias,
)
from .models import (
    ITEM_ENTRY,
    ITEM_HEADING,
    ITEM_INTRO,
    ITEM_NOTE,
    ITEM_OUTRO,
    ITEM_SUBHEADING,
    LAUNCH_KINDS,
    LAUNCH_OPEN,
    LAUNCH_READ,
    LAUNCH_SCHEDULED,
    Briefing,
    BriefingHeader,
    Freshness,
    Script,
    ScriptItem,
    Section,
    SectionAudio,
)
from .notion_client import (
    NotionClient,
    NotionError,
    PollResult,
    check_freshness,
    fetch_briefing,
    poll_for_briefing,
)
from .persona import (
    OUTCOME_ERROR,
    OUTCOME_GOOD,
    OUTCOME_WARN,
    Greeting,
    announcement,
    approvals_sentence,
    ask_failure_speech,
    ask_reply_speech,
    compose_opening,
    next_event_sentence,
    not_sent_text,
    outcome_text,
    outcome_tone,
    pick_ack,
    pick_greeting,
    research_reply_speech,
    salutation,
    spoken_when,
    with_address,
)
from .player import FINISHED, IDLE, PAUSED, PLAYING, WAITING, BriefingPlayer
from .prefs import PREFS_FILE, AssistantPrefs
from .recipients import NEW as RECIPIENT_NEW_KIND
from .recipients import OWN as RECIPIENT_OWN_KIND
from .recipients import classify
from .runstate import ANSWER_MAX_AGE, RunState, briefing_key, handled_slot_key, slot_for
from .speech import KIND_ACK, KIND_ANNOUNCE, KIND_GREETING, KIND_REPLY, KIND_RESULT, SayWorker, SilentVoice, Voice
from .text_prep import ACTIONS_KEY, build_script, describe_updated, format_time, updated_label
from .tts import SAPI, SpeechSynthesizer, TtsWorker

logger = logging.getLogger(__name__)

DOT = hud.MIDDLE_DOT
_QWIDGETSIZE_MAX = 16777215

# --------------------------------------------------------------------------
# Theme
# --------------------------------------------------------------------------


def apply_dark_theme(app: QApplication) -> None:
    """The HUD theme: Fusion, the dark HUD palette, Sora 13 px and the HUD stylesheet.

    Call ``hud.load_fonts`` first so the bundled typefaces are used.
    """
    hud.apply_theme(app)


def app_icon() -> QIcon:
    """The window and tray icon: the HUD logo (a cyan diamond) on the dark ground."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillPath(hud.chamfer_path(QRectF(2, 2, 60, 60), 12), QColor(hud.GROUND))
        painter.setPen(QPen(hud.rgba(hud.ACCENT, 0.45), 1.5))
        painter.drawPath(hud.chamfer_path(QRectF(2.75, 2.75, 58.5, 58.5), 11.5))
        painter.translate(32, 32)
        painter.rotate(45)
        painter.setPen(QPen(QColor(hud.ACCENT), 5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(-12, -12, 24, 24))
    finally:
        painter.end()
    return QIcon(pixmap)


# --------------------------------------------------------------------------
# Windows helpers (no-ops elsewhere and under the offscreen test platform)
# --------------------------------------------------------------------------

_VK_MENU = 0x12
_KEYEVENTF_KEYUP = 0x0002
_SW_SHOWNOACTIVATE = 4
_user32_dll: Any = None


def _native_windows() -> bool:
    """True only for real Win32 windows (never for the offscreen platform used in tests)."""
    return sys.platform == "win32" and QGuiApplication.platformName() == "windows"


def _user32() -> Any:
    global _user32_dll
    if _user32_dll is None:
        from ctypes import wintypes
        dll = ctypes.WinDLL("user32", use_last_error=True)
        dll.GetForegroundWindow.restype = wintypes.HWND
        dll.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
        dll.GetWindowThreadProcessId.restype = wintypes.DWORD
        dll.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        dll.BringWindowToTop.argtypes = [wintypes.HWND]
        dll.SetForegroundWindow.argtypes = [wintypes.HWND]
        dll.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
        dll.keybd_event.restype = None
        dll.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        dll.ShowWindow.restype = wintypes.BOOL
        dll.IsIconic.argtypes = [wintypes.HWND]
        dll.IsIconic.restype = wintypes.BOOL
        _user32_dll = dll
    return _user32_dll


def force_foreground(widget: QWidget) -> None:
    """Show, raise and focus ``widget``, even when launched in the background by Task Scheduler."""
    if widget.isMinimized():
        widget.showNormal()
    else:
        widget.show()
    widget.raise_()
    widget.activateWindow()
    if not _native_windows():
        return
    try:
        _win32_bring_to_front(int(widget.winId()))
    except Exception as exc:  # noqa: BLE001 - best effort
        logger.debug("Could not bring the window to the foreground: %s", exc)


def _win32_bring_to_front(hwnd: int) -> None:
    user32 = _user32()
    if int(user32.GetForegroundWindow() or 0) == hwnd:
        return
    if _set_foreground_attached(user32, hwnd):
        return
    # Last resort: a synthetic Alt press counts as recent input, which lifts
    # Windows' focus-stealing lock for this process.
    user32.keybd_event(_VK_MENU, 0, 0, 0)
    try:
        user32.SetForegroundWindow(hwnd)
    finally:
        user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)
    logger.debug("Foreground via the Alt key: %s", int(user32.GetForegroundWindow() or 0) == hwnd)


def _set_foreground_attached(user32: Any, hwnd: int) -> bool:
    """SetForegroundWindow while attached to the foreground thread's input queue."""
    foreground = user32.GetForegroundWindow()
    ours = ctypes.WinDLL("kernel32").GetCurrentThreadId()
    theirs = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    attached = bool(theirs and theirs != ours and user32.AttachThreadInput(theirs, ours, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(theirs, ours, False)
    return int(user32.GetForegroundWindow() or 0) == hwnd


def show_without_activating(widget: QWidget) -> None:
    """Show ``widget`` on top without taking keyboard focus from the current app.

    Once the native window exists, Qt's Windows plugin reads the QWindow
    property ``_q_showWithoutActivating`` (during show()), not the widget
    attribute; the attribute covers a first show before the window exists.
    A minimized window is restored where it was, also without the focus.
    """
    if widget.isVisible() and widget.isMinimized():
        _restore_without_activating(widget)
        return
    handle = widget.windowHandle()
    widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    if handle is not None:
        handle.setProperty("_q_showWithoutActivating", True)
    try:
        if widget.isMinimized():
            widget.showNormal()
        else:
            widget.show()
    finally:
        widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        if handle is not None:
            handle.setProperty("_q_showWithoutActivating", False)


_DESKTOP_SWITCHDESKTOP = 0x0100


def session_locked() -> bool:
    """True while the Windows session is locked (the lock screen owns the input desktop).

    OpenInputDesktop fails, or SwitchDesktop to it fails, while the session is
    locked (UAC's secure desktop reads as locked too, for a few seconds). Always
    False elsewhere, under the offscreen test platform and when the check fails.
    """
    if not _native_windows():
        return False
    try:
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.SwitchDesktop.argtypes = [wintypes.HANDLE]
        user32.SwitchDesktop.restype = wintypes.BOOL
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        desktop = user32.OpenInputDesktop(0, False, _DESKTOP_SWITCHDESKTOP)
        if not desktop:
            return True
        try:
            return not user32.SwitchDesktop(desktop)
        finally:
            user32.CloseDesktop(desktop)
    except Exception as exc:  # noqa: BLE001 - an unknown answer never holds anything back
        logger.debug("Could not check whether the session is locked: %s", exc)
        return False


def _restore_without_activating(widget: QWidget) -> None:
    """Un-minimize ``widget`` without taking the focus.

    Qt restores a minimized window with SW_SHOWNORMAL, which activates it; on
    Windows this asks for SW_SHOWNOACTIVATE instead (Qt follows the new state
    from the window's WM_SIZE). Elsewhere, or if that did not work, showNormal().
    """
    if _native_windows():
        try:
            user32 = _user32()
            hwnd = int(widget.winId())
            user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
            if not user32.IsIconic(hwnd):
                return
        except Exception as exc:  # noqa: BLE001 - best effort; showNormal() below still restores
            logger.debug("Could not restore the window without activating it: %s", exc)
    widget.showNormal()


# --------------------------------------------------------------------------
# Small widget helpers
# --------------------------------------------------------------------------

def _keeps_space(widget: QWidget | None) -> bool:
    """Space belongs to this focus widget, not to the reading view's play / pause shortcut: a
    button (pressed, then clicked on release) or a text field such as Ask Jarvis's bar (it types a
    space). The read-only transcript is neither, so Space there still plays and pauses."""
    if isinstance(widget, (QAbstractButton, QLineEdit, QPlainTextEdit)):
        return True
    return isinstance(widget, QTextEdit) and not widget.isReadOnly()


def _label(font: QFont, color: str, *, wrap: bool = False, name: str = "") -> QLabel:
    """A plain-text label (page text is never interpreted as HTML)."""
    return hud.make_label("", font, color, wrap=wrap, name=name)


def _caps(font: QFont) -> QFont:
    """``font`` drawn in capitals; the label's text() keeps the original case."""
    font.setCapitalization(QFont.Capitalization.AllUppercase)
    return font


def _caps_meta(panel: hud.ChamferPanel) -> None:
    """Draw the panel's right-hand meta in capitals ("8 SECTIONS"); its text() keeps the case."""
    panel.meta_label.setFont(_caps(panel.meta_label.font()))


def _set_status_style(label: QLabel, is_error: bool, normal: str) -> None:
    """Amber for problems; the "error" property stays readable for anyone inspecting the label."""
    hud.set_label_color(label, hud.AMBER if is_error else normal)
    label.setProperty("error", is_error)


class _ElidedLabel(QLabel):
    """One line of plain text, elided at its width (the whole text is the tooltip then); it never
    asks for more width than it gets, so a toolbar never pushes its window wider."""

    def __init__(self, font: QFont, color: str) -> None:
        super().__init__()
        self._full = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setFont(font)
        hud.set_label_color(self, color)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def set_full_text(self, text: str) -> None:
        self._full = text or ""
        self.setAccessibleName(self._full)
        self._render()

    def full_text(self) -> str:
        return self._full

    def _render(self) -> None:
        shown = QFontMetricsF(self.font()).elidedText(self._full, Qt.TextElideMode.ElideRight,
                                                     max(0, self.contentsRect().width() - 1))
        self.setToolTip(hud.plain_tooltip(self._full) if shown != self._full else "")
        super().setText(shown)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self._render()


class _LivePage(QWidget):
    """The LIVE tab's page: it never needs more height than the JARVIS page (``like``), so the tab
    row above the panel stays where it is when the tab changes."""

    def __init__(self, like: QWidget) -> None:
        super().__init__()
        self._like = like

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(0, self._like.minimumSizeHint().height())


def _screen_area(point: QPoint) -> QRect:
    screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
    return screen.availableGeometry()


def _short(text: str, limit: int = 90) -> str:
    """One line of at most ``limit`` characters (activity sub-lines)."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit - 3].rstrip() + "..."


def _section_title(section: Section) -> str:
    if section.title:
        return section.title
    return {"intro": "Intro", "outro": "Outro"}.get(section.key, "Section")


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _needs_ok(count: int) -> str:
    """"1 needs your OK" / "2 need your OK" (prompt chip and calendar chip tooltip)."""
    return f"{count} needs your OK" if count == 1 else f"{count} need your OK"


# --------------------------------------------------------------------------
# Views: prompt
# --------------------------------------------------------------------------

_PROMPT_ORB_PX = 112
_DETAIL_CAPS_MAX = 48     # longer details (errors, setup hints) read better as prose than mono caps


class PromptView(QWidget):
    """The "Your AM briefing is ready. Hear it now?" screen: status orb, question and answers."""

    readNow = Signal()
    later = Signal(int)
    dismiss = Signal()

    def __init__(self, short_minutes: int, long_minutes: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.orb = hud.Orb(_PROMPT_ORB_PX, hud.ORB_WAITING)
        self.state_label = hud.StateLabel(hud.ORB_WAITING)
        self.headline = hud.SpeechLabel()
        self.headline.setObjectName("headline")
        self.detail = _label(_caps(hud.mono_font(11, 400, 0.08)), hud.TEXT_MUTED, wrap=True, name="detail")
        self._detail_prose = False
        self.status = _label(hud.mono_font(11, 400, 0.02), hud.TEXT_SOFT, wrap=True, name="status")
        self.pending_chip = hud.HudChip("")
        self.pending_chip.hide()
        self.countdown = _label(hud.mono_font(11, 400, 0.04), hud.TEXT_DIM, name="countdown")
        self.read_button = hud.HudButton("Read now", hud.PRIMARY)
        self.short_button = hud.HudButton(f"Later ({short_minutes} min)")
        self.long_button = hud.HudButton(f"Later ({long_minutes} min)")
        self.dismiss_button = hud.HudButton("Dismiss", hud.LINK)
        self.read_button.clicked.connect(lambda: self.readNow.emit())
        self.short_button.clicked.connect(lambda: self.later.emit(short_minutes))
        self.long_button.clicked.connect(lambda: self.later.emit(long_minutes))
        self.dismiss_button.clicked.connect(lambda: self.dismiss.emit())
        self._build_layout()

    def _build_layout(self) -> None:
        texts = QVBoxLayout()
        texts.setSpacing(6)
        for widget in (self.state_label, self.headline, self.detail, self.status):
            texts.addWidget(widget)
        chip_row = QHBoxLayout()
        chip_row.setContentsMargins(0, 2, 0, 0)
        chip_row.addWidget(self.pending_chip)
        chip_row.addStretch(1)
        texts.addLayout(chip_row)
        texts.addStretch(1)
        body = QHBoxLayout()
        body.setContentsMargins(14, 14, 14, 0)
        body.setSpacing(18)
        body.addWidget(self.orb, 0, Qt.AlignmentFlag.AlignTop)
        body.addLayout(texts, 1)
        # Room around the buttons: the Read now glow is drawn outside the button.
        buttons = QHBoxLayout()
        buttons.setContentsMargins(14, 0, 14, 0)
        buttons.setSpacing(6)
        for button in (self.read_button, self.short_button, self.long_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        footer = QHBoxLayout()
        footer.setContentsMargins(12, 0, 14, 0)
        footer.addWidget(self.dismiss_button)
        footer.addStretch(1)
        footer.addWidget(self.countdown)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(body, 1)
        layout.addSpacing(14)
        layout.addLayout(buttons)
        layout.addSpacing(8)
        layout.addLayout(footer)

    def set_content(self, headline: str, detail: str, status: str, status_is_error: bool,
                    can_read: bool) -> None:
        self.headline.setText(headline)
        self._set_detail(detail)
        self.status.setText(status)
        self.status.setVisible(bool(status))
        _set_status_style(self.status, status_is_error, hud.TEXT_SOFT)
        for button in (self.read_button, self.short_button, self.long_button):
            button.setEnabled(can_read)

    def _set_detail(self, text: str) -> None:
        prose = len(text) > _DETAIL_CAPS_MAX or "\n" in text
        if prose != self._detail_prose:
            self._detail_prose = prose
            self.detail.setFont(hud.body_font(13) if prose else _caps(hud.mono_font(11, 400, 0.08)))
            hud.set_label_color(self.detail, hud.TEXT_SOFT if prose else hud.TEXT_MUTED)
        self.detail.setText(text)
        self.detail.setVisible(bool(text))

    def set_countdown(self, text: str) -> None:
        self.countdown.setText(text)
        self.countdown.setVisible(bool(text))

    def set_state(self, state: str) -> None:
        """Orb and state label: waiting on the user, working (first fetch) or standby (problem)."""
        self.orb.set_state(state)
        if state != self.state_label.state():
            self.state_label.set_state(state)

    def set_pending(self, count: int) -> None:
        """The amber "2 NEED YOUR OK" chip (hidden when nothing waits for a decision)."""
        if count > 0:
            self.pending_chip.set_text(_needs_ok(count))
        self.pending_chip.setVisible(count > 0)


# --------------------------------------------------------------------------
# Views: reading
# --------------------------------------------------------------------------

_PROPORTIONAL = QTextBlockFormat.LineHeightTypes.ProportionalHeight.value
_FIXED_HEIGHT = QTextBlockFormat.LineHeightTypes.FixedHeight.value
_PAUSE_GAP_PX = 8.0
_HEADING_TOP_PX = 18.0
_ROW_GAP_PX = 4.0
_ROW_PAD_PX = 3.0             # highlight band above and below the item's text
_INDENT_PX = 18
_DOC_MARGIN_PX = 14
_SCROLL_ANCHOR = 0.35          # highlighted block sits this far down the panel
_MANUAL_SCROLL_HOLD_S = 4.0
_BULLET = "\u2022 "   # typographic bullet before list entries
_LIST_PREFIX_RE = re.compile(r"^(?:\d+\.\s|\[[ xX]\]\s)")
_APPROVALS_MAX_PX = 340        # the cards scroll beyond this, leaving room for the activity log
_SECTIONS_POPOVER_WIDTH = 300  # the SECTIONS list (the artboard's left-column width)
_SECTIONS_POPOVER_MIN_PX = 220  # it may reach past the window bottom rather than get smaller
_ITEM_COLORS = {ITEM_NOTE: hud.AMBER, ITEM_INTRO: hud.TEXT_SOFT, ITEM_OUTRO: hud.TEXT_SOFT,
                ITEM_SUBHEADING: hud.TEXT_BRIGHT}
_NOT_READ_SUFFIX = "  (not read aloud)"


def _line_percent(font: QFont, css_line_height: float) -> int:
    """Qt's proportional line height for a CSS ``line-height`` (Qt's is relative to the font's own height)."""
    natural = QFontMetricsF(font).height()
    return round(100 * css_line_height * font.pixelSize() / natural) if natural > 0 else 100


class _TranscriptStyle:
    """Fonts and line heights of the transcript (built once the fonts are loaded)."""

    def __init__(self) -> None:
        self.body = hud.body_font(14)
        self.heading = _caps(hud.mono_font(11, 500, 0.16))
        self.subheading = hud.body_font(14, 600)
        self.body_metrics = QFontMetricsF(self.body)
        self.body_line = _line_percent(self.body, 1.6)
        self.heading_line = _line_percent(self.heading, 1.5)


class _Transcript(QTextBrowser):
    """The reading panel's text on a transparent ground.

    The current item is drawn like the artboard's agenda rows (a glowing 3 px
    accent bar on the left and a left-to-right cyan gradient) under the text,
    plus an ExtraSelection that brightens the item's own text.
    """

    def __init__(self, style: _TranscriptStyle) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.viewport().setAutoFillBackground(False)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setUndoRedoEnabled(False)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.document().setDefaultFont(style.body)
        self.document().setDocumentMargin(_DOC_MARGIN_PX)
        self.document().setIndentWidth(_INDENT_PX)
        self._row = -1

    def set_row(self, block_number: int) -> None:
        """Draw the agenda-row highlight behind this block (-1: none)."""
        if block_number != self._row:
            self._row = block_number
            self.viewport().update()

    def row(self) -> int:
        return self._row

    def row_rect(self) -> QRectF | None:
        """The highlighted row in viewport coordinates, or None."""
        if self._row < 0:
            return None
        block = self.document().findBlockByNumber(self._row)
        layout = block.layout() if block.isValid() and block.text() else None
        if layout is None or layout.lineCount() == 0:
            return None
        first, last = layout.lineAt(0), layout.lineAt(layout.lineCount() - 1)
        top = layout.position().y() + first.y()
        bottom = layout.position().y() + last.y() + last.height()
        offset = self.verticalScrollBar().value()
        return QRectF(2, top - offset - _ROW_PAD_PX, self.viewport().width() - 4,
                      bottom - top + 2 * _ROW_PAD_PX)

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        rect = self.row_rect()
        if rect is not None:
            painter = QPainter(self.viewport())
            try:
                hud.paint_row_highlight(painter, rect, hud.ACCENT, dpr=self.devicePixelRatioF())
            finally:
                painter.end()
        super().paintEvent(event)


class _DashedRule(QWidget):
    """A 1 px dashed line (the artboard's ``border-bottom: 1px dashed``)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(1)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            pen = QPen(hud.rgba(hud.ACCENT, 0.2), 1)
            pen.setDashPattern([4.0, 3.0])
            painter.setPen(pen)
            painter.drawLine(0, 0, self.width(), 0)
        finally:
            painter.end()


@dataclasses.dataclass(frozen=True)
class _Tier:
    """Column widths and orb size for reading views at least ``min_width`` wide."""

    min_width: int
    left: int
    right: int
    orb: int


# Wide (artboard), default 1120 px window, minimum 900 px window, and screens too small for that.
_TIERS = (_Tier(1320, 300, 372, 168), _Tier(1040, 260, 340, 168), _Tier(860, 230, 300, 112),
          _Tier(0, 190, 250, 96))
_COMPACT_BAR_BELOW = 570   # a reading view shorter than this (a window under ~640 px) gets the compact bar
# A reading view shorter than this (a window under ~580 px; with Ask's command bar, add its height)
# gets a smaller orb, a 2-line spoken line and a tighter panel strip: room for the JARVIS / BRIEFING tabs.
_SHORT_VIEW_BELOW = 520
_SHORT_ORB_PX = 80
_SHORT_STRIP_PAD = 6
_SPEECH_LINES = 3          # the spoken line under the orb is cut to this many lines
_SHORT_SPEECH_LINES = 2
STRIP_CAPTION = f"Conversation {DOT} this session"   # the centre panel's strip on the JARVIS tab
STRIP_CAPTION_SHORT = "Conversation"                 # ... when the whole caption does not fit
LIVE_CAPTION = f"Live steps {DOT} this session"      # ... and on the LIVE tab
LIVE_CAPTION_SHORT = "Live steps"
LIVE_AWAY_TEXT = "LIVE is in its own window."        # the LIVE page while the pop-out shows the steps
POP_OUT_TEXT = "Pop out"
POP_OUT_TIP = "Show LIVE in its own window - put it on a second screen"
CLEAR_TEXT = "Clear"
CLEAR_TIP = "Clear what is shown here (nothing else changes; nothing was saved)"
DOCK_TEXT = "Dock"
DOCK_TIP = "Put LIVE back in the main window"
SHOW_WINDOW_TEXT = "Show window"
BRING_BACK_TEXT = "Bring it back here"
SEE_STEPS_TEXT = "See every step"                    # the link under an Ask's answer in the conversation


class ReadingView(QWidget):
    """The assistant screen (the reading screen of older versions).

    Left: compact STATUS telemetry, then one panel with the TODAY / TOMORROW
    agenda and the DEADLINES list (its Connect link emits
    ``connectCalendar``). Middle: the orb with its state, the line being
    spoken, the command bar, the controls, the JARVIS / BRIEFING tabs and the
    centre panel. JARVIS shows the conversation (``conversation``, a
    hud.ConversationLog); BRIEFING the transcript (with a moving highlight) and,
    in the panel's header strip, the step chips and the SECTIONS button that
    opens the section list (click a row to jump); LIVE (``set_live_available``)
    every step Jarvis takes (``live_log``, a hud.LiveLog fed by the app's
    _LiveFeed), or "LIVE is in its own window." while the pop-out shows it
    (``set_live_popped``); its strip shows ``live_status`` (hud.LiveStatus, the
    summary as its tooltip) where the briefing's LIVE marker sits on the other
    tabs, then Pop out and Clear (Clear only where it fits; no toolbar row, so
    the log has that height). ``tabChanged(index)`` when the current tab changes (a click,
    Ctrl+1 / Ctrl+2 / Ctrl+3, ``set_tab``). Right: the NEEDS YOUR OK cards and
    the ACTIVITY log.
    """

    playPause = Signal()
    skip = Signal()
    readEverything = Signal()
    openNotion = Signal()
    done = Signal()
    sectionClicked = Signal(int)
    approveClicked = Signal(str)
    denyClicked = Signal(str)
    lockedClicked = Signal(str)
    openLink = Signal(str)
    openSource = Signal(str)     # a card's Open (tools row): the action id, never the URL
    copyText = Signal(str)       # a card's Copy (tools row): the action id
    undoAction = Signal(str)     # a card's Undo during its countdown: the action id
    editAction = Signal(str)     # a card's Edit (tools row): the action id
    signInAccount = Signal(str)  # a card's Sign in (tools row): the action id
    connectCalendar = Signal()
    tabChanged = Signal(int)     # hud.TAB_JARVIS / hud.TAB_BRIEFING / hud.TAB_LIVE became current
    livePopOut = Signal()        # the LIVE page's Pop out
    liveDock = Signal()          # "Bring it back here" (the pop-out docks)
    liveClear = Signal()         # the LIVE page's Clear
    liveShowWindow = Signal()    # "Show window" (raise the pop-out)
    liveLink = Signal(str)       # a result link or an internal link id in the LIVE log

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._style = _TranscriptStyle()
        self._block_numbers: dict[tuple[int, int], int] = {}
        self._current = (-1, -1)
        self._user_scrolled_at = -math.inf
        self._speech_fallback = ""
        self._speech_text = ""                # what set_speech was given ("" shows the fallback)
        self._new_label = ""                  # "Your AM briefing is ready to view" while NEW
        self._rows: tuple[hud.SectionRowInfo, ...] = ()
        self._steps: list[tuple[str, str]] = []
        self._shown_section: int | None = None
        self._tier: _Tier | None = None
        self._short = False                   # a very short view (_fit_short)
        self._doc_folded = False              # ... on the narrowest tier: no title line on BRIEFING
        self._state_text: str | None = None   # the state label's own text (PLANNING), None for the state's
        self._marker_live = True              # the briefing's LIVE marker should show (as built; not on LIVE)
        self._make_status_column()
        self._make_center_column()
        self._make_sections_popover()
        self._make_side_column()
        self._scroll_animation = QPropertyAnimation(self.text.verticalScrollBar(), b"value", self)
        self._scroll_animation.setDuration(220)
        self._scroll_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.text.verticalScrollBar().actionTriggered.connect(self._on_user_scroll)
        # Space plays/pauses unless a button has focus (BriefingWindow.event lets the button have it).
        shortcut = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        shortcut.setAutoRepeat(False)   # holding Space must not toggle over and over
        shortcut.activated.connect(lambda: self.playPause.emit())
        for keys, index in (("Ctrl+1", hud.TAB_JARVIS), ("Ctrl+2", hud.TAB_BRIEFING), ("Ctrl+3", hud.TAB_LIVE)):
            tab_shortcut = QShortcut(QKeySequence(keys), self)
            tab_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            tab_shortcut.activated.connect(lambda index=index: self.set_tab(index))
        self._build_layout()
        self._show_tab_page(self.tabs.current())

    # ---- construction ----------------------------------------------------

    def _make_status_column(self) -> None:
        # Compact (tighter than the artboard), so the agenda below gets the room.
        self.status_panel = hud.ChamferPanel("Status", padding=(16, 12, 16, 12), spacing=8)
        self.progress_bar = hud.TelemetryBar("Progress", "0 / 0", 0.0, hud.ACCENT)
        self.voice_bar = hud.TelemetryBar("Voice", "--", 0.0, hud.TEXT_DIM)
        self.updated_bar = hud.TelemetryBar("Updated", "--", 0.0, hud.TEXT_DIM)
        self.approvals_bar = hud.TelemetryBar("Needs your OK", "0", 0.0, hud.AMBER)
        self.status = _label(hud.mono_font(11, 400, 0.02), hud.TEXT_SOFT, wrap=True, name="status")
        for widget in (self.progress_bar, self.voice_bar, self.updated_bar, self.approvals_bar,
                       self.status):
            self.status_panel.body_layout.addWidget(widget)
        self.agenda_panel = hud.AgendaPanel("Today")
        self.agenda_panel.linkClicked.connect(self.connectCalendar)

    def _make_sections_popover(self) -> None:
        """SECTIONS: a button in the transcript strip and the list it opens (rows keep their states)."""
        self.sections_button = hud.ChipButton("Sections")
        self.sections_button.setToolTip("The briefing's sections: click one to play from there")
        self.sections_button.setEnabled(False)
        self.sections_button.toggled.connect(self._on_sections_toggled)
        self.sections_popover = hud.Popover("Sections", "", parent=self)
        self.sections_popover.closed.connect(self._on_sections_closed)
        self.sections_panel = self.sections_popover.panel
        _caps_meta(self.sections_panel)
        self.sections = hud.SectionList()
        self.sections.rowClicked.connect(self._on_section_row)
        self.sections_panel.body_layout.addWidget(self.sections, 1)

    def _make_center_column(self) -> None:
        self.orb = hud.Orb(168, hud.ORB_WORKING)
        self.state_label = hud.StateLabel(hud.ORB_WORKING)
        self.speech = hud.SpeechLabel(max_lines=_SPEECH_LINES)
        self.speech.setObjectName("speech")
        self._make_buttons()
        self.title = _label(_caps(hud.display_font(15, 600, 0.12)), hud.TEXT_BRIGHT, name="title")
        self._title_dot = _label(hud.mono_font(11), hud.TEXT_DIM)
        self._title_dot.setText(DOT)
        self.subtitle = _label(_caps(hud.mono_font(11, 400, 0.08)), hud.TEXT_MUTED, wrap=True,
                               name="subtitle")
        self.stale = _label(hud.mono_font(11, 400, 0.04), hud.AMBER, wrap=True, name="stale")
        self.stale.hide()
        self.text = _Transcript(self._style)
        self.transcript_panel = hud.MainPanel()
        self.transcript_panel.steps.installEventFilter(self)   # re-fit the chips when it resizes
        self.transcript_panel.strip.installEventFilter(self)   # and the JARVIS caption
        self._step_meter = hud.StepStrip(self)                # measures chips, never shown
        self._step_meter.setVisible(False)
        # Ask Jarvis's command bar (between the orb row and the controls); shown only while
        # [ask] is enabled (set_ask_available).
        self.command_bar = hud.CommandBar()
        self._command_holder = QWidget()
        holder = QVBoxLayout(self._command_holder)
        holder.setContentsMargins(14, 10, 14, 0)
        holder.setSpacing(0)
        holder.addWidget(self.command_bar)
        self._command_holder.setVisible(False)
        # The JARVIS / BRIEFING tabs above the centre panel, the conversation (JARVIS) and the
        # strip's caption on the JARVIS page (where the step chips and SECTIONS are hidden).
        self.tabs = hud.TabStrip(live=True)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.conversation = hud.ConversationLog()
        self.strip_caption = _label(_caps(hud.mono_font(11, 400, 0.12)), hud.TEXT_DIM, name="stripCaption")
        self.strip_caption.setText(STRIP_CAPTION)
        self.live_status = hud.LiveStatus()
        self.live_status.hide()
        self._make_live_page()

    def _make_live_page(self) -> None:
        """The LIVE tab's page: the log, or one line and two links while the pop-out window shows
        the steps. Pop out and Clear sit in the panel's strip on the LIVE tab (no toolbar row of
        their own: the log gets that height)."""
        self._live_summary = ""
        self.live_pop_button = hud.HudButton(POP_OUT_TEXT, hud.LINK)
        self.live_pop_button.setToolTip(POP_OUT_TIP)
        self.live_pop_button.clicked.connect(lambda: self.livePopOut.emit())
        self.live_clear_button = hud.HudButton(CLEAR_TEXT, hud.LINK)
        self.live_clear_button.setToolTip(CLEAR_TIP)
        self.live_clear_button.clicked.connect(lambda: self.liveClear.emit())
        # Both in one box for the strip (shown on the LIVE tab only).
        self._live_links = QWidget()
        links_row = QHBoxLayout(self._live_links)
        links_row.setContentsMargins(0, 0, 0, 0)
        links_row.setSpacing(_LIVE_LINK_GAP)
        links_row.addWidget(self.live_pop_button, 0, Qt.AlignmentFlag.AlignVCenter)
        links_row.addWidget(self.live_clear_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self._live_links.hide()
        self.live_log = hud.LiveLog()
        self.live_log.linkClicked.connect(self.liveLink)
        self.live_away = QWidget()
        away = QVBoxLayout(self.live_away)
        away.setContentsMargins(14, 14, 14, 14)
        away.setSpacing(8)
        self.live_away_label = _label(hud.body_font(13), hud.TEXT_SOFT, wrap=True, name="liveAway")
        self.live_away_label.setText(LIVE_AWAY_TEXT)
        self.live_show_button = hud.HudButton(SHOW_WINDOW_TEXT, hud.LINK)
        self.live_show_button.clicked.connect(lambda: self.liveShowWindow.emit())
        self.live_back_button = hud.HudButton(BRING_BACK_TEXT, hud.LINK)
        self.live_back_button.clicked.connect(lambda: self.liveDock.emit())
        links_holder = QWidget()
        links = hud.FlowLayout(links_holder, 12, 4)
        links.addWidget(self.live_show_button)
        links.addWidget(self.live_back_button)
        away.addWidget(self.live_away_label)
        away.addWidget(links_holder)
        away.addStretch(1)
        self._live_switch = _PageSwitch()
        self._live_switch.addWidget(self.live_log)
        self._live_switch.addWidget(self.live_away)
        self.live_page = _LivePage(self.conversation)
        page = QVBoxLayout(self.live_page)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(0)
        page.addWidget(self._live_switch, 1)
        self.set_live_summary("Nothing running")

    def _make_buttons(self) -> None:
        self.play_button = hud.HudButton("", hud.PRIMARY)
        # "Pause" / "Play" / "Replay" / "Retry" without the row jumping around.
        widths = []
        for text in ("Play", "Replay", "Retry", "Pause"):
            self.play_button.setText(text)
            widths.append(self.play_button.sizeHint().width())
        self.play_button.setMinimumWidth(max(widths))
        self.skip_button = hud.HudButton("Skip section")
        self.everything_button = hud.HudButton("Read everything")
        self.notion_button = hud.HudButton("Open in Notion")
        self.done_button = hud.HudButton("Done")
        self.play_button.clicked.connect(lambda: self.playPause.emit())
        self.skip_button.clicked.connect(lambda: self.skip.emit())
        self.everything_button.clicked.connect(lambda: self.readEverything.emit())
        self.notion_button.clicked.connect(lambda: self.openNotion.emit())
        self.done_button.clicked.connect(lambda: self.done.emit())

    def _make_side_column(self) -> None:
        self.approvals_panel = hud.ChamferPanel("Needs your OK", "0 pending", variant=hud.PANEL_AMBER)
        _caps_meta(self.approvals_panel)   # "2 PENDING", as on the artboard
        self.approvals = hud.ActionList(max_height=_APPROVALS_MAX_PX)
        self.approvals_panel.body_layout.addWidget(self.approvals)
        self.activity_panel = hud.ChamferPanel("Activity", "live", border_alpha=0.18, spacing=8)
        self.activity = hud.ActivityLog()
        self.activity_panel.body_layout.addWidget(self.activity, 1)

    def _build_layout(self) -> None:
        self._left = QWidget()
        left = QVBoxLayout(self._left)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(12)
        left.addWidget(self.status_panel)
        left.addWidget(self.agenda_panel, 1)
        self._right = QWidget()
        right = QVBoxLayout(self._right)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(12)
        right.addWidget(self.approvals_panel)
        right.addWidget(self.activity_panel, 1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(self._left)
        layout.addWidget(self._build_center(), 1)
        layout.addWidget(self._right)
        self._apply_tier(READING_SIZE.width())

    def _build_center(self) -> QWidget:
        voice = QVBoxLayout()
        voice.setSpacing(8)
        voice.addStretch(1)
        voice.addWidget(self.state_label)
        voice.addWidget(self.speech)
        voice.addStretch(1)
        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(14, 4, 14, 0)
        top_layout.setSpacing(20)
        top_layout.addWidget(self.orb, 0, Qt.AlignmentFlag.AlignVCenter)
        top_layout.addLayout(voice, 1)
        # The controls wrap onto a second row in a narrow column; Done stays bottom-right.
        # Margins leave room for the Pause glow, which is drawn outside the button.
        flow_holder = QWidget()
        flow = hud.FlowLayout(flow_holder, 6, 6)
        flow.setContentsMargins(14, 8, 0, 10)
        for button in (self.play_button, self.skip_button, self.everything_button, self.notion_button):
            flow.addWidget(button)
        done_column = QVBoxLayout()
        done_column.setContentsMargins(0, 8, 0, 10)
        done_column.addStretch(1)
        done_column.addWidget(self.done_button)
        controls = QWidget()
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 14, 0)
        controls_layout.setSpacing(6)
        controls_layout.addWidget(flow_holder, 1)
        controls_layout.addLayout(done_column)
        self._title_line = QWidget()
        title_row = QHBoxLayout(self._title_line)
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        title_row.addWidget(self.title, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addWidget(self._title_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addWidget(self.subtitle, 1, Qt.AlignmentFlag.AlignVCenter)
        self._doc_rule = _DashedRule()
        self._doc_header = doc_header = QWidget()
        doc_layout = QVBoxLayout(doc_header)
        doc_layout.setContentsMargins(_DOC_MARGIN_PX, 10, _DOC_MARGIN_PX, 2)
        doc_layout.setSpacing(4)
        doc_layout.addWidget(self._title_line)
        doc_layout.addWidget(self.stale)
        doc_layout.addSpacing(4)
        doc_layout.addWidget(self._doc_rule)
        strip = self.transcript_panel.strip_layout
        strip.insertWidget(0, self.strip_caption, 1, Qt.AlignmentFlag.AlignVCenter)
        # The LIVE tab's status tag, in the slot of the briefing's LIVE marker (hidden there).
        strip.insertWidget(strip.indexOf(self.transcript_panel.live) + 1, self.live_status, 0,
                           Qt.AlignmentFlag.AlignVCenter)
        strip.addWidget(self.sections_button, 0, Qt.AlignmentFlag.AlignVCenter)
        # The LIVE tab's Pop out and Clear (shown on that tab only; Clear gives way on a narrow strip).
        strip.addWidget(self._live_links, 0, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        # As tall as the step chips and SECTIONS it stands in for: the strip (and the tab row above
        # the panel) keeps its height when the tab changes (the links too: on LIVE the caption may
        # give way to them).
        strip_height = max(self.sections_button.sizeHint().height(), self.transcript_panel.steps.sizeHint().height())
        self.strip_caption.setMinimumHeight(strip_height)
        self._live_links.setMinimumHeight(strip_height)
        self.briefing_page = QWidget()
        briefing = QVBoxLayout(self.briefing_page)
        briefing.setContentsMargins(0, 0, 0, 0)
        briefing.setSpacing(0)
        briefing.addWidget(doc_header)
        briefing.addWidget(self.text, 1)
        self._pages = _PageSwitch()
        self._pages.addWidget(self.conversation)      # hud.TAB_JARVIS
        self._pages.addWidget(self.briefing_page)     # hud.TAB_BRIEFING
        self._pages.addWidget(self.live_page)         # hud.TAB_LIVE
        self.transcript_panel.body_layout.addWidget(self._pages, 1)
        # Folder tabs: left-aligned right above the panel, the current one's underline on its edge.
        tab_row = QWidget()
        tab_layout = QHBoxLayout(tab_row)
        tab_layout.setContentsMargins(14, 0, 14, 0)
        tab_layout.setSpacing(0)
        tab_layout.addWidget(self.tabs, 1)
        center = QWidget()
        column = QVBoxLayout(center)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(top)
        column.addWidget(self._command_holder)
        column.addWidget(controls)
        column.addWidget(tab_row)
        column.addWidget(self.transcript_panel, 1)
        return center

    def _apply_tier(self, width: int) -> None:
        tier = next(tier for tier in _TIERS if width >= tier.min_width)
        if tier == self._tier:
            return
        self._tier = tier
        self._left.setFixedWidth(tier.left)
        self._right.setFixedWidth(tier.right)
        self._fit_short(self.height() if self.height() > 0 else READING_SIZE.height())

    def _fit_short(self, height: int) -> None:
        """The orb at its tier's size, the spoken line in up to 3 lines and the strip's padding; a
        very short view (under about 520 px, plus the command bar's height when Ask is on: the
        900 x 600 window with Ask, or a small screen's window) gets a smaller orb, 2 lines and a
        tighter strip, so the controls and the tab row above the panel keep their room."""
        tier = self._tier
        if tier is None:
            return
        # The command bar (Ask on) takes its own height: such a view counts as short sooner.
        bar = self._command_holder.sizeHint().height() if self.ask_available() else 0
        short = height < _SHORT_VIEW_BELOW + bar
        orb = min(tier.orb, _SHORT_ORB_PX) if short else tier.orb
        if (self.orb.width(), self.orb.height()) != (orb, orb):
            self.orb.setFixedSize(orb, orb)
        if short != self._short:
            self._short = short
            self.speech.set_max_lines(_SHORT_SPEECH_LINES if short else _SPEECH_LINES)
        # A narrow transcript strip still needs room for a step chip beside SECTIONS.
        side = 20 if tier.orb >= 168 else 12
        pad = _SHORT_STRIP_PAD if short else 12
        self.transcript_panel.strip_layout.setContentsMargins(side, pad, side, pad)
        # Short and narrow (a 760 px window: the controls wrap into four rows): the BRIEFING page's
        # title line ("AM BRIEFING . UPDATED ...", also under STATUS and in the first spoken line) and
        # its rule fold away, so both pages need the same height and the tab row stays put.
        self._fold_doc_header(short and tier.orb < _TIERS[2].orb)

    def _fold_doc_header(self, folded: bool) -> None:
        if folded == self._doc_folded:
            return
        self._doc_folded = folded
        self._sync_doc_header()

    def _sync_doc_header(self) -> None:
        """The BRIEFING page's header: the title line and the rule unless folded (_fit_short); the
        amber stale line whenever there is one."""
        self._title_line.setVisible(not self._doc_folded)
        self._doc_rule.setVisible(not self._doc_folded)
        self._doc_header.setVisible(not self._doc_folded or bool(self.stale.text()))

    def doc_header_folded(self) -> bool:
        return self._doc_folded

    # ---- header, status and controls ---------------------------------------

    def set_header(self, title: str, subtitle: str, stale_text: str = "") -> None:
        """Briefing title and "Updated ..." above the transcript; the stale warning under them."""
        self.title.setText(title)
        self.subtitle.setText(subtitle)
        self.subtitle.setVisible(bool(subtitle))
        self._title_dot.setVisible(bool(subtitle))
        self.stale.setText(stale_text)
        self.stale.setVisible(bool(stale_text))
        self._sync_doc_header()
        self._speech_fallback = title
        if not self._speech_text:
            self.speech.setText(self._new_label or self._speech_fallback)

    # ---- tabs ----------------------------------------------------------------------

    def set_tab(self, index: int) -> None:
        """Make JARVIS (hud.TAB_JARVIS), BRIEFING (hud.TAB_BRIEFING) or LIVE (hud.TAB_LIVE, while it
        is available) the current tab."""
        self.tabs.set_current(index)

    def current_tab(self) -> int:
        return self.tabs.current()

    def set_new_briefing(self, new: bool, label: str = "") -> None:
        """A briefing nobody has viewed yet: the NEW badge on BRIEFING, and ``label`` ("Your AM
        briefing is ready to view") as the speech line while nothing is being said."""
        self.tabs.set_badge(new)
        self._new_label = label if new else ""
        if not self._speech_text:
            self.speech.setText(self._new_label or self._speech_fallback)

    def is_new_briefing(self) -> bool:
        return self.tabs.badge()

    def mark_unread(self) -> None:
        """Something was added to the conversation: a dot on JARVIS unless it is the current tab."""
        self.tabs.set_unread(True)

    def _on_tab_changed(self, index: int) -> None:
        self._show_tab_page(index)
        self.tabChanged.emit(index)

    def _show_tab_page(self, index: int) -> None:
        """The panel's page for ``index``; on JARVIS and LIVE the strip shows its caption instead of
        the step chips and SECTIONS (both stay in the strip, hidden); on LIVE the briefing's LIVE
        marker gives its slot to ``live_status``."""
        captioned = index in (hud.TAB_JARVIS, hud.TAB_LIVE)
        page = self._pages.widget(index)
        if page is not None:
            self._pages.setCurrentWidget(page)
        self.strip_caption.setVisible(captioned)
        self.transcript_panel.steps.setVisible(not captioned)
        self.sections_button.setVisible(not captioned)
        self._live_links.setVisible(index == hud.TAB_LIVE)   # Clear: _fit_caption decides
        self._sync_marker()
        self._fit_caption()
        if captioned:
            self.sections_popover.hide()
        else:
            QTimer.singleShot(0, self._after_briefing_page_shown)

    def _sync_marker(self) -> None:
        """The briefing's LIVE marker on JARVIS / BRIEFING (its space kept while hidden); on the LIVE
        tab it is out of the strip and ``live_status`` shows in its place."""
        marker = self.transcript_panel.live
        on_live = self.tabs.current() == hud.TAB_LIVE
        policy = marker.sizePolicy()
        if policy.retainSizeWhenHidden() == on_live:
            policy.setRetainSizeWhenHidden(not on_live)
            marker.setSizePolicy(policy)
        marker.set_live(self._marker_live and not on_live)
        self.live_status.setVisible(on_live)

    # ---- the LIVE tab --------------------------------------------------------------

    def set_live_available(self, available: bool) -> None:
        """``[live] enabled``: the LIVE tab and its page (hidden: JARVIS when LIVE was current)."""
        self.tabs.set_live_visible(available)

    def live_available(self) -> bool:
        return self.tabs.live_visible()

    def set_live_popped(self, popped: bool) -> None:
        """The pop-out window shows the steps (the page says so, with Show window and Bring it back
        here) or the page shows them again."""
        self._live_switch.setCurrentWidget(self.live_away if popped else self.live_log)
        self.live_pop_button.setEnabled(not popped)

    def live_popped(self) -> bool:
        return self._live_switch.currentWidget() is self.live_away

    def live_room(self) -> int:
        """The height the LIVE tab's log has (the panel's page area, whichever tab is current)."""
        return self._pages.height()

    def set_live_summary(self, text: str) -> None:
        """The LIVE summary ("3 tasks - 1 running"): the strip's status tag's tooltip and description."""
        self._live_summary = text or ""
        self.live_status.setToolTip(hud.plain_tooltip(self._live_summary) if self._live_summary else "")
        self.live_status.setAccessibleDescription(self._live_summary)

    def live_summary_text(self) -> str:
        return self._live_summary

    def set_live_state(self, state: str, text: str | None = None, *, since: float | None = None,
                       deadline: float | None = None) -> None:
        """The LIVE tab's strip tag (hud.LiveStatus.set_state); the caption beside it fits again."""
        old = self.live_status.sizeHint().width()
        self.live_status.set_state(state, text, since=since, deadline=deadline)
        if self.live_status.sizeHint().width() != old:
            self._fit_caption()

    def _after_briefing_page_shown(self) -> None:
        if not shiboken6.isValid(self) or self.tabs.current() != hud.TAB_BRIEFING:
            return
        self._fit_steps()
        self._apply_highlight(scroll_to=True)

    def set_status(self, text: str, is_error: bool = False) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))
        _set_status_style(self.status, is_error, hud.TEXT_SOFT)

    def set_controls(self, primary: str, primary_enabled: bool, can_skip: bool) -> None:
        self.play_button.setText(primary)
        self.play_button.setEnabled(primary_enabled)
        self.skip_button.setEnabled(can_skip)

    def set_read_everything(self, enabled: bool, tooltip: str) -> None:
        self.everything_button.setEnabled(enabled)
        self.everything_button.setToolTip(tooltip)

    def set_speech(self, text: str) -> None:
        """The line being spoken (Sora Light 20 px); "" shows "Your AM briefing is ready to view"
        while the briefing is NEW, else the briefing title."""
        self._speech_text = text or ""
        self.speech.setText(text or self._new_label or self._speech_fallback)

    def set_activity_state(self, state: str, live: bool, label: str | None = None) -> None:
        """Orb and state label (SPEAKING, STANDBY, WORKING, or ``label`` such as PLANNING) and the
        transcript's LIVE marker."""
        self.orb.set_state(state)
        if state != self.state_label.state() or (label or None) != self._state_text:
            self.state_label.set_state(state, label or None)
            self._state_text = label or None
        self._marker_live = bool(live)
        self._sync_marker()

    def set_ask_available(self, available: bool) -> None:
        """Show Ask Jarvis's command bar ([ask] enabled) or keep it out of the layout."""
        self._command_holder.setVisible(available)
        if self.height() > 0:
            self._fit_short(self.height())

    def ask_available(self) -> bool:
        return not self._command_holder.isHidden()

    def set_sections(self, rows: Sequence[hud.SectionRowInfo], meta: str, current: int | None = None) -> None:
        """Section rows; the list scrolls to ``current`` when it changes (not on every update)."""
        rows = tuple(rows)
        if rows != self._rows:
            self._rows = rows
            self.sections.set_rows(rows)
        self.sections_panel.set_meta(meta)
        self.sections_button.set_meta(str(len(rows)) if rows else "")
        self.sections_button.setEnabled(bool(rows))
        if not rows:
            self.sections_popover.hide()
        if current is not None and current != self._shown_section:
            self.sections.ensure_visible(current)
        self._shown_section = current

    # ---- sections popover ------------------------------------------------------

    def open_sections(self) -> None:
        """Open the SECTIONS list under its button, with the current section in view."""
        if not self.sections_button.isChecked():
            self.sections_button.setChecked(True)   # toggled() comes back here
            return
        if self.sections_popover.isVisible() or not self._rows:
            return
        panel = self.sections_panel
        margins = panel.body_layout.contentsMargins()
        chrome = (margins.top() + margins.bottom() + panel.title_label.sizeHint().height()
                  + panel.body_layout.spacing() + 4)
        # A drop-down over the transcript: it ends at the bottom of this view and scrolls beyond.
        below = self.mapToGlobal(QPoint(0, self.height())).y() - self.sections_button.mapToGlobal(
            QPoint(0, self.sections_button.height())).y() - 2 * hud.Popover.MARGIN
        height = min(self.sections.content_height() + chrome, max(_SECTIONS_POPOVER_MIN_PX, below))
        self.sections_popover.open_below(self.sections_button, _SECTIONS_POPOVER_WIDTH, height)
        target = self._shown_section if self._shown_section is not None else 0
        self.sections.ensure_visible(target)
        if 0 <= target < self.sections.row_count() and self.sections.row(target).isEnabled():
            self.sections.row(target).setFocus(Qt.FocusReason.PopupFocusReason)

    def close_sections(self) -> None:
        self.sections_popover.hide()

    def _on_sections_toggled(self, checked: bool) -> None:
        if checked:
            self.open_sections()
        else:
            self.sections_popover.hide()

    def _on_sections_closed(self) -> None:
        self.sections_button.setChecked(False)

    def _on_section_row(self, index: int) -> None:
        self.sections_popover.hide()
        self.sectionClicked.emit(index)

    def set_steps(self, steps: Sequence[tuple[str, str]]) -> None:
        """Section chips above the transcript: as many as fit, always with the active one."""
        self._steps = list(steps)
        self._fit_steps()

    def _fit_steps(self) -> None:
        steps = list(self._steps)
        room = self.transcript_panel.steps.width()
        states = [state for _, state in steps]
        # Keep the active chip; with none (finished), the last one that was read.
        if hud.STEP_ACTIVE in states:
            active = states.index(hud.STEP_ACTIVE)
        else:
            active = max((i for i, state in enumerate(states) if state == hud.STEP_DONE), default=0)
        while len(steps) > 1:
            self._step_meter.set_steps(steps)
            if self._step_meter.sizeHint().width() <= room:
                break
            if active > len(steps) - 1 - active:   # drop the end farther from the active chip
                steps.pop(0)
                active -= 1
            else:
                steps.pop()
        if steps != self.transcript_panel.steps.steps():
            self.transcript_panel.steps.set_steps(steps)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if watched is self.transcript_panel.steps and event.type() == QEvent.Type.Resize:
            self._fit_steps()
        elif watched is self.transcript_panel.strip and event.type() == QEvent.Type.Resize:
            self._fit_caption()
        return super().eventFilter(watched, event)

    def _fit_caption(self) -> None:
        """The JARVIS (LIVE) strip's caption in full, or "CONVERSATION" ("LIVE STEPS") alone where it
        does not fit; on LIVE after the room for the status tag, Pop out and (where it fits) Clear."""
        strip = self.transcript_panel.strip
        margins = strip.layout().contentsMargins()
        spacing = strip.layout().spacing()
        on_live = self.tabs.current() == hud.TAB_LIVE
        tag = self.live_status if on_live else self.transcript_panel.live
        full, short = (LIVE_CAPTION, LIVE_CAPTION_SHORT) if on_live else (STRIP_CAPTION, STRIP_CAPTION_SHORT)
        room = (strip.width() - margins.left() - margins.right() - tag.sizeHint().width() - spacing)
        if on_live:
            # Pop out always; Clear when it fits beside the tag (else it is in the pop-out window). The
            # caption comes last and is left out (taking no room) where it does not fit.
            room -= self.live_pop_button.sizeHint().width() + spacing
            clear = self.live_clear_button.sizeHint().width() + _LIVE_LINK_GAP
            fits = room + spacing - clear >= 0
            if self.live_clear_button.isHidden() == fits:
                self.live_clear_button.setVisible(fits)
            if fits:
                room -= clear
        metrics = QFontMetricsF(self.strip_caption.font())
        text = full if metrics.horizontalAdvance(full) + 2 <= room else short
        if on_live and metrics.horizontalAdvance(short) + 2 > room:
            text = ""   # a narrow strip on LIVE: the tab above already says LIVE; the tag and links keep their room
        if self.strip_caption.text() != text:
            self.strip_caption.setText(text)
        if on_live and self.strip_caption.isHidden() == bool(text):
            self.strip_caption.setVisible(bool(text))

    def set_pending(self, pending: int, total: int) -> None:
        """Pending proposals: the panel's meta and the STATUS bar."""
        self.approvals_panel.set_meta(f"{pending} pending")
        self.approvals_bar.set_value(str(pending), pending / total if total else 0.0)

    # ---- approval cards ------------------------------------------------------

    def clear_action_cards(self) -> None:
        self.approvals.clear()

    def add_action_card(self, action_id: str, kind: str, title: str, detail: str,
                        actionable: bool, **options: Any) -> hud.ActionCard:
        """A NEEDS YOUR OK card; ``options`` are ActionCard's keyword arguments
        (approve_text, body, title_lines, open_text, copy_text, edit_text, sign_in_text,
        check_line)."""
        card = hud.ActionCard(action_id, kind, title, detail, actionable=actionable, **options)
        card.approveClicked.connect(self.approveClicked)
        card.denyClicked.connect(self.denyClicked)
        card.lockedClicked.connect(self.lockedClicked)
        card.openClicked.connect(self.openLink)
        card.sourceClicked.connect(self.openSource)
        card.copyClicked.connect(self.copyText)
        card.undoClicked.connect(self.undoAction)
        card.editClicked.connect(self.editAction)
        card.signInClicked.connect(self.signInAccount)
        return self.approvals.add_card(card)

    def action_card(self, action_id: str) -> hud.ActionCard | None:
        return self.approvals.card(action_id)

    # ---- panel content ------------------------------------------------------

    def show_message(self, text: str, is_error: bool = False) -> None:
        """Replace the panel with one line (loading text or an error)."""
        self._block_numbers = {}
        self._current = (-1, -1)
        self.text.set_row(-1)
        self.text.setExtraSelections([])
        document = self.text.document()
        document.clear()
        char_format = QTextCharFormat()
        char_format.setFont(self._style.body)
        char_format.setForeground(QColor(hud.AMBER if is_error else hud.TEXT_SOFT))
        block_format = QTextBlockFormat()
        block_format.setLineHeight(self._style.body_line, _PROPORTIONAL)
        cursor = QTextCursor(document)
        cursor.setBlockFormat(block_format)
        cursor.insertText(text, char_format)

    def set_script(self, script: Script, include_ignored: bool) -> None:
        """Render one text block per script item; keeps the scroll position and highlight."""
        bar = self.text.verticalScrollBar()
        scroll = bar.value()
        document = self.text.document()
        document.clear()
        cursor = QTextCursor(document)
        self._block_numbers = {}
        for section_index, section in enumerate(script.sections):
            dim = section.ignored and not include_ignored
            amber = section.key == ACTIONS_KEY
            for item_index, item in enumerate(section.items):
                first = not self._block_numbers
                block_format, char_format, text = _item_format(item, dim, first, self._style, amber)
                if first:
                    cursor.setBlockFormat(block_format)
                    cursor.setBlockCharFormat(char_format)
                else:
                    cursor.insertBlock(block_format, char_format)
                _insert_item_text(cursor, item, text, char_format, dim)
                self._block_numbers[(section_index, item_index)] = cursor.blockNumber()
        # The browser's own cursor is left at the end by the inserts, and showing the
        # panel scrolls to it. Move it to the start first: setTextCursor scrolls too,
        # so the saved position (kept when re-rendering) is restored after it.
        self.text.setTextCursor(QTextCursor(document))
        bar.setValue(scroll)
        self._apply_highlight(scroll_to=False)

    # ---- highlight and auto-scroll -----------------------------------------

    def highlight(self, section_index: int, item_index: int) -> None:
        """Highlight the spoken item; (-1, -1) clears the highlight."""
        self._current = (section_index, item_index)
        self._apply_highlight(scroll_to=True)

    def highlighted_block(self) -> int:
        """Block number of the agenda-row highlight (-1 when none)."""
        return self.text.row()

    def _apply_highlight(self, *, scroll_to: bool) -> None:
        number = self._block_numbers.get(self._current)
        block = self.text.document().findBlockByNumber(number) if number is not None else None
        if block is None or not block.isValid() or not block.text():
            self.text.set_row(-1)
            self.text.setExtraSelections([])
            return
        self.text.document().documentLayout().blockBoundingRect(block)   # make sure it is laid out
        self.text.setExtraSelections(_highlight_selections(block))
        self.text.set_row(number)
        if scroll_to:
            self._auto_scroll(block)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._apply_tier(event.size().width())
        self._fit_command_bar(event.size().height())
        self._fit_short(event.size().height())
        self._apply_highlight(scroll_to=False)   # line wrapping changed

    def _fit_command_bar(self, height: int) -> None:
        """The narrowest tier or a short screen gets the compact bar (one status line), so the
        controls and the transcript below keep their room."""
        compact = (self._tier is not None and self._tier.orb < _TIERS[2].orb) or height < _COMPACT_BAR_BELOW
        self.command_bar.set_compact(compact)
        self._command_holder.layout().setContentsMargins(14, 6 if compact else 10, 14, 0)

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._apply_highlight(scroll_to=True)    # an item highlighted while hidden comes into view

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self.sections_popover.hide()

    def _auto_scroll(self, block: QTextBlock) -> None:
        if time.monotonic() - self._user_scrolled_at < _MANUAL_SCROLL_HOLD_S:
            return
        bar = self.text.verticalScrollBar()
        top = self.text.document().documentLayout().blockBoundingRect(block).top()
        target = int(top - self.text.viewport().height() * _SCROLL_ANCHOR)
        target = max(bar.minimum(), min(bar.maximum(), target))
        if abs(target - bar.value()) < 4:
            return
        self._scroll_animation.stop()
        self._scroll_animation.setStartValue(bar.value())
        self._scroll_animation.setEndValue(target)
        self._scroll_animation.start()

    def _on_user_scroll(self, _action: int) -> None:
        # actionTriggered fires for wheel, drag, clicks and keys, never for setValue().
        self._user_scrolled_at = time.monotonic()
        self._scroll_animation.stop()


def _item_format(item: ScriptItem, dim: bool, first: bool, style: _TranscriptStyle,
                 amber: bool) -> tuple[QTextBlockFormat, QTextCharFormat, str]:
    """Block format, character format and text for one script item."""
    block_format = QTextBlockFormat()
    block_format.setLineHeight(style.body_line, _PROPORTIONAL)
    block_format.setBottomMargin(_ROW_GAP_PX)
    char_format = QTextCharFormat()
    char_format.setFont(style.body)
    text = item.display
    if not text:   # a pause: a small visual gap, never a text line
        block_format.setLineHeight(_PAUSE_GAP_PX, _FIXED_HEIGHT)
        block_format.setBottomMargin(0)
        return block_format, char_format, ""
    color = _ITEM_COLORS.get(item.kind, hud.TEXT_BODY)
    if item.kind == ITEM_HEADING:
        char_format.setFont(style.heading)
        block_format.setLineHeight(style.heading_line, _PROPORTIONAL)
        block_format.setTopMargin(0 if first else _HEADING_TOP_PX)
        block_format.setBottomMargin(6)
        color = hud.AMBER if amber else hud.ACCENT
        text += _NOT_READ_SUFFIX if dim else ""
    elif item.kind == ITEM_SUBHEADING:
        char_format.setFont(style.subheading)
        block_format.setTopMargin(6)
        block_format.setIndent(item.depth)
    elif item.kind == ITEM_ENTRY:
        block_format.setIndent(item.depth + 1)
        text = _entry_text(text)
        _hang_first_line(block_format, text, style.body_metrics)
    else:
        block_format.setIndent(item.depth)
    char_format.setForeground(QColor(hud.TEXT_DIM if dim else color))
    return block_format, char_format, text


def _insert_item_text(cursor: QTextCursor, item: ScriptItem, text: str, char_format: QTextCharFormat,
                      dim: bool) -> None:
    """Insert ``text``; a list entry's bullet is drawn in a quiet accent."""
    if item.kind == ITEM_ENTRY and text.startswith(_BULLET):
        bullet_format = QTextCharFormat(char_format)
        bullet_format.setForeground(hud.rgba(hud.TEXT_DIM if dim else hud.ACCENT, 0.7))
        cursor.insertText(_BULLET, bullet_format)
        cursor.insertText(text[len(_BULLET):], char_format)
    else:
        cursor.insertText(text, char_format)


def _entry_text(display: str) -> str:
    return display if _LIST_PREFIX_RE.match(display) else _BULLET + display


def _hang_first_line(block_format: QTextBlockFormat, text: str, metrics: QFontMetricsF) -> None:
    """Wrapped lines of a list entry line up with its text, not with its bullet or number."""
    match = _LIST_PREFIX_RE.match(text)
    prefix = match.group(0) if match else _BULLET
    width = metrics.horizontalAdvance(prefix)
    block_format.setLeftMargin(width)
    block_format.setTextIndent(-width)


def _highlight_selections(block: QTextBlock) -> list[QTextEdit.ExtraSelection]:
    """Brighter text for the whole highlighted block (the row itself is painted by _Transcript)."""
    cursor = QTextCursor(block)
    cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
    char_format = QTextCharFormat()
    char_format.setForeground(QColor(hud.TEXT_BRIGHT))
    selection = QTextEdit.ExtraSelection()
    selection.cursor = cursor
    selection.format = char_format
    return [selection]


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------

PROMPT_SIZE = QSize(600, 300)   # wide enough for the header's clock, chips, minimize and close
ASK_CHIP = "claude"             # Ask Jarvis's header chip ([ask] enabled only)
READING_ONLY_CHIPS = (ASK_CHIP,)   # hidden on the prompt (no room in 600 px)
READING_SIZE = QSize(1120, 700)
READING_MIN_SIZE = QSize(900, 600)
SCREEN_MARGIN = 24
_PROMPT_MARGINS = (10, 4, 10, 10)
_READING_MARGINS = (12, 4, 12, 12)
MAIN_WINDOW_TITLE = "Jarvis"                  # the taskbar button, Alt+Tab and the window's accessible name
HEADER_SUBTITLE = f"assistant {DOT} desktop"  # next to the JARVIS wordmark (the LIVE pop-out has its own)


class _PageSwitch(QWidget):
    """Shows one page at a time and sizes to that page only.

    Not a QStackedWidget: its layout reports the largest minimum and
    height-for-width of ALL pages, so the hidden reading view (whose wrapped
    text grows tall at the prompt's width) would prop the fixed-size prompt
    window open. Real Windows applies height-for-width to top-level windows
    (offscreen tests never do), which left the prompt 668 px tall with its
    buttons cut off. A hidden widget takes no part in layout, so hiding the
    other pages keeps every size hint to the current page.
    """

    currentChanged = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._pages: list[QWidget] = []
        self._current: QWidget | None = None

    def addWidget(self, page: QWidget) -> None:  # noqa: N802 - mirrors QStackedWidget
        self._layout.addWidget(page)
        self._pages.append(page)
        if self._current is None:
            self._current = page
        else:
            page.hide()

    def currentWidget(self) -> QWidget | None:  # noqa: N802 - mirrors QStackedWidget
        return self._current

    def widget(self, index: int) -> QWidget | None:
        return self._pages[index] if 0 <= index < len(self._pages) else None

    def setCurrentWidget(self, page: QWidget) -> None:  # noqa: N802 - mirrors QStackedWidget
        changed = page is not self._current
        for other in self._pages:
            if other is not page:
                other.hide()
        self._current = page
        page.show()
        self.updateGeometry()
        if changed:
            self.currentChanged.emit(self._pages.index(page))


class BriefingWindow(hud.HudWindowFrame):
    """Frameless, always-on-top HUD window holding the prompt and reading views.

    The header bar is the title bar (drag to move); its close button and
    Alt+F4 go through ``closeRequested`` so the controller decides what
    closing means. Its minimize button minimizes the window to its taskbar
    button; ``visibilityChanged`` tells the controller.
    """

    closeRequested = Signal()
    visibilityChanged = Signal()   # shown, hidden, minimized or restored
    activeChanged = Signal(bool)   # the window became the active window (True) or stopped being it

    def __init__(self, short_minutes: int, long_minutes: int) -> None:
        super().__init__(margins=_PROMPT_MARGINS)
        self.setObjectName("BriefingWindow")
        self.setWindowTitle(MAIN_WINDOW_TITLE)
        self.setWindowIcon(app_icon())
        assert self.header is not None
        self.header.set_subtitle(HEADER_SUBTITLE)
        for name in ("notion", "voice", "calendar"):
            self.header.set_service(name, hud.STATUS_OFF)
        self.prompt = PromptView(short_minutes, long_minutes)
        self.reading = ReadingView()
        self._stack = _PageSwitch()
        self._stack.addWidget(self.prompt)
        self._stack.addWidget(self.reading)
        self.body_layout.addWidget(self._stack)
        self.set_glow_anchor(self.prompt.orb)
        self._speaker_available = False   # [assistant] speak: the header's speaker button (assistant screen)
        mute = QShortcut(QKeySequence("Ctrl+M"), self)
        mute.setContext(Qt.ShortcutContext.WindowShortcut)
        mute.setAutoRepeat(False)
        mute.activated.connect(self._on_mute_shortcut)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        event.ignore()   # the controller decides what closing means
        self.closeRequested.emit()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self.visibilityChanged.emit()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self.visibilityChanged.emit()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt override
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self.visibilityChanged.emit()
        elif event.type() == QEvent.Type.ActivationChange:
            self.activeChanged.emit(self.isActiveWindow())

    def event(self, event: QEvent) -> bool:
        # A Tab-focused button keeps Space (pressed, then clicked on release, as
        # everywhere else) and a text field types it (Ask Jarvis's bar, even while
        # it is read only); the reading view's Space shortcut only acts when
        # neither has focus.
        if (event.type() == QEvent.Type.ShortcutOverride and event.key() == Qt.Key.Key_Space
                and event.modifiers() in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier)
                and _keeps_space(self.focusWidget())):
            event.accept()
            return True
        return super().event(event)

    def is_reading_view(self) -> bool:
        return self._stack.currentWidget() is self.reading

    def set_hour24(self, hour24: bool) -> None:
        """The header clock and the activity times: "13:05" (True) or "1:05 PM" (False)."""
        assert self.header is not None
        self.header.set_hour24(hour24)
        self.reading.activity.set_hour24(hour24)
        self.reading.conversation.set_hour24(hour24)
        self.reading.live_log.set_hour24(hour24)

    # ---- views and sizes ------------------------------------------------------

    def show_prompt_view(self) -> None:
        self._stack.setCurrentWidget(self.prompt)
        self._show_reading_chips(False)
        self.layout().setContentsMargins(*_PROMPT_MARGINS)
        self.set_resizable(False)
        self.set_glow_anchor(self.prompt.orb)
        self.fit_prompt(force=True)

    def _show_reading_chips(self, shown: bool) -> None:
        """The header chips of the reading view only (``claude``: the narrow prompt has no room), and
        the speaker button (the assistant screen only)."""
        assert self.header is not None
        for name in READING_ONLY_CHIPS:
            self.header.set_service_visible(name, shown)
        self._show_speaker()

    def set_speaker_available(self, available: bool) -> None:
        """``[assistant] speak``: the header's speaker button (mute Jarvis's own voice) exists; it shows
        on the assistant screen only."""
        self._speaker_available = bool(available)
        self._show_speaker()

    def _show_speaker(self) -> None:
        assert self.header is not None
        self.header.set_speaker_visible(self._speaker_available and self.is_reading_view())

    def _on_mute_shortcut(self) -> None:
        """Ctrl+M: the speaker button, while it shows."""
        assert self.header is not None
        if self.header.speaker_visible():
            self.header.speaker_button.click()

    def fit_prompt(self, *, force: bool = False) -> None:
        """Fixed width; tall enough for the current text, at least the nominal height."""
        if self.is_reading_view():
            return
        left, top, right, bottom = _PROMPT_MARGINS
        body = self.prompt.heightForWidth(PROMPT_SIZE.width() - left - right)
        header = self.header.height() if self.header is not None else 0
        height = max(PROMPT_SIZE.height(), top + header + body + bottom)
        size = QSize(PROMPT_SIZE.width(), height)
        if force or size != self.size() or self.maximumSize() != size:
            self._apply_size(size)

    def show_reading_view(self) -> None:
        self._stack.setCurrentWidget(self.reading)
        self._show_reading_chips(True)
        self.layout().setContentsMargins(*_READING_MARGINS)
        self.set_resizable(True)
        self.set_glow_anchor(self.reading.orb)
        room = self._screen_room()
        minimum = READING_MIN_SIZE.boundedTo(room)   # a small screen gets a smaller window
        self._apply_size(READING_SIZE.boundedTo(room).expandedTo(minimum), minimum)
        self.reading.setFocus(Qt.FocusReason.OtherFocusReason)

    def _screen_room(self) -> QSize:
        area = _screen_area(self.geometry().center() if self.isVisible() else QCursor.pos())
        return area.size() - QSize(2 * SCREEN_MARGIN, 2 * SCREEN_MARGIN)

    def _apply_size(self, size: QSize, minimum: QSize | None = None) -> None:
        """Resize (fixed when ``minimum`` is None), keeping a visible window's bottom-right corner."""
        corner = self.geometry().bottomRight() if self.isVisible() else None
        self.setMinimumSize(0, 0)
        self.setMaximumSize(_QWIDGETSIZE_MAX, _QWIDGETSIZE_MAX)
        if minimum is None:
            self.setFixedSize(size)
        else:
            self.setMinimumSize(minimum)
            self.resize(size)
        if corner is not None:
            self.move(corner.x() - self.width() + 1, corner.y() - self.height() + 1)
            self.keep_on_screen()

    # ---- placement --------------------------------------------------------------

    def place(self) -> None:
        """Bottom-right of the available area of the screen under the cursor."""
        area = _screen_area(QCursor.pos())
        x = area.x() + area.width() - self.width() - SCREEN_MARGIN
        y = area.y() + area.height() - self.height() - SCREEN_MARGIN
        self.move(max(area.x(), x), max(area.y(), y))

    def keep_on_screen(self) -> None:
        """Move the window fully inside the available area of its screen."""
        _keep_on_screen(self)


def _keep_on_screen(widget: QWidget) -> None:
    """Move the top-level ``widget`` fully inside the available area of its screen."""
    frame = QRect(widget.pos(), widget.size())
    area = _screen_area(frame.center())
    x = max(area.x(), min(frame.x(), area.x() + area.width() - frame.width()))
    y = max(area.y(), min(frame.y(), area.y() + area.height() - frame.height()))
    if (x, y) != (frame.x(), frame.y()):
        widget.move(x, y)


# --------------------------------------------------------------------------
# The LIVE view: the feed from the stream and the pop-out window
# --------------------------------------------------------------------------

LIVE_WINDOW_TITLE = "Jarvis - Live"
LIVE_WINDOW_SIZE = QSize(520, 680)
LIVE_WINDOW_MIN_SIZE = QSize(360, 320)
_LIVE_WINDOW_GAP = 12      # between the main window and the pop-out placed beside it
_LIVE_DRAIN_MS = 100       # the feed takes what changed at most this often
# A LIVE tab shorter than this (a small screen's window) shows hardly a step: with another screen,
# auto-open "tab" shows the pop-out there instead (without the focus).
LIVE_DOCKED_MIN_PX = 120
_LIVE_LINK_GAP = 8          # between Pop out and Clear in the LIVE tab's strip
# Notion's page and block ids (a fetch error may quote one): never shown in the LIVE view.
_NOTION_ID_RE = re.compile(r"\b[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}\b")


@dataclasses.dataclass(frozen=True)
class LiveSummary:
    """What the LIVE view shows right now, for the tab's dot, the strip's tag and the pop-out's toolbar."""

    tasks: int = 0                        # the tasks shown (not the pinned "Background reads")
    running: int = 0                      # Asks and approved cards still running
    working_since: float | None = None    # the oldest of those started (monotonic)
    countdown_deadline: float | None = None   # an approved card's undo countdown runs out then
    countdown_word: str = ""              # "SENDING" / "ADDING"
    attention: bool = False               # this update brought steps of an Ask or an approved card

    def text(self) -> str:
        """"3 tasks - 1 running", "3 tasks - nothing running", "Nothing running"."""
        if not self.tasks and not self.running:
            return "Nothing running"
        running = f"{self.running} running" if self.running else "nothing running"
        return f"{_plural(self.tasks, 'task')} - {running}"


class _LiveBridge(QObject):
    changed = Signal()


class _LiveFeed(QObject):
    """Drains the LIVE stream on the GUI thread and hands what changed to every attached view.

    The stream's listener (``poke``, called from any thread, at most once until the next drain)
    emits a queued signal; the first one starts a 100 ms single-shot timer, and its timeout takes
    ``stream.changes(version)`` and applies it to every view (hud.LiveLog), then emits
    ``updated(LiveSummary)``. So a burst of any size costs at most about ten drains a second and
    one pending event. ``attach(view)`` starts a view from the whole snapshot. Nothing on a worker
    thread touches Qt widgets and nothing here waits for a worker.
    """

    updated = Signal(object)   # LiveSummary

    def __init__(self, stream: _live.LiveStream, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._stream = stream
        self._bridge = _LiveBridge(self)
        self._bridge.changed.connect(self._on_changed, Qt.ConnectionType.QueuedConnection)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(_LIVE_DRAIN_MS)
        self._timer.timeout.connect(self.drain)
        self._version = 0
        self._views: list[Any] = []
        self._tasks: dict[int, _live.TaskView] = {}
        self._stopped = False
        self.drains = 0
        self.summary_now = LiveSummary()
        if stream.enabled:
            stream.set_listener(self.poke)

    def poke(self) -> None:
        """The stream's listener (any thread): one queued signal to the GUI thread."""
        if self._stopped:
            return
        try:
            self._bridge.changed.emit()
        except RuntimeError:   # the bridge is gone (shutting down)
            pass

    def _on_changed(self) -> None:
        if not self._stopped and shiboken6.isValid(self._timer) and not self._timer.isActive():
            self._timer.start()

    def views(self) -> list[Any]:
        return list(self._views)

    def attach(self, view: Any) -> None:
        """Feed ``view`` too, starting from the whole stream as it is now."""
        if self._stopped or view in self._views:
            return
        self._views.append(view)
        self._apply(view, _live.LiveChanges(self._stream.version, self._stream.snapshot(), (), True))

    def detach(self, view: Any) -> None:
        if view in self._views:
            self._views.remove(view)

    def drain(self) -> None:
        """Take what changed since the last drain and show it everywhere (the timer calls it)."""
        if self._stopped:
            return
        changes = self._stream.changes(self._version)
        self._version = changes.version
        if changes.reset:
            self._tasks = {view.id: view for view in changes.tasks}
        else:
            for task_id in changes.removed:
                self._tasks.pop(task_id, None)
            for view in changes.tasks:
                self._tasks[view.id] = view
        if changes.tasks or changes.removed or changes.reset:
            for view in list(self._views):
                self._apply(view, changes)
        self.drains += 1
        attention = any(view.kind in _live.ATTENTION_KINDS for view in changes.tasks) and not changes.reset
        self.summary_now = self._summary(attention)
        try:
            self.updated.emit(self.summary_now)
        except RuntimeError:   # this feed's controller is gone
            self.stop()

    def _apply(self, view: Any, changes: _live.LiveChanges) -> None:
        if not shiboken6.isValid(view):
            self.detach(view)
            return
        try:
            view.apply(changes)
        except Exception as exc:  # noqa: BLE001 - a broken view never stops the feed or Jarvis
            logger.debug("Live view: apply failed (%s)", type(exc).__name__)

    def _summary(self, attention: bool) -> LiveSummary:
        shown = [view for view in self._tasks.values() if not view.pinned]
        running = [view for view in self._tasks.values()
                   if view.kind in _live.ATTENTION_KINDS and view.status == _live.STATUS_RUNNING]
        deadline, word = None, ""
        for view in running:
            step = next((step for step in reversed(view.steps)
                         if step.status == _live.STATUS_RUNNING and step.deadline is not None), None)
            if step is not None:
                deadline, word = step.deadline, step.status_text or "SENDING"
                break
        return LiveSummary(tasks=len(shown), running=len(running),
                           working_since=min((view.started for view in running), default=None),
                           countdown_deadline=deadline, countdown_word=word, attention=attention)

    def stop(self) -> None:
        """Shutdown: no more drains, no views."""
        self._stopped = True
        self._views.clear()
        try:
            self._timer.stop()
        except RuntimeError:
            pass


class LiveWindow(hud.HudWindowFrame):
    """The LIVE view in a window of its own ("Pop out"): a normal resizable window with its own
    taskbar button (never on top), for a second screen. Its header has the wordmark, "live - this
    session", the clock, minimize and close; a toolbar with the strip's status tag, the summary,
    Dock and Clear; and its own hud.LiveLog fed by the same _LiveFeed as the LIVE tab. Close,
    Alt+F4 and Dock ask to dock (``dockRequested``): the controller hides it, it never quits.
    """

    dockRequested = Signal()
    clearRequested = Signal()
    linkClicked = Signal(str)

    def __init__(self) -> None:
        super().__init__(margins=(10, 4, 10, 10), always_on_top=False)
        self.setObjectName("LiveWindow")
        self.setWindowTitle(LIVE_WINDOW_TITLE)
        self.setWindowIcon(app_icon())
        assert self.header is not None
        self.header.set_subtitle(f"live {DOT} this session")
        self.set_resizable(True)
        self.setMinimumSize(LIVE_WINDOW_MIN_SIZE)
        self.resize(LIVE_WINDOW_SIZE)
        self.status = hud.LiveStatus()
        self.summary = _ElidedLabel(hud.mono_font(10, 400, 0.04), hud.TEXT_DIM)
        self.summary.setObjectName("liveWindowSummary")
        self.dock_button = hud.HudButton(DOCK_TEXT, hud.LINK)
        self.dock_button.setToolTip(DOCK_TIP)
        self.dock_button.clicked.connect(lambda: self.dockRequested.emit())
        self.clear_button = hud.HudButton(CLEAR_TEXT, hud.LINK)
        self.clear_button.setToolTip(CLEAR_TIP)
        self.clear_button.clicked.connect(lambda: self.clearRequested.emit())
        toolbar = QWidget()
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(4, 8, 4, 6)
        row.setSpacing(10)
        row.addWidget(self.status, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.summary, 1, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.dock_button, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.clear_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.log = hud.LiveLog()
        self.log.linkClicked.connect(self.linkClicked)
        self.body_layout.addWidget(toolbar)
        self.body_layout.addWidget(self.log, 1)
        self.set_summary(LiveSummary())

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        event.ignore()   # closing docks the view back into the main window; the app never quits here
        self.dockRequested.emit()

    def set_hour24(self, hour24: bool) -> None:
        assert self.header is not None
        self.header.set_hour24(hour24)
        self.log.set_hour24(hour24)

    def set_summary(self, summary: LiveSummary) -> None:
        self.summary.set_full_text(summary.text())
        _show_live_state(self.status.set_state, summary)

    def keep_on_screen(self) -> None:
        _keep_on_screen(self)


def _show_live_state(set_state: Callable[..., None], summary: LiveSummary) -> None:
    """The strip's / pop-out's status tag (``set_state`` as hud.LiveStatus.set_state's): SENDING IN
    7 S, WORKING 0:12 or IDLE."""
    if summary.countdown_deadline is not None:
        set_state(hud.LIVE_COUNTDOWN, summary.countdown_word or "SENDING", deadline=summary.countdown_deadline)
    elif summary.running:
        set_state(hud.LIVE_WORKING, since=summary.working_since)
    else:
        set_state(hud.LIVE_IDLE)


# --------------------------------------------------------------------------
# Controller
# --------------------------------------------------------------------------

STATE_IDLE = "idle"
STATE_PROMPT = "prompt"
STATE_SNOOZED = "snoozed"
STATE_READING = "reading"        # the assistant screen (JARVIS / BRIEFING); not necessarily playing
STATE_WAITING = "waiting"        # a scheduled run polls with the window hidden (tray icon only)
STATE_QUITTING = "quitting"

TRAY_TOOLTIP = "Jarvis"
VIEW_BRIEFING_TEXT = "View briefing"   # the announcement's link in the conversation
VIEW_BRIEFING_LINK = "view-briefing"
ASK_CANCELLED_ENTRY = "Cancelled; nothing was proposed."   # the conversation's entry for a cancelled Ask
ASK_ACK_KEY = "ask-ack"                  # "One moment, sir." while an Ask plans (dropped when it ends first)
# How the conversation shows what Jarvis says after a card was carried out (persona.outcome_tone).
_RESULT_TONES = {OUTCOME_GOOD: hud.TONE_GOOD, OUTCOME_ERROR: hud.TONE_ERROR, OUTCOME_WARN: hud.TONE_WARN}
_GREETING_WAIT_MS = 3000          # an opening greeting waits this long for the first fetch answer
_GREETING_REPEAT_S = 120.0        # an open of a hidden or minimized window greets again after this
_LOCK_RECHECK_MS = 5000           # a held announcement checks the lock screen again this often
_NEXT_EVENT_WITHIN = timedelta(hours=3)   # the greeting may name an event starting this soon

LOADING_TEXT = "Fetching your briefing from Notion..."
ACTIONS_FILE = "actions.json"
CALENDAR_SETUP_NOTE = "Google Calendar is not set up yet - see README step 8"
_SESSION_MAX_AGE_S = 12 * 3600
_AUDIO_CLEANUP_WAIT_S = 15.0
_AUDIO_RELEASE_WAIT_S = 3.0
_NOTE_DURATION_MS = 8000
# Read now on a briefing that is not fresh asks Notion once more, but waits at
# most this long before reading the copy it already has.
_RECHECK_WAIT_MS = 5000
_SAME_RUN_WINDOW = timedelta(hours=12)   # a relaunch this soon is the same run, not the next day's
_WARMUP_INDEX = -1                       # TTS job that only fills the cache (noted intro)
_PRIMARY_LABELS = {PLAYING: "Pause", WAITING: "Pause", PAUSED: "Play", FINISHED: "Replay"}
_STATUS_LABELS = {PLAYING: "Playing", PAUSED: "Paused", FINISHED: "Finished"}
_ORB_FOR_PLAYER = {PLAYING: hud.ORB_SPEAKING, WAITING: hud.ORB_WORKING}
_STEP_WINDOW = 5                         # section chips shown above the transcript
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Action worker stages (the card shows WORKING / WAITING FOR GOOGLE SIGN-IN), and the undo
# countdown before an RSVP, Move or Cancel goes to Google.
_STAGE_WORKING = STAGE_WORKING
_STAGE_SIGNIN = STAGE_SIGNIN
_STAGE_SIGNED_IN = STAGE_SIGNED_IN
_STAGE_COUNTDOWN = "countdown"           # "SENDING IN 9 S" + Undo: nothing has been sent yet
_RESULT_KEY = "result"                   # the voice key of every "Done, sir" / "Sent, sir" sentence
_SIGN_IN_CHECK = "sign-in check"         # action worker job: report which accounts are signed in
_CARD_FOR_STATUS = {STATUS_CREATED: hud.CARD_ADDED, STATUS_EXISTS: hud.CARD_EXISTS,
                    STATUS_DENIED: hud.CARD_DENIED, STATUS_FAILED: hud.CARD_FAILED,
                    STATUS_DONE: hud.CARD_DONE, STATUS_SENT: hud.CARD_SENT,
                    STATUS_UNKNOWN: hud.CARD_UNKNOWN, STATUS_RUNNING: hud.CARD_WORKING}
# The undo countdown ([actions] undo_seconds): how often the card's "SENDING IN n S" is updated.
_COUNTDOWN_TICK_MS = 200
_GROUP_TITLES = {SOURCE_ASK: "Ask", SOURCE_BRIEFING: "Briefing"}   # the NEEDS YOUR OK group headers
_ASK_CARDS_MAX = 16                    # Ask cards kept (this run, memory only); the oldest go first
ALREADY_LISTED_TEXT = ALREADY_LISTED   # an Ask card the briefing also proposes (information only)
ASK_PLANNING_LABEL = "PLANNING"        # the reading orb's label while an Ask plans
ASK_SIGN_IN_FIRST = "Finish the Google sign-in in your browser first, then ask again"
ASK_MAIL_BUSY = "Finish the approval or sign-in that is running first, then allow reading mail"
ASK_MAIL_WAIT = "Wait for Ask to finish, then allow reading mail"
# At exit, a change already on its way to Google is waited for this long (the window is hidden).
_QUIT_SEND_WAIT_S = 5.0
SIGN_IN_LINK_TEXT = "Sign in"
EDIT_LINK_TEXT = "Edit"
EDITED_MARK = f" {DOT} edited"
_EVENT_LINK_TEXT = "Open event"          # the result link of an answered, moved or cancelled event
UNKNOWN_CARD_TEXT = "check the calendar before retrying"   # after "UNKNOWN: " on the card
UNKNOWN_MAIL_CARD_TEXT = "check Sent mail before retrying"   # a Reply / Email's
CHECK_SOON_TEXT = "Checking the event with Google..."
CHECK_LATER_TEXT = "Not checked with Google yet"
CHECK_FIRST_NOTE = "Checking the event with Google first - click {button} again once it shows above"
SIGN_IN_FIRST_NOTE = "Finish the Google sign-in in your browser, then check the event above"
SIGNED_IN_NOTE = "Signed in - check the event above, then click {button} again"
NOT_READY_NOTE = "Nothing was sent - see the line above"
# The first Approve on a card whose event Google shows with another title or time than the line
# only says so (the line above shows Google's); the next click goes ahead.
MISMATCH_NOTE = "Google's event has {what} than the briefing - check the line above, then click {button} again"
_MISMATCH_WHAT = {"title": "another title", "time": "another time", "title+time": "another title and time"}
# After a change Google's view is what the change made (not the check from before it).
AFTER_PREFIX = "Google now: "
_ANSWERED = {"yes": "you accepted", "no": "you declined", "maybe": "you said maybe"}
# The end of gcal's unknown-outcome message: the card's UNKNOWN line already says it.
_CHECK_BEFORE_RETRY_RE = re.compile(r"\s*[-;:,]\s*check (?:the calendar |sent mail )?before retrying\.?\s*$",
                                    re.IGNORECASE)
# The check line's short form of an account's sign-in problem (the whole message is its tooltip).
_PROBLEM_LINES = {
    PROBLEM_BLOCKED: "Sign-in blocked by the {alias} account's administrator",
    PROBLEM_EXPIRED: "The {alias} account's Google sign-in expired - Sign in again",
    PROBLEM_DENIED: "Google sign-in for the {alias} account was cancelled or denied",
    PROBLEM_SCOPE: "The {alias} account did not allow Calendar access - Sign in again",
    PROBLEM_TIMEOUT: "Google sign-in for the {alias} account was not finished",
    PROBLEM_IDENTITY: "Not the {alias} account's Google account - Sign in with that one",
}
_PROBLEM_LINE_OTHER = "Google sign-in for the {alias} account failed - Sign in to try again"
_VERBS = {RSVP: "answer", MOVE: "move", CANCEL: "cancel"}
# Sign-in problems of an account (google_auth PROBLEM_*): the card offers Sign in again.
_SIGN_IN_PROBLEMS = (PROBLEM_SIGNED_OUT, PROBLEM_EXPIRED, PROBLEM_BLOCKED, PROBLEM_DENIED, PROBLEM_SCOPE,
                     PROBLEM_TIMEOUT, PROBLEM_IDENTITY)
# A Reply / Email card: its Send signs the account in first (no countdown), opens Edit to confirm
# new recipients or to read a long message, and only then counts down. The card's amber note
# says what the last click on Send needs next.
# The notes never say "above": on a short window the card's FROM line may be scrolled out of view.
MAIL_SIGN_IN_NOTE = "Finish the Google sign-in in your browser; nothing is sent - then check From and click Send again"
MAIL_SIGNED_IN_NOTE = "Signed in - this card sends from {sender}; nothing is sent until you click Send"
CONFIRM_NOTE = "Tick each new recipient in Edit (or remove it), then click Send again"
CONFIRM_BANNER = ("Jarvis has not sent to the red addresses before. Tick \"Send to ...\" for each one you mean "
                  "(or remove it), Save, then click Send again.")
# Added to CONFIRM_BANNER when the card does not show all of the message either.
CONFIRM_READ_TOO = "The card doesn't show all of the message: read it here to its end too."
REVIEW_NOTE = "Read the whole message in Edit, then click Send again"
REVIEW_BANNER = ("The card doesn't show all of this message. Read all of it here, to its end; Save keeps any "
                 "change. Nothing is sent until you click Send again.")
FROM_CHANGED_NOTE = ("The sending account changed during the countdown; nothing was sent - check From and click "
                     "Send again")
# A new binding (an alias's first sign-in): is it the right Google account? Nothing is sent or
# changed for the alias until you answer Yes in the dialog (hud.AccountDialog).
ACCOUNT_CONFIRM_NOTE = ("Is this the right Google account for {alias}? Answer in the dialog - nothing is sent or "
                        "changed until you do")
ACCOUNT_CONFIRMED_MAIL_NOTE = "Confirmed - this card sends from {sender}; nothing is sent until you click Send"
ACCOUNT_CONFIRMED_NOTE = "Confirmed - check the event above, then click {button} again"
ACCOUNT_NOT_CONFIRMED_NOTE = ("Not confirmed yet: click {button} to say whether this is the right Google account for "
                              "{alias} - nothing is sent or changed until then")
ACCOUNT_CONFIRM_FAILED_NOTE = "Not confirmed: the {alias} account changed meanwhile - click {button} to sign in again"
ACCOUNT_RECONNECT_NOTE = ("Pick the right Google account for {alias} in your browser; nothing is sent - then check "
                          "it and click {button} again")
# The saved sign-in is a Google account Jarvis has no binding for (accounts.json deleted or unreadable):
# the click signs in again, which binds and asks; nothing is changed until then.
ACCOUNT_UNKNOWN_NOTE = ("Jarvis doesn't know which Google account {alias} is signed in as - pick it in your browser; "
                        "nothing is changed until you confirm it")
# A Calendar event's or Todo block's Approve while its account has no confirmed Google account: the
# sign-in and the question come before the countdown (nothing is counted down that can't be added).
ADD_SIGN_IN_FIRST_NOTE = ("Finish the Google sign-in in your browser; nothing is added - then confirm the account "
                          "and click {button} again")
ADD_SIGNED_IN_NOTE = "Signed in - click {button} again to add it"
ACCOUNT_CONFIRMED_ADD_NOTE = "Confirmed - click {button} again to add it"
# "No, use another account" when the saved sign-in can't be deleted: the account stays as it was.
DISCONNECT_FAILED_NOTE = "{message} - click {button} to answer again"
_ACCOUNT_NOTE_PREFIXES = tuple(text.split("{", 1)[0] for text in (
    ACCOUNT_CONFIRM_NOTE, ACCOUNT_CONFIRMED_NOTE, ACCOUNT_NOT_CONFIRMED_NOTE, ACCOUNT_CONFIRM_FAILED_NOTE,
    ACCOUNT_RECONNECT_NOTE, ACCOUNT_UNKNOWN_NOTE, ADD_SIGN_IN_FIRST_NOTE, ADD_SIGNED_IN_NOTE,
    ACCOUNT_CONFIRMED_ADD_NOTE)) + (DISCONNECT_FAILED_MESSAGE.split("{", 1)[0],)
MAIL_OFF_NOTE = "Google is turned off in config.toml ([calendar] enabled = false) - use {tools} instead"
# A Reply / Email that failed was certainly not sent: the result line says so; the reason is the note.
MAIL_FAILED_TEXT = "Nothing was sent"
_NOTHING_SENT_RE = re.compile(r"\s*;\s*nothing was sent\b.*$", re.IGNORECASE | re.DOTALL)
# A second click on the same card's Open this soon after the first opens nothing (no double tabs).
_SOURCE_OPEN_GUARD_S = 1.0
# The link next to BLOCK ADDED: the tools row may already have an "Open" (the Todo's own link).
_BLOCK_LINK_TEXT = "Open event"
# A key=value proposal's title (a subject can be 250 characters) in an ACTIVITY row; the card's
# tooltip has all of it.
_ACTIVITY_TITLE_CHARS = 80
# An Approve / Deny click this soon after the previous one (on any card) is
# ignored: a double click, or repeated clicks while the cards are updating,
# must never decide a second proposal.
_DECISION_CLICK_GUARD_S = 1.0
# While an approval (or a Connect sign-in) runs, the other cards are locked.
LOCK_TIP = "Finishing the previous approval..."
SIGN_IN_LOCK_TIP = "Finishing the Google sign-in..."
_LOCKED_MESSAGE = "Ignored an approval click: the previous approval is still running"
_SIGN_IN_LOCKED_MESSAGE = "Ignored an approval click: the Google sign-in is still running"

# The TODAY / TOMORROW agenda and DEADLINES (left column of the reading screen).
_AGENDA_TICK_MS = 60_000            # re-check "now" / "in 20 min" while the reading screen is visible
_AGENDA_REFRESH_S = 600.0           # ask Google again after this long (only while visible)
_AGENDA_INIT = "init"
_AGENDA_LOADING = "loading"
_AGENDA_OK = "ok"
_AGENDA_OFF = "off"                 # [calendar] enabled = false
_AGENDA_SETUP = "setup"             # no client secret file or Google packages
_AGENDA_SIGNED_OUT = "signedout"
_AGENDA_ERROR = "error"
AGENDA_OFF_TEXT = "Google Calendar is turned off (config.toml)"
AGENDA_SETUP_TEXT = "Google Calendar is not set up (README step 8)"
AGENDA_CONNECT_TEXT = "Connect Google Calendar to see your day"
AGENDA_CONNECT_LINK = "Connect"
AGENDA_SIGN_IN_TEXT = "Waiting for Google sign-in..."
AGENDA_LOADING_TEXT = "Loading..."
AGENDA_ERROR_TEXT = "Couldn't load the calendar"
AGENDA_EMPTY_TEXT = "Nothing on the calendar"
# Worker answers for the agenda ("" = the events came back).
_PROBLEM_SETUP = "setup"
_PROBLEM_SIGNED_OUT = "signedout"
_PROBLEM_ERROR = "error"
# Calendar worker job and stages for the agenda's Connect link.
_CONNECT_JOB = "connect"
_CONNECT_SIGN_IN = "signin"         # the browser sign-in is open
_CONNECT_SIGNED_IN = "signedin"     # signed in just now
_CONNECT_READY = "ready"            # was signed in already
_CONNECT_FAILED = "failed"
_CONNECT_KEPT = "kept"              # "No, use another account" could not forget the old sign-in


@dataclasses.dataclass(frozen=True)
class _AgendaJob:
    """One list_events call on the action worker (the range covers the agenda and the deadlines)."""

    request_id: int
    start: datetime
    end: datetime
    calendar_ids: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class _ExecJob:
    """Carry out one approved proposal (executor.run_action). ``writes_running``: its undo
    countdown ran out; "running" is saved right before its call to Google and the result right
    after, by the worker. ``expected_from``: a Reply / Email goes out only from this address (the
    one its card showed when the countdown started)."""

    action: ProposedAction
    writes_running: bool
    expected_from: str = ""
    live: Any = None                    # the card's LIVE view task (live.LiveTask), None without one


@dataclasses.dataclass(frozen=True)
class _PeekJob:
    """Read the events of these cards for their check lines (never signs in)."""

    request_id: int
    actions: tuple[ProposedAction, ...]


@dataclasses.dataclass(frozen=True)
class _ConnectJob:
    """The browser sign-in of one account (the agenda's Connect, a card's Sign in or Approve).
    ``action``: a Reply / Email whose Send signs its account in (executor.sign_in: sending must
    be allowed and the Google account known afterwards). ``disconnect``: forget the account's
    sign-in and binding first ("No, use another account"). ``force``: sign in even when the
    account is signed in already (Ask Jarvis's "Allow <alias> mail": one more consent that asks
    for every feature's permission, reading email included)."""

    alias: str
    action: ProposedAction | None = None
    disconnect: bool = False
    force: bool = False


class _Bridge(QObject):
    """Signals emitted by worker threads; connected queued, so slots run on the GUI thread."""

    fetchAttempt = Signal(int, object, object, int)   # fetch_id, Briefing | None, NotionError | None, attempt
    fetchDone = Signal(int, object)                   # fetch_id, PollResult
    ttsResult = Signal(int, int, object, object)      # generation, section index, SectionAudio | None, str | None
    calendarStatus = Signal(bool)                     # the personal account is signed in to Google Calendar
    accountStatus = Signal(object)                    # {alias: (signed in, PROBLEM_* or "", message)}
    actionProgress = Signal(str, str)                 # action id, _STAGE_*
    actionResult = Signal(str, object, object, str)   # action id, ExecResult | None, ExecError | None, status
    eventPeek = Signal(int, object)                   # request id, {action id: (EventDetails | None, error | None)}
    agendaResult = Signal(int, object, str, str)      # request id, events | None, _PROBLEM_* or "", message
    accountConnect = Signal(str, str, str, str)       # alias, _CONNECT_* stage, failure message, PROBLEM_*
    sayResult = Signal(int, object, object)           # utterance seq, SectionAudio | None, error | None ("tts-say")


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _emit_from_worker(bridge: _Bridge, signal_name: str, stop: threading.Event, *args: Any) -> None:
    """Emit a bridge signal from a worker thread unless stopped or the bridge is already gone."""
    if stop.is_set():
        return
    try:
        getattr(bridge, signal_name).emit(*args)
    except RuntimeError:
        pass   # the bridge was deleted during shutdown


def _run_fetch(bridge: _Bridge, fetch_id: int, stop: threading.Event,
               fetch: Callable[[], Briefing], poll_options: dict[str, Any]) -> None:
    """Body of a fetch thread: poll Notion and report only through the bridge."""
    def on_attempt(briefing: Briefing | None, _freshness: Any, error: NotionError | None,
                   attempt: int) -> None:
        _emit_from_worker(bridge, "fetchAttempt", stop, fetch_id, briefing, error, attempt)

    try:
        result = poll_for_briefing(fetch, wait=stop.wait, on_attempt=on_attempt, **poll_options)
    except Exception as exc:  # noqa: BLE001 - poll_for_briefing guards fetch(); this is a last resort
        logger.exception("Fetching the briefing failed unexpectedly")
        error = NotionError(f"Unexpected error while fetching the briefing ({type(exc).__name__}).")
        result = PollResult(briefing=None, freshness=None, error=error, attempts=0,
                            timed_out=False, stopped=False)
    _emit_from_worker(bridge, "fetchDone", stop, fetch_id, result)


class _ActionWorker:
    """Runs Google calls one at a time on a daemon thread named "calendar".

    Jobs: a sign-in check (the header chip and the cards' Sign in links), an
    approved proposal whose undo countdown ran out (``_ExecJob`` through
    executor.run_action: a Calendar event, a Todo's block, an RSVP, Move or
    Cancel signs its account in first when needed; then "running" is saved
    right before the one call and the result right after, on this thread, so
    the result is kept even when the app quits meanwhile; a Reply or Email
    goes out only when its account still is the one its card showed), reading
    the agenda and the cards' events (never a sign-in), and the browser
    sign-in of one account (a Reply / Email's through executor.sign_in). One
    at a time, so nothing is read while a sign-in runs. Results only go
    through the bridge.
    """

    def __init__(self, executor: Executor, calendars: Mapping[str, Any], store: ActionStore,
                 bridge: _Bridge, stop: threading.Event) -> None:
        self._executor = executor
        self._calendars = dict(calendars)
        self._calendar = self._calendars.get(DEFAULT_ACCOUNT)
        self._store = store
        self._bridge = bridge
        self._stop = stop
        self._idle = threading.Event()
        self._idle.set()
        self._taking = threading.Lock()   # taking a job and marking the worker busy happen together
        self._jobs: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, name="calendar", daemon=True)
        self._thread.start()

    def check_sign_in(self) -> None:
        self._jobs.put(_SIGN_IN_CHECK)

    def approve(self, action: ProposedAction) -> None:
        """A Calendar event or a Todo's block the older way (no countdown, nothing saved before
        the call). The app itself always uses execute."""
        self._jobs.put(_ExecJob(action, writes_running=False))

    def execute(self, action: ProposedAction, expected_from: str = "", live: Any = None) -> None:
        """A proposal whose countdown ran out: "running", the one call, the result. A Reply /
        Email goes out only from ``expected_from``. ``live``: its LIVE view task (finished here)."""
        self._jobs.put(_ExecJob(action, writes_running=True, expected_from=expected_from, live=live))

    def list_agenda(self, job: _AgendaJob) -> None:
        self._jobs.put(job)

    def peek(self, job: _PeekJob) -> None:
        self._jobs.put(job)

    def connect(self, alias: str = DEFAULT_ACCOUNT, action: ProposedAction | None = None, *,
                disconnect: bool = False, force: bool = False) -> None:
        self._jobs.put(_ConnectJob(alias, action, disconnect, force))

    def stop(self) -> None:
        self._jobs.put(None)

    def wait_idle(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for the job that is running (if any) to finish.

        Call it after ``stop`` was set: a job taken from the queue before that is then
        either running (and waited for) or never started.
        """
        with self._taking:
            pass
        return self._idle.wait(timeout)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            with self._taking:
                if job is None or self._stop.is_set():
                    return
                self._idle.clear()
            try:
                self._dispatch(job)
            except Exception as exc:  # noqa: BLE001 - a bug must not end the worker
                logger.error("The action worker failed unexpectedly (%s)", type(exc).__name__)
                logger.debug("Where:\n%s", "".join(traceback.format_tb(exc.__traceback__)))
            finally:
                self._idle.set()

    def _dispatch(self, job: Any) -> None:
        if isinstance(job, _AgendaJob):
            self._list(job)
        elif isinstance(job, _ExecJob):
            self._exec(job)
        elif isinstance(job, _PeekJob):
            self._peek(job)
        elif isinstance(job, _ConnectJob):
            if job.disconnect:
                failure = self._disconnect(job.alias)
                if failure:   # the wrong account's sign-in is still there: no new sign-in over it
                    self._emit("accountConnect", job.alias, _CONNECT_KEPT, failure, PROBLEM_FAILED)
                    return
            if job.action is not None:
                self._connect_mail(job.alias, job.action)
            else:
                self._connect(job.alias, force=job.disconnect or job.force)
        elif job == _SIGN_IN_CHECK:
            self._check_sign_ins()

    def _check_sign_ins(self) -> None:
        """Which accounts have a usable saved sign-in, and why not (files only, no network)."""
        statuses: dict[str, tuple[bool, str, str]] = {}
        for alias, calendar in self._calendars.items():
            problem, message = "", ""
            getter = getattr(calendar, "sign_in_problem", None)
            if callable(getter):
                try:
                    problem, message = getter()
                except Exception as exc:  # noqa: BLE001 - only decides what a card says
                    logger.debug("Could not read the sign-in problem (%s)", type(exc).__name__)
            statuses[alias] = (self._signed_in(calendar), problem or "", message or "")
        self._emit("accountStatus", statuses)
        if self._calendar is not None:
            self._emit("calendarStatus", statuses[DEFAULT_ACCOUNT][0])

    def _list(self, job: _AgendaJob) -> None:
        """Read the events for the agenda; a missing sign-in is reported, never started."""
        problem, message, events = "", "", None
        try:
            if self._calendar is None:
                raise CalendarSetupError("Google Calendar is not set up")
            events = list(self._calendar.list_events(job.start, job.end, job.calendar_ids))
        except CalendarSetupError as exc:
            problem, message = _PROBLEM_SETUP, str(exc)
        except CalendarAuthError as exc:   # CalendarNotSignedIn included
            problem, message = _PROBLEM_SIGNED_OUT, str(exc)
        except CalendarError as exc:   # gcal already logged it, with a safe message
            problem, message = _PROBLEM_ERROR, str(exc) or type(exc).__name__
        except Exception as exc:  # noqa: BLE001 - a bug must not end the worker
            logger.warning("Reading the calendar failed unexpectedly (%s)", type(exc).__name__)
            logger.debug("Where:\n%s", "".join(traceback.format_tb(exc.__traceback__)))
            problem, message = _PROBLEM_ERROR, f"unexpected error ({type(exc).__name__})"
        self._emit("agendaResult", job.request_id, events, problem, message)

    def _peek(self, job: _PeekJob) -> None:
        """Google's own view of each card's event, for its check line (never signs in)."""
        results: dict[str, tuple[Any, BaseException | None]] = {}
        for action in job.actions:
            if self._stop.is_set():
                return
            try:
                results[action.id] = (self._executor.peek(action), None)
            except (CalendarError, ExecError) as exc:   # a safe message (gcal logged a failure)
                results[action.id] = (None, exc)
            except Exception as exc:  # noqa: BLE001 - a bug must not end the worker
                logger.warning("Checking an event failed unexpectedly (%s)", type(exc).__name__)
                logger.debug("Where:\n%s", "".join(traceback.format_tb(exc.__traceback__)))
                results[action.id] = (None, CalendarError(f"unexpected error ({type(exc).__name__})"))
        self._emit("eventPeek", job.request_id, results)

    def _disconnect(self, alias: str) -> str:
        """Forget ``alias``'s saved sign-in and which Google account it is (files only), so the sign-in
        that follows may pick another account. "" when done; else why not (the saved sign-in could
        not be deleted: the binding stays as it was, unconfirmed, and no sign-in may follow)."""
        try:
            self._executor.disconnect(alias)
        except ExecError as exc:   # executor logged it; a safe message
            return str(exc) or DISCONNECT_FAILED_MESSAGE.format(alias=alias)
        except Exception as exc:  # noqa: BLE001 - a bug must not end the worker
            logger.warning("Could not disconnect the %s account (%s)", logged_alias(alias), type(exc).__name__)
            return DISCONNECT_FAILED_MESSAGE.format(alias=alias)
        logger.info("Disconnected the %s account's Google account (it was not the right one)", logged_alias(alias))
        return ""

    def _connect(self, alias: str, *, force: bool = False) -> None:
        """One account's browser sign-in, unless a usable sign-in of a Google account Jarvis knows is
        saved already. ``force``: always (right after "No, use another account")."""
        calendar = self._calendars.get(alias)
        if calendar is None:
            self._emit("accountConnect", alias, _CONNECT_FAILED, CALENDAR_SETUP_NOTE, "")
            return
        if not force and self._signed_in(calendar) and not self._account_unknown(calendar):
            self._emit("accountConnect", alias, _CONNECT_READY, "", "")
            return
        self._emit("accountConnect", alias, _CONNECT_SIGN_IN, "", "")
        try:
            calendar.sign_in()
        except CalendarError as exc:   # gcal already logged it, with a safe message
            self._emit("accountConnect", alias, _CONNECT_FAILED, str(exc) or type(exc).__name__,
                       getattr(exc, "problem", "") or "")
            return
        except Exception as exc:  # noqa: BLE001 - a bug must not end the worker
            logger.error("Google sign-in failed unexpectedly (%s)", type(exc).__name__)
            logger.debug("Where:\n%s", "".join(traceback.format_tb(exc.__traceback__)))
            self._emit("accountConnect", alias, _CONNECT_FAILED, f"unexpected error ({type(exc).__name__})", "")
            return
        self._emit("accountConnect", alias, _CONNECT_SIGNED_IN, "", "")

    def _connect_mail(self, alias: str, action: ProposedAction) -> None:
        """A Reply / Email's Send on an account that can't send yet: its browser sign-in (every
        feature of the account, and which Google account it is)."""
        self._emit("accountConnect", alias, _CONNECT_SIGN_IN, "", "")
        try:
            self._executor.sign_in(action)
        except ExecError as exc:   # a safe message (google_auth / gmail logged the failure)
            self._emit("accountConnect", alias, _CONNECT_FAILED, str(exc) or type(exc).__name__,
                       exc.problem or PROBLEM_FAILED)
            return
        except Exception as exc:  # noqa: BLE001 - a bug must not end the worker
            logger.error("Google sign-in failed unexpectedly (%s)", type(exc).__name__)
            logger.debug("Where:\n%s", "".join(traceback.format_tb(exc.__traceback__)))
            self._emit("accountConnect", alias, _CONNECT_FAILED, f"unexpected error ({type(exc).__name__})",
                       PROBLEM_FAILED)
            return
        self._emit("accountConnect", alias, _CONNECT_SIGNED_IN, "", "")

    @staticmethod
    def _signed_in(calendar: Any) -> bool:
        try:
            return bool(calendar.is_signed_in())
        except Exception as exc:  # noqa: BLE001 - only decides whether to sign in first
            logger.debug("Could not check the Google sign-in (%s)", type(exc).__name__)
            return False

    @staticmethod
    def _account_unknown(calendar: Any) -> bool:
        """The saved sign-in is a Google account Jarvis has no binding for (accounts.json deleted,
        edited or unreadable): a sign-in click signs in again, which binds and asks."""
        getter = getattr(calendar, "change_problem", None)
        if not callable(getter):
            return False
        try:
            return getter()[0] == PROBLEM_IDENTITY
        except Exception as exc:  # noqa: BLE001 - only decides whether to sign in; changes are refused anyway
            logger.debug("Could not check which Google account the sign-in is (%s)", type(exc).__name__)
            return False

    def _exec(self, job: _ExecJob) -> None:
        action = job.action
        task = job.live if job.live is not None else _live.NO_TASK

        def on_stage(stage: str) -> None:
            self._emit("actionProgress", action.id, stage)

        if job.writes_running and action.sends_mail:
            problem = _mail_from_problem(self._executor, action, job.expected_from)
            if problem:   # never from another account than the card showed: nothing is sent
                logger.info("Action %s (%s, %s) not sent: the sending account is not the one the card showed",
                            action.id, action.kind, logged_alias(action.account))
                self._store.set(action.id, STATUS_FAILED, message=problem, kind=action.kind, account=action.account)
                _live_exec_refused(task, problem)
                self._emit("actionResult", action.id, None, ExecError(problem), STATUS_FAILED)
                return
        extra = {"live": job.live} if job.live is not None else {}
        outcome = run_action(self._executor, action, store=self._store if job.writes_running else None,
                             writes_running=job.writes_running, on_stage=on_stage,
                             proceed=lambda: not self._stop.is_set(), **extra)
        _live_exec_finished(task, action, outcome)
        self._emit("actionResult", action.id, outcome.result, outcome.error, outcome.status)

    def _emit(self, signal_name: str, *args: Any) -> None:
        _emit_from_worker(self._bridge, signal_name, self._stop, *args)


_CalendarWorker = _ActionWorker   # its name before it also answered, moved and cancelled events


def _mail_from_problem(executor: Executor, action: ProposedAction, expected: str) -> str:
    """Why a Reply / Email may not go out now from ``expected`` (the address its card showed when
    the countdown started): "" when the account is ready and still that address."""
    try:
        status = executor.mail_status(action)
    except Exception as exc:  # noqa: BLE001 - treated as not ready: nothing is sent
        logger.debug("Could not check the sending account (%s)", type(exc).__name__)
        return FROM_CHANGED_NOTE
    if not expected or status.sender.casefold() != expected.casefold():
        return FROM_CHANGED_NOTE
    if not status.ready:
        return status.note or FROM_CHANGED_NOTE
    return ""


def _default_calendars(config: Config) -> dict[str, GoogleCalendar]:
    """One GoogleCalendar per account (executor.build_calendars): "personal" (the agenda,
    Calendar proposals, to-do blocks) and every [accounts.<alias>] with the calendar feature.
    The single google_token.json of older versions becomes the personal account's sign-in."""
    return build_calendars(config)


def _default_calendar(config: Config) -> GoogleCalendar:
    """The personal account's calendar."""
    return _default_calendars(config)[DEFAULT_ACCOUNT]


def _agenda_fetch_range(now: datetime, agenda: AgendaConfig) -> tuple[datetime, datetime]:
    """One list_events range for both panels: today 00:00 to the end of the agenda day or the
    deadline horizon, whichever is later."""
    _, start, end = agenda_window(now, agenda.evening_from_hour)
    _, today, _ = agenda_window(now, 24)   # 24: always today's bounds
    return min(start, today), max(end, now + timedelta(days=agenda.deadline_days))


def _agenda_rows(events: Sequence[CalendarEvent], now: datetime, start: datetime,
                 end: datetime, hour24: bool = False) -> list[hud.AgendaRowInfo]:
    return [hud.AgendaRowInfo(row.time_text, row.title, row.meta, row.state)
            for row in event_rows(events_in_window(events, start, end), now, hour24=hour24)]


def _deadline_rows(deadlines: Sequence[Deadline], now: datetime) -> list[hud.DeadlineRowInfo]:
    rows = []
    for item in deadlines:
        text, urgency = due_label(item.due, now)
        rows.append(hud.DeadlineRowInfo(item.title, item.source, text, urgency))
    return rows


def _next_days(days: int) -> str:
    """"next 14 days" / "next day" (the empty DEADLINES line)."""
    return "next day" if days == 1 else f"next {days} days"


def _normalize_run(value: Any) -> str | None:
    text = str(value).strip().upper() if value else ""
    return text if text in ("AM", "PM") else None


def _launch_kind(launch: str | None, *, now_mode: bool, ask_mode: bool, expected_run: str | None,
                 classic: bool = False) -> str:
    """``launch`` when it is one of LAUNCH_KINDS; else derived from the older arguments: ``now_mode``
    READ, ``ask_mode`` OPEN, an expected run SCHEDULED, nothing OPEN (in the classic mode, where a
    plain start used to show the prompt, nothing is SCHEDULED: the prompt without a run, as before).
    The app always passes ``launch``; the derivation keeps older constructors working."""
    if launch in LAUNCH_KINDS:
        return str(launch)
    if launch is not None:
        logger.warning("Unknown launch kind %r; deriving it from the other arguments", str(launch)[:20])
    if now_mode:
        return LAUNCH_READ
    if ask_mode:
        return LAUNCH_OPEN
    return LAUNCH_SCHEDULED if expected_run or classic else LAUNCH_OPEN


def _ready_headline(run: str | None) -> str:
    return f"Your {run} briefing is ready. Hear it now?" if run else "Your briefing is ready. Hear it now?"


def _split_startup_error(error: str) -> tuple[str, str]:
    """(headline, detail): the first sentence of a startup error and the rest.

    "Notion page ID missing. Add BRIEFING_PAGE_ID ..." -> ("Notion page ID missing.", "Add BRIEFING_PAGE_ID ...").
    """
    head, separator, rest = error.partition(". ")
    if not separator:
        return error, ""
    return head + ".", rest.strip()


def _startup_error_chip(error: str) -> str:
    """"Notion: token missing (see README)" for the header's notion chip."""
    headline = _split_startup_error(error)[0].rstrip(".")
    return f"Notion: {headline.removeprefix('Notion ')} (see README)"


def _describe_poll(result: PollResult) -> str:
    if result.stopped:
        return "stopped"
    if result.timed_out:
        return "gave up waiting for a fresh briefing"
    if result.error is not None:
        return f"error: {result.error}"
    return "fresh" if result.freshness is not None and result.freshness.fresh else "done"


def _clock_text(moment: datetime, now: datetime, hour24: bool = False) -> str:
    """"10:04 AM" (or "10:04" with ``hour24``) in ``now``'s time zone, like the HUD clock."""
    try:
        local = moment.astimezone(now.tzinfo) if now.tzinfo is not None else moment
    except (TypeError, ValueError, OverflowError):
        local = moment
    return format_time(local, hour24=hour24)


def _short_updated(header: BriefingHeader, now: datetime, hour24: bool = False, *,
                   compact: bool = False) -> str:
    """"today 10:04 AM", "yesterday 11:31 PM", "Oct 2 9:00 AM" or "not yet" (STATUS telemetry).

    ``hour24``: "today 10:04", "yesterday 23:31", "Oct 2 09:00". ``compact``
    names yesterday by its weekday ("Mon 11:31 PM") and an older day by its
    date alone ("Oct 2"), for a narrow STATUS column.
    """
    updated = header.updated_at
    if updated is None:
        return "not yet"
    try:
        local = updated.astimezone(now.tzinfo) if now.tzinfo is not None else updated
        days = (now.date() - local.date()).days
    except (TypeError, ValueError, OverflowError):
        return "unknown"
    clock = format_time(local, hour24=hour24)
    if days == 0:
        return f"today {clock}"
    if days == 1:
        return f"{_WEEKDAYS[local.weekday()]} {clock}" if compact else f"yesterday {clock}"
    day = f"{_MONTHS[local.month - 1]} {local.day}"
    return day if compact else f"{day} {clock}"


def _activity_title(action: ProposedAction) -> str:
    """``action``'s title for an ACTIVITY row: cut to one line for key=value kinds (Calendar as before)."""
    return _short(action.title, _ACTIVITY_TITLE_CHARS) if action.structured else action.title


def _card_options(action: ProposedAction, view: CardView) -> dict[str, Any]:
    """ActionCard keyword arguments for ``view`` (a CardView of ``action``)."""
    options: dict[str, Any] = {"approve_text": view.approve_text or "Approve", "body": view.body,
                               "open_text": view.open_text, "copy_text": view.copy_text,
                               "title_lines": 2 if action.structured else 0, "deny_text": view.deny_text}
    if action.sends_mail:
        # A Reply / Email: FROM and the recipients' chips, the text as sent (line breaks kept,
        # links highlighted, 10 lines), Edit; the right-hand button reads Send, Retry or Done.
        options.update(edit_text=EDIT_LINK_TEXT, mail=True, body_lines=view.body_lines,
                       body_links=body_links(action.body), approve_alternatives=(SEND_TEXT, DONE_TEXT))
        return options
    if view.editable:
        # An RSVP, Move or Cancel: the check line, and Edit in a place that shows Sign in instead
        # while its account is blocked, or Google's own "Open event" when the line has no link.
        options.update(edit_text=EDIT_LINK_TEXT, check_line=view.check, sign_in_text=SIGN_IN_LINK_TEXT,
                       event_text="" if view.open_text else hud.OPEN_EVENT_TEXT)
    return options


def _review_digest(subject: str, body: str) -> str:
    """What a review in the Edit dialog covered: the subject and the message as the card has them
    (whitespace as an edit keeps it). A digest, in memory only."""
    subject = " ".join(str(subject or "").split())
    body = str(body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(f"{subject}\0{body}".encode("utf-8", "surrogatepass")).hexdigest()


def _for_button(note: str, button: str) -> str:
    """A Reply / Email note's "click Send ..." in the words of the button the card shows (Retry)."""
    return re.sub(r"\bclick Send\b", f"click {button}", note) if button and button != SEND_TEXT else note


@dataclasses.dataclass
class _Countdown:
    """The undo countdown of one approved card; nothing is sent before it runs out."""

    action: ProposedAction       # exactly what will be carried out (the Edit dialog's changes included)
    deadline: float              # time.monotonic() when it runs out
    started: float               # time.monotonic() of the click that started it (Undo's double-click guard)
    shown: int = -1              # the seconds the card shows
    sender: str = ""             # a Reply / Email: the From address its card showed (it goes out only from it)
    task: Any = _live.NO_TASK    # its LIVE view task (live.LiveTask)


def _action_phrase(action: ProposedAction) -> str:
    """"Accept Speaker series", "Move Project sync", "Cancel Study group", "Add Project sync",
    "Add a block for Problem set 3", or a Reply's / Email's subject (activity rows)."""
    title = _activity_title(action)
    if action.kind == CALENDAR:
        return f"Add {title}"
    if action.kind == TODO:
        return f"Add a block for {title}"
    if action.kind in (REPLY, EMAIL):
        return _kind_title(action)
    if action.kind == RSVP:
        verb = {"yes": "Accept", "no": "Decline", "maybe": "Answer maybe to"}.get(action.field("answer"), "Answer")
        return f"{verb} {title or 'an invitation'}"
    verb = "Move" if action.kind == MOVE else "Cancel"
    return f"{verb} {title or 'a meeting'}"


def _kind_title(action: ProposedAction) -> str:
    """``action``'s title for an ACTIVITY row, or what it is when the line has no title."""
    title = _activity_title(action)
    if title:
        return title
    return {RSVP: "an invitation", MOVE: "a meeting", CANCEL: "a meeting", REPLY: "a reply",
            EMAIL: "an email"}.get(action.kind, action.kind)


def _card_detail(action: ProposedAction, view: CardView, today: Any) -> str:
    """The card's detail line: a Reply / Email shows only when it is due ("Due tomorrow"), since
    its recipients are chips; every other card its CardView detail."""
    if not action.sends_mail:
        return view.detail
    words = due_words(action, today)
    return words[:1].upper() + words[1:]


def _adds_event(action: ProposedAction) -> bool:
    """A Calendar event or a Todo's block (added to the personal calendar after the countdown)."""
    return action.kind in (CALENDAR, TODO)


def _recipient_count(count: int) -> str:
    return f"{count} recipient" + ("" if count == 1 else "s")


def _time_texts(action: ProposedAction, hour24: bool = False) -> tuple[str, str, str]:
    """A Move's new time for the Edit dialog, on the app's clock: ("2026-10-08", "2:00 PM",
    "3:00 PM"), or "14:00" / "15:00" with ``hour24``; empty otherwise."""
    if action.kind != MOVE or action.start is None or action.end is None:
        return "", "", ""

    def clock(moment: datetime) -> str:
        if hour24:
            return moment.strftime("%H:%M")
        return f"{(moment.hour % 12) or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"

    return action.start.date().isoformat(), clock(action.start), clock(action.end)


def _remove_audio_dir_after(workers: threading.Thread | Sequence[threading.Thread] | None, audio_dir: Path) -> None:
    """Delete ``audio_dir`` again once the speech workers (the briefing's "tts-worker" and Jarvis's
    own "tts-say") have finished the job they are running.

    After stop() a worker still completes its current job, which writes
    into (and may recreate) the folder after shutdown removed it, and the media
    players can keep the last file open for a moment after they stopped. This
    non-daemon thread keeps the process alive for at most
    _AUDIO_CLEANUP_WAIT_S + _AUDIO_RELEASE_WAIT_S so no file outlives the app.
    """
    if workers is None:
        threads: list[threading.Thread] = []
    elif isinstance(workers, threading.Thread):
        threads = [workers]
    else:
        threads = list(workers)

    def run() -> None:
        deadline = time.monotonic() + _AUDIO_CLEANUP_WAIT_S
        for worker in threads:
            worker.join(max(0.0, deadline - time.monotonic()))
        deadline = time.monotonic() + _AUDIO_RELEASE_WAIT_S
        shutil.rmtree(audio_dir, ignore_errors=True)
        while audio_dir.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
            shutil.rmtree(audio_dir, ignore_errors=True)
        if audio_dir.exists():
            logger.debug("Could not remove the audio folder %s; the next start cleans it up",
                         audio_dir.name)

    threading.Thread(target=run, name="audio-cleanup", daemon=False).start()


def _remove_old_sessions(root: Path, keep: Path) -> None:
    """Best-effort removal of audio folders left behind by earlier runs (older than 12 h)."""
    try:
        candidates = [path for path in root.glob("session-*") if path.is_dir() and path != keep]
        for path in candidates:
            if time.time() - path.stat().st_mtime > _SESSION_MAX_AGE_S:
                shutil.rmtree(path, ignore_errors=True)
                logger.debug("Removed old audio folder %s", path.name)
    except OSError as exc:
        logger.debug("Could not clean old audio folders: %s", exc)


# --------------------------------------------------------------------------
# The LIVE view's steps of approved cards and briefing fetches (on screen only; quiet)
# --------------------------------------------------------------------------

_LIVE_KIND_WORDS = {CALENDAR: "Add event", TODO: "Add block", RSVP: "RSVP", MOVE: "Move", CANCEL: "Cancel",
                    REPLY: "Reply", EMAIL: "Email"}


def _live_action_title(action: ProposedAction) -> str:
    """"Reply work: Re: Budget", "Move work: Project sync", "Add event: Dentist"."""
    word = _LIVE_KIND_WORDS.get(action.kind, action.kind.capitalize())
    title = action.title or _kind_title(action)
    if action.kind in (CALENDAR, TODO):
        return f"{word}: {title}"
    return f"{word} {action.account}: {title}" if action.account else f"{word}: {title}"


@_live.quiet()
def _live_countdown_ran_out(countdown: _Countdown) -> None:
    verb = "adding" if _adds_event(countdown.action) else "sending"
    countdown.task.find(_live.ACTION_COUNTDOWN).done(_live.STATUS_OK, summary=f"ran out - {verb} now")


@_live.quiet()
def _live_countdown_end(countdown: _Countdown, status: str, word: str, summary: str, *,
                        task_summary: str = "") -> None:
    """The countdown ended without a call (Undo, a stop, the sending account changed, Jarvis closed)."""
    task = countdown.task
    task.find(_live.ACTION_COUNTDOWN).done(status, status_text=word, summary=summary)
    task.finish(status, status_text=word, summary=task_summary or summary)


def _without_ids(text: str) -> str:
    """A fetch error for the LIVE view: any Notion page or block id it quotes is left out."""
    return _NOTION_ID_RE.sub("[id not shown]", text)


@_live.quiet()
def _live_fetch_task(stream: _live.LiveStream, poll_run: str | None) -> Any:
    """A briefing fetch's compact LIVE view task (the page id is never shown: it is in .env)."""
    task = stream.task(_live.TASK_BRIEFING, f"{poll_run} briefing from Notion" if poll_run else "Briefing from Notion",
                       compact=True)
    fields = [("Waiting for", f"the {poll_run} run")] if poll_run else []
    task.step(_live.BRIEFING_FETCH, "Read the briefing page in Notion (read only)", fields=fields)
    return task


@_live.quiet()
def _live_exec_refused(task: Any, problem: str) -> None:
    task.step(_live.ACTION_CHECKS, "Last checks before the call", status=_live.STATUS_BLOCKED, summary=problem)
    task.finish(_live.STATUS_BLOCKED, summary=problem)


@_live.quiet()
def _live_exec_finished(task: Any, action: ProposedAction, outcome: Any) -> None:
    """The approved card's LIVE view task ends as run_action did."""
    status = outcome.status
    if status in (STATUS_CREATED, STATUS_EXISTS, STATUS_SENT) and outcome.result is not None:
        words = outcome.result.result_text or result_text(action, status) or {
            STATUS_CREATED: "Added", STATUS_EXISTS: "Already on the calendar"}.get(status, "Done")
        task.finish(_live.STATUS_OK, summary=words)
    elif status == STATUS_UNKNOWN:
        task.finish(_live.STATUS_WARN, status_text="UNKNOWN",
                    summary="it may have happened - check Sent mail / the calendar before retrying")
    else:
        task.finish(_live.STATUS_FAILED, summary=str(outcome.error or "") or "it did not happen")


class AppController(QObject):
    """State machine behind the window: prompt -> (snoozed ->) reading -> quit.

    A launch for a newer run takes the reading screen back to the prompt once
    nothing is playing there. Proposed actions are shown as cards. Every card
    Jarvis carries out starts an undo countdown first (``[actions]
    undo_seconds``; Undo sends nothing) and only its end queues the one call:
    Approve creates an event, Add block a Todo's block, Accept / Decline /
    Maybe, Move and Cancel event answer an invitation, move or cancel an event
    (only after the card shows Google's own view of that event, its check
    line read by id, and that view allows it), and Send sends a Reply or Email
    through Gmail (only once its account is signed in and its card shows From,
    every new recipient was ticked in Edit and a long message was read in
    Edit). "running" is saved before that call; a call that may or may not
    have happened shows UNKNOWN with Retry and is never retried by itself.
    Each account signs in only on a click (a card's Sign in, Approve or Send,
    the agenda's Connect). Edit changes such a card in memory, and what the
    card shows is what is carried out. The other proposals are hand-offs:
    Open (an allowlisted link, only on a click), Copy (the drafted text to the
    clipboard), Done and Deny; nothing is sent.
    ``calendar_factory(config)`` builds the calendar clients (tests inject a
    fake): one calendar (the personal account's) or a mapping alias ->
    calendar; ``sender_factory(config)`` the Gmail senders (a mapping alias ->
    sender). Without either, both are built from one Google account per alias
    (one sign-in each); with only one of them injected, the other is empty
    (tests never reach a real account). Neither is called when ``[calendar]
    enabled`` is false (Google is off). An Approve
    or Deny within ``_DECISION_CLICK_GUARD_S`` of the previous one is ignored;
    ``click_clock`` (seconds, monotonic; default ``time.monotonic``, looked up
    at each click so tests may also patch it) times that. While an approval
    (or the agenda's Connect sign-in) runs, the other cards are locked.

    The reading screen's agenda (TODAY / TOMORROW and the calendar part of
    DEADLINES) is read on the calendar worker when the reading screen opens,
    every 10 minutes while it is visible, after a sign-in and after an
    Approve; it never signs in by itself. ``slots`` and ``run_state`` record
    which scheduled briefing was shown and answered (for the catch-up launch).

    Ask Jarvis (``[ask] enabled``; ask_ui.AskController) adds the command bar to
    the reading screen and a ``claude`` chip to its header. Its proposals are
    cards like the briefing's (an ASK group above the BRIEFING one; one list
    per source, merged for the cards), never read out with the briefing, and
    carried out only through the same click, undo countdown and executor path.
    ``ask_factory(config, calendars, senders, accounts)`` builds the planner
    (tests inject a fake; the app's runs the owner's own Claude Code).
    ``ask_mode`` (``--ask``): open the reading screen with the bar focused,
    without playing the briefing or settling a scheduled slot.

    ``launch`` (models.LAUNCH_*) says how this start was asked for; None derives
    it from the older arguments (``now_mode`` READ, ``ask_mode`` OPEN,
    ``expected_run`` SCHEDULED, else OPEN). OPEN shows the assistant screen on
    its JARVIS tab with a greeting; READ reads at once on the BRIEFING tab;
    SCHEDULED waits invisibly (STATE_WAITING) for the run's fresh briefing,
    then shows it without taking the focus and announces it once ("Sir, your
    AM briefing is ready to view."), or with ``[assistant] scheduled_prompt``
    shows the classic "Hear it now?" prompt. A briefing nobody has viewed is
    NEW (badge, speech line, STATUS, tray) until its BRIEFING tab is seen in
    the active window or it is played; announced and viewed briefings are
    remembered in ``run_state`` (in memory without one). Jarvis's own words go
    to the JARVIS conversation and to ``voice`` (speech.Voice: a SilentVoice
    unless ``voice_factory(controller)`` builds one; the real voice is made by
    ``start()`` only). ``locked()`` says whether the session is locked (an
    announcement waits for the unlock).

    Page pictures of web research (``[live] page_snapshots``, with ``[live]
    enabled``): a snapshot_engine.PageSnapshotter, or what
    ``snapshot_factory(controller)`` builds (tests inject a fake: no web
    engine), is handed to the Ask planner; an Ask's Cancel stops its pictures,
    LIVE's Clear drops the ones nobody would see, and quitting stops them all.
    """

    def __init__(self, config: Config, client: NotionClient, *, expected_run: str | None,
                 now_mode: bool, startup_error: str | None = None, volume: float = 1.0,
                 now_func: Callable[[], datetime] = _local_now,
                 calendar_factory: Callable[[Config], Any] | None = None,
                 sender_factory: Callable[[Config], Any] | None = None,
                 action_store: ActionStore | None = None,
                 click_clock: Callable[[], float] | None = None,
                 slots: Mapping[str, str] | None = None,
                 run_state: RunState | None = None,
                 on_shutdown: Callable[[], Any] | None = None,
                 ask_factory: Callable[..., Any] | None = None,
                 ask_mode: bool = False,
                 launch: str | None = None,
                 voice_factory: Callable[[Any], Voice] | None = None,
                 locked: Callable[[], bool] | None = None,
                 prefs: AssistantPrefs | None = None,
                 snapshot_factory: Callable[[Any], Any] | None = None) -> None:
        super().__init__()
        self.config = config
        self.client = client
        self._on_shutdown = on_shutdown   # e.g. an exit watchdog (the app passes one; tests do not)
        self._snapshot_factory = snapshot_factory   # tests: a fake page snapshotter (no web engine)
        self.expected_run = _normalize_run(expected_run)
        self.now_mode = bool(now_mode)
        self.ask_mode = bool(ask_mode)
        self.launch = _launch_kind(launch, now_mode=self.now_mode, ask_mode=self.ask_mode,
                                   expected_run=self.expected_run, classic=config.assistant.scheduled_prompt)
        self.startup_error = startup_error
        self.state = STATE_IDLE
        self._minimized = False            # the window is minimized (_on_window_state)
        self._now = now_func
        self._click_clock = click_clock
        # Scheduled run times ({"AM": "10:12", "PM": "23:42"}) and where answers are
        # remembered for the catch-up launch; without a run state nothing is recorded.
        self.slots: dict[str, str] = dict(slots or {})
        self.run_state = run_state
        self._closing = threading.Event()
        self._shut_down = False
        self._bridge = _Bridge(self)
        # The LIVE view's event stream ([live]; memory only): created before the Ask controller.
        live_config = getattr(config, "live", None)
        self.live = _live.LiveStream(enabled=bool(getattr(live_config, "enabled", True)),
                                     keep_text=bool(getattr(live_config, "text", True)),
                                     hour24=bool(config.display.hour24),
                                     pictures=bool(getattr(live_config, "pictures", True)))
        self._volume = volume
        self.window = BriefingWindow(config.prompt.later_short_minutes, config.prompt.later_long_minutes)
        self.window.set_hour24(config.display.hour24)
        self.window.header.set_clock(now_func)
        self.player = self._create_player()
        self.tray: QSystemTrayIcon | None = None
        self._tray_menu: QMenu | None = None
        self._tray_quit_action: Any = None
        self._tts: TtsWorker | None = None
        self._audio_dir: Path | None = None
        if action_store is None:
            action_store = ActionStore(config.data_dir / ACTIONS_FILE)
        self._store = action_store
        self._calendars: dict[str, Any]
        self._senders: dict[str, Any]
        self._google_accounts: dict[str, Any] = {}   # build_accounts' (no factories): shared by Ask's readers
        self._calendars, self._senders = self._create_google(calendar_factory, sender_factory)
        self.calendar = self._calendars.get(DEFAULT_ACCOUNT)   # the agenda's and Calendar proposals'
        self._history = recipient_history(config)   # whom Jarvis sent to (recipients.json; read on use)
        self._executor = build_executor(config, self._calendars, self._senders, history=self._history,
                                        now=now_func)
        self._calendar_worker: _ActionWorker | None = None
        self._init_fetch_state()
        self._init_script_state()
        self._init_action_state()
        self._init_agenda_state()
        self._init_talk_state(prefs, locked)
        self._init_timers()
        self._connect_signals()
        self._init_live_view()
        self.ask: Any = self._create_ask(ask_factory)   # ask_ui.AskController, or None while [ask] is off
        # Jarvis's own voice: silent unless injected; start() builds the real one ([assistant] speak).
        self._voice_injected = voice_factory is not None
        self.voice: Voice = voice_factory(self) if voice_factory is not None else SilentVoice()
        self.window.set_speaker_available(bool(config.assistant.speak))
        self._sync_speaker()

    def _init_fetch_state(self) -> None:
        self._fetch_id = 0
        self._fetch_stop: threading.Event | None = None
        self._fetch_task: Any = _live.NO_TASK      # the current fetch's LIVE view task
        self._fetch_poll_run: str | None = None
        self._fetch_attempts = 0                 # attempts finished by the current fetch
        self._poll_deadline = self._now()
        self._briefing: Briefing | None = None
        self._fetch_error: NotionError | None = None
        self._checked_at = -math.inf             # time.monotonic() of the last successful fetch
        # When the current run began waiting: freshness is judged against that day
        # too, so a PM briefing that was fresh at 23:42 is still fresh at 00:12.
        self._run_started = self._now()
        self._logged_fetch = ""                  # content key / error last written to the activity log
        self._logged_error = ""

    def _init_script_state(self) -> None:
        self._prefetch: Script | None = None     # script prepared while the prompt shows
        self._script: Script | None = None       # frozen script on the reading screen
        self._generation = 0
        self._audio: dict[int, SectionAudio] = {}
        self._failed: set[int] = set()
        self._engine: str | None = None
        self._include_ignored = False
        self._has_played = False
        self._note = ""
        self._pending_run: str | None = None    # a newer run that launched during a reading
        self._reading_error = False              # the reading screen shows a load error
        self._audio_ready_logged = False
        self._sections_started: set[int] = set()
        self._last_section: int | None = None    # for "Read <section>" in the activity log
        self._skip_pending = False               # the next section start follows a skip or jump

    def _init_action_state(self) -> None:
        self._actions: list[ProposedAction] = []   # the cards shown: ASK + BRIEFING (_merged_actions)
        # One list per source (memory only; the decisions are saved by id like every card's).
        self._source_lists: dict[str, list[ProposedAction]] = {SOURCE_ASK: [], SOURCE_BRIEFING: []}
        self._ask_busy = False                     # an Ask is planning (the orb says PLANNING)
        self._autoplay = True                      # the reading starts playing once it is shown (not --ask)
        self._reading_settled = False              # this reading answered its scheduled slot (Read now, Play)
        self._actions_key = ""                   # content key of the page the actions came from
        self._actions_lines: tuple = ()
        self._jobs: dict[str, str] = {}            # action id -> _STAGE_* while counting down, queued or running
        self._running: dict[str, tuple[ProposedAction, bool]] = {}   # id -> (what was sent, writes_running)
        self._countdown: _Countdown | None = None  # at most one: the other cards are locked meanwhile
        self._calendar_signed_in = False
        self._decision_clicked_at = -math.inf      # click clock of the last Approve / Deny click
        self._source_opened_at: dict[str, float] = {}   # action id -> click clock of its last Open
        self._connect_alias: str | None = None     # the account whose sign-in is queued or running
        self._connect_card = ""                    # the card whose Sign in / Approve started it
        self._connect_note = ""                    # why the last personal sign-in failed (the agenda line)
        self._accounts_state: dict[str, tuple[bool, str, str]] = {}   # alias -> (signed in, PROBLEM_*, message)
        self._checks: dict[str, EventCheck] = {}   # action id -> Google's view of its event (the check line)
        self._peek_seq = 0
        self._peeking: set[str] = set()            # action ids whose check is on its way
        self._hints: dict[str, str] = {}           # action id -> what its last Approve click needs next
        self._edits: dict[str, ActionEdit] = {}    # action id -> the Edit dialog's changes (memory only)
        self._carried: dict[str, ProposedAction] = {}   # action id -> what Jarvis carried out (this run)
        self._mismatch_seen: dict[str, EventCheck] = {}   # action id -> the check whose mismatch was shown
        self._dialog: hud.EditDialog | None = None
        # The first sign-in's question (is it the right Google account?) and the card that led to it.
        self._account_dialog: hud.AccountDialog | None = None
        self._confirm_card = ""
        self._confirm_action: ProposedAction | None = None
        # Reply / Email id -> _review_digest of the subject and message the Edit dialog showed whole
        # (this run, memory only): Send skips the review only while the card still says exactly that.
        self._reviewed: dict[str, str] = {}
        self._connect_mail = False                 # the queued / running sign-in is a Reply / Email's Send
        self._read_slot: str | None = None         # the scheduled slot this reading settled

    @property
    def _calendar_jobs(self) -> dict[str, str]:
        """The older name of ``_jobs``."""
        return self._jobs

    @property
    def _connect_running(self) -> bool:
        """A sign-in (the agenda's Connect, a card's Sign in or Approve) is queued or running."""
        return self._connect_alias is not None

    def _init_agenda_state(self) -> None:
        self._agenda_status = _AGENDA_INIT
        self._agenda_events: list[CalendarEvent] | None = None   # from the last successful read
        self._agenda_range: tuple[datetime, datetime] | None = None   # what that read covered
        self._agenda_problem = ""                  # reason of the last failure (tooltips only)
        self._agenda_job: _AgendaJob | None = None   # the read on the calendar worker
        self._agenda_again = False                 # read again once that one answers
        self._agenda_seq = 0
        self._agenda_requested_at = -math.inf     # time.monotonic() of the last read
        self._page_deadlines: list[Deadline] = []  # the page's "Deadlines" section

    def _init_talk_state(self, prefs: AssistantPrefs | None, locked: Callable[[], bool] | None) -> None:
        """NEW / announced / viewed, the greeting and the conversation (memory only)."""
        self._prefs = prefs                        # assistant.json; opened on first use (_prefs_store)
        self._locked = locked or session_locked
        self._rng = random.Random()
        self._viewed_keys: set[str] = set()        # briefing keys viewed (also without a run state)
        self._announced_keys: set[str] = set()     # briefing keys announced (also without a run state)
        self._held_key: str | None = None          # an announcement waiting for the unlock
        self._held_text = ""
        self._held_kind = KIND_ANNOUNCE
        self._announce_when_shown = False          # announce the next briefing rendered (late arrival)
        self._arriving = False                     # a scheduled arrival is being shown (announces itself)
        self._greeting_pending = False             # waiting for the first fetch answer (<= 3 s)
        self._greeting_line: Greeting | None = None
        self._greeting_entry: int | None = None
        self._last_greeting_at = -math.inf         # time.monotonic() of this process's last greeting
        self._background_run: str | None = None    # a run polled for while the assistant screen shows
        self._pending_briefing: Briefing | None = None   # a newer briefing waiting for the reading to end
        self._say_worker: SayWorker | None = None  # Jarvis's own speech worker ("tts-say"; start() only)
        self._utterance = ""                       # what Jarvis is saying right now (orb SPEAKING, speech line)
        self._briefing_speech = ""                 # the briefing's line for the speech line
        self._last_ack = ""                        # the last "One moment, sir." template (never twice in a row)

    def _init_timers(self) -> None:
        self._ignore_timer = self._make_timer(self._on_prompt_ignored, single_shot=True)
        self._tick_timer = self._make_timer(self._update_countdown, single_shot=False, interval_ms=1000)
        self._snooze_timer = self._make_timer(self._on_snooze_timeout, single_shot=True)
        self._note_timer = self._make_timer(self._clear_note, single_shot=True)
        self._recheck_timer = self._make_timer(self._on_recheck_timeout, single_shot=True)
        # Only while the reading screen is visible (no timer at all while hidden).
        self._agenda_timer = self._make_timer(self._on_agenda_tick, single_shot=False,
                                              interval_ms=_AGENDA_TICK_MS)
        # Only while a card counts down to its call to Google.
        self._countdown_timer = self._make_timer(self._on_countdown_tick, single_shot=False,
                                                 interval_ms=_COUNTDOWN_TICK_MS)
        # The opening greeting waits at most this long for the first fetch answer.
        self._greeting_timer = self._make_timer(self._finish_greeting, single_shot=True,
                                                interval_ms=_GREETING_WAIT_MS)
        # An announcement held while the session is locked looks again.
        self._held_timer = self._make_timer(self._on_held_timer, single_shot=True, interval_ms=_LOCK_RECHECK_MS)

    def _make_timer(self, slot: Callable[[], None], *, single_shot: bool, interval_ms: int = 0) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(single_shot)
        timer.setInterval(interval_ms)
        if single_shot:
            timer.setTimerType(Qt.TimerType.PreciseTimer)   # coarse timers may be 5% late
        timer.timeout.connect(slot)
        return timer

    def _connect_signals(self) -> None:
        queued = Qt.ConnectionType.QueuedConnection
        bridge = self._bridge
        bridge.fetchAttempt.connect(self._on_fetch_attempt, queued)
        bridge.fetchDone.connect(self._on_fetch_done, queued)
        bridge.ttsResult.connect(self._on_tts_result, queued)
        bridge.calendarStatus.connect(self._on_calendar_status, queued)
        bridge.accountStatus.connect(self._on_account_status, queued)
        bridge.actionProgress.connect(self._on_action_progress, queued)
        bridge.actionResult.connect(self._on_action_result, queued)
        bridge.eventPeek.connect(self._on_event_peek, queued)
        bridge.agendaResult.connect(self._on_agenda_result, queued)
        bridge.accountConnect.connect(self._on_account_connect, queued)
        prompt, reading = self.window.prompt, self.window.reading
        prompt.readNow.connect(self.read_now)
        prompt.later.connect(self.later)
        prompt.dismiss.connect(self.dismiss)
        reading.playPause.connect(self.toggle_play)
        reading.skip.connect(self.skip_section)
        reading.readEverything.connect(self.read_everything)
        reading.openNotion.connect(self.open_in_notion)
        reading.done.connect(self.done)
        reading.sectionClicked.connect(self.jump_to_section)
        reading.approveClicked.connect(self.approve_action)
        reading.denyClicked.connect(self.deny_action)
        reading.lockedClicked.connect(self._on_locked_click)
        reading.openLink.connect(self.open_action_link)
        reading.openSource.connect(self.open_action_source)
        reading.copyText.connect(self.copy_action_text)
        reading.undoAction.connect(self.undo_action)
        reading.editAction.connect(self.edit_action)
        reading.signInAccount.connect(self.sign_in_account)
        reading.connectCalendar.connect(self.connect_calendar)
        reading.tabChanged.connect(self._on_tab_changed)
        reading.conversation.linkClicked.connect(self._on_conversation_link)
        self.window.header.muteToggled.connect(self._on_mute_toggled)
        self.window.closeRequested.connect(self._on_close_requested)
        self.window.visibilityChanged.connect(self._sync_agenda_timer)
        self.window.visibilityChanged.connect(self._on_window_state)
        self.window.activeChanged.connect(self._on_window_active)

    def _init_live_view(self) -> None:
        """The LIVE tab ([live]): the feed from the stream to the views (the LIVE tab's log, and the
        pop-out's once it is made), the tab's dot, and what auto-open remembers (memory only)."""
        live_config = getattr(self.config, "live", None)
        self._live_mode = str(getattr(live_config, "auto_open", "tab"))
        self._live_feed = _LiveFeed(self.live, self)
        self._live_feed.updated.connect(self._on_live_updated)
        self._live_summary = LiveSummary()
        self._live_window: LiveWindow | None = None   # the pop-out, made on the first Pop out (kept)
        self._live_placed = False                  # the pop-out was placed once this session
        self._live_restored = False                # assistant.json's pop-out was looked at (once)
        self._live_unread = False                  # Ask / card steps came while LIVE was not in sight
        self._live_opened: list[int] = []          # tasks that already switched to LIVE (one switch each)
        self._ask_task_id = 0                      # the running (or last) Ask's LIVE task
        self._live_peeks = [0, 0, 0]               # event checks on their way: jobs, cards, problems
        # The calendar days Jarvis already read (today's agenda, Ask's reads): a day picture draws a
        # day's other events only from these (memory only; never a Google call for a picture).
        self._known_days = _pictures.KnownDays()
        # The pictures of the pages a web research read or cites ([live] page_snapshots; None when off).
        self._snapshots: Any = self._create_snapshotter(live_config)
        reading = self.window.reading
        reading.set_live_available(self.live.enabled)
        if self.live.enabled:
            self._live_feed.attach(reading.live_log)
        reading.livePopOut.connect(self.pop_out_live)
        reading.liveDock.connect(self.dock_live)
        reading.liveClear.connect(self.clear_live)
        reading.liveShowWindow.connect(self.show_live_window)
        reading.liveLink.connect(self._on_live_link)

    def _create_snapshotter(self, live_config: Any) -> Any:
        """The page snapshotter while LIVE and its page pictures are on (``snapshot_factory``'s
        object, else the real one: QtWebEngine is loaded only at the first picture); None when off
        or when it cannot be made (the research works the same without it)."""
        if not self.live.enabled or not bool(getattr(live_config, "page_snapshots", True)):
            return None
        from . import snapshots as _snapshots

        research = getattr(self.config, "research", None)
        try:
            fetches = int(getattr(research, "max_fetches", _snapshots.MAX_PER_RUN))
        except (TypeError, ValueError):
            fetches = _snapshots.MAX_PER_RUN
        try:
            if self._snapshot_factory is not None:
                return self._snapshot_factory(self)
            from .snapshot_engine import PageSnapshotter

            return PageSnapshotter(self, keep_text=self.live.keep_text,
                                   max_per_run=max(0, min(_snapshots.MAX_PER_RUN, fetches)))
        except Exception as exc:  # noqa: BLE001 - pictures are optional
            logger.warning("Page pictures could not be set up (%s)", type(exc).__name__)
            return None

    @_live.quiet()
    def _snapshots_call(self, name: str, *args: Any) -> None:
        """One call on the page snapshotter (when there is one); it never changes what Jarvis does."""
        snapper = self._snapshots
        if snapper is not None:
            getattr(snapper, name)(*args)

    def _create_player(self) -> BriefingPlayer:
        player = BriefingPlayer(self, section_gap_ms=self.config.voice.section_gap_ms,
                                volume=self._volume)
        player.itemChanged.connect(self._on_item_changed)
        player.sectionStarted.connect(self._on_section_started)
        player.stateChanged.connect(self._on_player_state)
        player.errorOccurred.connect(self._on_player_error)
        return player

    def _replace_player(self) -> None:
        """A new player for a new frozen script; the old one keeps the old audio registered."""
        old = self.player
        old.blockSignals(True)
        old.stop()
        old.deleteLater()
        self.player = self._create_player()

    def _create_google(self, calendar_factory: Callable[[Config], Any] | None,
                       sender_factory: Callable[[Config], Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
        """(calendars, senders) by alias; both empty when [calendar] enabled = false.

        Without factories one GoogleAccount per alias is shared by its calendar and its sender
        (one sign-in and one accounts.json binding per alias); nothing here opens the browser or
        talks to Google. An injected factory replaces its half; the other half is then empty.
        """
        if not self.config.calendar.enabled:
            return {}, {}
        if calendar_factory is None and sender_factory is None:
            try:
                accounts = build_accounts(self.config)
                built = build_calendars(self.config, accounts), build_senders(self.config, accounts)
            except Exception as exc:  # noqa: BLE001 - the briefing works without Google
                logger.warning("Could not set up the Google accounts (%s)", type(exc).__name__)
                return {}, {}
            self._google_accounts = dict(accounts)
            return built
        calendars = self._create_calendars(calendar_factory) if calendar_factory is not None else {}
        senders: dict[str, Any] = {}
        if sender_factory is not None:
            try:
                built = sender_factory(self.config)
            except Exception as exc:  # noqa: BLE001 - the cards stay Copy / Open hand-offs
                logger.warning("Could not set up sending email (%s)", type(exc).__name__)
                built = None
            if isinstance(built, Mapping):
                senders = {str(alias): sender for alias, sender in built.items() if sender is not None}
        return calendars, senders

    def _create_calendars(self, factory: Callable[[Config], Any] | None) -> dict[str, Any]:
        """alias -> calendar ({} when [calendar] enabled = false or it could not be set up).

        ``factory`` may return one calendar (the personal account's) or a mapping. Building
        them opens nothing and talks to no one; the single sign-in of older versions becomes
        the personal account's here (executor.build_calendars).
        """
        if not self.config.calendar.enabled:
            return {}
        try:
            built = (factory or _default_calendars)(self.config)
        except Exception as exc:  # noqa: BLE001 - the briefing works without a calendar
            logger.warning("Could not set up Google Calendar (%s)", type(exc).__name__)
            return {}
        if built is None:
            return {}
        if isinstance(built, Mapping):
            return {str(alias): calendar for alias, calendar in built.items() if calendar is not None}
        return {DEFAULT_ACCOUNT: built}

    # ---- startup ------------------------------------------------------------------

    def start(self) -> None:
        logger.info("Controller starting (launch: %s, expected run: %s, read immediately: %s)",
                    self.launch, self.expected_run or "any", self.launch == LAUNCH_READ)
        self._run_started = self._scheduled_start(self.expected_run)
        self._audio_dir = self._prepare_audio_dir()
        self._store.prune()
        self._adopt_older_answers()
        self._start_tts()
        self._start_voice()
        self._create_tray()
        self._start_calendar()
        self._update_service_chips()
        if self.ask is not None:
            self.ask.start()   # checks Claude Code on the ask thread (no model request)
        if self.launch == LAUNCH_READ:
            self.enter_reading()
            force_foreground(self.window)
            return
        if self.launch == LAUNCH_OPEN:
            if self.expected_run and not self.startup_error and not self.ask_mode:
                # --run X --open: the X briefing is waited for in the background and announced.
                self._background_run = self.expected_run
                self._start_fetch(self.expected_run)
            self.open_assistant(ask_requested=self.ask_mode)
            self._begin_greeting(wait=True)
            return
        if self._classic():
            if not self.startup_error:
                self._start_fetch(self.expected_run)
            self.show_prompt(take_focus=True)
            return
        self._start_waiting()

    def _adopt_older_answers(self) -> None:
        """The first start of this version: a briefing an older version read through its prompt
        (answered "read" / "done" in runstate.json) counts as viewed, so it is never announced as
        "ready to view" after the update (assistant.json remembers that this was done)."""
        if self.run_state is None:
            return
        prefs = self._prefs_store()
        if prefs.answers_adopted:
            return
        self.run_state.adopt_heard_answers()
        prefs.set_answers_adopted()

    def _classic(self) -> bool:
        """``[assistant] scheduled_prompt``: scheduled runs show the "Hear it now?" prompt."""
        return bool(self.config.assistant.scheduled_prompt)

    def _start_voice(self) -> None:
        """Jarvis's own voice, only here (a controller that is never started never speaks): kept as
        injected, else the real voice when ``[assistant] speak`` is on (``_create_voice``)."""
        if self._voice_injected or not self.config.assistant.speak:
            return
        try:
            voice = self._create_voice()
        except Exception as exc:  # noqa: BLE001 - Jarvis then shows his words without speaking
            logger.warning("Could not set up Jarvis's voice (%s); his words are shown only", type(exc).__name__)
            voice = None
        if voice is not None:
            self.voice = voice
        self._sync_speaker()

    def _create_voice(self) -> Voice | None:
        """The real voice: voice_ui.AssistantVoice with its own speech worker ("tts-say": the
        module-level SpeechSynthesizer with the [voice] settings, so Ryan with the Windows voice as
        fallback, writing into the session's "say" folder), its own player at the controller's
        volume, and assistant.json's mute switch. None keeps the SilentVoice."""
        from . import voice_ui

        settings = self.config.voice
        out_dir = (self._audio_dir if self._audio_dir is not None else self.config.audio_root) / "say"
        bridge, closing = self._bridge, self._closing

        def factory() -> SpeechSynthesizer:   # runs on the worker thread (SAPI/COM is thread-bound)
            return SpeechSynthesizer(voice=settings.voice, rate=settings.rate, volume=settings.volume,
                                     offline_voice=settings.offline_voice, out_dir=out_dir,
                                     divider_pause_ms=settings.divider_pause_ms)

        def on_result(seq: int, audio: SectionAudio | None, error: str | None) -> None:
            _emit_from_worker(bridge, "sayResult", closing, seq, audio, error)

        worker = SayWorker(factory, on_result)
        player = voice_ui.UtterancePlayer(self, volume=self._volume)
        voice = voice_ui.AssistantVoice(worker=worker, player=player, briefing_player=lambda: self.player,
                                        countdown_active=lambda: self._countdown is not None,
                                        prefs=self._prefs_store(), parent=self)
        bridge.sayResult.connect(voice.on_audio, Qt.ConnectionType.QueuedConnection)
        voice.speakingChanged.connect(self._on_voice_speaking)
        worker.start()
        self._say_worker = worker
        logger.info("Jarvis's voice is ready (%s)", "muted" if voice.muted else "on")
        return voice

    def _start_waiting(self) -> None:
        """A scheduled run (or the catch-up): wait invisibly for the run's fresh briefing; it is shown
        without the focus and announced when it arrives (_arrive). A setup error shows at once."""
        if self.startup_error:
            self._arrive()
            return
        self.state = STATE_WAITING
        self._start_fetch(self.expected_run)
        self._update_tray()
        logger.info("Waiting for the %s briefing with the window hidden", self.expected_run or "next")

    def _prepare_audio_dir(self) -> Path:
        root = self.config.audio_root
        audio_dir = root / f"session-{os.getpid()}"
        try:
            audio_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create the audio folder %s: %s", audio_dir, exc)
        _remove_old_sessions(root, keep=audio_dir)
        return audio_dir

    def _start_tts(self) -> None:
        voice, out_dir = self.config.voice, self._audio_dir
        bridge, closing = self._bridge, self._closing

        def factory() -> SpeechSynthesizer:   # runs on the worker thread (SAPI/COM is thread-bound)
            return SpeechSynthesizer(voice=voice.voice, rate=voice.rate, volume=voice.volume,
                                     offline_voice=voice.offline_voice, out_dir=out_dir,
                                     divider_pause_ms=voice.divider_pause_ms)

        def on_result(generation: int, index: int, audio: SectionAudio | None, error: str | None) -> None:
            _emit_from_worker(bridge, "ttsResult", closing, generation, index, audio, error)

        self._tts = TtsWorker(factory, on_result)
        self._tts.start()

    def _start_calendar(self) -> _ActionWorker | None:
        """The action worker, started once Google Calendar or sending email is set up (it checks
        the sign-ins first)."""
        if self._calendar_worker is None and self._any_google_configured() and not self._closing.is_set():
            self._calendar_worker = _ActionWorker(self._executor, self._calendars, self._store, self._bridge,
                                                  self._closing)
            self._calendar_worker.check_sign_in()
        return self._calendar_worker

    def _calendar_configured(self) -> bool:
        """The personal account's calendar (the agenda, Calendar proposals) is set up."""
        return self._configured(self.calendar)

    def _any_calendar_configured(self) -> bool:
        return any(self._configured(calendar) for calendar in self._calendars.values())

    def _any_google_configured(self) -> bool:
        return self._any_calendar_configured() or any(self._configured(sender) for sender in self._senders.values())

    @staticmethod
    def _configured(calendar: Any) -> bool:
        try:
            return calendar is not None and bool(calendar.is_configured())
        except Exception as exc:  # noqa: BLE001 - treated as not set up
            logger.debug("Could not check the Google Calendar setup (%s)", type(exc).__name__)
            return False

    def _create_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.info("No system tray available; continuing without a tray icon")
            return
        menu = QMenu()
        menu.addAction("Show briefing").triggered.connect(self._on_tray_show)
        self._tray_quit_action = menu.addAction("Dismiss")
        self._tray_quit_action.triggered.connect(self._on_tray_quit)
        tray = QSystemTrayIcon(app_icon(), self)
        tray.setToolTip(TRAY_TOOLTIP)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self.tray, self._tray_menu = tray, menu

    # ---- tray -------------------------------------------------------------------------

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._on_tray_show()

    def _on_tray_show(self) -> None:
        """Show briefing (tray menu or a click): a restore, never an "open" - no greeting."""
        if self.state == STATE_SNOOZED:
            self.show_prompt(take_focus=True)
        elif self.state == STATE_WAITING:
            self._open_from_waiting(greet=False)
        elif self.state in (STATE_PROMPT, STATE_READING):
            force_foreground(self.window)

    def _on_tray_quit(self) -> None:
        logger.info("Quit from the tray menu")
        if self.state == STATE_READING:
            self._record_done()
        elif self.state in (STATE_PROMPT, STATE_SNOOZED):
            self.record_answer("dismissed")   # the menu item says Dismiss there
        self.quit()   # waiting (nothing shown yet): nothing is recorded

    def _update_tray(self, tooltip: str | None = None) -> None:
        """The tray tooltip (``tooltip``, else the state's: waiting, "AM briefing ready to view"
        while one is NEW, else the app's name) and the menu's Quit / Dismiss."""
        if self.tray is not None:
            self.tray.setToolTip(tooltip if tooltip is not None else self._tray_tooltip())
        if self._tray_quit_action is not None:
            quits = self.state in (STATE_READING, STATE_WAITING)
            self._tray_quit_action.setText("Quit" if quits else "Dismiss")

    def _tray_tooltip(self) -> str:
        if self.state == STATE_WAITING:
            return f"Jarvis: waiting for the {self.expected_run or 'next'} briefing"
        if self._new_key() is not None:
            run = self._run_label()
            return f"Jarvis: {run + ' ' if run else ''}briefing ready to view"
        return TRAY_TOOLTIP

    # ---- activity log and header chips -------------------------------------------------

    def _activity(self, tag: str, message: str, sub: str = "") -> None:
        self.window.reading.activity.add(tag, message, sub, when=self._now())

    def _update_service_chips(self) -> None:
        header = self.window.header
        header.set_service("notion", *self._notion_chip())
        header.set_service("voice", *self._voice_chip())
        header.set_service("calendar", *self._calendar_chip())

    def _notion_chip(self) -> tuple[str, str]:
        if self.startup_error:
            return hud.STATUS_ERROR, _startup_error_chip(self.startup_error)
        if self._fetch_error is not None:
            if self.fetching:
                return hud.STATUS_WARN, f"Notion: retrying - {_short(str(self._fetch_error))}"
            return hud.STATUS_ERROR, f"Notion: {_short(str(self._fetch_error))}"
        if self._briefing is not None:
            return hud.STATUS_OK, "Notion: connected"
        return hud.STATUS_OFF, "Notion: checking..."

    def _voice_chip(self) -> tuple[str, str]:
        if self._uses_offline_voice():
            return hud.STATUS_WARN, "Voice: offline (Windows voice)"
        if self._engine is not None:
            return hud.STATUS_OK, "Voice: online (edge-tts)"
        if self._failed and not self._audio:
            return hud.STATUS_ERROR, "Voice: speech synthesis failed"
        return hud.STATUS_OFF, "Voice: no audio prepared yet"

    def _calendar_chip(self) -> tuple[str, str]:
        pending = len(self._pending_actions())
        waiting = f" - {_needs_ok(pending)}" if pending else ""
        if not self.config.calendar.enabled:
            return hud.STATUS_OFF, "Google Calendar: turned off in config.toml" + waiting
        if not self._calendar_configured():
            return hud.STATUS_OFF, "Google Calendar: not set up (README step 8)" + waiting
        others = self._other_accounts_text()
        if self._calendar_signed_in:
            return hud.STATUS_OK, "Google Calendar: signed in" + others + waiting
        return (hud.STATUS_WARN, "Google Calendar: not signed in (Approve opens the sign-in)" + others
                + waiting)

    def _other_accounts_text(self) -> str:
        """"; work account: not signed in" for each account besides the personal one."""
        parts = []
        for alias in self._calendars:
            if alias == DEFAULT_ACCOUNT:
                continue
            signed_in, problem, _message = self._accounts_state.get(alias, (False, "", ""))
            if signed_in:
                state = "signed in"
            elif self._connect_alias == alias:
                state = "signing in"
            elif problem == PROBLEM_BLOCKED:
                state = "blocked by its administrator"
            elif problem in (PROBLEM_EXPIRED, PROBLEM_DENIED, PROBLEM_SCOPE, PROBLEM_TIMEOUT, PROBLEM_FAILED,
                             PROBLEM_IDENTITY):
                state = "sign in again"
            else:
                state = "not signed in"
            parts.append(f"; {alias} account: {state}")
        return "".join(parts)

    # ---- fetching ----------------------------------------------------------------------

    @property
    def fetching(self) -> bool:
        return self._fetch_stop is not None

    @property
    def _polling(self) -> bool:
        """Waiting for the expected run's briefing (not a single fetch)."""
        return self.fetching and self._fetch_poll_run is not None

    def _freshness(self, briefing: Briefing) -> Freshness:
        return check_freshness(briefing.header, self.expected_run, self._now(),
                               run_started=self._run_started)

    def _cached_is_fresh(self) -> bool:
        """The briefing we have is today's (and the expected run's, if one is expected)."""
        return self._briefing is not None and self._freshness(self._briefing).fresh

    def _checked_recently(self) -> bool:
        """Notion was read successfully less than one polling interval ago."""
        return time.monotonic() - self._checked_at < self.config.polling.interval_seconds

    def _needs_recheck(self) -> bool:
        """Nothing is fetching and the briefing we have is not fresh: worth asking Notion again."""
        return (not self.startup_error and not self.fetching and self._briefing is not None
                and not self._cached_is_fresh())

    def _start_fetch(self, poll_run: str | None) -> None:
        """Start a fetch thread; ``poll_run`` set means keep polling until that run is fresh."""
        self._fetch_task.finish(_live.STATUS_CANCELLED, summary="replaced")   # a fetch still running
        self._stop_fetch()
        self._fetch_id += 1
        stop = threading.Event()
        self._fetch_stop = stop
        self._fetch_poll_run = poll_run
        self._fetch_error = None
        self._fetch_attempts = 0
        polling = self.config.polling
        self._poll_deadline = self._now() + timedelta(minutes=polling.timeout_minutes)
        client, page_id = self.client, self.config.page_id
        options = {"expected_run": poll_run, "interval_s": float(polling.interval_seconds),
                   "timeout_s": polling.timeout_minutes * 60.0, "now": self._now,
                   "run_started": self._run_started}
        thread = threading.Thread(
            target=_run_fetch, name=f"fetch-{self._fetch_id}", daemon=True,
            args=(self._bridge, self._fetch_id, stop, lambda: fetch_briefing(client, page_id), options))
        self._fetch_task = _live_fetch_task(self.live, poll_run)
        thread.start()
        logger.info("Fetching the briefing (%s)",
                    f"waiting for the {poll_run} run" if poll_run else "first successful fetch")

    def _stop_fetch(self) -> None:
        if self._fetch_stop is not None:
            self._fetch_stop.set()
            self._fetch_stop = None
        self._fetch_task.finish(_live.STATUS_CANCELLED, summary="stopped")   # nothing once it ended

    def _on_fetch_attempt(self, fetch_id: int, briefing: Briefing | None, error: NotionError | None,
                          attempt: int) -> None:
        if fetch_id != self._fetch_id or self.state == STATE_QUITTING:
            return
        self._fetch_attempts += 1
        self._live_fetch_attempt(briefing, error, attempt)
        if briefing is None:
            logger.info("Fetch attempt %d failed: %s", attempt, error)
            self._fetch_error = error
            self._log_fetch_error(error)
            self._on_loading_error()
        else:
            logger.info("Fetch attempt %d: updated=%r run=%s fresh=%s", attempt,
                        briefing.header.updated_raw, briefing.header.run or "-",
                        self._freshness(briefing).fresh)
            self._fetch_error = None
            self._checked_at = time.monotonic()
            self._log_fetch(briefing)
            if self._fetch_attempts == 1 and self._polling and not self._freshness(briefing).fresh:
                self._activity(hud.TAG_WAIT, f"Waiting for the {self._fetch_poll_run} briefing",
                               f"checking Notion {self._interval_text()}")
            self._accept_briefing(briefing)
            if self.state == STATE_WAITING and self._cached_is_fresh():
                self._arrive()
        self._refresh_views()
        if self._greeting_pending and self._fetch_attempts == 1:
            self._finish_greeting()   # the first answer (a briefing or an error) is in: greet now

    def _log_fetch(self, briefing: Briefing) -> None:
        """One activity row per new page content (polling re-reads the same page every minute)."""
        key = briefing.content_key
        if key == self._logged_fetch:
            return
        first = not self._logged_fetch
        self._logged_fetch, self._logged_error = key, ""
        run = briefing.header.run
        sub = _plural(len(briefing.lines), "line") + (f" {DOT} {run} run" if run else "")
        self._activity(hud.TAG_DONE, "Fetched briefing" if first else "Briefing updated", sub)

    def _log_fetch_error(self, error: NotionError | None) -> None:
        text = _short(str(error)) if error is not None else ""
        if text and text != self._logged_error:
            self._logged_error = text
            self._activity(hud.TAG_STOP, "Couldn't load the briefing", text)

    def _accept_briefing(self, briefing: Briefing) -> None:
        if self._script is not None:
            if self._is_newer_briefing(briefing):
                self._newer_briefing(briefing)
            else:
                logger.info("Ignoring a newer briefing: the reading screen already shows one")
            return
        self._briefing = self._take_actions(briefing)
        if self.state == STATE_READING:
            self._begin_reading()
        else:
            self._refresh_prefetch()

    def _on_fetch_done(self, fetch_id: int, result: PollResult) -> None:
        if fetch_id != self._fetch_id or self.state == STATE_QUITTING:
            return
        self._fetch_stop = None
        self._live_fetch_done(result)
        logger.info("Fetching finished after %d attempt(s): %s", result.attempts, _describe_poll(result))
        if result.briefing is None and result.error is not None:
            self._fetch_error = result.error
        if self.state == STATE_WAITING:
            # Nothing new arrived in time (or Notion kept failing): nothing is shown or said; the
            # slot stays open, so the catch-up looks again at the next logon or unlock.
            logger.info("No new %s briefing arrived; nothing shown", self.expected_run or "")
            self.quit()
            return
        background = self._background_run
        self._background_run = None
        if result.timed_out and self.expected_run:
            later = "the last one stays under BRIEFING" if background else "Read now plays the last one"
            self._activity(hud.TAG_WAIT, f"No new {self.expected_run} briefing yet", later)
        if self.state == STATE_READING and self._script is None:
            if self._briefing is not None:
                self._begin_reading()
            else:
                self._on_loading_error()
            self._update_service_chips()
            return
        self._refresh_prefetch()
        self._refresh_views()

    @_live.quiet()
    def _live_fetch_attempt(self, briefing: Briefing | None, error: NotionError | None, attempt: int) -> None:
        """One note per attempt on the fetch's LIVE view step (the page id is never shown)."""
        step = self._fetch_task.find(_live.BRIEFING_FETCH)
        if step is _live.NO_STEP:
            return
        if briefing is None:
            step.note(f"Attempt {attempt}: {_without_ids(str(error)) if error else 'no answer'}")
            return
        hour24 = self.config.display.hour24
        run = f", {briefing.header.run} run" if briefing.header.run else ""
        fresh = self._freshness(briefing).fresh
        state = ("new" if fresh else "not new yet") if self._fetch_poll_run else "read"
        step.note(f"Attempt {attempt}: updated {_short_updated(briefing.header, self._now(), hour24)}{run} - {state}")

    @_live.quiet()
    def _live_fetch_done(self, result: PollResult) -> None:
        task = self._fetch_task
        step = task.find(_live.BRIEFING_FETCH)
        if step is _live.NO_STEP:
            return
        briefing = result.briefing
        if briefing is not None:
            step.field("Updated", _short_updated(briefing.header, self._now(), self.config.display.hour24))
            if briefing.header.run:
                step.field("Run", briefing.header.run)
            step.field("Lines", str(len(briefing.lines)))
            step.field("Proposals found", str(len(self._source_lists.get(SOURCE_BRIEFING, ()))))
            step.field("Deadlines found", str(len(self._page_deadlines)))
        if result.stopped:
            status, word, summary = _live.STATUS_CANCELLED, "", "stopped"
        elif result.timed_out:
            status, word = _live.STATUS_WARN, "NOTHING NEW"
            summary = f"no new {self._fetch_poll_run or ''} briefing yet"
        elif briefing is None and result.error is not None:
            status, word, summary = _live.STATUS_FAILED, "", _without_ids(str(result.error))
        else:
            status, word, summary = _live.STATUS_OK, "", _plural(result.attempts, "attempt")
        step.done(status, status_text=word, summary=summary)
        task.finish(status, status_text=word, summary=summary)

    # ---- proposed actions ------------------------------------------------------------------

    def _take_actions(self, briefing: Briefing) -> Briefing:
        """Move the page's "Proposed actions" section into the approval cards and its
        "Deadlines" section into DEADLINES; neither is shown in the transcript or spoken.

        Polling reads the same page every minute, so the sections are only
        parsed again when the page content changed.
        """
        key = briefing.content_key
        if key != self._actions_key:
            # Deadlines first, ending at a "Proposed actions" heading: either section may be
            # nested inside the other and both still land where they belong.
            headings = self.config.actions.headings
            deadlines, lines = extract_deadlines(briefing.lines, stop_names=headings)
            actions, lines = extract_actions(lines, headings, link_hosts=self.config.actions.link_hosts)
            self._actions_key, self._actions_lines = key, tuple(lines)
            self._page_deadlines = deadlines
            self._set_source(SOURCE_BRIEFING, actions)
            self._render_agenda()
        if len(self._actions_lines) == len(briefing.lines):
            return briefing
        return dataclasses.replace(briefing, lines=self._actions_lines)

    def _pending_actions(self) -> list[ProposedAction]:
        """Proposals that take a decision and have not been decided yet (as their cards show
        them: with the Edit dialog's changes), Ask's included."""
        return [self._effective(action) for action in self._actions
                if action.decidable and not self._store.is_decided(action.id)]

    def _pending_page_actions(self) -> list[ProposedAction]:
        """The briefing's own pending proposals: what the spoken "Needs your OK" reads (never an
        Ask card) and what Ask sees of the briefing."""
        return [action for action in self._pending_actions() if action.source != SOURCE_ASK]

    def _set_source(self, source: str, actions: Sequence[ProposedAction]) -> None:
        """Replace one source's cards (the briefing's on a new page, Ask's after an answer); the
        cards shown are all sources merged (_merged_actions), so a briefing refresh keeps the Ask
        cards and an Ask keeps the briefing's."""
        self._source_lists[source] = list(actions)
        self._set_actions(self._merged_actions())

    def _merged_actions(self) -> list[ProposedAction]:
        """ASK first, then BRIEFING. An Ask card whose id the briefing also has is information
        only ("Already in your list under BRIEFING"): the briefing's card stays the one to decide
        (the same id is the same proposal and has one decision)."""
        briefing = self._source_lists[SOURCE_BRIEFING]
        page_ids = {action.id for action in briefing}
        merged: list[ProposedAction] = []
        seen: set[str] = set()
        for action in self._source_lists[SOURCE_ASK]:
            if action.id in page_ids and not action.error:
                action = with_error(action, ALREADY_LISTED_TEXT)
            if action.id not in seen and action.id not in page_ids:
                seen.add(action.id)
                merged.append(action)
        return merged + briefing

    def _take_ask_cards(self, cards: Sequence[ProposedAction]) -> None:
        """An Ask's cards, above the earlier Ask cards. A card counting down, queued or on its way
        keeps its content (the same id from the new answer is left out); at most _ASK_CARDS_MAX
        stay, the oldest that is not on its way going first."""
        old = self._source_lists[SOURCE_ASK]
        old_ids = {action.id for action in old}
        fresh = [card for card in cards if not (card.id in self._jobs and card.id in old_ids)]
        fresh_ids = {card.id for card in fresh}
        merged = fresh + [action for action in old if action.id not in fresh_ids]
        while len(merged) > _ASK_CARDS_MAX:
            victim = next((action for action in reversed(merged) if action.id not in self._jobs), None)
            if victim is None:
                break
            merged.remove(victim)
        self._set_source(SOURCE_ASK, merged)

    def _action(self, action_id: str) -> ProposedAction | None:
        """The proposal as the page has it."""
        return next((action for action in self._actions if action.id == action_id), None)

    def _current(self, action_id: str) -> ProposedAction | None:
        """The proposal as its card shows it (and as Approve carries it out): the page's line
        with the Edit dialog's changes."""
        action = self._action(action_id)
        return self._effective(action) if action is not None else None

    def _shown(self, action_id: str) -> ProposedAction | None:
        """The proposal as its card shows it: while its countdown ran out and the call is queued or
        on its way, and after Jarvis carried it out, exactly what was sent (a newer page with the
        same id never changes that card); otherwise as _current."""
        running = self._running.get(action_id)
        if running is not None and running[1]:
            return running[0]
        if action_id in self._carried and self._store.get(action_id):
            return self._carried[action_id]
        return self._current(action_id)

    def _is_edited(self, page: ProposedAction) -> bool:
        """The Edit dialog changed what the card carries out (ticking new recipients alone does not
        count: the message is the same)."""
        if page.id not in self._edits:
            return False
        effective = self._effective(page)
        return dataclasses.replace(effective, confirmed=page.confirmed) != page

    def _effective(self, action: ProposedAction) -> ProposedAction:
        edit = self._edits.get(action.id)
        if edit is None:
            return action
        try:
            return apply_edit(action, edit)
        except EditError:
            # The page changed under the edit (same id, other content): the card shows the page.
            logger.info("Dropped the edit of action %s: it no longer fits the briefing's line", action.id)
            self._edits.pop(action.id, None)
            return action

    def _set_actions(self, actions: list[ProposedAction]) -> None:
        """Show ``actions`` (all sources, merged) as the cards. With Ask cards among them each
        source gets a group header (ASK, then BRIEFING) and Ask's labels read "ASK \u00b7 MOVE"."""
        if actions == self._actions:
            return
        self._actions = list(actions)
        ids = {action.id for action in self._actions}
        for kept in (self._edits, self._checks, self._hints, self._carried, self._mismatch_seen, self._reviewed):
            for action_id in [key for key in kept if key not in ids]:
                del kept[action_id]
        countdown = self._countdown
        if countdown is not None and self._current(countdown.action.id) != countdown.action:
            self._cancel_countdown("the briefing changed")   # never send what the card no longer shows
        if self._dialog is not None and self._dialog.action_id not in ids:
            self._dialog.reject()
        reading = self.window.reading
        reading.clear_action_cards()
        today = self._now().date()
        grouped = any(action.source == SOURCE_ASK for action in self._actions)
        group = None
        for page_action in self._actions:
            if grouped and page_action.source != group:
                group = page_action.source
                reading.approvals.add_group_header(_GROUP_TITLES.get(group, group),
                                                   sum(1 for action in self._actions if action.source == group))
            action = self._shown(page_action.id) or self._effective(page_action)
            view = card_view(action, today)
            label = view.kind_label + (EDITED_MARK if self._is_edited(page_action) else "")
            if page_action.source == SOURCE_ASK:
                label = f"{_GROUP_TITLES[SOURCE_ASK]} {DOT} {label}"
            reading.add_action_card(action.id, label, view.title, _card_detail(action, view, today),
                                    view.decidable, **_card_options(action, view))
            self._show_card_state(action.id)   # also sets the note (warnings) while it waits
        self._sync_card_locks()
        self._refresh_actions_ui()
        self._request_peeks()

    def _show_card_state(self, action_id: str) -> None:
        """The card's status from the countdown, the running job or the saved decision.

        While the card waits for a decision (pending, or failed / unknown and kept for
        a retry) its note is shown again: WORKING... clears the note. An RSVP, Move or
        Cancel card also gets its check line, its Edit / Copy links and the text of its
        right-hand button (Accept, Sign in, Retry, or Done when Jarvis may not change
        the event) here; a Reply / Email card its FROM line and recipient chips, Edit
        and Send / Retry (Done when Jarvis can't send it: Copy and Open instead).
        """
        card = self.window.reading.action_card(action_id)
        if card is None or not card.actionable:
            return
        action = self._shown(action_id)
        stage = self._jobs.get(action_id)
        entry = self._store.get(action_id) or {}
        status = entry.get("status", "")
        event = action is not None and action.changes_event
        mail = action is not None and action.sends_mail
        mail_status = self._mail_status(action) if mail else None
        if event:
            self._sync_countdown_card(card, action, status, stage)
        elif mail and mail_status is not None:
            self._sync_mail_card(card, action, status, stage, mail_status)
        elif action is not None and _adds_event(action) and stage is None:
            # After an unknown outcome the button says what it does: count down and add again
            # (Google is asked for the existing event first, so a retry never adds a second one).
            approve = card_view(action, self._now().date()).approve_text or "Approve"
            card.set_approve_text(RETRY_TEXT if status == STATUS_UNKNOWN else approve)
        if stage == _STAGE_COUNTDOWN:
            card.set_status(hud.CARD_COUNTDOWN, self._countdown_text())
            return
        if stage is not None:
            if stage == _STAGE_SIGNIN:
                card.set_status(hud.CARD_SIGNIN,
                                f"Waiting for Google sign-in ({action.account})" if event or mail else "")
            else:
                card.set_status(hud.CARD_WORKING)
            return
        if status == STATUS_FAILED:
            # A Reply / Email that failed was certainly not sent: say that on the result line (two
            # lines at most); Gmail's reason goes into the note, which shows all of it.
            message = MAIL_FAILED_TEXT if mail else entry.get("message", "")
        elif status == STATUS_UNKNOWN and action is not None and action.countdown:
            message = UNKNOWN_MAIL_CARD_TEXT if mail else UNKNOWN_CARD_TEXT
        elif status == STATUS_SENT:
            message = entry.get("message", "") or (result_text(action, status) if action is not None else "")
        else:
            message = result_text(action, status) if action is not None else ""
        shown = _CARD_FOR_STATUS.get(status, hud.CARD_PENDING)
        block = action is not None and action.kind == TODO
        link_text = _BLOCK_LINK_TEXT if block else (_EVENT_LINK_TEXT if event else "")
        card.set_status(shown, message, entry.get("link", ""), link_text)
        if action is None or shown not in (hud.CARD_PENDING, hud.CARD_FAILED, hud.CARD_UNKNOWN):
            return
        if event:
            card.set_note(self._countdown_note(action, status, entry))
        elif mail and mail_status is not None:
            card.set_note(self._mail_card_note(action, status, entry, mail_status))
        elif status == STATUS_UNKNOWN and _adds_event(action):
            reason = _CHECK_BEFORE_RETRY_RE.sub("", entry.get("message", ""))
            card.set_note(f" {DOT} ".join(part for part in (reason, card_view(action, self._now().date()).note)
                                          if part))
        elif card.note() != CALENDAR_SETUP_NOTE:
            # A Calendar event or a Todo's block: the question about its account, if any, first.
            hint = self._hints.get(action_id, "")
            note = f" {DOT} ".join(part for part in (hint, card_view(action, self._now().date()).note) if part)
            if note or card.note().startswith(_ACCOUNT_NOTE_PREFIXES):
                card.set_note(note)

    def _sync_countdown_card(self, card: hud.ActionCard, action: ProposedAction, status: str,
                             stage: str | None) -> None:
        """An RSVP, Move or Cancel card's check line, tools row (Edit / Sign in / Open event,
        Copy) and button text."""
        decided = self._store.is_decided(action.id)
        idle = stage is None and not decided and self.state != STATE_QUITTING
        text, tooltip, warn = self._check_view(action, status)
        card.set_check_line(text, tooltip, warn=warn)
        card.set_tool_slot(self._tool_slot(action, status))
        card.set_edit_enabled(self._tools_enabled(action.id))
        card.set_copy_text(self._copy_label(action))
        if idle:
            card.set_approve_text(self._approve_text(action, status))

    # ---- Reply / Email cards ------------------------------------------------------------------

    def _mail_status(self, action: ProposedAction) -> MailStatus:
        """What a Reply / Email card shows about sending (executor.mail_status; files only)."""
        try:
            status = self._executor.mail_status(action)
        except Exception as exc:  # noqa: BLE001 - the card becomes a hand-off, nothing is sent
            logger.warning("Could not check the sending account of action %s (%s)", action.id, type(exc).__name__)
            return MailStatus(action.account, "", f"From: {action.account}", PROBLEM_SETUP,
                              f"Couldn't check the {action.account} account - use {hand_off_tools(action)} instead",
                              hand_off=True)
        if not self.config.calendar.enabled and status.hand_off:
            status = dataclasses.replace(status, note=MAIL_OFF_NOTE.format(tools=hand_off_tools(action)))
        return status

    def _recipient_chips(self, action: ProposedAction, status: MailStatus, *,
                         badges: bool = True) -> tuple[list[hud.RecipientChip], list[hud.RecipientChip]]:
        """The card's (or Edit dialog's) To and Cc chips: what is sent, each new one red until it is
        ticked in Edit; the sending account's own address red with SENDING ACCOUNT (Send refuses
        until it is removed in Edit)."""
        review = status.review
        if review is None:
            return ([hud.RecipientChip(address) for address in action.recipients()],
                    [hud.RecipientChip(address) for address in action.cc()])
        ticked = {address.casefold() for address in action.confirmed}

        def chip(address: str) -> hud.RecipientChip:
            kind = review.kind_of(address)
            if badges and kind == RECIPIENT_OWN_KIND:   # the sending account itself: Send refuses
                return hud.RecipientChip(address, hud.RECIPIENT_OWN)
            if not badges or kind != RECIPIENT_NEW_KIND:
                return hud.RecipientChip(address)
            confirmed = address.casefold() in ticked
            return hud.RecipientChip(address, hud.RECIPIENT_CONFIRMED if confirmed else hud.RECIPIENT_NEW)

        return [chip(address) for address in review.to], [chip(address) for address in review.cc]

    def _recipient_state(self, status: MailStatus, address: str) -> str:
        """An address typed into the Edit dialog: hud.RECIPIENT_OWN / _NEW / _KNOWN."""
        try:
            kind = classify([address], own=status.sender, trusted_domains=self.config.actions.trusted_domains,
                            history=self._history).get(address, RECIPIENT_NEW_KIND)
        except Exception as exc:  # noqa: BLE001 - unknown counts as new (it needs a tick)
            logger.debug("Could not classify a recipient (%s)", type(exc).__name__)
            kind = RECIPIENT_NEW_KIND
        if kind == RECIPIENT_OWN_KIND:
            return hud.RECIPIENT_OWN
        return hud.RECIPIENT_NEW if kind == RECIPIENT_NEW_KIND else hud.RECIPIENT_KNOWN

    def _sync_mail_card(self, card: hud.ActionCard, action: ProposedAction, status: str, stage: str | None,
                        mail: MailStatus) -> None:
        """A Reply / Email card's FROM line, chips, Edit and button text."""
        decided = self._store.is_decided(action.id)
        idle = stage is None and not decided and self.state != STATE_QUITTING
        to, cc = self._recipient_chips(action, mail, badges=not mail.hand_off and not decided)
        card.set_recipients(mail.from_text.removeprefix("From: "), to, cc)
        card.set_edit_enabled(self._tools_enabled(action.id))
        if idle:
            card.set_approve_text(self._mail_button(status, mail))

    @staticmethod
    def _mail_button(status: str, mail: MailStatus) -> str:
        """Done while Jarvis can't send from this card (not set up, blocked, empty: Copy / Open
        instead); Retry after an unknown outcome, and after a failure unless the account must sign
        in again first (the note then says "click Send to sign in again"); else Send."""
        if mail.hand_off:
            return DONE_TEXT
        if status == STATUS_UNKNOWN or (status == STATUS_FAILED and not mail.sign_in):
            return RETRY_TEXT
        return SEND_TEXT

    def _mail_card_note(self, action: ProposedAction, status: str, entry: Mapping[str, Any],
                        mail: MailStatus) -> str:
        """A Reply / Email card's amber note: what its last Send click needs next, why its outcome
        is unknown, why it can't send (sign-in, new recipients), then the line's own notes."""
        button = self._mail_button(status, mail)   # the notes name the button the card shows
        parts = [_for_button(self._hints.get(action.id, ""), button)]
        if status == STATUS_UNKNOWN:
            parts.append(_CHECK_BEFORE_RETRY_RE.sub("", entry.get("message", "")))
        elif status == STATUS_FAILED:
            parts.append(_NOTHING_SENT_RE.sub("", entry.get("message", "")))
        asking = self._account_dialog is not None and self._account_dialog.alias == action.account
        if not self._signing_in(action.account) and not asking:   # meanwhile the hint says what to do
            parts.append(_for_button(mail.note, button))
        parts.append(card_view(action, self._now().date()).note)
        return f" {DOT} ".join(dict.fromkeys(part for part in parts if part))

    def _approve_text(self, action: ProposedAction, status: str) -> str:
        """Done while Jarvis can't act for the card's account (not set up for it, or its
        administrator blocks the app: the card is a hand-off, as in earlier versions); Sign in
        while the account is signed out (that click opens Google's sign-in); Done when Google's
        view of the event does not let Jarvis carry the card out; Retry after a failure or an
        unknown outcome; else Accept / Decline / Maybe, Move or Cancel event. A click does what
        the button says (approve_action asks this same function)."""
        if self._hand_off(action):
            return DONE_TEXT
        if self._needs_sign_in(action):
            return SIGN_IN_LINK_TEXT
        if self._blocked(action.id):
            return DONE_TEXT
        if status in (STATUS_FAILED, STATUS_UNKNOWN):
            return RETRY_TEXT
        return card_view(action, self._now().date()).approve_text or "Approve"

    def _hand_off(self, action: ProposedAction) -> bool:
        """Jarvis can't carry out cards of this account here: it is not set up for them
        (executor.readiness) or its administrator blocks the app (until a new Sign in works)."""
        if self._executor.readiness(action):
            return True
        state = self._accounts_state.get(action.account)
        return (state is not None and not state[0] and state[1] == PROBLEM_BLOCKED
                and self._connect_alias != action.account)

    def _tool_slot(self, action: ProposedAction, status: str) -> str:
        """The tools row's last place: Sign in while the account's administrator blocks the app
        (its right-hand button is Done), Google's own "Open event" when the line has no link and
        Jarvis won't act on the event or its outcome is unknown (to check it), else Edit."""
        if not self._executor.readiness(action) and self._hand_off(action):
            return hud.TOOL_SIGN_IN
        if (not action.link and self._google_link(action.id)
                and (self._blocked(action.id) or status == STATUS_UNKNOWN)):
            return hud.TOOL_OPEN
        return hud.TOOL_EDIT

    def _tools_enabled(self, action_id: str) -> bool:
        """Edit and Sign in open a dialog or the browser: only while nothing else runs (no
        countdown, job or sign-in on any card), so nothing ever covers a running card's Undo."""
        return (self.state != STATE_QUITTING and not self._jobs and self._countdown is None
                and not self._connect_running and not self._store.is_decided(action_id))

    def _copy_label(self, action: ProposedAction) -> str:
        """The tools row's Copy: a Move's or Cancel's note (never sent); an RSVP's note too when
        Jarvis won't send the answer (the card is a hand-off or Done)."""
        label = copy_text(action)
        if not label and action.kind == RSVP and action.body and (self._hand_off(action) or self._blocked(action.id)):
            return "Copy note"
        return label

    def _google_link(self, action_id: str) -> str:
        """Google's own page of the card's event (from the check), when it is an allowed https link."""
        check = self._checks.get(action_id)
        link = check.link if check is not None else ""
        return link if link.startswith("https://") and link_allowed(link, self.config.actions.link_hosts) else ""

    def _blocked(self, action_id: str) -> bool:
        """Google's view of the card's event says Jarvis may not carry it out (not the
        organizer, not a guest, a series, all-day, not found)."""
        check = self._checks.get(action_id)
        return check is not None and not check.allowed and bool(check.reason)

    def _account_signed_in(self, alias: str) -> bool:
        state = self._accounts_state.get(alias)
        return state is not None and state[0]

    def _needs_sign_in(self, action: ProposedAction) -> bool:
        """The card's account is known to be signed out (its right-hand button reads Sign in)."""
        if self._executor.readiness(action):
            return False
        state = self._accounts_state.get(action.account)
        return state is not None and not state[0] and self._connect_alias != action.account

    def _check_view(self, action: ProposedAction, status: str = "") -> tuple[str, str, bool]:
        """The check line (text, tooltip, amber): Google's own view of the event (in amber when
        its title or time is not the line's), why Jarvis won't act on it, or why it is not
        shown (setup, sign-in); after Jarvis carried the card out, what Google has now.
        Everything that arrives by itself (a check, a sign-in state) goes here, in the room the
        card reserved for it, so it never moves a card; the amber note below the buttons only
        changes after a click on that card."""
        if status == STATUS_SENT:
            return self._after_view(action)
        reason = self._executor.readiness(action)
        if reason:
            return reason, "", True
        alias = action.account
        if self._connect_alias == alias:
            return f"Waiting for the Google sign-in of the {alias} account...", "", False
        signed_in, problem, message = self._accounts_state.get(alias, (True, "", ""))
        if not signed_in and problem and problem != PROBLEM_SIGNED_OUT:
            line = _PROBLEM_LINES.get(problem, _PROBLEM_LINE_OTHER).format(alias=alias)
            return line, sign_in_note(alias, problem=problem, message=message, sends_mail=self._sends_mail(alias),
                                      reads_mail=self._reads_mail(alias)), True
        check = self._checks.get(action.id)
        if check is not None:
            if check.reason:   # Jarvis won't act on this event: the reason, Google's view on hover
                return check.reason, f"{check.reason}\n{check.tooltip or check.text}", True
            return check.text, check.tooltip, not check.allowed or bool(check.mismatch)
        if action.id in self._peeking:
            return CHECK_SOON_TEXT, "", False
        if alias in self._accounts_state and not signed_in:
            return check_failure(action, CalendarNotSignedIn(problem=PROBLEM_SIGNED_OUT)).text, "", False
        return CHECK_LATER_TEXT, "", False

    def _after_view(self, action: ProposedAction) -> tuple[str, str, bool]:
        """The check line once Jarvis carried the card out: what Google has now, by the answer
        Google gave ("Google now: Speaker series \u00b7 you accepted")."""
        check = self._checks.get(action.id)
        title = check.title if check is not None else ""
        if action.kind == RSVP:
            what = _ANSWERED.get(action.field("answer"), "answered")
        elif action.kind == MOVE:
            what = when_text(action, self._now().date())
        else:
            what = "cancelled"
        text = AFTER_PREFIX + f" {DOT} ".join(part for part in (title, what) if part)
        return text, "", False

    def _countdown_note(self, action: ProposedAction, status: str, entry: Mapping[str, Any]) -> str:
        """The amber note of an RSVP, Move or Cancel card: what the last click on it needs next,
        why its outcome is unknown, then the line's own warnings."""
        parts = [self._hints.get(action.id, "")]
        if status == STATUS_UNKNOWN:   # why (the line above the note already says to check first)
            parts.append(_CHECK_BEFORE_RETRY_RE.sub("", entry.get("message", "")))
        parts.append(card_view(action, self._now().date()).note)
        return f" {DOT} ".join(dict.fromkeys(part for part in parts if part))

    def _sends_mail(self, alias: str) -> bool:
        """The account has the gmail_send feature in config.toml (its sign-ins ask to send email)."""
        account = self.config.accounts.get(alias)
        return account is not None and GMAIL_FEATURE in account.features

    def _reads_mail(self, alias: str) -> bool:
        """The account has the gmail_read feature in config.toml (its sign-ins ask to read email)."""
        account = self.config.accounts.get(alias)
        return account is not None and GMAIL_READ_FEATURE in account.features

    def _hint(self, action_id: str, text: str) -> None:
        """What the card's last Approve click needs next (its amber note until the next click)."""
        if text:
            self._hints[action_id] = text
        else:
            self._hints.pop(action_id, None)
        self._show_card_state(action_id)

    def _refresh_countdown_cards(self) -> None:
        """Check lines, links, buttons and notes of every RSVP, Move and Cancel card, and the FROM
        line, chips and notes of every Reply / Email card (a sign-in changes them)."""
        for action in self._actions:
            if action.editable:
                self._show_card_state(action.id)

    def _refresh_actions_ui(self) -> None:
        """Pending count on the prompt chip, the STATUS bar, the panel meta and the calendar chip."""
        pending = len(self._pending_actions())
        total = sum(1 for action in self._actions if action.decidable)
        self.window.prompt.set_pending(pending)
        self.window.reading.set_pending(pending, total)
        self._update_service_chips()

    def _decision_click_too_soon(self) -> bool:
        """True (and logged) for an Approve / Deny within 1 s of the previous one, on any card.

        Every click counts, also an ignored one, so a burst of clicks decides
        at most one proposal until the mouse rests for a second.
        """
        now = self._click_now()
        previous, self._decision_clicked_at = self._decision_clicked_at, now
        if now - previous < _DECISION_CLICK_GUARD_S:
            logger.info("Ignored a second approval click within 1 s")
            return True
        return False

    def _click_now(self) -> float:
        return self._click_clock() if self._click_clock is not None else time.monotonic()

    # ---- approval lock ----------------------------------------------------------------------

    def _lock_tip(self) -> str:
        """Why the cards are locked ("" when they are not)."""
        if self._connect_running:
            return SIGN_IN_LOCK_TIP
        return LOCK_TIP if self._jobs else ""

    def _sync_card_locks(self) -> None:
        """Lock every card but the one being approved while an approval (its countdown included)
        or a sign-in runs."""
        tip = self._lock_tip()
        for card in self.window.reading.approvals.cards():
            card.set_locked(bool(tip) and card.action_id not in self._jobs, tip)
            action = self._action(card.action_id)
            if card.actionable and action is not None and action.editable:
                card.set_edit_enabled(self._tools_enabled(card.action_id))   # never a dialog beside Undo

    def _locked_out(self, action_id: str) -> bool:
        """True (and logged, without titles) for an Approve / Deny on a locked card."""
        if self._connect_running:
            logger.info(_SIGN_IN_LOCKED_MESSAGE)
            return True
        if self._jobs and action_id not in self._jobs:
            logger.info(_LOCKED_MESSAGE)
            return True
        return False

    def _on_locked_click(self, action_id: str) -> None:
        """A click on a dimmed Deny / Approve: ignored, but it counts for the 1 s guard."""
        if not self._decision_click_too_soon() and not self._locked_out(action_id):
            logger.info(_LOCKED_MESSAGE)

    def approve_action(self, action_id: str) -> None:
        """The card's right-hand button.

        Approve (a Calendar event) and Add block (a Todo's block) start the undo
        countdown; at its end the action worker signs in when needed and adds
        the event. Accept / Decline / Maybe, Move and Cancel event (and Retry)
        start the countdown once the card shows Google's own view of its event
        and that view allows it; before that the click signs the account in or
        checks the event, and the card says to click again. Send (a Reply or
        Email) signs its account in first, opens Edit to confirm new recipients
        or to read a long message, and otherwise starts the countdown. Done
        (cards Jarvis does not carry out, or whose event Jarvis may not change,
        or a message it can't send) records that you handled it.
        """
        if self._decision_click_too_soon() or self._locked_out(action_id):
            return
        action = self._current(action_id)
        if (action is None or not action.decidable or self.state == STATE_QUITTING
                or action_id in self._jobs or self._store.is_decided(action_id)
                or self._account_dialog is not None):
            return
        if not action.actionable:
            self._mark_done(action)
            return
        if action.changes_event:
            status = (self._store.get(action_id) or {}).get("status", "")
            if self._approve_text(action, status) == DONE_TEXT:   # what the button says
                self._mark_done(action)
                return
            self._approve_countdown(action)
            return
        if action.sends_mail:
            self._approve_mail(action)
            return
        if action.kind == TODO and action.block_event() is None:
            return
        card = self.window.reading.action_card(action_id)
        worker = self._start_calendar()
        if worker is None or self._executor.readiness(action):
            logger.info("Approve %s: Google Calendar is not set up", action_id)
            if card is not None:
                card.set_note(CALENDAR_SETUP_NOTE)
            self._activity(hud.TAG_WAIT, "Google Calendar is not set up", "README step 8")
            return
        alias = account_of(action)
        unknown = self._account_unknown(alias)
        if unknown or (not self._account_signed_in(alias) and self._account_unconfirmed(alias)):
            # Its sign-in binds a Google account to confirm first: sign in and ask before the
            # countdown, so a countdown never runs for an event that can't be added.
            logger.info("Approve %s: signing in to the %s account first (its Google account is confirmed before "
                        "anything is added)", action_id, logged_alias(alias))
            button = self._confirm_button(action_id)
            self._hint(action_id, ACCOUNT_UNKNOWN_NOTE.format(alias=alias) if unknown
                       else ADD_SIGN_IN_FIRST_NOTE.format(button=button))
            self._start_sign_in(alias, card_id=action_id)
            return
        if self._ask_account(alias, card_id=action_id):
            logger.info("Approve %s: asking first whether the %s account is the right Google account", action_id,
                        logged_alias(alias))
            return
        self._start_countdown(action)   # a Todo's block keeps the Todo's id, so the result lands on its card

    def _approve_mail(self, action: ProposedAction) -> None:
        """Send / Retry on a Reply or Email: sign in first (no countdown) while its account can't
        send, ask whether a new binding is the right Google account, refuse while a recipient is
        the sending account itself (the note says so; Edit fixes it), open Edit while a new
        recipient is not ticked or a long message was not read there, else count down. Done
        (Jarvis can't send it here) records that you handled it."""
        action_id = action.id
        mail = self._mail_status(action)
        if mail.hand_off:
            self._mark_done(action)
            return
        if self._start_calendar() is None:   # no sending account is set up at all
            self._hint(action_id, mail.note or MAIL_OFF_NOTE.format(tools=hand_off_tools(action)))
            return
        if mail.sign_in:
            logger.info("Send %s: signing in to the %s account first", action_id, logged_alias(action.account))
            self._hint(action_id, MAIL_SIGN_IN_NOTE)
            self._start_sign_in(action.account, card_id=action_id, action=action)
            return
        if mail.confirm:
            logger.info("Send %s: asking first whether the %s account is the right Google account", action_id,
                        logged_alias(action.account))
            if not self._ask_account(action.account, card_id=action_id, action=action):
                self._hint(action_id, mail.note)
            return
        if mail.refused:
            review = mail.review
            own = bool(review is not None and review.own)
            logger.info("Send %s: not sent - %s", action_id,
                        "a recipient is the sending account itself" if own else "nobody is in To")
            self._hint(action_id, "")   # the note says why (it names the address): edit the recipients
            self._activity(hud.TAG_STOP, f"Not sent: {_kind_title(action)}",
                           f"{kind_label(action).upper()} {DOT} "
                           + ("a recipient is the sending account itself" if own else "nobody in To"))
            return
        if mail.needs_edit:
            review = mail.review
            logger.info("Send %s: %d new recipient(s) to confirm in Edit first", action_id,
                        len(review.unconfirmed) if review is not None else 0)
            banner = CONFIRM_BANNER
            if self._unread(action):
                banner = f"{banner} {CONFIRM_READ_TOO}"
            self._hint(action_id, CONFIRM_NOTE)
            self._open_mail_dialog(action, banner)
            return
        if self._unread(action):
            logger.info("Send %s: opening Edit to read the whole message first", action_id)
            self._hint(action_id, REVIEW_NOTE)
            self._open_mail_dialog(action, REVIEW_BANNER)
            return
        if not mail.ready:
            self._hint(action_id, mail.note or NOT_READY_NOTE)
            return
        self._start_countdown(action, sender=mail.sender)

    def _unread(self, action: ProposedAction) -> bool:
        """A Reply / Email whose card does not show all of its subject or message (cut at the card's
        width, or more lines than the card has) and whose Edit dialog has not shown exactly this
        subject and message whole yet: Send opens the dialog to read it first."""
        card = self.window.reading.action_card(action.id)
        cut = body_needs_review(action) or card is None or card.mail_cut()
        return cut and self._reviewed.get(action.id) != _review_digest(action.title, action.body)

    def _approve_countdown(self, action: ProposedAction) -> None:
        """Accept / Move / Cancel event / Retry: sign in or check first when needed, else count down."""
        action_id = action.id
        card = self.window.reading.action_card(action_id)
        button = card.approve_text() if card is not None else "Approve"
        reason = self._executor.readiness(action)
        if not reason and self._start_calendar() is None:
            reason = CALENDAR_SETUP_NOTE
        if reason:
            logger.info("Approve %s: Jarvis can't carry it out here (%s, %s)", action_id, action.kind,
                        logged_alias(action.account))
            self._hint(action_id, NOT_READY_NOTE if reason == self._check_view(action)[0] else reason)
            self._activity(hud.TAG_WAIT, f"Can't {_VERBS.get(action.kind, 'do')} this yet: {_kind_title(action)}",
                           _short(reason))
            return
        unknown = self._account_unknown(action.account)
        if unknown or not self._account_signed_in(action.account):
            logger.info("Approve %s: signing in to the %s account first%s", action_id, logged_alias(action.account),
                        " (Jarvis has no binding for its saved sign-in)" if unknown else "")
            self._hint(action_id, ACCOUNT_UNKNOWN_NOTE.format(alias=action.account) if unknown else SIGN_IN_FIRST_NOTE)
            self._start_sign_in(action.account, card_id=action_id)
            return
        if self._ask_account(action.account, card_id=action_id):
            logger.info("Approve %s: asking first whether the %s account is the right Google account", action_id,
                        logged_alias(action.account))
            return
        check = self._checks.get(action_id)
        if check is None or not check.allowed:
            logger.info("Approve %s: checking the event with Google first", action_id)
            self._hint(action_id, CHECK_FIRST_NOTE.format(button=button))
            self._request_peeks([action], force=True)
            return
        if check.mismatch and self._mismatch_seen.get(action_id) != check:
            # Google's title or time is not the line's: say so once; the next click goes ahead.
            logger.info("Approve %s: Google's event differs from the line (%s); asking again", action_id,
                        check.mismatch)
            self._mismatch_seen[action_id] = check
            self._hint(action_id, MISMATCH_NOTE.format(what=_MISMATCH_WHAT.get(check.mismatch, "other details"),
                                                       button=button))
            return
        self._start_countdown(action)

    # ---- the undo countdown --------------------------------------------------------------

    def _start_countdown(self, action: ProposedAction, *, sender: str = "") -> None:
        """Count down to the one call (Undo until then). ``sender``: a Reply / Email's From address
        as its card shows it; it goes out only from that address."""
        seconds = int(self.config.actions.undo_seconds)
        now = time.monotonic()
        self._hints.pop(action.id, None)
        self._countdown = _Countdown(action, deadline=now + seconds, started=now, sender=sender)
        self._countdown.task = self._live_action_task(action, seconds, now + seconds)
        self._live_auto_open(_live.TASK_ACTION, self._countdown.task.id)
        self._jobs[action.id] = _STAGE_COUNTDOWN
        logger.info("Approved action %s (%s, %s): %s in %d s unless undone", action.id, action.kind,
                    logged_alias(account_of(action)), "adding" if _adds_event(action) else "sending", seconds)
        self._show_card_state(action.id)
        self._sync_card_locks()
        if action.kind == CALENDAR:
            self._activity(hud.TAG_RUN, f"Adding in {seconds} s: {action.title}", "Google Calendar")
        elif action.kind == TODO:
            self._activity(hud.TAG_RUN, f"Adding in {seconds} s: a block for {_activity_title(action)}",
                           "Google Calendar")
        else:
            self._activity(hud.TAG_RUN, f"Sending in {seconds} s: {_action_phrase(action)}",
                           self._activity_sub(action))
        self._countdown_timer.start()
        # An earlier card's "Done, sir" that has not started yet is dropped (its entry is shown):
        # said after this approval it would sound like this card's result, which only comes later.
        self._voice_call("cancel_key", _RESULT_KEY)
        self._pump_voice()   # nothing Jarvis says starts during the countdown

    @_live.quiet(_live.NO_TASK)
    def _live_action_task(self, action: ProposedAction, seconds: int, deadline: float) -> Any:
        """An approved card's LIVE view task: "You approved", what it will send or change exactly
        (Executor.preview: no network) and the undo countdown."""
        if not self.live.enabled:
            return _live.NO_TASK
        saved = self._store.get(action.id) or {}
        retry = saved.get("status") in (STATUS_FAILED, STATUS_UNKNOWN)
        task = self.live.task(_live.TASK_ACTION, _live_action_title(action) + (" (retry)" if retry else ""))
        today = self._now().date()
        view = card_view(action, today)
        kind = {RSVP: "RSVP"}.get(action.kind, action.kind.upper())
        fields: list[Any] = [("Card", f"{action.source.upper()} {kind}"), ("Account", account_of(action)),
                             ("Card says", " - ".join(part for part in (view.title, _card_detail(action, view, today))
                                                      if part))]
        check = self._checks.get(action.id)
        if action.changes_event and check is not None:
            fields.append(("Google's event now", check.text))
        if retry:
            fields.append(("Retry", f"the last try ended {str(saved.get('status')).upper()}"))
        task.step(_live.ACTION_APPROVED, "You approved", status=_live.STATUS_OK, fields=fields)
        preview = self._executor.preview(action)
        if preview is not None:
            payload = task.step(_live.ACTION_PAYLOAD, preview.title, status_text="PREVIEW")
            payload.show(preview)   # a Reply / Email's picture comes with it
            if action.kind in (CALENDAR, TODO, MOVE, CANCEL, RSVP):
                payload.picture(self._live_day_picture(action))
        task.step(_live.ACTION_COUNTDOWN, "Undo countdown", status_text="ADDING" if _adds_event(action) else "SENDING",
                  deadline=deadline, fields=[("Length", f"{seconds} s"), ("Undo", "on the card")])
        return task

    def _activity_sub(self, action: ProposedAction) -> str:
        """"MOVE \u00b7 WORK"; a Reply / Email adds its recipient count ("REPLY \u00b7 WORK \u00b7 2 recipients")."""
        label = kind_label(action).upper()
        if not action.sends_mail:
            return label
        review = self._mail_status(action).review
        count = len(review.recipients) if review is not None else len(action.mail_recipients())
        return f"{label} {DOT} {_recipient_count(count)}"

    def _countdown_text(self) -> str:
        countdown = self._countdown
        if countdown is None:
            return ""
        seconds = max(0, math.ceil(countdown.deadline - time.monotonic()))
        countdown.shown = seconds
        if countdown.action.sends_mail:   # the FROM line may be scrolled out of view: say which account
            return f"Sending from {countdown.action.account} in {seconds} s"
        return f"{'Adding' if _adds_event(countdown.action) else 'Sending'} in {seconds} s"

    def _on_countdown_tick(self) -> None:
        countdown = self._countdown
        if countdown is None:
            self._countdown_timer.stop()
            return
        remaining = countdown.deadline - time.monotonic()
        if remaining <= 0:
            self._finish_countdown()
            return
        if math.ceil(remaining) != countdown.shown:
            card = self.window.reading.action_card(countdown.action.id)
            if card is not None:
                card.set_status(hud.CARD_COUNTDOWN, self._countdown_text())

    def _finish_countdown(self) -> None:
        """The countdown ran out: queue the one call (the worker saves "running" right before it)."""
        countdown, self._countdown = self._countdown, None
        self._countdown_timer.stop()
        self._pump_voice()   # what waited for the countdown may be said now (never "Done": the result says that)
        if countdown is None:
            return
        action = countdown.action
        worker = self._calendar_worker
        if self.state == STATE_QUITTING or worker is None:
            self._jobs.pop(action.id, None)
            _live_countdown_end(countdown, _live.STATUS_CANCELLED, "", "Jarvis closed - nothing was sent")
            return
        if action.sends_mail:
            problem = _mail_from_problem(self._executor, action, countdown.sender)
            if problem:   # the account (or its address) changed under the countdown: nothing is sent
                _live_countdown_end(countdown, _live.STATUS_BLOCKED, "",
                                    "the sending account changed during the countdown - nothing was sent")
                self._jobs.pop(action.id, None)
                logger.info("Action %s (%s, %s) not sent: the sending account changed during the countdown",
                            action.id, action.kind, logged_alias(action.account))
                self._activity(hud.TAG_STOP, f"Not sent: {_kind_title(action)}", _short(problem))
                self._hint(action.id, problem)
                self._sync_card_locks()
                self._refresh_actions_ui()
                self._tell_not_sent(action)
                return
        self._jobs[action.id] = _STAGE_WORKING
        self._running[action.id] = (action, True)
        self._show_card_state(action.id)
        if action.kind == CALENDAR:
            self._activity(hud.TAG_RUN, f"Adding {action.title}", "Google Calendar")
        elif action.kind == TODO:
            self._activity(hud.TAG_RUN, f"Adding a block for {_activity_title(action)}", "Google Calendar")
        else:
            self._activity(hud.TAG_RUN, f"Sending: {_action_phrase(action)}", self._activity_sub(action))
        _live_countdown_ran_out(countdown)
        task = countdown.task
        worker.execute(action, expected_from=countdown.sender, live=task if task is not _live.NO_TASK else None)

    def undo_action(self, action_id: str) -> None:
        """Undo during the countdown: nothing is sent and nothing is saved; the card waits again.

        A click in the first double-click interval after the approval is
        ignored, so a double click on Accept never undoes it at once.
        """
        countdown = self._countdown
        if countdown is None or countdown.action.id != action_id or self.state == STATE_QUITTING:
            return
        if (time.monotonic() - countdown.started) * 1000 < QApplication.doubleClickInterval():
            logger.info("Ignored an Undo click right after the approval")
            return
        action = countdown.action
        self._cancel_countdown("undone")
        nothing = "nothing was created" if _adds_event(action) else "nothing was sent"
        self._activity(hud.TAG_STOP, f"Undone: {_action_phrase(action)}",
                       f"{kind_label(action).upper()} {DOT} {nothing}")
        self._refresh_actions_ui()

    def _cancel_countdown(self, reason: str) -> None:
        countdown, self._countdown = self._countdown, None
        self._countdown_timer.stop()
        self._pump_voice()
        if countdown is None:
            return
        action_id = countdown.action.id
        self._jobs.pop(action_id, None)
        logger.info("Action %s undone (%s); nothing was sent", action_id, reason)
        nothing = "nothing was created" if _adds_event(countdown.action) else "nothing was sent"
        if reason == "undone":
            _live_countdown_end(countdown, _live.STATUS_CANCELLED, "UNDONE", nothing,
                                task_summary=f"Undone - {nothing}")
        else:
            _live_countdown_end(countdown, _live.STATUS_CANCELLED, "STOPPED", f"{reason} - {nothing}")
        self._show_card_state(action_id)
        self._sync_card_locks()

    def _send_in_flight(self) -> bool:
        """A call is queued or on its way to Google ("running" is or is about to be saved); not
        while its account's browser sign-in waits (nothing was sent then)."""
        return any(writes and self._jobs.get(action_id) == _STAGE_WORKING
                   for action_id, (_action, writes) in self._running.items())

    # ---- other decisions and the tools row -----------------------------------------------------

    def _mark_done(self, action: ProposedAction) -> None:
        """Done on a card Jarvis does not carry out: you handled it yourself."""
        logger.info("Marked action %s done", action.id)
        self._hints.pop(action.id, None)
        self._store.set(action.id, STATUS_DONE, kind=action.kind, account=action.account)
        self._show_card_state(action.id)
        self._activity(hud.TAG_DONE, f"Done: {_activity_title(action)}", kind_label(action).upper())
        self._refresh_actions_ui()

    def deny_action(self, action_id: str) -> None:
        if self._decision_click_too_soon() or self._locked_out(action_id):
            return
        action = self._current(action_id)
        if (action is None or not action.decidable or action_id in self._jobs
                or self._store.is_decided(action_id)):
            return
        logger.info("Denied action %s", action_id)
        self._hints.pop(action_id, None)
        self._store.set(action_id, STATUS_DENIED, kind=action.kind, account=action.account)
        self._show_card_state(action_id)
        if action.kind == CALENDAR:
            self._activity(hud.TAG_STOP, f"Denied {action.title}", "nothing was created")
        elif action.kind == TODO:
            self._activity(hud.TAG_STOP, f"Dismissed {_activity_title(action)}", "nothing was created")
        elif action.changes_event:   # Skip: its card reads SKIPPED
            self._activity(hud.TAG_STOP, f"Skipped {_kind_title(action)}", "nothing was sent")
        else:
            self._activity(hud.TAG_STOP, f"Dismissed {_kind_title(action)}", "nothing was sent")
        self._refresh_actions_ui()

    def open_action_source(self, action_id: str) -> None:
        """A card's Open (tools row): its own link, checked again, in the browser; never by itself.
        A web research card's link (``action.web``: a source's Open page, a Todo with a source's
        link) follows the web rule instead of the Open allowlist: any public https site
        (actions.web_link_allowed), never a local or private address, checked again here."""
        action = self._action(action_id)
        if action is None or self.state == STATE_QUITTING:
            return
        link = action.link or (self._google_link(action_id) if action.changes_event else "")
        if not link:
            return
        now = self._click_now()
        previous, self._source_opened_at[action_id] = self._source_opened_at.get(action_id, -math.inf), now
        if now - previous < _SOURCE_OPEN_GUARD_S:
            logger.info("Ignored a second Open of action %s within 1 s", action_id)
            return
        url = QUrl(link, QUrl.ParsingMode.StrictMode)
        host = url.host(QUrl.ComponentFormattingOption.FullyEncoded).rstrip(".").casefold()
        allowed = web_link_allowed(link) if action.web else link_allowed(link, self.config.actions.link_hosts)
        if not allowed or not url.isValid() or url.scheme() != "https" or host != link_host(link):
            logger.info("Not opening the link of action %s: not an allowed https link", action_id)
            return
        logger.info("Open the link of action %s (%s)", action_id, action.kind)
        QDesktopServices.openUrl(url)

    def copy_action_text(self, action_id: str) -> None:
        """A card's Copy (tools row): the drafted text as the card shows it on the clipboard;
        decides nothing."""
        action = self._current(action_id)
        if action is None or not action.body or self.state == STATE_QUITTING:
            return
        if not (self._copy_label(action) if action.changes_event else copy_text(action)):
            return
        QGuiApplication.clipboard().setText(action.body)
        card = self.window.reading.action_card(action_id)
        if card is not None:
            card.show_copied()
        self._activity(hud.TAG_DONE, copied_text(action), kind_label(action).upper())
        logger.info("Copied the text of action %s (%s, %d characters)", action_id, action.kind,
                    len(action.body))

    def open_action_link(self, link: str) -> None:
        """A result's Open (the event, or the sent message's thread): only an allowlisted https link."""
        if not link.startswith("https://") or not link_allowed(link, self.config.actions.link_hosts):
            logger.info("Not opening a result link that is not an allowed https link")
            return
        logger.info("Open the result of an action")
        QDesktopServices.openUrl(QUrl(link))

    # ---- the Edit dialog --------------------------------------------------------------------------

    def edit_action(self, action_id: str) -> None:
        """A card's Edit: the window-modal Edit dialog of an RSVP, Move, Cancel, Reply or Email.
        Save keeps the changes on the card (memory only, same id); nothing is sent."""
        if self.state == STATE_QUITTING or self._dialog is not None or self._account_dialog is not None:
            return
        if self._countdown is not None or self._jobs or self._connect_running:
            # The dialog is window-modal: beside another card's countdown it would cover that
            # card's Undo while the countdown runs out.
            logger.info("Ignored Edit of action %s: another card's approval or sign-in is running", action_id)
            return
        action = self._current(action_id)
        if action is None or not action.editable or self._store.is_decided(action_id):
            return
        if action.sends_mail:
            self._open_mail_dialog(action)
            return
        hour24 = self.config.display.hour24
        date_text, start_text, end_text = _time_texts(action, hour24)
        view = card_view(action, self._now().date())
        check_text, check_tip, _warn = self._check_view(action)
        dialog = hud.EditDialog(action_id, action.kind, kind_label(action), view.title,
                                answer=action.field("answer", "yes"), notify=action.field("notify", "all"),
                                date_text=date_text, start_text=start_text, end_text=end_text,
                                note=action.body, check_text=check_tip or check_text, hour24=hour24,
                                parent=self.window)
        dialog.saved.connect(self._on_edit_saved)
        dialog.finished.connect(self._on_edit_closed)
        self._dialog = dialog
        logger.info("Edit action %s (%s)", action_id, action.kind)
        dialog.open()
        self._sync_card_locks()

    def _open_mail_dialog(self, action: ProposedAction, banner: str = "") -> None:
        """The Edit dialog of a Reply / Email: FROM, To / Cc (new recipients red, each with a
        "Send to <address>" tick; the sending account's own address red with SENDING ACCOUNT, to
        remove), the subject and the message exactly as it will be sent."""
        if self.state == STATE_QUITTING or self._dialog is not None or self._account_dialog is not None:
            return
        mail = self._mail_status(action)
        to, cc = self._recipient_chips(action, mail)
        dialog = hud.EditDialog(action.id, action.kind, kind_label(action), action.title,
                                sender=mail.from_text.removeprefix("From: "), to=to, cc=cc, body=action.body,
                                classify=lambda address: self._recipient_state(mail, address),
                                normalize=email_address, find_links=body_links, banner=banner,
                                max_recipients=MAX_RECIPIENTS, parent=self.window)
        dialog.saved.connect(self._on_edit_saved)
        dialog.finished.connect(self._on_edit_closed)
        self._dialog = dialog
        logger.info("Edit action %s (%s)", action.id, action.kind)
        dialog.open()
        self._sync_card_locks()

    def _mail_edit(self, page: ProposedAction, values: Mapping[str, Any]) -> ActionEdit:
        """The Edit dialog's Reply / Email values as an edit of the page's line: only what differs from
        the line is changed (the recipients as the card shows them, without the account's own address,
        count as unchanged), so ticking new recipients alone is no edit of the message."""
        shown = self._mail_status(page).review

        def addresses(key: str, before: Sequence[str]) -> tuple[str, ...] | None:
            typed = tuple(values.get(key) or ())
            return None if [a.casefold() for a in typed] == [a.casefold() for a in before] else typed

        to = addresses("to", shown.to if shown is not None else page.recipients())
        cc = addresses("cc", shown.cc if shown is not None else page.cc())
        subject = values.get("subject") if page.kind == EMAIL else None
        body = values.get("body", "")
        return ActionEdit(to=to, cc=cc, subject=None if subject == page.title else subject,
                          body=None if body == page.body else body,
                          confirmed_new=frozenset(values.get("confirmed") or ()))

    def _on_edit_saved(self, action_id: str, values: Any) -> None:
        """Save in the Edit dialog: checked like a line; a problem keeps the dialog open."""
        dialog = self._dialog
        if dialog is None or dialog.action_id != action_id:
            return
        page = self._action(action_id)
        if page is None or self.state == STATE_QUITTING:
            dialog.reject()
            return
        if action_id in self._jobs or self._store.is_decided(action_id):
            dialog.show_error("This card is already being carried out or decided; nothing was changed")
            return
        values = dict(values) if isinstance(values, Mapping) else {}
        previous = self._effective(page)
        try:
            if page.sends_mail:
                edit = self._mail_edit(page, values)
            else:
                start = end = None
                if page.kind == MOVE:
                    start, end = parse_time_range(values.get("date", ""), values.get("start", ""),
                                                  values.get("end", ""))
                edit = ActionEdit(answer=values.get("answer") if page.kind == RSVP else None,
                                  notify=values.get("notify"), start=start, end=end, body=values.get("note", ""))
            edited = apply_edit(page, edit)
        except (EditInvalid, EditError) as exc:
            dialog.show_error(str(exc))
            return
        if edited == page:
            self._edits.pop(action_id, None)
        else:
            self._edits[action_id] = edit
        dialog.accept()
        self._hints.pop(action_id, None)
        card = self.window.reading.action_card(action_id)
        if card is not None:
            view = card_view(edited, self._now().date())
            card.set_texts(view.kind_label + (EDITED_MARK if self._is_edited(page) else ""), view.title,
                           _card_detail(edited, view, self._now().date()))
            card.set_body(view.body, body_links(edited.body) if edited.sends_mail else ())
        self._show_card_state(action_id)
        logger.info("Edited action %s (%s)", action_id, edited.kind)
        # Ticking new recipients alone leaves the message as it was: say what was saved.
        what = "Edited"
        if edited.sends_mail and not self._is_edited(page):
            what = "Recipients confirmed" if edited.confirmed != previous.confirmed else "Saved, unchanged"
        self._activity(hud.TAG_DONE, f"{what}: {_kind_title(edited)}",
                       f"{kind_label(edited).upper()} {DOT} nothing was sent")
        self._refresh_actions_ui()

    def _on_edit_closed(self, _result: int = 0) -> None:
        dialog, self._dialog = self._dialog, None
        if dialog is not None:
            if dialog.mail and dialog.whole_seen():
                # What the dialog showed whole (saved or not): Send needs no review while the card
                # says exactly this. A Cancel after typing leaves the card's text unread.
                values = dialog.values()
                self._reviewed[dialog.action_id] = _review_digest(values.get("subject", ""), values.get("body", ""))
            dialog.deleteLater()
        if self.state != STATE_QUITTING:
            self._sync_card_locks()

    # ---- results from the action worker ------------------------------------------------------------

    def _on_calendar_status(self, signed_in: bool) -> None:
        if self.state == STATE_QUITTING:
            return
        self._calendar_signed_in = bool(signed_in)
        self._update_service_chips()

    def _on_account_status(self, statuses: Any) -> None:
        """Which accounts are signed in (and why not): the cards' check lines and buttons."""
        if self.state == STATE_QUITTING or not isinstance(statuses, Mapping):
            return
        for alias, (signed_in, problem, message) in statuses.items():
            previous = self._accounts_state.get(alias)
            if not signed_in and not problem and previous is not None and not previous[0]:
                problem, message = previous[1], previous[2]   # keep why the last sign-in failed
            self._accounts_state[alias] = (bool(signed_in), problem or "", message or "")
        self._refresh_countdown_cards()
        self._update_service_chips()
        self._request_peeks()
        self._refresh_ask()   # which accounts' mail Ask may read

    def _request_peeks(self, actions: Sequence[ProposedAction] | None = None, *, force: bool = False) -> None:
        """Read the events of RSVP / Move / Cancel cards for their check lines: only signed-in
        accounts (never a sign-in), only cards still waiting, one read job at a time per card."""
        worker = self._calendar_worker
        if worker is None or self.state == STATE_QUITTING:
            return
        if actions is None:
            actions = [self._effective(action) for action in self._actions]
        todo = []
        for action in actions:
            if (not action.changes_event or action.id in self._peeking or action.id in self._jobs
                    or self._store.is_decided(action.id) or (action.id in self._checks and not force)):
                continue
            if self._executor.readiness(action) or not self._account_signed_in(action.account):
                continue
            todo.append(action)
        if not todo:
            return
        self._peek_seq += 1
        self._peeking.update(action.id for action in todo)
        self._live_peeks_started(len(todo))
        worker.peek(_PeekJob(self._peek_seq, tuple(todo)))
        for action in todo:
            self._show_card_state(action.id)

    def _on_event_peek(self, _request_id: int, results: Any) -> None:
        """Google's view of the cards' events came back: their check lines and buttons."""
        if self.state == STATE_QUITTING or not isinstance(results, Mapping):
            return
        today = self._now().date()
        accounts_changed = False
        for action_id, (details, error) in results.items():
            self._peeking.discard(action_id)
            action = self._current(action_id)
            if action is None:
                continue
            if details is not None:
                self._checks[action_id] = check_event(action, details, today)
                logger.info("Checked the event of action %s with Google", action_id)
                continue
            problem = getattr(error, "problem", "") or ""
            if isinstance(error, CalendarNotSignedIn) or problem in _SIGN_IN_PROBLEMS:
                # The account's saved sign-in is missing or was rejected: Sign in again.
                self._checks.pop(action_id, None)
                text = "" if problem in ("", PROBLEM_SIGNED_OUT) else str(error)
                previous = self._accounts_state.get(action.account)
                if not text and previous is not None and not previous[0] and previous[1]:
                    problem, text = previous[1], previous[2]   # keep why it was rejected (expired, ...)
                self._accounts_state[action.account] = (False, "" if problem == PROBLEM_SIGNED_OUT else problem,
                                                        text)
                accounts_changed = True
            else:
                self._checks[action_id] = check_failure(action, error)
            logger.info("Could not check the event of action %s (%s)", action_id, type(error).__name__)
        self._live_peeks_done(results)
        self._refresh_countdown_cards()
        if accounts_changed:
            self._update_service_chips()

    def _on_action_progress(self, action_id: str, stage: str) -> None:
        if self.state == STATE_QUITTING or action_id not in self._jobs:
            return
        running = self._running.get(action_id)
        alias = account_of(running[0]) if running is not None else DEFAULT_ACCOUNT
        if stage == _STAGE_SIGNIN:
            self._jobs[action_id] = _STAGE_SIGNIN
            self._activity(hud.TAG_WAIT, "Google sign-in", "finish in your browser")
        elif stage == _STAGE_SIGNED_IN:
            self._jobs[action_id] = _STAGE_WORKING
            self._accounts_state[alias] = (True, "", "")
            if alias == DEFAULT_ACCOUNT:
                self._calendar_signed_in = True
                self._connect_note = ""
                self._activity(hud.TAG_DONE, "Signed in to Google Calendar")
                self._request_agenda()   # queued behind this approval
            else:
                self._activity(hud.TAG_DONE, f"Signed in to Google ({alias} account)")
            self._update_service_chips()
        else:
            return   # the call itself is on its way: the card already reads WORKING...
        self._show_card_state(action_id)
        self._render_agenda()

    def _on_action_result(self, action_id: str, result: ExecResult | None, error: ExecError | None,
                          status: str) -> None:
        if self.state == STATE_QUITTING:
            return
        self._jobs.pop(action_id, None)
        running = self._running.pop(action_id, None)
        action = running[0] if running is not None else self._current(action_id)
        writes_running = running[1] if running is not None else bool(action is not None and action.countdown)
        if writes_running and action is not None and not _adds_event(action):
            self._countdown_result(action, result, error, status)
        else:   # the worker saved a countdown's result itself (writes_running)
            self._calendar_result(action_id, action, result, error, status, saved=writes_running)
        self._show_card_state(action_id)
        self._sync_card_locks()
        if self._calendar_worker is not None:
            self._calendar_worker.check_sign_in()   # a revoked sign-in turns the chip amber again
        self._refresh_actions_ui()
        self._render_agenda()   # a sign-in that waited may be over
        if (getattr(error, "problem", "") == PROBLEM_CONFIRM and action is not None
                and not action.sends_mail):
            # A sign-in during the run bound an account you have not confirmed (nothing was
            # changed): ask now, so the next click goes ahead.
            self._ask_account(account_of(action), card_id=action_id)

    def _calendar_result(self, action_id: str, action: ProposedAction | None, result: ExecResult | None,
                         error: ExecError | None, status: str, *, saved: bool = False) -> None:
        """A Calendar event or a Todo's block: created / exists / failed / unknown. ``saved``: the
        worker saved it already (after the countdown); else it is saved here (the older way)."""
        title = _activity_title(action) if action is not None else "the event"
        today = self._now().date()
        block = action is not None and action.kind == TODO
        event = action.block_event() if block else action   # a Todo's block: its time is the detail
        detail = event.describe(today) if event is not None else ""
        kind = action.kind if action is not None else ""
        account = action.account if action is not None else ""
        if result is not None and error is None:
            if not saved:
                self._store.set(action_id, status, link=result.link, kind=kind, account=account)
            if status == STATUS_EXISTS:
                self._activity(hud.TAG_DONE, f"Already on the calendar: {title}", detail)
            elif block:
                self._activity(hud.TAG_DONE, f"Added a block for {title}", detail)
            else:
                self._activity(hud.TAG_DONE, f"Added {title}", detail)
            self._request_agenda()   # the new event may be on today's agenda
            self._tell_result(action, STATUS_EXISTS if status == STATUS_EXISTS else STATUS_CREATED)
        else:
            message = (str(error) if error is not None else "") or "unknown error"
            if not saved:
                self._store.set(action_id, STATUS_FAILED, message=message, kind=kind, account=account)
            if status == STATUS_UNKNOWN and action is not None:
                self._activity(hud.TAG_WAIT, f"Unknown: {_action_phrase(action)}", UNKNOWN_CARD_TEXT)
            else:
                failed = f"Couldn't add a block for {title}" if block else f"Couldn't add {title}"
                self._activity(hud.TAG_STOP, failed, _short(message))
            self._tell_result(action, STATUS_UNKNOWN if status == STATUS_UNKNOWN else STATUS_FAILED)

    def _countdown_result(self, action: ProposedAction, result: ExecResult | None, error: ExecError | None,
                          status: str) -> None:
        """An RSVP, Move, Cancel, Reply or Email: the worker saved sent / failed / unknown over
        "running"."""
        self._hints.pop(action.id, None)
        label = kind_label(action).upper()
        mail = action.sends_mail
        sub = self._activity_sub(action) if mail else label
        if result is not None and error is None:
            self._carried[action.id] = action   # the card keeps showing what was sent
            text = result.result_text or result_text(action, STATUS_SENT) or "Done"
            self._activity(hud.TAG_DONE, f"{text}: {_kind_title(action)}", sub)
            if not mail and account_of(action) == DEFAULT_ACCOUNT:
                self._request_agenda()   # the agenda may show the change
            self._tell_result(action, STATUS_SENT)
            return
        message = (str(error) if error is not None else "") or "unknown error"
        problem = getattr(error, "problem", "") or ""
        self._tell_result(action, STATUS_UNKNOWN if status == STATUS_UNKNOWN else STATUS_FAILED)
        if mail:   # the worker's sign-in check refreshes the accounts; the card says why
            if status == STATUS_UNKNOWN:
                self._activity(hud.TAG_WAIT, f"Unknown: {_action_phrase(action)}", UNKNOWN_MAIL_CARD_TEXT)
            else:
                self._activity(hud.TAG_STOP, f"Couldn't send {_kind_title(action)}", _short(message))
            return
        if problem in _SIGN_IN_PROBLEMS:
            self._accounts_state[action.account] = (False, "" if problem == PROBLEM_SIGNED_OUT else problem,
                                                    "" if problem == PROBLEM_SIGNED_OUT else message)
            self._update_service_chips()
            self._refresh_countdown_cards()   # every card of that account: Sign in, and why
        if status == STATUS_UNKNOWN:
            self._activity(hud.TAG_WAIT, f"Unknown: {_action_phrase(action)}", UNKNOWN_CARD_TEXT)
        else:
            self._activity(hud.TAG_STOP, f"Couldn't {_VERBS.get(action.kind, 'do')} {_kind_title(action)}",
                           _short(message))
        self._request_peeks([action], force=True)   # Google's view now, before any Retry

    # ---- sign-in per account ---------------------------------------------------------------------

    def _signing_in(self, alias: str | None = None) -> bool:
        """A browser sign-in is queued or open (``alias``: that account's)."""
        if self._connect_alias is not None and alias in (None, self._connect_alias):
            return True
        for action_id, stage in self._jobs.items():
            if stage != _STAGE_SIGNIN:
                continue
            running = self._running.get(action_id)
            if alias is None or running is None or account_of(running[0]) == alias:
                return True
        return False

    def _set_signed_in(self, signed_in: bool) -> None:
        if signed_in != self._calendar_signed_in:
            self._calendar_signed_in = signed_in
            self._update_service_chips()

    def connect_calendar(self) -> None:
        """The agenda's Connect link: the personal account's Google sign-in, only on this click."""
        if self.state != STATE_READING or self._signing_in() or self._countdown is not None:
            return
        if self._start_calendar() is None:
            self._render_agenda()
            return
        logger.info("Connect Google Calendar")
        self._start_sign_in(DEFAULT_ACCOUNT)

    def sign_in_account(self, action_id: str) -> None:
        """A card's Sign in link: that account's Google sign-in in the browser, only on this click."""
        if self.state == STATE_QUITTING or self._signing_in() or self._countdown is not None or self._jobs:
            return
        action = self._current(action_id)
        if action is None or not action.changes_event or self._store.is_decided(action_id):
            return
        reason = self._executor.readiness(action)
        if not reason and self._start_calendar() is None:
            reason = CALENDAR_SETUP_NOTE
        if reason:
            self._hint(action_id, NOT_READY_NOTE if reason == self._check_view(action)[0] else reason)
            return
        logger.info("Sign in to Google for action %s (%s account)", action_id, logged_alias(action.account))
        self._start_sign_in(action.account, card_id=action_id)

    def _start_sign_in(self, alias: str, *, card_id: str = "", action: ProposedAction | None = None,
                       disconnect: bool = False, force: bool = False) -> None:
        """``alias``'s browser sign-in on the action worker; ``action``: a Reply / Email whose Send
        started it (sending must be allowed and the Google account known afterwards).
        ``disconnect``: forget the account's sign-in and binding first ("No, use another
        account"), so Google's chooser may pick another account. ``force``: sign in even when
        signed in already (one more consent, e.g. so Ask may read the account's mail)."""
        worker = self._start_calendar()
        if worker is None:
            return
        self._connect_alias = alias
        self._connect_card = card_id
        self._connect_mail = action is not None
        if alias == DEFAULT_ACCOUNT and action is None:
            self._connect_note = ""
        self._sync_card_locks()
        self._refresh_countdown_cards()
        self._update_service_chips()
        self._live_background().step(_live.ACCOUNT_SIGNIN, f"Google sign-in for {alias} in your browser",
                                     key=f"signin:{alias}", fields=[("Account", alias)])
        worker.connect(alias, action, disconnect=disconnect, force=force)
        self._render_agenda()

    # ---- the first sign-in's question: is it the right Google account? -----------------------

    def _account_unknown(self, alias: str) -> bool:
        """``alias``'s saved sign-in is a Google account Jarvis has no binding for (accounts.json deleted,
        edited or unreadable): its next click signs in again, which binds and asks (files only)."""
        try:
            return self._executor.change_problem(alias) == PROBLEM_IDENTITY
        except Exception as exc:  # noqa: BLE001 - the executor still refuses to act
            logger.debug("Could not check the %s account's binding (%s)", logged_alias(alias), type(exc).__name__)
            return False

    def _account_unconfirmed(self, alias: str) -> bool:
        """``alias`` has no Google account you confirmed: its next sign-in leaves one to confirm."""
        try:
            return self._executor.needs_confirmation(alias)
        except Exception as exc:  # noqa: BLE001 - the executor still refuses to act
            logger.debug("Could not check the %s account's binding (%s)", logged_alias(alias), type(exc).__name__)
            return False

    def _pending_account(self, alias: str) -> str:
        """The address ``alias`` was bound to while you have not confirmed it ("" = nothing to ask)."""
        try:
            return self._executor.pending_confirmation(alias)
        except Exception as exc:  # noqa: BLE001 - nothing to ask; the executor still refuses to act
            logger.debug("Could not read the %s account's binding (%s)", logged_alias(alias), type(exc).__name__)
            return ""

    def _ask_account(self, alias: str, *, card_id: str = "", action: ProposedAction | None = None) -> bool:
        """Ask "Signed in as X for "alias" - is that right?" while ``alias``'s binding is not
        confirmed (hud.AccountDialog): True when there is something to confirm (the dialog is open
        now), False when there is not. Nothing is sent or changed for the alias until Yes."""
        address = self._pending_account(alias)
        if not address or self.state == STATE_QUITTING:
            return False
        if self._account_dialog is not None or self._dialog is not None:
            return True
        dialog = hud.AccountDialog(alias, address, sends_mail=self._sends_mail(alias), parent=self.window)
        dialog.answered.connect(self._on_account_answered)
        dialog.finished.connect(self._on_account_dialog_closed)
        self._account_dialog = dialog
        self._confirm_card, self._confirm_action = card_id, action
        if card_id:
            self._hints[card_id] = ACCOUNT_CONFIRM_NOTE.format(alias=alias)
            self._show_card_state(card_id)   # a Calendar event's or Todo's card too
        self._refresh_countdown_cards()
        logger.info("Asking whether the %s account's Google account is the right one", logged_alias(alias))
        self._activity(hud.TAG_WAIT, f"Is this the right Google account? ({alias} account)",
                       "answer in the dialog - nothing is sent until you do")
        dialog.open()
        self._sync_card_locks()
        return True

    def _confirm_button(self, card_id: str) -> str:
        card = self.window.reading.action_card(card_id) if card_id else None
        return card.approve_text() if card is not None else "Send"

    def _on_account_answered(self, alias: str, address: str, yes: bool) -> None:
        """Yes: the binding is confirmed (accounts.json) and the card says what is next. No: the
        account is disconnected and its sign-in opens again, to pick another account."""
        if self.state == STATE_QUITTING:
            return
        card_id, action = self._confirm_card, self._confirm_action
        card_action = self._current(card_id) if card_id else None
        button = self._confirm_button(card_id)
        if yes:
            if self._executor.confirm_account(alias, address):
                logger.info("The %s account's Google account was confirmed", logged_alias(alias))
                self._activity(hud.TAG_DONE, f"Google account confirmed ({alias} account)", "nothing was sent")
                if card_id:
                    if card_action is not None and card_action.sends_mail:
                        sender = self._mail_status(card_action).from_text.removeprefix("From: ")
                        self._hints[card_id] = ACCOUNT_CONFIRMED_MAIL_NOTE.format(sender=sender)
                    elif card_action is not None and _adds_event(card_action):
                        self._hints[card_id] = ACCOUNT_CONFIRMED_ADD_NOTE.format(button=button)
                    else:
                        self._hints[card_id] = ACCOUNT_CONFIRMED_NOTE.format(button=button)
            else:
                logger.info("The %s account's binding changed before it was confirmed", logged_alias(alias))
                if card_id:
                    self._hints[card_id] = ACCOUNT_CONFIRM_FAILED_NOTE.format(alias=alias, button=button)
        else:
            logger.info("The %s account's Google account was not the right one: signing in again",
                        logged_alias(alias))
            self._activity(hud.TAG_STOP, f"Not the right Google account ({alias} account)",
                           "signing in again - nothing was sent")
            if card_id:
                self._hints[card_id] = ACCOUNT_RECONNECT_NOTE.format(alias=alias, button=button)
            mail = action if action is not None and action.sends_mail else None
            self._start_sign_in(alias, card_id=card_id, action=mail, disconnect=True)
        if card_id:   # a Calendar event's or Todo's card is not among the countdown cards
            self._show_card_state(card_id)
        self._refresh_countdown_cards()
        self._update_service_chips()
        self._render_agenda()

    def _on_account_dialog_closed(self, _result: int = 0) -> None:
        dialog, self._account_dialog = self._account_dialog, None
        card_id = self._confirm_card
        self._confirm_card, self._confirm_action = "", None
        if dialog is not None:
            dialog.deleteLater()
            if card_id and self._hints.get(card_id, "").startswith(ACCOUNT_CONFIRM_NOTE.split("{", 1)[0]):
                # Closed without an answer: the card says how to answer later (a Reply / Email's
                # own note says it already).
                card_action = self._current(card_id)
                if card_action is not None and card_action.sends_mail:
                    self._hints.pop(card_id, None)
                else:
                    self._hints[card_id] = ACCOUNT_NOT_CONFIRMED_NOTE.format(button=self._confirm_button(card_id),
                                                                             alias=dialog.alias)
        if self.state != STATE_QUITTING:
            if card_id:
                self._show_card_state(card_id)
            self._refresh_countdown_cards()
            self._sync_card_locks()

    def _on_account_connect(self, alias: str, stage: str, message: str, problem: str) -> None:
        if self.state == STATE_QUITTING or self._connect_alias != alias:
            return
        self._live_sign_in(alias, stage, message)
        personal = alias == DEFAULT_ACCOUNT
        if stage == _CONNECT_SIGN_IN:
            self._activity(hud.TAG_WAIT, "Google sign-in",
                           "finish in your browser" if personal else f"{alias} account {DOT} finish in your browser")
            return
        card_id = self._connect_card
        self._connect_alias, self._connect_card = None, ""
        self._refresh_ask()   # a sign-in may have allowed (or not) reading this account's mail
        if stage == _CONNECT_KEPT:
            self._connect_mail = False
            self._account_kept(alias, message, card_id)
            return
        if self._connect_mail:
            self._connect_mail = False
            self._mail_connected(alias, stage, message, problem, card_id)
            return
        if stage == _CONNECT_FAILED:
            note = message or "the sign-in did not finish"
            self._accounts_state[alias] = (False, problem or PROBLEM_FAILED, note)
            if personal:
                self._connect_note = note
            failed_action = self._current(card_id) if card_id else None
            if failed_action is not None and _adds_event(failed_action):   # its button is Approve, not Sign in
                self._hints[card_id] = f"{note} - click {self._confirm_button(card_id)} to try again"
            elif card_id:   # the card whose click started it says why, in full
                self._hints[card_id] = sign_in_note(alias, problem=problem or PROBLEM_FAILED, message=note,
                                                    sends_mail=self._sends_mail(alias),
                                                    reads_mail=self._reads_mail(alias))
            self._activity(hud.TAG_STOP, "Google sign-in did not finish" if personal
                           else f"Google sign-in did not finish ({alias} account)", _short(note))
            if self._calendar_worker is not None:
                self._calendar_worker.check_sign_in()
        else:
            self._accounts_state[alias] = (True, "", "")
            if stage == _CONNECT_SIGNED_IN:
                self._activity(hud.TAG_DONE, "Signed in to Google Calendar" if personal
                               else f"Signed in to Google ({alias} account)")
            if personal:
                self._set_signed_in(True)
                self._request_agenda()
            card = self.window.reading.action_card(card_id) if card_id else None
            if card is not None:
                card_action = self._current(card_id)
                adds = card_action is not None and _adds_event(card_action)
                self._hints[card_id] = (ADD_SIGNED_IN_NOTE if adds else SIGNED_IN_NOTE).format(
                    button=card.approve_text())
            self._request_peeks([self._effective(action) for action in self._actions
                                 if action.account == alias], force=True)
        if card_id:   # a Calendar event's or Todo's card is not among the countdown cards
            self._show_card_state(card_id)
        self._sync_card_locks()
        self._refresh_countdown_cards()
        self._update_service_chips()
        self._render_agenda()
        if stage in (_CONNECT_SIGNED_IN, _CONNECT_READY):   # a first sign-in: is it the right Google account?
            self._ask_account(alias, card_id=card_id)

    def _account_kept(self, alias: str, message: str, card_id: str) -> None:
        """"No, use another account" could not forget the old sign-in (its file is in use): nothing
        changed, the account is still signed in and not confirmed, and no sign-in opened. The card
        says so; its next click asks again."""
        note = message or DISCONNECT_FAILED_MESSAGE.format(alias=alias)
        if card_id:
            self._hints[card_id] = DISCONNECT_FAILED_NOTE.format(message=note, button=self._confirm_button(card_id))
            self._show_card_state(card_id)
        self._activity(hud.TAG_STOP, f"Could not forget the Google sign-in ({alias} account)",
                       "nothing was sent - try again")
        if self._calendar_worker is not None:
            self._calendar_worker.check_sign_in()
        self._sync_card_locks()
        self._refresh_countdown_cards()
        self._update_service_chips()
        self._render_agenda()

    def _mail_connected(self, alias: str, stage: str, message: str, problem: str, card_id: str) -> None:
        """A Reply / Email's sign-in finished: its card says what is next (check From, click Send
        again) or why it failed. The calendar's state of that account is read again by the worker
        (a partial grant may leave Calendar working while sending is not allowed, or the other way)."""
        card_action = self._current(card_id) if card_id else None
        if stage == _CONNECT_FAILED:
            note = message or "the sign-in did not finish"
            if card_id:
                tools = hand_off_tools(card_action) if card_action is not None else "Copy and Open"
                self._hints[card_id] = mail_note(alias, problem or PROBLEM_FAILED, note, tools=tools)
            self._activity(hud.TAG_STOP, f"Google sign-in did not finish ({alias} account)", _short(note))
        else:
            if stage == _CONNECT_SIGNED_IN:
                self._activity(hud.TAG_DONE, f"Signed in to Google ({alias} account)", "sending email")
            if card_id:
                sender = (self._mail_status(card_action).from_text.removeprefix("From: ")
                          if card_action is not None and card_action.sends_mail else alias)
                self._hints[card_id] = MAIL_SIGNED_IN_NOTE.format(sender=sender)
            if alias == DEFAULT_ACCOUNT:
                self._request_agenda()
            self._request_peeks([self._effective(action) for action in self._actions
                                 if action.account == alias], force=True)
        if self._calendar_worker is not None:
            self._calendar_worker.check_sign_in()
        self._sync_card_locks()
        self._refresh_countdown_cards()
        self._update_service_chips()
        self._render_agenda()
        if stage == _CONNECT_SIGNED_IN:   # a first sign-in: is it the right Google account?
            self._ask_account(alias, card_id=card_id, action=card_action)

    # ---- agenda: TODAY / TOMORROW and DEADLINES -----------------------------------------------

    def _agenda_active(self) -> bool:
        """The reading screen is up and visible: the only time the agenda refreshes by itself."""
        window = self.window
        return (self.state == STATE_READING and window.is_reading_view() and window.isVisible()
                and not window.isMinimized())

    def _sync_agenda_timer(self) -> None:
        """The agenda timer runs only while the reading screen is visible; it catches up when shown."""
        if not self._agenda_active():
            self._agenda_timer.stop()
            return
        if not self._agenda_timer.isActive():
            self._agenda_timer.start()
            self._on_agenda_tick()

    def _on_agenda_tick(self) -> None:
        """Every minute while visible: read again when 10 minutes have passed, re-draw the times."""
        if not self._agenda_active():
            self._agenda_timer.stop()
            return
        if time.monotonic() - self._agenda_requested_at >= _AGENDA_REFRESH_S:
            self._request_agenda()
        self._render_agenda()

    def _request_agenda(self) -> None:
        """Read the agenda on the calendar worker: one read at a time, never a sign-in."""
        if self.state != STATE_READING or not self.config.calendar.enabled:
            return
        worker = self._start_calendar()
        if worker is None:
            return
        if self._agenda_job is not None:
            self._agenda_again = True   # once the read on its way answers
            return
        agenda = self.config.agenda
        start, end = _agenda_fetch_range(self._now(), agenda)
        self._agenda_seq += 1
        self._agenda_job = _AgendaJob(self._agenda_seq, start, end, tuple(agenda.calendars))
        self._agenda_requested_at = time.monotonic()
        if self._agenda_events is None and (self._agenda_status == _AGENDA_INIT or (
                self._agenda_status == _AGENDA_SIGNED_OUT and self._calendar_signed_in)):
            self._agenda_status = _AGENDA_LOADING
        self._live_agenda_started(self._agenda_job)
        worker.list_agenda(self._agenda_job)

    def _on_agenda_result(self, request_id: int, events: list[CalendarEvent] | None, problem: str,
                          message: str) -> None:
        job = self._agenda_job
        if self.state == STATE_QUITTING or job is None or job.request_id != request_id:
            return   # stale: not the read this controller waits for
        self._agenda_job = None
        self._live_agenda_done(events, problem, message)
        if not problem:
            self._agenda_events, self._agenda_range = list(events or ()), (job.start, job.end)
            self._remember_days(DEFAULT_ACCOUNT, _pictures.SOURCE_AGENDA, job.start, job.end, self._agenda_events,
                                self._now())
            self._agenda_status, self._agenda_problem, self._connect_note = _AGENDA_OK, "", ""
            logger.debug("Agenda: %d event(s)", len(self._agenda_events))
            self._set_signed_in(True)
        elif problem == _PROBLEM_ERROR:
            self._agenda_status, self._agenda_problem = _AGENDA_ERROR, message   # older rows stay
        else:
            self._agenda_events = self._agenda_range = None
            self._agenda_status = _AGENDA_SETUP if problem == _PROBLEM_SETUP else _AGENDA_SIGNED_OUT
            self._agenda_problem = message
            if problem == _PROBLEM_SIGNED_OUT and not self._signing_in(DEFAULT_ACCOUNT):
                self._set_signed_in(False)
        if self._agenda_again:
            self._agenda_again = False
            self._request_agenda()
        self._render_agenda()

    def _agenda_covers(self, start: datetime, end: datetime) -> bool:
        """The last read covers the agenda day from ``start`` to ``end``."""
        if self._agenda_range is None:
            return False
        first, last = self._agenda_range
        try:
            return first <= start and end <= last
        except TypeError:   # naive and aware times mixed: never ask again because of it
            return True

    def _render_agenda(self) -> None:
        """TODAY / TOMORROW (rows or one state line) and DEADLINES, as of now."""
        panel = self.window.reading.agenda_panel
        agenda = self.config.agenda
        now = self._now()
        label, start, end = agenda_window(now, agenda.evening_from_hour)
        events = self._agenda_events
        if events is not None and not self._agenda_covers(start, end):
            events = None   # the day moved past what was read (after midnight, at 18:00): read again
            if self._agenda_job is None:
                self._request_agenda()
        panel.set_title(label)
        if events is None:
            text, color, link, tip = self._agenda_line()
            panel.set_meta("")
            # Google's error text and sign-in messages: never read as HTML.
            panel.set_message(text, color, link=link, tooltip=hud.plain_tooltip(tip))
        else:
            self._show_agenda_rows(panel, _agenda_rows(events, now, start, end, self.config.display.hour24))
        self._show_deadlines(panel, now)

    def _agenda_line(self) -> tuple[str, str, str, str]:
        """(text, colour, link, tooltip) of TODAY when there are no rows to show."""
        status = self._agenda_status
        if not self.config.calendar.enabled:
            return AGENDA_OFF_TEXT, hud.TEXT_DIM, "", ""
        if status == _AGENDA_SETUP or not self._calendar_configured():
            return AGENDA_SETUP_TEXT, hud.TEXT_DIM, "", self._agenda_problem
        if self._signing_in(DEFAULT_ACCOUNT):
            return AGENDA_SIGN_IN_TEXT, hud.TEXT_SOFT, "", "Finish the Google sign-in in your browser"
        if status == _AGENDA_SIGNED_OUT:
            if self._connect_note:
                return _short(self._connect_note, 70), hud.AMBER, AGENDA_CONNECT_LINK, self._connect_note
            return (AGENDA_CONNECT_TEXT, hud.TEXT_SOFT, AGENDA_CONNECT_LINK,
                    "Opens the Google sign-in in your browser")
        if status == _AGENDA_ERROR:
            return AGENDA_ERROR_TEXT, hud.AMBER, "", self._agenda_problem
        return AGENDA_LOADING_TEXT, hud.TEXT_DIM, "", ""

    def _show_agenda_rows(self, panel: hud.AgendaPanel, rows: list[hud.AgendaRowInfo]) -> None:
        stale = self._agenda_status == _AGENDA_ERROR   # the last read failed: older rows
        if stale:
            panel.set_meta("not updated", hud.AMBER)
        else:
            panel.set_meta(_plural(len(rows), "block") if rows else "", hud.TEXT_DIM)
        panel.meta_label.setToolTip(hud.plain_tooltip(self._agenda_problem) if stale else "")
        if rows:
            panel.set_events(rows)
        else:
            panel.set_message(AGENDA_EMPTY_TEXT, hud.TEXT_DIM)

    def _show_deadlines(self, panel: hud.AgendaPanel, now: datetime) -> None:
        """The page's deadlines and calendar events named like deadlines, soonest first."""
        agenda = self.config.agenda
        from_calendar = deadlines_from_events(self._agenda_events or (), now, agenda.deadline_days,
                                              agenda.deadline_keywords)
        items = merge_deadlines(self._page_deadlines, from_calendar, now, agenda.deadline_days)
        panel.set_deadlines(_deadline_rows(items, now), f"No deadlines in the {_next_days(agenda.deadline_days)}")
        panel.set_deadlines_meta(_plural(agenda.deadline_days, "day"))   # the horizon: "14 DAYS"

    # ---- the LIVE view's "Background reads" ([live] background) -----------------------------------

    @_live.quiet(_live.NO_TASK)
    def _live_background(self) -> Any:
        """The rolling "Background reads" task (pinned, never finished; its steps are replaced in
        place), or NO_TASK when [live] background is off."""
        live_config = getattr(self.config, "live", None)
        if not getattr(live_config, "background", True) or not self.live.enabled:
            return _live.NO_TASK
        return self.live.task(_live.TASK_BACKGROUND, _live.BACKGROUND_TITLE, compact=True, key=_live.BACKGROUND_KEY)

    @_live.quiet()
    def _live_agenda_started(self, job: _AgendaJob) -> None:
        first, last = job.start, job.end
        span = (f"{_WEEKDAYS[first.weekday()]} {_MONTHS[first.month - 1]} {first.day} to "
                f"{_WEEKDAYS[last.weekday()]} {_MONTHS[last.month - 1]} {last.day}")
        self._live_background().step(_live.AGENDA_READ, "Agenda: read the personal calendar (read only)",
                                     key="agenda", fields=[("Range", span),
                                                           ("Calendars", ", ".join(job.calendar_ids))])

    @_live.quiet()
    def _live_agenda_done(self, events: Any, problem: str, message: str) -> None:
        step = self._live_background().find(_live.AGENDA_READ, "agenda")
        if not problem:
            step.field("Events", str(len(events or ())))
            step.done(_live.STATUS_OK, summary=_plural(len(events or ()), "event"))
        elif problem == _PROBLEM_ERROR:
            step.done(_live.STATUS_FAILED, summary=message or "Google Calendar could not be read")
        else:
            step.done(_live.STATUS_WARN, summary=message or "not signed in - not read")

    @_live.quiet()
    def _remember_days(self, alias: str, source: str, start: datetime, end: datetime, events: Any,
                       at: datetime) -> None:
        """A calendar read Jarvis did anyway, kept for the LIVE day pictures (memory only; only while
        LIVE keeps pictures)."""
        if self.live.enabled and self.live.pictures:
            self._known_days.remember(alias, source, start, end, events or (), at=at)

    @_live.quiet()
    def _remember_ask_read(self, outcome: Any) -> None:
        read = getattr(outcome, "calendar_read", None)
        if read is None:
            return
        for alias, briefs in read.events:
            self._remember_days(alias, _pictures.SOURCE_ASK, read.start, read.end, briefs, read.at)

    @_live.quiet(None)
    def _live_day_picture(self, action: ProposedAction) -> Any:
        """A calendar card's change drawn on its day (no network: what Jarvis already read)."""
        return _pictures.day_change_picture(action, now=self._now(), hour24=self.config.display.hour24,
                                            check=self._checks.get(action.id), known=self._known_days)

    @staticmethod
    def _live_peeks_title(count: int) -> str:
        cards = "1 card's" if count == 1 else f"{count} cards'"
        return f"Checked {cards} events with Google (read only)"

    @_live.quiet()
    def _live_peeks_started(self, count: int) -> None:
        """A read job for cards' events: one "event checks" step for every job on its way (a second
        job while one runs adds its cards to the same step, which ends when the last job is back)."""
        jobs, cards, _problems = self._live_peeks
        background = self._live_background()
        if jobs:
            self._live_peeks = [jobs + 1, cards + count, _problems]
            background.find(_live.EVENT_CHECK, "peek").update(title=self._live_peeks_title(cards + count))
            return
        self._live_peeks = [1, count, 0]
        background.step(_live.EVENT_CHECK, self._live_peeks_title(count), key="peek")

    @_live.quiet()
    def _live_peeks_done(self, results: Any) -> None:
        jobs, cards, problems = self._live_peeks
        jobs = max(0, jobs - 1)
        step = self._live_background().find(_live.EVENT_CHECK, "peek")
        for action_id, (details, _error) in results.items():
            check = self._checks.get(action_id)
            if details is not None and check is not None:
                step.item(check.text, status=_live.STATUS_OK if check.allowed else _live.STATUS_WARN,
                          note=check.reason)
            else:
                problems += 1
                step.item(check.text if check is not None else "not checked (the account is not signed in)",
                          status=_live.STATUS_WARN)
        self._live_peeks = [jobs, cards, problems]
        if jobs:
            return   # another job's events are still being read
        step.done(_live.STATUS_WARN if problems else _live.STATUS_OK,
                  summary=f"{_plural(cards, 'event')} checked")

    @_live.quiet()
    def _live_sign_in(self, alias: str, stage: str, message: str) -> None:
        step = self._live_background().find(_live.ACCOUNT_SIGNIN, f"signin:{alias}")
        if stage == _CONNECT_SIGN_IN:
            step.note("Google's sign-in is open in your browser")
        elif stage == _CONNECT_SIGNED_IN:
            step.done(_live.STATUS_OK, summary="signed in")
        elif stage == _CONNECT_READY:
            step.done(_live.STATUS_OK, summary="was signed in already")
        elif stage == _CONNECT_KEPT:
            step.done(_live.STATUS_BLOCKED, summary=message or "the old sign-in could not be forgotten")
        else:
            step.done(_live.STATUS_FAILED, summary=message or "the sign-in did not finish")

    # ---- Ask Jarvis -------------------------------------------------------------------------

    def _create_ask(self, factory: Callable[..., Any] | None) -> Any:
        """The command bar's controller while [ask] is enabled; None otherwise, or when the planner
        could not be set up (the bar stays hidden and nothing else changes)."""
        if not self.config.ask.enabled:
            return None
        from . import ask_ui

        try:
            planner = (factory or ask_ui.default_planner)(self.config, self._calendars, self._senders,
                                                          self._google_accounts)
        except Exception as exc:  # noqa: BLE001 - the briefing works without Ask
            logger.warning("Could not set up Ask Jarvis (%s)", type(exc).__name__)
            return None
        if self._snapshots is not None and hasattr(planner, "page_snapshots"):
            planner.page_snapshots = self._snapshots   # the pages a research read or cites, pictured in LIVE
        reading = self.window.reading
        controller = ask_ui.AskController(self.config, planner, reading.command_bar, context=self._ask_context,
                                          blocked=self._ask_blocked, calendars=self._calendars, live=self.live,
                                          parent=self)
        controller.busyChanged.connect(self._on_ask_busy)
        controller.outcomeReady.connect(self._on_ask_outcome)
        controller.askStarted.connect(self._on_ask_started)
        controller.askCancelled.connect(self._on_ask_cancelled)
        controller.chipChanged.connect(self._set_ask_chip)
        controller.mailSignInRequested.connect(self.sign_in_for_mail)
        reading.command_bar.cancelRequested.connect(self._on_ask_cancel_clicked)   # after the controller's
        reading.set_ask_available(True)
        self._set_ask_chip(*controller.chip())
        return controller

    def _set_ask_chip(self, state: str, tooltip: str) -> None:
        """The header's ``claude`` chip (reading screen only): OK, SIGN IN, LIMIT, OFF or ERR."""
        from .ask_ui import CHIP_STATUSES

        header = self.window.header
        header.set_service(ASK_CHIP, CHIP_STATUSES.get(state, hud.STATUS_OFF), tooltip)
        header.set_service_visible(ASK_CHIP, self.window.is_reading_view())

    def _ask_context(self) -> tuple[Any, list[str]]:
        """What an Ask may see of the briefing (its pending cards, sections and deadlines; never the
        Ask cards) and the ids of the briefing's cards (an Ask card with one of them is information
        only). GUI thread."""
        from .ask.planner import briefing_context

        page_cards = [action for action in self._actions if action.source != SOURCE_ASK]
        try:
            context = briefing_context(self._briefing, self._pending_page_actions(), self._page_deadlines,
                                       config=self.config, now=self._now())
        except Exception as exc:  # noqa: BLE001 - the Ask goes on with the calendar alone
            logger.warning("Ask: could not prepare the briefing for the request (%s)", type(exc).__name__)
            context = None
        return context, [action.id for action in page_cards]

    def _ask_blocked(self) -> str:
        """Why an Ask may not start now ("" when it may): a Google sign-in is open in the browser
        (the calendar can't be read meanwhile)."""
        if self.state == STATE_QUITTING:
            return "Jarvis is closing"
        if self._signing_in():
            return ASK_SIGN_IN_FIRST
        return ""

    def _on_ask_busy(self, busy: bool) -> None:
        """An Ask started (the briefing pauses; the orb shows PLANNING; Jarvis says "One moment,
        sir.") or ended."""
        self._ask_busy = bool(busy)
        if busy:
            self._voice_call("owner_acted")   # the Ask holds the briefing now: the voice never resumes it
            if self.state == STATE_READING and self.player.state in (PLAYING, WAITING):
                self.player.pause()
            self._say_ack()
        self._update_reading_controls()

    def _say_ack(self) -> None:
        """A short acknowledgement while an Ask plans (dropped if the answer is there first)."""
        assistant = self.config.assistant
        if not assistant.speak_replies:
            return
        self._last_ack = pick_ack(self._last_ack, self._rng)
        self._say(KIND_ACK, with_address(self._last_ack, assistant.address), key=ASK_ACK_KEY)

    def _on_ask_started(self, _seq: int, text: str) -> None:
        """An Ask really started: the owner's words as a YOU entry, and the LIVE tab comes up
        ([live] auto_open = "tab", the default: every step as it happens; a "web:" request's WEB task
        as an Ask's), else the JARVIS tab (it shows the whole answer when it arrives)."""
        if self.state == STATE_QUITTING:
            return
        self._you(text)
        self._ask_task_id = self.ask.task_id if self.ask is not None else 0
        kind = (self.ask.running_kind if self.ask is not None else "") or _live.TASK_ASK
        if not self._live_auto_open(kind, self._ask_task_id):
            self.window.reading.set_tab(hud.TAB_JARVIS)

    def _on_ask_cancel_clicked(self) -> None:
        """Cancel on the bar: the running Ask's page pictures stop at once (the run itself stops
        within a second; _on_ask_cancelled follows)."""
        ask = self.ask
        task_id = ask.task_id if ask is not None and ask.busy else 0
        if task_id:
            self._snapshots_call("cancel_task", task_id)

    def _on_ask_cancelled(self, _seq: int) -> None:
        if self._ask_task_id:
            self._snapshots_call("cancel_task", self._ask_task_id)   # its queued and running pictures stop
        if self.state == STATE_QUITTING:
            return
        self._voice_call("cancel_key", ASK_ACK_KEY)
        self._jarvis(ASK_CANCELLED_ENTRY, tone=hud.TONE_IDLE)

    def _converse_ask(self, outcome: Any) -> None:
        """A finished Ask in the conversation: one JARVIS entry with the whole answer (the planner's
        say and question and Jarvis's own note; the bar above keeps its short preview) or the whole
        reason nothing was proposed; then what Jarvis says about it ([assistant] speak_replies)."""
        from .ask_ui import _SOFT_KINDS, AskController

        assistant = self.config.assistant
        if outcome.ok and getattr(outcome, "research", None) is not None:
            self._converse_research(outcome)
            return
        if outcome.ok:
            decidable = sum(1 for card in outcome.cards if not card.error)
            sub = [f"{_plural(decidable, 'proposal')} under NEEDS YOUR OK" if decidable else "no proposals"]
            if outcome.runs:
                sub.append(f"{outcome.duration_ms / 1000:.1f} s")
            question_only = bool(outcome.question) and not outcome.cards
            task_id = self._ask_task_id
            link = (SEE_STEPS_TEXT, f"live:{task_id}") if self.live.enabled and task_id else ("", "")
            self._jarvis(AskController._answer(outcome), tone=hud.TONE_WARN if question_only else hud.TONE_DONE,
                         sub=f" {DOT} ".join(sub), link=link)
            if assistant.speak_replies:
                reply = ask_reply_speech(outcome, assistant.address)
                if reply.guarded:
                    logger.info("Ask reply not spoken as written (it claimed something was done)")
                self._say(KIND_REPLY, reply.text)
            return
        if outcome.kind in ("empty", "busy"):
            return
        if outcome.kind == "cancelled":
            self._on_ask_cancelled(0)
            return
        self._jarvis(outcome.message or "Ask failed; nothing was proposed.",
                     tone=hud.TONE_WARN if outcome.kind in _SOFT_KINDS else hud.TONE_ERROR)
        spoken = ask_failure_speech(outcome.kind, assistant.address) if assistant.speak_replies else ""
        if spoken:
            self._say(KIND_REPLY, spoken)
        else:
            self._voice_call("cancel_key", ASK_ACK_KEY)

    def _converse_research(self, outcome: Any) -> None:
        """A web research's answer in the conversation: one JARVIS entry with the answer and its
        numbered sources (each with its site; what was left out and why), a sub line of counts and
        "See every step"; then Jarvis says the answer without its [n] marks, that the sources are on
        screen and how many suggestions wait for the OK (persona.research_reply_speech)."""
        from .ask.research_validate import cards_words, entry_text, steps_words
        from .ask.research_validate import notes as research_notes
        from .ask_ui import research_proposals

        assistant = self.config.assistant
        report = outcome.research
        text = entry_text(report)
        # Jarvis's own notes the entry does not already show (a thread the planner could not read,
        # Claude Code refusing its web tools); the left-out lines are in the entry itself.
        shown = {note for note in research_notes(report) if "left out" in note}
        extra = [note for note in (outcome.message or "").split("; ") if note and note not in shown]
        for note in extra:
            note = note[:1].upper() + note[1:]
            text += "\n" + (note if note.endswith((".", "!", "?", ")")) else note + ".")
        proposals = research_proposals(outcome)
        cards = cards_words(len(report.sources), proposals)
        sub = ["web research", f"{cards} under NEEDS YOUR OK" if report.sources or proposals else cards]
        if outcome.runs:
            sub.append(f"{outcome.duration_ms / 1000:.1f} s")
        sub.append(steps_words(report))
        task_id = self._ask_task_id
        link = (SEE_STEPS_TEXT, f"live:{task_id}") if self.live.enabled and task_id else ("", "")
        self._jarvis(text, tone=hud.TONE_DONE, sub=f" {DOT} ".join(sub), link=link)
        if assistant.speak_replies:
            reply = research_reply_speech(report.answer, sources=len(report.sources), cards=proposals,
                                          address=assistant.address)
            if reply.guarded:
                logger.info("Web research answer not spoken as written (it claimed something was done)")
            self._say(KIND_REPLY, reply.text)

    def _on_ask_outcome(self, outcome: Any) -> None:
        """A finished Ask: its cards go under ASK (above the briefing's; a countdown on another
        card goes on), the conversation gets the whole answer (and Jarvis says it), and ACTIVITY
        gets one row of counts (never the request or the answer)."""
        if self.state == STATE_QUITTING:
            return
        self._remember_ask_read(outcome)
        self._converse_ask(outcome)
        report = getattr(outcome, "research", None) if outcome.ok else None
        if report is not None:
            # Web research: counts only (never the question, the answer, a title or a site).
            from .ask.research_validate import cards_words, steps_words
            from .ask_ui import research_proposals

            self._take_ask_cards(list(outcome.cards))
            message = "Web research: " + cards_words(len(report.sources), research_proposals(outcome))
            sub = [f"{outcome.duration_ms / 1000:.1f} s", _plural(outcome.runs, "run"), steps_words(report)]
            self._activity(hud.TAG_ASK, message, f" {DOT} ".join(sub))
            logger.info("Ask: %d card(s) shown under ASK (%d not doable)", len(outcome.cards), outcome.refused)
        elif outcome.ok:
            self._take_ask_cards(list(outcome.cards))
            decidable = sum(1 for card in outcome.cards if not card.error)
            if decidable:
                message = _plural(decidable, "proposal")
            else:
                message = "A question back" if outcome.question and not outcome.cards else "No proposals"
            if outcome.refused:
                message += f", {outcome.refused} not doable"
            sub = [f"{outcome.duration_ms / 1000:.1f} s", _plural(outcome.runs, "planner run")]
            if outcome.mail.threads:
                sub.append(f"read {_plural(outcome.mail.threads, 'mail thread')}")
            self._activity(hud.TAG_ASK, message, f" {DOT} ".join(sub))
            logger.info("Ask: %d card(s) shown under ASK (%d not doable)", len(outcome.cards), outcome.refused)
        elif outcome.kind not in ("empty", "busy"):
            self._activity(hud.TAG_STOP, "Ask: nothing was proposed", _short(outcome.message or outcome.kind))
        self._refresh_actions_ui()

    def _refresh_ask(self) -> None:
        """A Google sign-in changed: which accounts' mail Ask may read (never Claude Code's checks)."""
        if self.ask is not None:
            self.ask.refresh(full=False)

    def sign_in_for_mail(self, alias: str) -> None:
        """The command bar's "Allow <alias> mail": that account's Google sign-in once more, asking
        for every feature's permission (reading email included), only on this click. Never while
        an Ask runs: the sign-in holds the account's calendar for the whole browser flow, and the
        Ask would wait on it (the bar hides the link meanwhile; this is the second guard)."""
        if self.state == STATE_QUITTING or alias not in self._calendars:
            return
        if self.ask is not None and self.ask.busy:
            logger.info("Ask: the mail sign-in waits for the running Ask")
            self._flash_note(ASK_MAIL_WAIT)
            return
        if self._signing_in() or self._countdown is not None or self._jobs:
            self.window.reading.command_bar.set_status(ASK_MAIL_BUSY, hud.TONE_WARN)
            return
        if self._start_calendar() is None:
            return
        logger.info("Google sign-in again for the %s account, so Ask may read its mail", logged_alias(alias))
        self._start_sign_in(alias, force=True)

    def open_ask(self) -> None:
        """``--ask``, or an activation with "ask": true: the assistant screen with the command bar
        focused (open_assistant; with [ask] off the status says so)."""
        self.open_assistant(ask_requested=True)

    def open_assistant(self, *, focus_bar: bool = True, ask_requested: bool = False) -> None:
        """An OPEN launch: the assistant screen in front, the JARVIS tab when it was not showing,
        the command bar focused. The briefing does not start playing and no scheduled slot is
        settled (Play does both); a reading in progress just comes forward. The greeting and the
        announcement are the caller's (start, handle_activation)."""
        if self.state == STATE_QUITTING:
            return
        if ask_requested:
            logger.info("Opening Ask Jarvis (%s)", "on" if self.ask is not None else "off")
        else:
            logger.info("Opening the assistant screen")
        if self.state != STATE_READING:
            self.enter_reading(autoplay=False, settle=False)
        force_foreground(self.window)
        if self.ask is not None and focus_bar:
            self.window.reading.command_bar.focus_input()
        elif self.ask is None and ask_requested:
            from .ask.planner import DISABLED_MESSAGE

            self._flash_note(DISABLED_MESSAGE)

    # ---- the conversation and Jarvis's voice ----------------------------------------------

    def _jarvis(self, text: str, *, tone: str = hud.TONE_DONE, sub: str = "",
                link: tuple[str, str] = ("", "")) -> int:
        """A JARVIS entry in the conversation (display only: never logged); its id for later updates.
        A dot on the JARVIS tab tells the owner when another tab is current."""
        reading = self.window.reading
        entry = reading.conversation.add(hud.ROLE_JARVIS, text, when=self._now(), tone=tone, sub=sub,
                                         link_text=link[0], link_id=link[1])
        reading.mark_unread()
        return entry

    def _you(self, text: str) -> int:
        """A YOU entry (the owner's request, as typed; display only, never logged)."""
        return self.window.reading.conversation.add(hud.ROLE_YOU, text, when=self._now())

    def _say(self, kind: str, text: str, *, key: str = "") -> bool:
        """Hand ``text`` to Jarvis's voice; False when it will not be spoken (muted, off, failed, or
        the app is closing)."""
        if self.state == STATE_QUITTING:
            return False
        try:
            return bool(self.voice.say(kind, text, key=key))
        except Exception as exc:  # noqa: BLE001 - his words are on screen either way
            logger.warning("Jarvis's voice failed (%s); shown only", type(exc).__name__)
            return False

    def _voice_call(self, name: str, *args: Any) -> None:
        """``voice.<name>(*args)``; a broken voice never stops the app."""
        try:
            getattr(self.voice, name)(*args)
        except Exception as exc:  # noqa: BLE001 - his words are on screen either way
            logger.warning("Jarvis's voice failed (%s)", type(exc).__name__)

    def _pump_voice(self) -> None:
        """The voice looks again whether it may start the next utterance (a countdown started or
        ended, the briefing's playback changed)."""
        self._voice_call("pump")

    def _owner_playback(self) -> None:
        """The owner pressed Play, Pause, Skip, a section or Read everything: Jarvis stops talking at
        once (his queue goes) and never resumes the briefing on his own; the owner's action wins."""
        self._voice_call("owner_acted")
        self._voice_call("stop")

    def _on_voice_speaking(self, text: str) -> None:
        """Jarvis started saying ``text`` (the orb says SPEAKING and the speech line shows his words)
        or finished (""; the briefing's own state and line come back)."""
        self._utterance = text or ""
        if self.state != STATE_READING:
            return
        self.window.reading.set_speech(self._utterance or self._briefing_speech)
        self._update_reading_controls()

    def _set_briefing_speech(self, text: str) -> None:
        """The speech line for the briefing (the line being read; "" for its fallback). While Jarvis
        says something himself, his words stay until he is done."""
        self._briefing_speech = text or ""
        if not self._utterance:
            self.window.reading.set_speech(self._briefing_speech)

    def _on_mute_toggled(self, muted: bool) -> None:
        """The header's speaker button (or Ctrl+M): Jarvis's own voice off or on (remembered in
        assistant.json); the briefing still plays when you press Play."""
        self._voice_call("set_muted", bool(muted))
        self._sync_speaker()

    def _sync_speaker(self) -> None:
        """The speaker button shows the voice's mute state."""
        try:
            muted = bool(self.voice.muted)
        except Exception:  # noqa: BLE001 - unknown: shown as on
            muted = False
        self.window.header.set_muted(muted)

    # ---- what Jarvis says after a card was carried out ---------------------------------------

    def _tell_result(self, action: ProposedAction | None, status: str) -> None:
        """One sentence after the executor's result for an approved card (created / exists / sent,
        failed, unknown): a JARVIS entry, spoken unless [assistant] speak_results is off. Never at
        the click or during the countdown: only here. Titles and subjects only; never the error
        message (the card says why), a body or a recipient."""
        if action is None or self.state == STATE_QUITTING:
            return
        assistant = self.config.assistant
        answer = action.field("answer") if action.kind == RSVP else ""
        text = outcome_text(action.kind, status, action.title, when=self._spoken_when(action), answer=answer,
                            address=assistant.address)
        self._jarvis(text, tone=_RESULT_TONES.get(outcome_tone(status), hud.TONE_DONE))
        spoken = assistant.speak_results and self._say(KIND_RESULT, text, key=_RESULT_KEY)
        logger.info("Told the result of action %s (%s, %s)", action.id, outcome_tone(status),
                    "spoken" if spoken else "text only")

    def _tell_not_sent(self, action: ProposedAction) -> None:
        """A Reply / Email not sent at the end of its countdown (the sending account changed)."""
        if self.state == STATE_QUITTING:
            return
        assistant = self.config.assistant
        text = not_sent_text(action.kind, action.title, assistant.address)
        self._jarvis(text, tone=hud.TONE_WARN)
        if assistant.speak_results:
            self._say(KIND_RESULT, text, key=_RESULT_KEY)

    def _spoken_when(self, action: ProposedAction) -> str:
        """When an approved card's event is, said aloud: a Calendar event's start, a Todo's block, a
        Move's new time ("tomorrow at 3 PM"); "" for the other kinds."""
        try:
            now = self._now()
            if action.kind == CALENDAR:
                if action.all_day_start is not None:
                    return spoken_when(action.all_day_start, None, now, all_day=True)
                return spoken_when(action.start, None, now)
            if action.kind == TODO:
                block = action.block_event()
                return spoken_when(block.start, None, now) if block is not None else ""
            if action.kind == MOVE:
                return spoken_when(action.start, None, now)
        except Exception as exc:  # noqa: BLE001 - the sentence goes on without the time
            logger.debug("No spoken time for action %s (%s)", action.id, type(exc).__name__)
        return ""

    def _prefs_store(self) -> AssistantPrefs:
        """assistant.json (mute, the recent greetings), opened on first use."""
        if self._prefs is None:
            self._prefs = AssistantPrefs(self.config.data_dir / PREFS_FILE)
        return self._prefs

    def _on_conversation_link(self, link_id: str) -> None:
        if link_id == VIEW_BRIEFING_LINK:
            self.window.reading.set_tab(hud.TAB_BRIEFING)
        elif link_id.startswith("live:"):
            self._reveal_live(link_id)

    # ---- the LIVE view -----------------------------------------------------------------------

    def _on_live_updated(self, summary: LiveSummary) -> None:
        """The feed drained: the summary, the strip's tag (and the pop-out's), the tab's dot."""
        self._live_summary = summary
        reading = self.window.reading
        reading.set_live_summary(summary.text())
        _show_live_state(reading.set_live_state, summary)
        window = self._live_window
        if window is not None:
            window.set_summary(summary)
        if summary.attention and not self._live_popped() and reading.current_tab() != hud.TAB_LIVE:
            self._live_unread = True
        self._sync_live_dot()

    def _live_popped(self) -> bool:
        """The pop-out window shows the steps (shown or minimized)."""
        window = self._live_window
        return window is not None and window.isVisible()

    def _sync_live_dot(self) -> None:
        """The LIVE tab's dot: blinking while an Ask or an approved card runs, steady for steps the
        owner has not seen; none while LIVE is current or popped out."""
        reading = self.window.reading
        if self._live_popped() or reading.current_tab() == hud.TAB_LIVE or not self.live.enabled:
            self._live_unread = False
            reading.tabs.set_activity("")
            return
        if self._live_summary.running:
            reading.tabs.set_activity(hud.ACTIVITY_WORKING)
        else:
            reading.tabs.set_activity(hud.ACTIVITY_UNREAD if self._live_unread else "")

    def _live_auto_open(self, kind: str, task_id: int) -> bool:
        """[live] auto_open when Jarvis starts on an Ask or an approved card (once per task): "tab"
        makes LIVE current (not while the briefing plays, for a card), "window" shows the pop-out
        without the focus, "off" nothing. "tab" shows the pop-out too (without the focus) when the
        LIVE tab would be shorter than LIVE_DOCKED_MIN_PX and there is another screen. Never while
        the main window is hidden, minimized or on the prompt; an open pop-out keeps everything as
        with "off" (a minimized one is restored without the focus). True when LIVE became the
        current tab."""
        if not self.live.enabled or not task_id or self.state == STATE_QUITTING or task_id in self._live_opened:
            return False
        self._live_opened = (self._live_opened + [task_id])[-64:]
        mode = self._live_mode
        window = self._live_window
        if window is not None and window.isVisible():
            if mode != "off" and window.isMinimized():
                _restore_without_activating(window)
            return False
        if mode == "off":
            return False
        main = self.window
        if self.state != STATE_READING or not main.isVisible() or main.isMinimized() or not main.is_reading_view():
            return False
        if mode == "window" or (self.window.reading.live_room() < LIVE_DOCKED_MIN_PX and self._other_screen()):
            # "window"; or the LIVE tab would show hardly a step and there is another screen for it.
            self.pop_out_live(activate=False)
            return False
        if kind == _live.TASK_ACTION and self.player.state in (PLAYING, WAITING):
            return False   # the briefing plays on BRIEFING: the tab stays, LIVE gets its working dot
        self.window.reading.set_tab(hud.TAB_LIVE)
        return True

    def _other_screen(self) -> bool:
        """There is a screen besides the main window's (tests replace this)."""
        main = self.window.screen()
        return any(screen is not main for screen in QGuiApplication.screens())

    def _ensure_live_window(self) -> LiveWindow:
        window = self._live_window
        if window is None:
            window = self._live_window = LiveWindow()
            window.set_hour24(self.config.display.hour24)
            window.header.set_clock(self._now)
            window.dockRequested.connect(self.dock_live)
            window.clearRequested.connect(self.clear_live)
            window.linkClicked.connect(self._on_live_link)
            window.set_summary(self._live_summary)
        return window

    def pop_out_live(self, *, activate: bool = True) -> None:
        """Pop out: the LIVE view in its own window (made once, kept for the session), on another
        screen when there is one; the LIVE tab says where it is. ``activate``: the owner clicked
        (the window comes to the front with the focus); else it shows without the focus."""
        if self.state == STATE_QUITTING or not self.live.enabled:
            return
        window = self._ensure_live_window()
        if not window.isVisible():
            self._live_feed.attach(window.log)
            self._place_live_window(window)
            self.window.reading.set_live_popped(True)
            if activate:
                force_foreground(window)
            else:
                show_without_activating(window)
            logger.info("LIVE popped out")
            self._remember_live_window(popped=True)
        elif activate:
            force_foreground(window)
        else:
            show_without_activating(window)
        self._sync_live_dot()

    def show_live_window(self) -> None:
        """"Show window" on the LIVE tab: the pop-out to the front (restored when minimized)."""
        window = self._live_window
        if window is None or not window.isVisible():
            self.pop_out_live()
            return
        force_foreground(window)

    def dock_live(self) -> None:
        """Dock (the pop-out's close button, Alt+F4, Dock, or "Bring it back here"): the window hides
        (it is kept, never quits) and the LIVE tab shows the steps again. The hidden window's log is
        emptied (no fed view keeps steps or pictures: Clear and the picture caps reach every view
        that is seen) and a picture it showed larger closes; popping out again replays the stream."""
        window = self._live_window
        if window is not None and window.isVisible():
            self._remember_live_window(popped=False)
            window.hide()
            self._live_feed.detach(window.log)
            window.log.clear()
            logger.info("LIVE docked")
        self.window.reading.set_live_popped(False)
        self._sync_live_dot()

    def clear_live(self) -> None:
        """Clear: every finished task leaves both views (a running one stays whole); nothing else
        changes and nothing was saved. A page picture still to be taken for a step that went is not
        taken (nobody would see it)."""
        self.live.clear()
        self._snapshots_call("drop_cleared")
        self._live_unread = False
        self._sync_live_dot()

    def _close_live_window(self) -> None:
        """Quitting: the pop-out goes too (its place and whether it was open are remembered)."""
        window, self._live_window = self._live_window, None
        if window is None:
            return
        try:
            self._remember_live_window(popped=window.isVisible(), window=window)
        except Exception as exc:  # noqa: BLE001 - quitting goes on
            logger.debug("Could not remember the LIVE window (%s)", type(exc).__name__)
        self._live_feed.detach(window.log)
        window.hide()
        window.deleteLater()

    def _place_live_window(self, window: LiveWindow) -> None:
        """The pop-out's first place this session: where it was left last time (assistant.json, if
        that is still on a screen), else on another screen than the main window's (centred), else
        beside the main window (left if it fits, else right), else overlapping it 40 px down-left.
        Later it stays where the owner left it."""
        if self._live_placed:
            window.keep_on_screen()
            return
        self._live_placed = True
        saved = self._saved_live_geometry()
        if saved is not None:
            window.setGeometry(saved)
            window.keep_on_screen()
            return
        main = self.window
        main_screen = main.screen() if main.isVisible() else QGuiApplication.primaryScreen()
        others = [screen for screen in QGuiApplication.screens() if screen is not main_screen]
        target = others[0] if others else main_screen
        area = target.availableGeometry() if target is not None else QRect(0, 0, 1280, 800)
        window.resize(LIVE_WINDOW_SIZE.boundedTo(area.size() - QSize(2 * SCREEN_MARGIN, 2 * SCREEN_MARGIN))
                      .expandedTo(LIVE_WINDOW_MIN_SIZE))
        width, height = window.width(), window.height()
        if others or not main.isVisible():
            x, y = area.x() + (area.width() - width) // 2, area.y() + (area.height() - height) // 2
        else:
            frame = main.frameGeometry()
            y = max(area.top(), min(frame.top(), area.bottom() - height))
            if frame.left() - _LIVE_WINDOW_GAP - width >= area.left():
                x = frame.left() - _LIVE_WINDOW_GAP - width
            elif frame.right() + _LIVE_WINDOW_GAP + width <= area.right():
                x = frame.right() + _LIVE_WINDOW_GAP
            else:
                x, y = frame.left() - 40, frame.top() + 40
        window.move(x, y)
        window.keep_on_screen()

    def _saved_live_geometry(self) -> QRect | None:
        """The pop-out's rectangle from assistant.json, when its centre is still on a screen."""
        try:
            saved = self._prefs_store().live_window
        except Exception:  # noqa: BLE001 - placed afresh then
            return None
        if saved is None:
            return None
        rect = QRect(*saved)
        if rect.width() < LIVE_WINDOW_MIN_SIZE.width() or rect.height() < LIVE_WINDOW_MIN_SIZE.height():
            rect.setSize(rect.size().expandedTo(LIVE_WINDOW_MIN_SIZE))
        center = rect.center()
        if not any(screen.availableGeometry().contains(center) for screen in QGuiApplication.screens()):
            return None
        return rect

    def _remember_live_window(self, *, popped: bool, window: LiveWindow | None = None) -> None:
        """assistant.json keeps the pop-out's place and size and whether it was open (U7b)."""
        window = window or self._live_window
        if window is None:
            return
        rect = window.normalGeometry() if window.isMinimized() else window.geometry()
        if rect.width() <= 0 or rect.height() <= 0:
            return
        self._prefs_store().set_live_window((rect.x(), rect.y(), rect.width(), rect.height()), popped)

    def _restore_live_window(self) -> None:
        """The first time the assistant screen opens: the pop-out comes back (without the focus) when
        it was open when Jarvis last quit."""
        if self._live_restored or not self.live.enabled:
            return
        self._live_restored = True
        try:
            popped = self._prefs_store().live_popped_out
        except Exception:  # noqa: BLE001 - not restored then
            popped = False
        if popped:
            self.pop_out_live(activate=False)

    def _reveal_live(self, link_id: str) -> None:
        """"See every step" (live:<task id>): LIVE comes up (the pop-out to the front when it is
        out) with that task in view and open."""
        try:
            task_id = int(link_id.partition(":")[2])
        except ValueError:
            return
        window = self._live_window
        if window is not None and window.isVisible():
            self.show_live_window()
            window.log.reveal(task_id)
            return
        reading = self.window.reading
        reading.set_tab(hud.TAB_LIVE)
        if reading.current_tab() == hud.TAB_LIVE:
            reading.live_log.reveal(task_id)

    def _on_live_link(self, link: str) -> None:
        """A link in the LIVE log: "tab:jarvis" makes JARVIS current, "live:<id>" shows that task,
        and a result link opens only if it is an allowed https link (open_action_link checks)."""
        if link == "tab:jarvis":
            self.window.reading.set_tab(hud.TAB_JARVIS)
            if self._live_popped() and self.window.isVisible():
                force_foreground(self.window)
        elif link.startswith("live:"):
            self._reveal_live(link)
        elif link.startswith("https://"):
            self.open_action_link(link)

    # ---- NEW briefings and viewing ---------------------------------------------------------

    def _shown_key(self) -> str | None:
        """The briefing key (runstate.briefing_key) of the briefing on the BRIEFING tab, or None
        (nothing rendered yet, a setup error, or a page without an "Updated:" time)."""
        if self.startup_error or self._script is None or self._briefing is None:
            return None
        return briefing_key(self._briefing.header, self.slots)

    def _new_key(self) -> str | None:
        """The shown briefing's key while it is NEW: not viewed, and at most 18 hours old."""
        key = self._shown_key()
        if key is None or self._is_viewed(key):
            return None
        updated = self._briefing.header.updated_at if self._briefing is not None else None
        try:
            if updated is None or self._now() - updated > ANSWER_MAX_AGE:
                return None
        except TypeError:   # naive and aware times mixed: not called new
            return None
        return key

    def _is_viewed(self, key: str) -> bool:
        return key in self._viewed_keys or (self.run_state is not None and self.run_state.is_viewed(key))

    def _is_announced(self, key: str) -> bool:
        return key in self._announced_keys or (self.run_state is not None and self.run_state.is_announced(key))

    def _run_label(self) -> str:
        """"AM" / "PM" of the shown briefing (its run, else the expected run), or ""."""
        if self._script is not None and self._script.run_label in ("AM", "PM"):
            return self._script.run_label
        run = self._briefing.header.run if self._briefing is not None else None
        return run if run in ("AM", "PM") else (self.expected_run or "")

    def _window_active(self) -> bool:
        """The window is the active one (the owner can see the BRIEFING tab); tests replace this."""
        return self.window.isActiveWindow()

    def _on_tab_changed(self, index: int) -> None:
        if index == hud.TAB_LIVE:
            self._live_unread = False
        self._sync_live_dot()
        self._mark_viewed_if_seen()

    def _on_window_active(self, _active: bool) -> None:
        self._mark_viewed_if_seen()

    def _show_briefing_tab(self) -> None:
        """Play, Replay, a section or Read everything from the JARVIS tab: the transcript comes up."""
        if self.window.reading.current_tab() != hud.TAB_BRIEFING:
            self.window.reading.set_tab(hud.TAB_BRIEFING)

    def _mark_viewed_if_seen(self) -> None:
        """A NEW briefing counts as viewed once its BRIEFING tab is current in the active window."""
        if self.state != STATE_READING:
            return
        key = self._new_key()
        if key is None or self.window.reading.current_tab() != hud.TAB_BRIEFING:
            return
        try:
            active = self._window_active()
        except Exception:  # noqa: BLE001 - unknown: not seen yet
            active = False
        if active:
            self._mark_viewed(key)

    def _mark_viewed(self, key: str) -> None:
        """The briefing ``key`` was viewed (or played): remembered, any announcement of it still to
        come is dropped, and every NEW indicator goes."""
        if key not in self._viewed_keys:
            self._viewed_keys.add(key)
            recorded = self.run_state.record_viewed(key) if self.run_state is not None else False
            if not recorded:
                logger.info("The %s briefing was viewed", key)
        self.voice.cancel_key(f"announce:{key}")
        if self._held_key == key:
            self._held_key = None
            self._held_timer.stop()
        self._announce_when_shown = False
        self._sync_new_indicators()

    def _sync_new_indicators(self) -> None:
        """NEW everywhere or nowhere: the BRIEFING tab's badge, the speech line while idle, STATUS
        "Updated" and the tray tooltip."""
        key = self._new_key()
        run = self._run_label()
        label = f"Your {run + ' ' if run else ''}briefing is ready to view"
        self.window.reading.set_new_briefing(key is not None, label)
        if self.state == STATE_READING:
            self._update_telemetry()
        if self.state in (STATE_READING, STATE_WAITING):
            self._update_tray()

    def _after_briefing_shown(self) -> None:
        """A briefing was rendered on the BRIEFING tab: NEW indicators, viewing, and the late
        announcement of an open whose greeting could not wait for it."""
        self._sync_new_indicators()
        self._mark_viewed_if_seen()
        if self._announce_when_shown and not self._greeting_pending and not self._arriving:
            self._announce_when_shown = False
            self._announce()

    # ---- greeting and announcement ---------------------------------------------------------

    def _pick_greeting(self) -> Greeting:
        """A random greeting for now that was not heard recently (never the last one); remembered."""
        prefs = self._prefs_store()
        line = pick_greeting(self._now(), prefs.recent_greetings, self._rng)
        prefs.remember_greeting(line.id)
        return line

    def _begin_greeting(self, *, wait: bool) -> None:
        """An OPEN (a new process, or an activation that restores a hidden or minimized window): the
        greeting line goes into the conversation at once; it is spoken (with "Your AM briefing is
        ready to view." and one safe fact folded in) once the first fetch answered, at most 3 s
        later (``wait``), or at once when the briefing is already known."""
        self._last_greeting_at = time.monotonic()
        assistant = self.config.assistant
        self._greeting_timer.stop()
        self._greeting_line = self._pick_greeting() if assistant.greet else None
        self._greeting_entry = None
        if self._greeting_line is not None:
            self._greeting_entry = self._jarvis(self._greeting_line.text(assistant.address))
        self._greeting_pending = True
        if wait and self._script is None and self.fetching and not self.startup_error:
            self._greeting_timer.start(_GREETING_WAIT_MS)
        else:
            self._finish_greeting()

    def _finish_greeting(self) -> None:
        """Compose and say the greeting (at most three sentences); an announcement that could not
        be folded in comes on its own (now, or when the briefing is rendered)."""
        if not self._greeting_pending:
            return
        self._greeting_pending = False
        self._greeting_timer.stop()
        assistant = self.config.assistant
        line = self._greeting_line
        key = self._new_key()
        if key is None and self._script is None and not self.startup_error:
            self._announce_when_shown = True   # announced on its own once it is shown
        fold = (key is not None and not self._is_announced(key) and key != self._held_key
                and not self._locked_now())
        if line is None:
            if key is not None:
                self._announce()
            return
        announce_text = announcement(self._run_label(), assistant.address, after_greeting=True) if fold else ""
        context = self._greeting_context() if assistant.greeting_context else ""
        # A question ("How can I help, sir?") or a second "ready" never comes before the rest.
        text = compose_opening(line.text(assistant.address), announce_text, context,
                               plain=salutation(self._now()).text(assistant.address))
        if self._greeting_entry is not None:
            link = (VIEW_BRIEFING_TEXT, VIEW_BRIEFING_LINK) if fold else (None, None)
            self.window.reading.conversation.update(self._greeting_entry, text=text, link_text=link[0],
                                                    link_id=link[1])
        spoken = self._say(KIND_GREETING, text, key=f"announce:{key}" if fold else "")
        logger.info("Greeting %s%s (%s)", line.id, " with the announcement" if fold else "",
                    "spoken" if spoken else "text only")
        if fold and key is not None:
            self._set_announced(key, spoken)
        elif key is not None:
            self._announce()   # e.g. held while the session is locked

    def _greeting_context(self) -> str:
        """One fact that is safe to say aloud: proposals waiting for the owner's OK, else the next
        timed event within 3 hours (only when the agenda is loaded; never waits for Google)."""
        try:
            pending = len(self._pending_actions())
            if pending:
                return approvals_sentence(pending)
            if self._agenda_status != _AGENDA_OK or not self._agenda_events:
                return ""
            now = self._now()
            upcoming = [event for event in self._agenda_events
                        if event.start is not None and not event.all_day
                        and now < event.start <= now + _NEXT_EVENT_WITHIN]
            if not upcoming:
                return ""
            first = min(upcoming, key=lambda event: event.start)
            start = first.start.astimezone(now.tzinfo) if now.tzinfo is not None else first.start
            return next_event_sentence(first.title, start)
        except Exception as exc:  # noqa: BLE001 - the greeting goes on without a fact
            logger.debug("No greeting fact (%s)", type(exc).__name__)
            return ""

    def _locked_now(self) -> bool:
        try:
            return bool(self._locked())
        except Exception:  # noqa: BLE001 - unknown: not locked
            return False

    def _announce(self, *, greeting: bool = False) -> bool:
        """"Sir, your AM briefing is ready to view." once per briefing: a conversation entry with a
        View briefing link and one utterance (with a greeting before it when ``greeting``). Never
        for a viewed, old, placeholder or erroneous briefing. While the session is locked the entry
        is added at once and the utterance waits for the unlock. True when it was announced or
        held now."""
        key = self._new_key()
        if key is None or self._is_announced(key) or key == self._held_key:
            return False
        assistant = self.config.assistant
        run = self._run_label()
        # A scheduled arrival: the owner did not open Jarvis, so no "Welcome back" or "How can I
        # help?" - the hour's plain "Good morning, sir." before the announcement.
        line = salutation(self._now()) if greeting and assistant.greet else None
        if line is not None:
            self._prefs_store().remember_greeting(line.id)
            self._last_greeting_at = time.monotonic()
            text = compose_opening(line.text(assistant.address),
                                   announcement(run, assistant.address, after_greeting=True))
            kind = KIND_GREETING
        else:
            text, kind = announcement(run, assistant.address), KIND_ANNOUNCE
        self._jarvis(text, link=(VIEW_BRIEFING_TEXT, VIEW_BRIEFING_LINK))
        if line is not None:
            logger.info("Greeting %s with the announcement", line.id)
        if self._locked_now():
            self._held_key, self._held_text, self._held_kind = key, text, kind
            self._held_timer.start(_LOCK_RECHECK_MS)
            logger.info("Announced the %s briefing (held: session locked)", key)
            return True
        self._set_announced(key, self._say(kind, text, key=f"announce:{key}"))
        return True

    def _set_announced(self, key: str, spoken: bool) -> None:
        how = "spoken" if spoken else "text only"
        self._announced_keys.add(key)
        if self.run_state is not None:
            self.run_state.record_announced(key, how)
        logger.info("Announced the %s briefing (%s)", key, how)

    def _on_held_timer(self) -> None:
        """An announcement held while the session was locked: said after the unlock, if the
        briefing is still NEW and unannounced."""
        key = self._held_key
        if key is None or self.state == STATE_QUITTING:
            return
        if self._locked_now():
            self._held_timer.start(_LOCK_RECHECK_MS)
            return
        self._held_key = None
        if self._new_key() != key or self._is_announced(key):
            return
        self._set_announced(key, self._say(self._held_kind, self._held_text, key=f"announce:{key}"))

    # ---- scheduled arrival and newer briefings -----------------------------------------------

    def _show_assistant(self, *, take_focus: bool) -> None:
        """The assistant screen on its JARVIS tab: in front with the focus, or shown on top without
        taking it (a scheduled arrival; a minimized window is restored where it was)."""
        if self.state != STATE_READING:
            self.enter_reading(autoplay=False, settle=False)
        self.window.reading.set_tab(hud.TAB_JARVIS)
        if take_focus:
            force_foreground(self.window)
        else:
            show_without_activating(self.window)

    def _arrive(self) -> None:
        """The scheduled run's fresh briefing is here: shown without the focus and announced with a
        greeting before it, unless it was announced or viewed already (then the process just ends).
        A setup error is shown the same way, as text only."""
        if self.startup_error:
            self._arriving = True
            try:
                self._show_assistant(take_focus=False)
            finally:
                self._arriving = False
            headline = _split_startup_error(self.startup_error)[0].rstrip(".")
            self._jarvis(f"I can't reach your briefing: {headline}.", tone=hud.TONE_WARN)
            logger.info("Showing the setup error without the focus (nothing is said)")
            return
        key = briefing_key(self._briefing.header, self.slots) if self._briefing is not None else None
        if key is not None and (self._is_announced(key) or self._is_viewed(key)):
            logger.info("The %s briefing was already announced; nothing to show", self.expected_run or key)
            self.quit()
            return
        logger.info("The %s briefing arrived: showing it without the focus", self.expected_run or "new")
        self._arriving = True
        try:
            self._show_assistant(take_focus=False)
        finally:
            self._arriving = False
        self.record_shown()   # diagnostics: when its slot was first shown
        self._announce(greeting=True)

    def _open_from_waiting(self, *, greet: bool) -> None:
        """WAITING -> the assistant screen with the focus (an OPEN activation, or the tray's Show);
        the poll goes on in the background and its arrival is announced (no greeting)."""
        if self.expected_run and self.fetching:
            self._background_run = self.expected_run
        self._show_assistant(take_focus=True)
        if self.ask is not None:
            self.window.reading.command_bar.focus_input()
        if greet:
            self._begin_greeting(wait=False)
        else:
            self._announce_when_shown = self._script is None

    def _is_newer_briefing(self, briefing: Briefing) -> bool:
        """While a briefing is shown: the run waited for in the background has its fresh one, and it
        is another briefing (another key) than the one on screen."""
        if self._background_run is None or not self._freshness(briefing).fresh:
            return False
        return briefing_key(briefing.header, self.slots) != self._shown_key()

    def _newer_briefing(self, briefing: Briefing) -> None:
        """Show the newer briefing at once when nothing is playing; never cut a listen short (it
        shows when this reading ends; Done quits as usual)."""
        run = self._background_run or self.expected_run or ""
        self._background_run = None
        self._stop_fetch()
        if self.player.state in (IDLE, FINISHED):
            self._replace_briefing(briefing)
            return
        logger.info("The %s briefing is ready; it shows when this reading ends", run)
        self._pending_briefing = briefing
        self._pending_run = run or "new"
        self._flash_note(self._pending_note(), sticky=True)

    def _replace_briefing(self, briefing: Briefing) -> None:
        """The shown briefing gives way to a newer one: rendered from the top, NEW, announced."""
        logger.info("Showing the newer %s briefing", briefing.header.run or "")
        self._reset_reading("a newer briefing replaced the shown one")
        self._autoplay = False
        self._briefing = self._take_actions(briefing)
        self._begin_reading()
        if self.window.isVisible() and self.window.isMinimized():
            show_without_activating(self.window)
        self._announce()

    def _reset_reading(self, reason: str) -> None:
        """Clear the reading screen for another briefing (nothing is playing)."""
        self._cancel_countdown(reason)   # never send from a card that goes away
        if self._dialog is not None:
            self._dialog.reject()
        if self._account_dialog is not None:
            self._account_dialog.reject()   # unanswered: asked again at the next Send or Approve
        self._recheck_timer.stop()
        self._note_timer.stop()
        self._replace_player()
        self._script = None
        self._include_ignored = False
        self._has_played = False
        self._pending_run = None
        self._pending_briefing = None
        self._note = ""
        self._sections_started = set()
        self._last_section = None
        self._read_slot = None
        self._reading_settled = False
        self._autoplay = True
        reading = self.window.reading
        reading.show_message("")   # the next briefing renders from the top
        reading.set_sections([], "")
        reading.set_steps([])
        self._set_briefing_speech("")

    # ---- scripts and speech -------------------------------------------------------------

    def _build_script(self, include_note: bool) -> Script:
        briefing = self._briefing
        assert briefing is not None
        return build_script(
            briefing, now=self._now(), expected_run=self.expected_run,
            freshness=self._freshness(briefing),
            include_note=include_note, ignore_names=self.config.sections.ignore,
            announce=self.config.sections.announce, actions=self._pending_page_actions())

    def _refresh_prefetch(self) -> None:
        """Prepare audio for the briefing while the prompt shows (rebuilt when it changes)."""
        if self.state in (STATE_READING, STATE_QUITTING) or self._briefing is None:
            return
        # No stale note while still waiting for the run; a one-off re-check keeps it.
        include_note = not self._polling or self.expected_run is None
        script = self._build_script(include_note)
        unchanged = self._prefetch is not None and script.sections == self._prefetch.sections
        self._prefetch = script
        if not unchanged:
            self._submit_all(script)
            if not include_note:
                self._warm_noted_intro(script)

    def _warm_noted_intro(self, script: Script) -> None:
        """Also synthesize the intro with the stale note, so Read now during the wait starts at once.

        Audio files are content-addressed, so the reading script's intro is then a cache hit.
        """
        noted = self._build_script(include_note=True)
        if self._tts is None or not noted.stale or noted.sections[0] == script.sections[0]:
            return
        self._tts.submit(self._generation, _WARMUP_INDEX, noted.sections[0])

    def _submit_all(self, script: Script) -> None:
        """New TTS generation for ``script``: non-ignored sections first, ignored ones last."""
        self._generation += 1
        self._audio, self._failed = {}, set()
        self._audio_ready_logged = False
        if self._tts is None:
            return
        self._tts.set_generation(self._generation)
        order = script.section_indices(False)
        order += [index for index in range(len(script.sections)) if index not in order]
        for index in order:
            self._tts.submit(self._generation, index, script.sections[index])
        logger.info("Preparing audio for %d section(s)", len(order))
        self._activity(hud.TAG_RUN, "Preparing audio", _plural(len(order), "section"))

    def _on_tts_result(self, generation: int, index: int, audio: SectionAudio | None,
                       error: str | None) -> None:
        if generation != self._generation or self.state == STATE_QUITTING:
            return
        if index == _WARMUP_INDEX:   # only warmed the synthesizer's cache
            return
        if audio is None:
            self._on_tts_failed(index, error)
        else:
            self._on_tts_audio(index, audio)
        self._log_audio_ready()
        self._refresh_views()

    def _on_tts_audio(self, index: int, audio: SectionAudio) -> None:
        self._audio[index] = audio
        if audio.segments and audio.engine != self._engine:
            logger.info("Speech engine: %s", "Windows offline voice (SAPI)" if audio.engine == SAPI
                        else "edge-tts online voice")
            self._engine = audio.engine
        if self._script is not None:
            self.player.set_audio(index, audio)

    def _on_tts_failed(self, index: int, error: str | None) -> None:
        logger.warning("No audio for section %d: %s", index, error)
        self._failed.add(index)
        script = self._script or self._prefetch
        if script is not None and 0 <= index < len(script.sections):
            self._activity(hud.TAG_STOP, "Couldn't generate audio", _section_title(script.sections[index]))
        if self._script is not None:
            self.player.mark_failed(index)
            self._flash_note("Couldn't generate audio for one section")

    def _log_audio_ready(self) -> None:
        script = self._script or self._prefetch
        if script is None or self._audio_ready_logged:
            return
        needed = script.section_indices(False)
        if any(index not in self._audio and index not in self._failed for index in needed):
            return
        self._audio_ready_logged = True
        if all(index in self._failed for index in needed):
            return
        voice = "offline voice" if self._uses_offline_voice() else "edge voice"
        self._activity(hud.TAG_DONE, "Audio ready", f"{voice} {DOT} {_plural(len(needed), 'section')}")

    def _uses_offline_voice(self) -> bool:
        return any(audio.engine == SAPI and audio.segments for audio in self._audio.values())

    # ---- prompt ------------------------------------------------------------------------

    def show_prompt(self, take_focus: bool) -> None:
        if self.state in (STATE_READING, STATE_QUITTING):
            return
        self._snooze_timer.stop()
        self.state = STATE_PROMPT
        if not self.startup_error and self._briefing is None and not self.fetching:
            self._start_fetch(self.expected_run)   # retry after an earlier failure
        elif self._needs_recheck():
            # One look, not a new 15-minute wait: the headline stays "may be stale"
            # until a fresh page arrives (e.g. a briefing that landed after the poll).
            logger.info("Re-checking Notion: the cached briefing is stale")
            self._start_fetch(None)
        minimized = self.window.isVisible() and self.window.isMinimized()
        if minimized:   # back where it was minimized, first, so it is laid out as a normal window
            self._bring_up(take_focus)
        self.window.show_prompt_view()
        self._refresh_prompt()
        if not minimized:
            self.window.place()
            self._bring_up(take_focus)
        self.window.keep_on_screen()
        self.window.prompt.setFocus(Qt.FocusReason.OtherFocusReason)
        self._start_prompt_timers()
        self._update_tray()
        self._sync_agenda_timer()
        logger.info("Prompt shown (%s)", "taking focus" if take_focus else "without taking focus")
        self.record_shown()   # only the first show of a slot is kept

    def _bring_up(self, take_focus: bool) -> None:
        if take_focus:
            force_foreground(self.window)
        else:
            show_without_activating(self.window)

    def _start_prompt_timers(self) -> None:
        if self.startup_error:   # Later is disabled, so there is nothing to count down to
            self._stop_prompt_timers()
            return
        self._ignore_timer.start(self.config.prompt.ignore_after_seconds * 1000)
        self._tick_timer.start()
        self._update_countdown()

    def _stop_prompt_timers(self) -> None:
        self._ignore_timer.stop()
        self._tick_timer.stop()
        self.window.prompt.set_countdown("")

    def _update_countdown(self) -> None:
        if not self._ignore_timer.isActive():
            self.window.prompt.set_countdown("")
            return
        seconds = math.ceil(max(0, self._ignore_timer.remainingTime()) / 1000)
        minutes = self.config.prompt.later_short_minutes
        self.window.prompt.set_countdown(
            f"Asking again in {minutes} min if there is no answer ({seconds // 60}:{seconds % 60:02d})")

    def later(self, minutes: int) -> None:
        """Hide the prompt and bring it back after ``minutes``."""
        if self.state != STATE_PROMPT or self.startup_error:
            return
        minutes = max(1, int(minutes))
        self._stop_prompt_timers()
        self.window.hide()
        self.state = STATE_SNOOZED
        self._snooze_timer.start(minutes * 60_000)
        at = format_time(self._now() + timedelta(minutes=minutes))
        self._update_tray(f"Briefing: asking again at {at}")
        logger.info("Later (%d min): asking again at %s", minutes, at)

    def _on_prompt_ignored(self) -> None:
        if self.state != STATE_PROMPT:
            return
        logger.info("Prompt ignored for %d s", self.config.prompt.ignore_after_seconds)
        self.later(self.config.prompt.later_short_minutes)

    def _on_snooze_timeout(self) -> None:
        if self.state == STATE_SNOOZED or (self.state == STATE_PROMPT and self._minimized):
            self.show_prompt(take_focus=self.config.prompt.refocus_on_reprompt)

    # ---- minimize ---------------------------------------------------------------------------

    def _on_window_state(self) -> None:
        """The window was minimized or restored: its minimize button, its taskbar button, Win+Down,
        the tray icon or the hotkey."""
        window = self.window
        minimized = window.isVisible() and window.isMinimized()
        if minimized == self._minimized:
            return
        self._minimized = minimized
        if minimized:
            logger.info("Window minimized (%s)", self.state)
            self._on_minimized()
        elif window.isVisible():   # not when a minimized window is hidden (Close from the taskbar, quit)
            logger.info("Window restored (%s)", self.state)
            self._on_restored()

    def _on_minimized(self) -> None:
        """Out of sight, nothing is decided for you: an undo countdown stops (its Undo would be out of
        sight too) and a prompt asks again after the short Later, from its taskbar button. The
        reading keeps playing."""
        countdown = self._countdown
        if countdown is not None and self.state != STATE_QUITTING:
            action = countdown.action
            self._cancel_countdown("the window was minimized")
            nothing = "nothing was created" if _adds_event(action) else "nothing was sent"
            self._activity(hud.TAG_STOP, f"Undone: {_action_phrase(action)}",
                           f"{kind_label(action).upper()} {DOT} window minimized, {nothing}")
            self._refresh_actions_ui()
        if self.state == STATE_PROMPT and not self.startup_error:
            minutes = self.config.prompt.later_short_minutes
            self._stop_prompt_timers()
            self._snooze_timer.start(minutes * 60_000)
            at = format_time(self._now() + timedelta(minutes=minutes))
            self._update_tray(f"Briefing: asking again at {at}")
            logger.info("Prompt minimized: asking again at %s unless it is restored first", at)

    def _on_restored(self) -> None:
        """A prompt restored by hand waits for an answer again, with a new countdown."""
        if self.state == STATE_PROMPT and self.window.isVisible() and self._snooze_timer.isActive():
            self._snooze_timer.stop()
            self._start_prompt_timers()
            self._update_tray()

    def read_now(self) -> None:
        if self.state != STATE_PROMPT or self.startup_error:
            return
        logger.info("Read now")
        self.enter_reading()

    def dismiss(self) -> None:
        logger.info("Dismissed")
        self.record_answer("dismissed")
        self.quit()

    def _on_close_requested(self) -> None:
        if self.state == STATE_READING:
            logger.info("Window closed on the reading screen")
            self.done()
        elif self.state == STATE_PROMPT and self.startup_error:
            self.dismiss()
        elif self.state == STATE_PROMPT:
            logger.info("Window closed on the prompt")
            self.later(self.config.prompt.later_short_minutes)

    # ---- prompt text ----------------------------------------------------------------------

    def _refresh_views(self) -> None:
        if self.state in (STATE_PROMPT, STATE_SNOOZED):
            self._refresh_prompt()
        elif self.state == STATE_READING:
            self._update_reading_controls()
        self._update_service_chips()

    def _refresh_prompt(self) -> None:
        headline, detail, status, is_error = self._prompt_text()
        prompt = self.window.prompt
        prompt.set_content(headline, detail, status, is_error, can_read=not self.startup_error)
        prompt.set_state(self._prompt_orb_state())
        prompt.set_pending(len(self._pending_actions()))
        if self.state == STATE_PROMPT:
            self.window.fit_prompt()

    def _prompt_orb_state(self) -> str:
        """Waiting on the user; working while the first fetch runs; standby when there is a problem."""
        if self.startup_error:
            return hud.ORB_STANDBY
        if self._briefing is None:
            return hud.ORB_WORKING if self._fetch_error is None else hud.ORB_STANDBY
        return hud.ORB_WAITING

    def _prompt_text(self) -> tuple[str, str, str, bool]:
        """(headline, detail, status, status_is_error) for the current situation."""
        if self.startup_error:   # "Notion token missing." / "Notion page ID missing." and what to do
            headline, detail = _split_startup_error(self.startup_error)
            return headline, detail, "", False
        briefing = self._briefing
        run = self.expected_run or (briefing.header.run if briefing else None)
        if briefing is not None:
            return self._prompt_text_for(briefing, _ready_headline(run))
        if self._fetch_error is None:
            return _ready_headline(run), "Checking Notion...", "", False
        retry = f"Retrying {self._interval_text()}." if self.fetching else ""
        return "Couldn't load your briefing.", str(self._fetch_error), retry, True

    def _prompt_text_for(self, briefing: Briefing, ready: str) -> tuple[str, str, str, bool]:
        now = self._now()
        fresh = self._freshness(briefing).fresh
        label = updated_label(briefing.header, now) + self._run_suffix(briefing)
        audio_status, audio_error = self._audio_status()
        if not self.expected_run or fresh:
            return ready, label, audio_status, audio_error
        if self._polling:
            status = (f"Checking {self._interval_text()} until {format_time(self._poll_deadline)}. "
                      "Read now plays the last one.")
            return (f"Your {self.expected_run} briefing hasn't arrived yet.",
                    self._last_update_text(briefing, now), status, False)
        return (f"Your {self.expected_run} briefing may be stale. Hear it anyway?",
                label, audio_status, audio_error)

    def _run_suffix(self, briefing: Briefing) -> str:
        run = briefing.header.run
        return f" - {run} run" if self.expected_run and run and run != self.expected_run else ""

    @staticmethod
    def _last_update_text(briefing: Briefing, now: datetime) -> str:
        when = describe_updated(briefing.header.updated_at, now)
        if not when:
            return updated_label(briefing.header, now)
        run = f" ({briefing.header.run} run)" if briefing.header.run else ""
        return f"Last update: {when}{run}"

    def _audio_status(self) -> tuple[str, bool]:
        script = self._prefetch
        needed = script.section_indices(False) if script is not None else []
        if script is None or any(i not in self._audio and i not in self._failed for i in needed):
            return "Preparing audio...", False
        failed = [i for i in needed if i in self._failed]
        if failed:
            whole = len(failed) == len(needed)
            return ("Couldn't prepare the audio" if whole else "Audio ready, but part of it failed"), True
        return ("Audio ready (offline voice)" if self._uses_offline_voice() else "Audio ready"), False

    def _interval_text(self) -> str:
        seconds = self.config.polling.interval_seconds
        if seconds == 60:
            return "every minute"
        if seconds % 60 == 0:
            return f"every {seconds // 60} minutes"
        return f"every {seconds} seconds"

    # ---- reading -------------------------------------------------------------------------

    def enter_reading(self, *, autoplay: bool = True, settle: bool = True) -> None:
        """Switch to the reading screen (Read now, --now, or an activation with now=true).

        ``autoplay=False`` (``--ask``): the briefing waits for Play. ``settle=False``: the
        scheduled slot stays open until the briefing is played (Play), so the catch-up still asks
        about a briefing that was never heard."""
        if self.state in (STATE_READING, STATE_QUITTING):
            return
        self._stop_prompt_timers()
        self._snooze_timer.stop()
        was_visible = self.window.isVisible()
        self.state = STATE_READING
        self._autoplay = autoplay
        self._reading_settled = False
        if settle:
            self._settle_reading()   # Read now, --read, or an older hotkey
        # Reading at once shows the transcript; opening for the assistant shows the conversation.
        self.window.reading.set_tab(hud.TAB_BRIEFING if autoplay else hud.TAB_JARVIS)
        self.window.show_reading_view()
        if not was_visible:
            self.window.place()
        self._update_tray()
        self._render_agenda()
        self._request_agenda()
        self._sync_agenda_timer()
        self._restore_live_window()
        if self.startup_error:
            self._show_reading_error(self.startup_error, can_retry=False)
        elif self._briefing is None:
            self._start_loading(restart=False)
        elif self._worth_rechecking_first():
            self._recheck_before_reading()
        else:
            self._begin_reading()

    def _settle_reading(self) -> None:
        """This reading answers its scheduled slot (once): at Read now, or at the first Play of a
        reading screen opened for Ask."""
        if self._reading_settled:
            return
        self._reading_settled = True
        if not self.startup_error:
            self._read_slot = self.record_answer("read")

    def _worth_rechecking_first(self) -> bool:
        """The briefing we have is not fresh and Notion may have a newer one by now."""
        if self._cached_is_fresh():
            return False
        if self.fetching:
            return self._fetch_attempts == 0   # its first answer is on the way
        return not self._checked_recently()

    def _recheck_before_reading(self) -> None:
        """Ask Notion once more before freezing a stale copy, but never wait long for it."""
        logger.info("Re-checking Notion before reading: the cached briefing is stale")
        self._show_loading()
        if not self.fetching:
            self._start_fetch(None)
        self._recheck_timer.start(_RECHECK_WAIT_MS)

    def _on_recheck_timeout(self) -> None:
        if self.state == STATE_READING and self._script is None and self._briefing is not None:
            logger.info("Notion did not answer in time; reading the briefing fetched earlier")
            self._begin_reading()

    def _start_loading(self, *, restart: bool) -> None:
        self._show_loading()
        if restart or not self.fetching or self._fetch_error is not None:
            # Take the first successful fetch now (freshness is still judged for the
            # note) instead of waiting out a polling interval after an error. A fetch
            # whose first attempt is still running (no briefing, no error) is left to
            # finish: that attempt hands over whatever it finds. A run waited for in the
            # background (a scheduled run the owner opened Jarvis into) keeps being
            # waited for: its poll starts over at once instead of giving way.
            keep = self._fetch_poll_run if self._background_run is not None and self._polling else None
            self._start_fetch(keep)

    def _show_loading(self) -> None:
        reading = self.window.reading
        self._reading_error = False
        reading.set_header(f"{self.expected_run} briefing" if self.expected_run else "Briefing", "")
        reading.show_message(LOADING_TEXT)
        reading.set_status("Loading")
        self._set_briefing_speech("")
        reading.set_controls("Play", False, False)
        reading.set_read_everything(False, "Available once the briefing has loaded")
        self._update_reading_controls()

    def _on_loading_error(self) -> None:
        """A fetch attempt failed while the reading screen waits for a briefing."""
        if self.state != STATE_READING or self._script is not None or self._fetch_error is None:
            return
        if self._briefing is not None:
            logger.info("Notion could not be reached; reading the briefing fetched earlier")
            self._begin_reading()
            return
        message = str(self._fetch_error)
        if self.fetching:
            message += f" Retrying {self._interval_text()}."
        self._show_reading_error(message, can_retry=True)

    def _show_reading_error(self, message: str, *, can_retry: bool) -> None:
        reading = self.window.reading
        self._reading_error = True
        if not reading.title.text():
            reading.set_header("Briefing", "")
        reading.show_message(message, is_error=True)
        reading.set_status("Couldn't load the briefing", is_error=True)
        self._set_briefing_speech("")
        reading.set_controls("Retry", can_retry, False)
        reading.set_read_everything(False, "Available once the briefing has loaded")
        self._update_reading_controls()

    def retry(self) -> None:
        if self.state != STATE_READING or self._script is not None or self.startup_error:
            return
        logger.info("Retry loading the briefing")
        self._start_loading(restart=True)

    def _begin_reading(self) -> None:
        """Freeze the briefing, render it and start playback (unless it waits for Play)."""
        if not self._keeps_polling():
            self._stop_fetch()
        self._recheck_timer.stop()
        script = self._build_script(include_note=True)
        if self._prefetch is None or script.sections != self._prefetch.sections:
            self._submit_all(script)
        self._script = script
        self._include_ignored = False
        self._reading_error = False
        self._sections_started = set()
        self._last_section = None
        self._skip_pending = False
        logger.info("Reading the briefing: %d section(s)%s%s", len(script.section_indices(False)),
                    ", with a stale note" if script.stale else "", "" if self._autoplay else " (waiting for Play)")
        self._render_reading()
        if self._autoplay:
            self._start_playback(script.section_indices(False))
        self._after_briefing_shown()

    def _keeps_polling(self) -> bool:
        """A background wait for a run (--run X --open, a scheduled launch while the assistant screen
        shows) goes on after a briefing is shown, until that run's fresh one is in."""
        return self._background_run is not None and self._polling and not self._cached_is_fresh()

    def _render_reading(self) -> None:
        script, reading = self._script, self.window.reading
        assert script is not None
        title = f"{script.run_label} briefing" if script.run_label else "Briefing"
        stale = f"May be stale - {script.updated_label}" if script.stale else ""
        reading.set_header(title, script.updated_label, stale)
        reading.set_script(script, self._include_ignored)
        self._set_briefing_speech("")
        self._update_read_everything()
        self._update_reading_controls()

    def _start_playback(self, queue_: list[int]) -> None:
        for index, audio in self._audio.items():
            self.player.set_audio(index, audio)
        for index in self._failed:
            self.player.mark_failed(index)
        self._has_played = False
        key = self._shown_key()
        if key is not None:
            self._mark_viewed(key)   # playing it is viewing it
        self.player.start(queue_)
        # Sections whose audio already failed while the prompt showed are skipped
        # at once; say so instead of silently jumping ahead (or straight to "Finished").
        failed = [index for index in queue_ if index in self._failed]
        if len(failed) == len(queue_) and failed:
            self._flash_note("Couldn't generate the audio", sticky=True)
        elif failed:
            self._flash_note("Couldn't generate audio for one section" if len(failed) == 1
                             else f"Couldn't generate audio for {len(failed)} sections")

    def _update_read_everything(self) -> None:
        script = self._script
        if script is None:
            enabled, tip = False, "Available once the briefing has loaded"
        elif not script.has_ignored:
            enabled, tip = False, "Nothing was skipped in this briefing"
        elif self._include_ignored:
            enabled, tip = False, "The skipped sections are already included"
        else:
            names = ", ".join(section.title for section in script.sections if section.ignored)
            enabled, tip = True, f"Also read the sections that are normally skipped: {names}"
        self.window.reading.set_read_everything(enabled, tip)

    def _update_reading_controls(self) -> None:
        if self.state != STATE_READING:
            return
        reading = self.window.reading
        if self._script is None:
            if self._utterance:   # Jarvis is saying something himself
                reading.set_activity_state(hud.ORB_SPEAKING, False)
            elif self._ask_busy:
                reading.set_activity_state(hud.ORB_WORKING, False, ASK_PLANNING_LABEL)
            else:
                reading.set_activity_state(hud.ORB_STANDBY if self._reading_error else hud.ORB_WORKING, False)
            self._update_sections()
            self._update_telemetry()
            return
        state = self.player.state
        reading.set_controls(_PRIMARY_LABELS.get(state, "Play"), True, state in (PLAYING, PAUSED, WAITING))
        reading.set_status(self._reading_status(state), is_error=bool(self._note))
        if self._utterance:   # Jarvis is saying something himself (LIVE stays the briefing's)
            reading.set_activity_state(hud.ORB_SPEAKING, state == PLAYING)
        elif self._ask_busy and state != PLAYING:   # Ask is planning (Play during it speaks again)
            reading.set_activity_state(hud.ORB_WORKING, False, ASK_PLANNING_LABEL)
        else:
            reading.set_activity_state(_ORB_FOR_PLAYER.get(state, hud.ORB_STANDBY), state == PLAYING)
        self._update_sections()
        self._update_telemetry()

    def _reading_status(self, state: str) -> str:
        if state == WAITING:
            text = "Waiting for audio..." if self._has_played else "Preparing audio..."
        else:
            text = _STATUS_LABELS.get(state, "")
        if text and state in (PLAYING, PAUSED) and self._uses_offline_voice():
            text += " (offline voice)"
        if self._note:
            text = f"{text} - {self._note}" if text else self._note
        return text

    def _flash_note(self, note: str, *, sticky: bool = False) -> None:
        """Show ``note`` after the reading status (for a few seconds unless ``sticky``)."""
        self._note = note
        if sticky:
            self._note_timer.stop()
        else:
            self._note_timer.start(_NOTE_DURATION_MS)
        self._update_reading_controls()

    def _clear_note(self) -> None:
        self._note = self._pending_note()
        self._update_reading_controls()

    def _pending_note(self) -> str:
        if not self._pending_run:
            return ""
        if self._pending_briefing is not None:
            return f"Your {self._pending_run} briefing is ready - it shows when this reading ends"
        return f"Your {self._pending_run} briefing is due"

    # ---- sections, steps and telemetry ---------------------------------------------------

    def _current_section(self) -> int | None:
        """The section being read (also while paused or waiting for its audio)."""
        if self.player.state in (PLAYING, PAUSED, WAITING):
            return self.player.current_section
        return None

    def _update_sections(self) -> None:
        """Section rows on the left and the step chips above the transcript."""
        script, reading = self._script, self.window.reading
        if script is None:
            reading.set_sections([], "")
            reading.set_steps([])
            return
        current = self._current_section()
        order = script.section_indices(self._include_ignored)
        rows = [self._section_row(index, section, current, index in order)
                for index, section in enumerate(script.sections)]
        reading.set_sections(rows, _plural(len(script.sections), "section"), current)
        reading.set_steps(self._steps(script, order, current))

    def _section_row(self, index: int, section: Section, current: int | None,
                     included: bool) -> hud.SectionRowInfo:
        title = _section_title(section)
        if not included:
            return hud.SectionRowInfo(title, "not read", hud.ROW_IGNORED, clickable=False)
        meta = "no audio" if index in self._failed else self._section_meta(section)
        if index == current:
            return hud.SectionRowInfo(title, meta, hud.ROW_CURRENT, flag="now")
        state = hud.ROW_PLAYED if index in self._sections_started else hud.ROW_UPCOMING
        return hud.SectionRowInfo(title, meta, state)

    def _section_meta(self, section: Section) -> str:
        if section.key == "intro":
            # Short enough for the narrowest section column: "AM run · 10:04 AM" / "updated 10:04 AM".
            run = self._script.run_label if self._script is not None else ""
            updated = self._briefing.header.updated_at if self._briefing is not None else None
            clock = _clock_text(updated, self._now(), self.config.display.hour24) if updated is not None else ""
            if run:
                return f"{run} run {DOT} {clock}" if clock else f"{run} run"
            return f"updated {clock}" if clock else ""
        entries = sum(1 for item in section.items if item.kind == ITEM_ENTRY)
        if section.key == ACTIONS_KEY:
            return _plural(entries, "invite")
        return _plural(entries, "item") if entries else ""

    def _steps(self, script: Script, order: list[int], current: int | None) -> list[tuple[str, str]]:
        """The current section and its neighbours as step chips."""
        if not order:
            return []
        if current in order:
            position = order.index(current)
        elif self.player.state == FINISHED:
            position = len(order) - 1
        else:
            position = 0
        start = max(0, min(position - 2, len(order) - _STEP_WINDOW))
        steps = []
        for index in order[start:start + _STEP_WINDOW]:
            if index == current:
                state = hud.STEP_ACTIVE
            elif index in self._sections_started:
                state = hud.STEP_DONE
            else:
                state = hud.STEP_TODO
            steps.append((_section_title(script.sections[index]), state))
        return steps

    def _update_telemetry(self) -> None:
        reading, script = self.window.reading, self._script
        order = script.section_indices(self._include_ignored) if script is not None else []
        current = self._current_section()
        if current in order:
            position = order.index(current) + 1
        elif script is not None and self.player.state == FINISHED:
            position = len(order)
        else:
            position = 0
        reading.progress_bar.set_value(f"{position} / {len(order)}",
                                       position / len(order) if order else 0.0)
        if self._uses_offline_voice():
            reading.voice_bar.set_value("offline", 1.0, hud.AMBER)
        elif self._engine is not None:
            reading.voice_bar.set_value("online", 1.0, hud.GREEN)
        else:
            reading.voice_bar.set_value("--", 0.0, hud.TEXT_DIM)
        briefing = self._briefing
        if briefing is None:
            reading.updated_bar.set_value("--", 0.0, hud.TEXT_DIM)
        else:
            stale = script.stale if script is not None else not self._cached_is_fresh()
            now, hour24 = self._now(), self.config.display.hour24
            # In a narrow column the longer 12-hour values give way to "Mon 11:31 PM" / "Oct 2" (24-hour ones fit).
            short = "" if hour24 else _short_updated(briefing.header, now, compact=True)
            value = _short_updated(briefing.header, now, hour24)
            if self._new_key() is not None:
                # NEW: "new . today 10:04 AM" in amber ("new . 10:04 AM" when that does not fit).
                updated = briefing.header.updated_at
                clock = _clock_text(updated, now, hour24) if updated is not None else ""
                reading.updated_bar.set_value(f"new {DOT} {value}", 1.0, hud.AMBER,
                                              short=f"new {DOT} {clock}" if clock else "", value_color=hud.AMBER)
            else:
                reading.updated_bar.set_value(value, 1.0, hud.AMBER if stale else hud.TEXT_DIM, short=short)

    # ---- player signals --------------------------------------------------------------------

    def _on_item_changed(self, section_index: int, item_index: int) -> None:
        if self.state != STATE_READING:
            return
        reading = self.window.reading
        reading.highlight(section_index, item_index)
        self._set_briefing_speech(self._item_text(section_index, item_index))

    def _item_text(self, section_index: int, item_index: int) -> str:
        script = self._script
        if script is None or not 0 <= section_index < len(script.sections):
            return ""
        items = script.sections[section_index].items
        if not 0 <= item_index < len(items):
            return ""
        return items[item_index].display or items[item_index].spoken

    def _on_section_started(self, section_index: int) -> None:
        self._has_played = True
        logger.debug("Section %d started", section_index)
        script = self._script
        if script is None or self.state != STATE_READING or not 0 <= section_index < len(script.sections):
            return
        previous = self._last_section
        if (previous is not None and previous != section_index and not self._skip_pending
                and 0 <= previous < len(script.sections)):
            self._activity(hud.TAG_DONE, f"Read {_section_title(script.sections[previous])}")
        self._skip_pending = False
        self._last_section = section_index
        self._sections_started.add(section_index)
        order = script.section_indices(self._include_ignored)
        position = order.index(section_index) + 1 if section_index in order else len(order)
        self._activity(hud.TAG_RUN, f"Reading {_section_title(script.sections[section_index])}",
                       f"section {position} of {len(order)}")
        self._update_reading_controls()

    def _on_player_state(self, state: str) -> None:
        self._pump_voice()
        if state == FINISHED and self.state == STATE_READING and self._last_section is not None:
            self._last_section = None
            self._activity(hud.TAG_DONE, "Finished the briefing",
                           _plural(len(self._sections_started), "section") + " read")
        self._update_reading_controls()
        if state == FINISHED and self._pending_run and self.state == STATE_READING:
            QTimer.singleShot(0, self._offer_pending_run)   # outside the player's signal

    def _offer_pending_run(self) -> None:
        """The reading ended while a newer run was due: show that briefing now (or, in the classic
        mode, ask about it)."""
        if self.state == STATE_READING and self._pending_run and self.player.state == FINISHED:
            if self._pending_briefing is not None:
                self._replace_briefing(self._pending_briefing)
            else:
                self._back_to_prompt(self._pending_run, take_focus=False)

    def _on_player_error(self, message: str) -> None:
        logger.warning("Playback problem: %s", message)
        self._flash_note("Could not play part of the audio")

    # ---- reading controls --------------------------------------------------------------------

    def toggle_play(self) -> None:
        """Play/Pause; Replay when finished; Retry when loading failed."""
        if self.state != STATE_READING:
            return
        if self._script is None:
            # Retry is offered while an error is shown, also between polling attempts.
            if self._fetch_error is not None or not self.fetching:
                self.retry()
            return
        self._owner_playback()   # the owner's Play / Pause wins over anything Jarvis is saying
        if self.player.state in (FINISHED, IDLE):
            logger.info("Replay" if self._has_played or self.player.state == FINISHED else "Play")
            self._autoplay = True
            self._show_briefing_tab()
            self._settle_reading()   # a reading screen opened for Ask: the briefing is heard now
            self._start_playback(self._script.section_indices(self._include_ignored))
        else:
            if self.player.state == PAUSED:
                self._show_briefing_tab()   # Play again from the JARVIS tab shows the transcript
            self.player.toggle()

    def skip_section(self) -> None:
        if self.state == STATE_READING and self._script is not None:
            logger.info("Skip section")
            self._owner_playback()
            self._skip_pending = self.player.state in (PLAYING, PAUSED, WAITING)
            self.player.skip_section()

    def jump_to_section(self, index: int) -> None:
        """Play from section ``index`` (a click on the section list)."""
        script = self._script
        if self.state != STATE_READING or script is None:
            return
        order = script.section_indices(self._include_ignored)
        if index not in order:
            return
        logger.info("Jump to section %d", index)
        self._owner_playback()
        self._skip_pending = self._current_section() is not None
        self._show_briefing_tab()
        key = self._shown_key()
        if key is not None:
            self._mark_viewed(key)
        self.player.start([i for i in order if i >= index])

    def read_everything(self) -> None:
        """Also read the ignored sections, in document order from where playback is."""
        script = self._script
        if (self.state != STATE_READING or script is None or not script.has_ignored
                or self._include_ignored):
            return
        logger.info("Read everything")
        self._owner_playback()
        self._include_ignored = True
        self._show_briefing_tab()
        key = self._shown_key()
        if key is not None:
            self._mark_viewed(key)
        self.window.reading.set_script(script, include_ignored=True)
        self._update_read_everything()
        self._activity(hud.TAG_RUN, "Reading everything", "skipped sections included")
        ignored = [index for index, section in enumerate(script.sections) if section.ignored]
        current = self.player.current_section
        if self.player.state in (FINISHED, IDLE) or current is None:
            self.player.start(ignored)
        else:
            upcoming = [index for index in ignored if index < current]
            upcoming += list(range(current + 1, len(script.sections)))
            self.player.replace_upcoming(upcoming)
        self._update_reading_controls()

    def open_in_notion(self) -> None:
        logger.info("Open in Notion")
        QDesktopServices.openUrl(QUrl(self.config.notion_url))

    def done(self) -> None:
        logger.info("Done")
        if self.state == STATE_READING and self._pending_run and self._pending_briefing is None:
            self._back_to_prompt(self._pending_run, take_focus=True)   # it said "briefing is due"
            return
        self._record_done()   # a newer briefing waiting to be shown is NEW at the next open
        self.quit()

    # ---- run state (catch-up) -------------------------------------------------------------------

    def record_shown(self) -> str | None:
        """The prompt was shown: remember it for the current slot. The slot key, or None."""
        if self.run_state is None:
            return None
        return self.run_state.record_shown(self._now(), self.slots)

    def record_answer(self, how: str) -> str | None:
        """Read now / Dismiss / Done ("read", "dismissed", "done") settles the current slot."""
        if self.run_state is None:
            return None
        return self.run_state.record_answer(self._now(), self.slots, how)

    def _record_done(self) -> str | None:
        """Done on the reading screen settles the slot it read; a newer slot that came up
        meanwhile (its prompt was never shown here) stays open for the catch-up."""
        if self.run_state is None:
            return None
        if self.state == STATE_READING and not self._reading_settled:
            logger.info("Done: the briefing was not played on this reading screen (opened for Ask); "
                        "its slot stays open")
            return None
        if self.state == STATE_READING and self._read_slot is not None:
            key = handled_slot_key(self._now(), self.slots)
            if key is not None and key != self._read_slot:
                logger.info("Done: the %s slot was not read here; leaving it open", key)
                return None
        return self.record_answer("done")

    # ---- activation and shutdown ----------------------------------------------------------------

    def handle_activation(self, message: dict) -> None:
        """A second launch asked this instance to come forward.

        ``read`` -> READ; ``open`` or ``ask`` -> OPEN; an older launcher's ``now`` alone -> READ
        (its old meaning); a ``run`` alone -> SCHEDULED; nothing -> OPEN, or in the classic mode
        while the prompt is up or snoozed the prompt comes forward as before."""
        message = message if isinstance(message, dict) else {}
        run = _normalize_run(message.get("run"))
        now = message.get("now") is True
        ask = message.get("ask") is True
        if message.get("read") is True:
            kind: str | None = LAUNCH_READ
        elif message.get("open") is True or ask:
            kind = LAUNCH_OPEN
        elif now:
            kind = LAUNCH_READ
        elif run:
            kind = LAUNCH_SCHEDULED
        else:
            kind = None
        logger.info("Another launch asked this instance to come forward (run=%s, now=%s, ask=%s, launch=%s, "
                    "state=%s)", run or "-", now, ask, kind or "-", self.state)
        if self.state in (STATE_QUITTING, STATE_IDLE):
            return
        if self.state in (STATE_PROMPT, STATE_SNOOZED) and kind != LAUNCH_OPEN:
            # The classic prompt answers as it always did; READ (--read, or an older now:true) reads.
            self._legacy_activation(run, kind == LAUNCH_READ)
            return
        if kind is None:
            kind = LAUNCH_OPEN
        if self.state == STATE_WAITING:
            self._activation_while_waiting(kind, run)
        elif kind == LAUNCH_SCHEDULED:
            self._scheduled_activation(run)
        elif kind == LAUNCH_READ:
            self._read_activation()
        else:
            self._open_activation(ask=ask)

    def _legacy_activation(self, run: str | None, read: bool) -> None:
        """The classic prompt (or its snooze) and a SCHEDULED or READ message: as before (``read``:
        Read now, as the older ``now: true`` always did)."""
        if self.window.isVisible() and self.window.isMinimized():
            self.window.showNormal()   # laid out again as a normal window; brought forward below
        if run and run != self.expected_run:
            self._switch_expected_run(run)
        elif run:
            self._restart_run(poll=not read)
        # show_prompt / enter_reading look at Notion again when the copy we have is stale.
        if read:
            self.enter_reading()
            force_foreground(self.window)
        else:
            self.show_prompt(take_focus=True)

    def _activation_while_waiting(self, kind: str, run: str | None) -> None:
        """WAITING (hidden, polling): OPEN shows the screen with a greeting, READ plays what is
        there now, SCHEDULED for another run waits for that one instead."""
        if kind == LAUNCH_SCHEDULED:
            if run and run != self.expected_run:
                self._switch_expected_run(run)
            else:
                logger.info("Already waiting for the %s briefing", self.expected_run or "next")
            return
        if kind == LAUNCH_READ:
            self._stop_fetch()
            self.enter_reading()
            force_foreground(self.window)
            return
        self._open_from_waiting(greet=True)

    def _open_activation(self, *, ask: bool) -> None:
        """OPEN into a running app: the legacy prompt gives way to the assistant screen; a hidden or
        minimized window is restored with a greeting (unless one was said in the last 2 minutes);
        a visible one just comes forward, its tab unchanged. A NEW briefing is announced once."""
        window = self.window
        restored = self.state != STATE_READING or not window.isVisible() or window.isMinimized()
        if window.isVisible() and window.isMinimized():
            window.showNormal()   # laid out again as a normal window; brought forward below
        if self.state != STATE_READING:
            self._stop_prompt_timers()
        self.open_assistant(ask_requested=ask)
        if restored and self.state == STATE_READING:
            self.window.reading.set_tab(hud.TAB_JARVIS)
        greet = restored and time.monotonic() - self._last_greeting_at >= _GREETING_REPEAT_S
        if greet:
            self._begin_greeting(wait=False)
        else:
            self._announce()

    def _read_activation(self) -> None:
        """READ into the assistant screen: in front on the BRIEFING tab, playing when it was not."""
        if self.window.isVisible() and self.window.isMinimized():
            self.window.showNormal()
        force_foreground(self.window)
        self._show_briefing_tab()
        if self._script is not None and self.player.state in (IDLE, FINISHED):
            self.toggle_play()

    def _scheduled_activation(self, run: str | None) -> None:
        """SCHEDULED for ``run`` while the assistant screen is open. Classic mode: as before (a
        newer run's prompt). Else nothing comes forward: when the shown briefing is not already that
        run's fresh one, it is waited for in the background and replaces the shown one (5.3)."""
        if self._classic():
            if self.window.isVisible() and self.window.isMinimized():
                self.window.showNormal()
            self._activation_while_reading(run)
            return
        if not run:
            return
        briefing = self._briefing
        if (self._script is not None and briefing is not None
                and check_freshness(briefing.header, run, self._now(), run_started=self._run_start_for(run)).fresh):
            logger.info("The %s briefing is already shown", run)
            return
        if self.startup_error:
            return
        if run != self.expected_run:
            self._switch_expected_run(run)
        else:
            self._run_started = self._run_start_for(run)
            if not self._polling:
                self._start_fetch(run)
        self._background_run = run
        logger.info("Waiting for the %s briefing in the background", run)

    def _switch_expected_run(self, run: str) -> None:
        logger.info("Now waiting for the %s briefing", run)
        self.expected_run = run
        self._run_started = self._scheduled_start(run)
        self._prefetch = None
        if not self.startup_error:
            self._start_fetch(run)
        self._refresh_prefetch()

    def _run_start_for(self, run: str | None) -> datetime:
        """When ``run`` began: a relaunch of the same run within hours keeps the original start,
        so last night's PM briefing is not called stale after midnight; anything else starts at
        its scheduled slot (when that passed within the catch-up window) or now."""
        now = self._now()
        if run == self.expected_run and now - self._run_started < _SAME_RUN_WINDOW:
            return self._run_started
        return self._scheduled_start(run)

    def _scheduled_start(self, run: str | None) -> datetime:
        """The time ``run``'s scheduled slot passed, when that was within the catch-up window
        (3 hours) before now; else now.

        A PM catch-up at 00:30 (or a PM task that started late) then waits for the PM
        briefing of the evening before, as the 23:42 task would have, instead of asking
        for one dated today. Without slots (tests, harnesses) it is always now.
        """
        now = self._now()
        if not run or run not in self.slots:
            return now
        slot = slot_for(now, {run: self.slots[run]})
        return slot.at if slot is not None else now

    def _restart_run(self, *, poll: bool) -> None:
        """The expected run was launched again (next day's task or by hand): wait for it anew."""
        self._run_started = self._run_start_for(self.expected_run)
        if poll and not self.startup_error and not self.fetching and not self._cached_is_fresh():
            logger.info("Waiting for the %s briefing again", self.expected_run)
            self._start_fetch(self.expected_run)

    def _activation_while_reading(self, run: str | None) -> None:
        """A launch while the reading screen is open: a newer run's briefing is offered."""
        if not run or not self._reading_is_outdated(run):
            force_foreground(self.window)
            return
        if self._script is not None and self.player.state in (PLAYING, WAITING, PAUSED):
            # Never cut off a listen in progress: ask about the new run when it ends
            # (or on Done).
            logger.info("The %s briefing is due; asking about it after this reading", run)
            self._pending_run = run
            self._flash_note(self._pending_note(), sticky=True)
            force_foreground(self.window)
            return
        self._back_to_prompt(run, take_focus=True)

    def _reading_is_outdated(self, run: str) -> bool:
        """Whether a launch for ``run`` asks for something other than what is on screen."""
        if self._script is None:   # still loading or showing an error
            return run != self.expected_run
        briefing = self._briefing
        return briefing is None or not check_freshness(
            briefing.header, run, self._now(), run_started=self._run_start_for(run)).fresh

    def _back_to_prompt(self, run: str, *, take_focus: bool) -> None:
        """Close the reading screen (nothing is playing) and ask about ``run``'s briefing."""
        logger.info("Leaving the reading screen to ask about the %s briefing", run)
        self._reset_reading("the reading screen closed")   # never send from a hidden card
        self.state = STATE_PROMPT
        self._switch_expected_run(run)
        self.show_prompt(take_focus=take_focus)

    def quit(self) -> None:
        self.shutdown()

    def shutdown(self) -> None:
        """Stop everything and quit the event loop. Safe to call more than once.

        A running Google sign-in or event insert is not waited for: its thread
        is a daemon and its result is dropped. An undo countdown is cancelled
        (nothing is sent). An answer, move or cancel already on its way to
        Google is waited for at most 5 s, with the window already hidden, so
        its result is saved; after that the app exits regardless and the next
        start shows that card as UNKNOWN.
        """
        if self._shut_down:
            return
        self._shut_down = True
        logger.info("Shutting down")
        self.live.set_listener(None)   # the LIVE view's feed hears nothing more
        self._live_feed.stop()
        if self._on_shutdown is not None:
            self._on_shutdown()
        self.state = STATE_QUITTING
        self._closing.set()
        for timer in (self._ignore_timer, self._tick_timer, self._snooze_timer, self._note_timer,
                      self._recheck_timer, self._agenda_timer, self._countdown_timer, self._greeting_timer,
                      self._held_timer):
            timer.stop()
        countdown, self._countdown = self._countdown, None
        if countdown is not None:
            self._jobs.pop(countdown.action.id, None)
            logger.info("Action %s undone (app closed); nothing was sent", countdown.action.id)
            _live_countdown_end(countdown, _live.STATUS_CANCELLED, "", "Jarvis closed - nothing was sent")
        if self.ask is not None:
            self.ask.shutdown()   # a running Ask is cancelled: nothing is proposed
        self._snapshots_call("shutdown")   # no page is opened any more; the hidden view goes
        self._close_live_window()
        if self._dialog is not None:
            self._dialog.reject()
        if self._account_dialog is not None:
            self._account_dialog.reject()
        # Hide first: the media player has (rarely) hung in stop(), and the window
        # should be gone either way. Jarvis's voice has a media player of its own.
        self.window.hide()
        try:
            self.voice.shutdown()   # stops speaking and drops what was queued
        except Exception as exc:  # noqa: BLE001 - shutting down goes on
            logger.debug("Jarvis's voice did not shut down cleanly (%s)", type(exc).__name__)
        self.player.stop()
        self._stop_fetch()
        if self._tts is not None:
            self._tts.stop()
        if self._calendar_worker is not None:
            if self._send_in_flight():
                logger.info("Waiting up to %d s for a change on its way to Google", int(_QUIT_SEND_WAIT_S))
                finished = self._calendar_worker.wait_idle(_QUIT_SEND_WAIT_S)
                logger.info("The change on its way to Google %s", "finished" if finished
                            else "did not finish in time; it shows as unknown at the next start")
            self._calendar_worker.stop()
        self.live.close()   # a worker that still records hits no-ops (its pictures go too)
        self._known_days.clear()
        if self.tray is not None:
            self.tray.hide()
        if self._audio_dir is not None:
            shutil.rmtree(self._audio_dir, ignore_errors=True)
            busy = [worker for worker in (self._tts, self._say_worker) if worker is not None and worker.is_alive()]
            if busy or self._audio_dir.exists():
                _remove_audio_dir_after(busy, self._audio_dir)   # both speech workers ("tts-say" too)
        QCoreApplication.quit()
