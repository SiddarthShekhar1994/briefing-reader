"""Meeting recording, part 1: what the owner sees (hologram look, British spelling; amber only when a decision
waits - the start card - and coral for the recording itself).

Nothing here records, reads or downloads anything: every widget shows what the app hands it and every click only
emits a signal. The app (meetings_ui.MeetingsController) starts a recording only from the start card's Start, and
the card builds that request only while its consent tick is ticked.

Public API
==========
``ConsentTick(text)``: an unticked box (the NEW RECIPIENT tick's look): "Everyone in this meeting has agreed to be
    recorded", and the card's own "summarise it" box. ``StartCard``: ``open_card(info, mic, call, problem="",
    choices=(), summaries=False, kind=KIND_CALL, title="")`` always opens UNTICKED (never remembered); Start stays
    disabled until the consent tick, which counts for CONSENT_FRESH_S only (then it unticks: tick again) and goes
    whenever the card closes - Cancel, Start, another screen, the window hidden or minimised. Any meeting (owner
    decisions 2026-10-22): a title box (``title_edit``; left blank the meeting is called "Meeting, Wed 22 Oct 2:05
    PM"), "In person" / "On a call" (``kind_choice``; ``kind`` the one recorded last - in person only the
    microphone, "Room", is recorded) and an OPTIONAL calendar link: ``info`` the calendar meeting the card opens
    linked to (Record on a calendar meeting), None for none; "Link a calendar meeting" links one of ``choices`` (the
    meetings on now, then today's) - filling in its title only when the box is empty, its guests, event and times -
    and "Unlink" removes the link (both unticking). With ``summaries`` (Ask Jarvis on) a second unticked box asks
    for the summary: only then does the transcript go to the owner's Claude Code by itself once it is written down.
    ``startClicked(StartRequest)`` (``consent_at`` = when the box was ticked), ``cancelled``; ``device_texts()``,
    ``info()`` / ``linked()``, ``link_choices()``, ``link_meeting(index)``, ``unlink()``, ``kind()`` /
    ``set_kind(kind)``.
``KindChoice``: the card's "In person" / "On a call" choice (``kind()``, ``set_kind()``, ``kindChanged(str)``).
``RecBadge(home=False)``: the alert dot, "REC 12:04" and Stop (the header on every screen; Home's larger one).
    ``set_elapsed(text)``, ``set_warning(text)`` (coral: "No call audio" while it can't be recorded);
    ``stopClicked``.
``RecPill``: Jarvis's own always-on-top pill "Recording 12:04" with Stop; a tool window that never takes the focus
    (the toast's flags) and stays while Jarvis is minimised or hidden. ``show_recording(text, warning="")``,
    ``set_elapsed``, ``warning()``, ``dismiss()``, ``text()``; ``set_held(bool)`` / ``is_held()`` (hidden while the
    desktop HUD shows REC as a chip of its own); ``stopClicked``.
``MeetingRowInfo`` / ``MeetingsStage``: the Meetings screen - Record a meeting, what is missing (exact add-ons and
    the model, with Download), the start card, then the meetings (date, title, length, status: Recording /
    Writing it down 40% / Transcript ready / Writing the summary / Summary ready / Failed + Retry) with Delete
    (asked twice: audio, transcript and summary go at once). Signals ``recordClicked``, ``stopClicked``,
    ``deleteClicked(id)``, ``retryClicked(id)``, ``downloadClicked``, ``cancelDownloadClicked``,
    ``startRequested(StartRequest)``. Part 2: a meeting with its summary opens it (``openSummaryClicked(id)``;
    the list gives way to meeting_summary_view.SummaryView, ``show_summary(info)`` / ``show_list()``); one written
    down before summaries existed, or whose summary failed, has Summarise (``summariseClicked(id)``: the owner's
    click; nothing is sent). The summary view's clicks come out as ``addBlockClicked(id, index)``,
    ``keepNoteClicked(id, index)``, ``dismissNoteClicked(id, index)``, ``summaryLinkClicked(link)``;
    ``pageChanged()`` when the list or a summary comes up.
``MeetingsSettingsPanel``: Settings > Meetings - the microphone and the call-audio device (Follow the Windows
    default, or one picked from a menu), the speech model with Download; ``microphonePicked(str)``,
    ``callAudioPicked(str)`` ("" = follow the default), ``downloadClicked``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QGuiApplication, QPainter, QPalette, QPen
from PySide6.QtWidgets import (QAbstractButton, QHBoxLayout, QLineEdit, QMenu, QScrollArea, QSizePolicy,
                               QStackedWidget, QVBoxLayout, QWidget)

from . import holo, hud
from . import meetings as mt
from .meeting_summary_view import SummaryInfo, SummaryView
from .memory_view import COLUMN_MAX, PANEL_PAD, PanelFrame, Rule, SectionHead, body_label

MEETINGS_TITLE = "MEETINGS"
RECORD_TEXT = "Record a meeting"
START_TEXT = "Start recording"
CANCEL_TEXT = "Cancel"
STOP_TEXT = "Stop"
DELETE_TEXT = "Delete"
DELETE_SURE_TEXT = "Delete for good"
KEEP_TEXT = "Keep"
RETRY_TEXT = "Retry"
OPEN_SUMMARY_TEXT = "Open summary"
SUMMARISE_TEXT = "Summarise"
SUMMARY_OFF_NOTE = "A summary needs Ask Jarvis (your Claude Code) to be on"
DOWNLOAD_BUTTON = "Download"
CANCEL_DOWNLOAD = "Stop the download"
CARD_KICKER = "RECORD A MEETING"
CARD_NOTE = ("Jarvis records only after you click Start. The audio stays on this PC, never uploaded: kept 7 days, "
             "the transcript 30 days.")
SUMMARY_TICK_TEXT = ("When it is written down, summarise it with my Claude Code - the transcript and the guests' "
                     "names go to Claude (Anthropic)")
SUMMARY_TICK_PLAIN = ("When it is written down, summarise it with my Claude Code - its title and transcript go to "
                      "Claude (Anthropic)")           # a meeting with no guests (not linked, or none invited)
SUMMARY_NOTE = ("A summary is written only when you ask: this box, or Summarise on the meeting later. Without it "
                "the transcript stays on this PC.")
CONSENT_FRESH_S = 5 * 60          # the consent tick counts this long; then it unticks (tick it again)
CONSENT_STALE_TEXT = "The tick is more than 5 minutes old - tick it again for this meeting."
TITLE_PLACEHOLDER = "Title (optional) - left blank: {default}"     # default: "Meeting, Wed 22 Oct 2:05 PM"
IN_PERSON_TEXT = "In person"
ON_A_CALL_TEXT = "On a call"
IN_PERSON_NAME = "In person: only the microphone is recorded, nothing this PC plays"
ON_A_CALL_NAME = "On a call: your microphone and the call audio are recorded"
LINK_TEXT = "Link a calendar meeting"
UNLINK_TEXT = "Unlink"
LINKED_KICKER = "LINKED TO YOUR CALENDAR"
UNLINKED_KICKER = "NOT LINKED TO YOUR CALENDAR"
UNLINKED_NOTE = "Stop it yourself. No guests and no email drafts: nothing of your calendar is used."
CALL_NOT_RECORDED = "Call audio: not recorded in person - nothing this PC plays is recorded"
CALL_MISSING_TEXT = "No call audio"
CALL_MISSING_ROW = ("No call audio: the call audio device could not be opened, so Others are not recorded - check "
                    "Settings > Meetings")
DELETE_ASK = "Delete the audio, transcript and summary of this meeting? This cannot be undone."
EMPTY_TEXT = "No meetings recorded yet. Recording starts only when you click Start on the card."
MISSING_KICKER = "MISSING"
FOLLOW_DEFAULT = "Follow the Windows default"
PILL_SIZE = QSize(232, 44)
PILL_MARGIN = 12
PILL_FLAGS = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
              | Qt.WindowType.WindowDoesNotAcceptFocus)
DOT = 8
TONE_RECORDING = "recording"
TONE_WORKING = "working"
TONE_READY = "ready"
TONE_FAILED = "failed"
TONE_QUIET = "quiet"
_TONE_COLORS = {TONE_RECORDING: holo.ALERT_TX, TONE_WORKING: holo.HOLO, TONE_READY: holo.OK,
                TONE_FAILED: holo.ALERT_TX, TONE_QUIET: holo.INK_3}


def device_line(name: str, follows: bool) -> str:
    """"Laptop Microphone (Windows default)", "Headset (picked in Settings)", or "None found"."""
    if not name:
        return "None found"
    return f"{name} (Windows default)" if follows else f"{name} (picked in Settings)"


def recording_text(elapsed: str) -> str:
    return f"Recording {elapsed}"


# --------------------------------------------------------------------------
# The consent tick and the start card
# --------------------------------------------------------------------------

class ConsentTick(QAbstractButton):
    """A tick box on the start card: unticked until the owner ticks it (a click, or Space with the focus on it)."""

    def __init__(self, text: str = mt.CONSENT_TEXT, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(False)
        self.setText(text)
        self.setAccessibleName(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.toggled.connect(lambda _on: self.update())

    def _lines(self, width: float) -> list[str]:
        from .offer_view import wrap_lines

        return wrap_lines(self.text(), holo.font("body"), max(80.0, width - holo.TICK_BOX - 12), 3)

    def set_words(self, text: str) -> None:
        """New words (the box keeps its state): the height follows the wrapped sentence."""
        if text == self.text():
            return
        self.setText(text)
        self.setAccessibleName(text)
        if self.width() > 0:
            self.setFixedHeight(self.heightForWidth(self.width()))
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        width = max(240, self.width())
        lines = max(1, len(self._lines(width)))
        return QSize(width, max(holo.TICK_BOX + 8, lines * holo.line_height("body") + 8))

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        return max(holo.TICK_BOX + 8, max(1, len(self._lines(width))) * holo.line_height("body") + 8)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        height = self.heightForWidth(self.width())
        if height != self.height():
            self.setFixedHeight(height)             # the wrapped sentence keeps its room in the card

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            box = QRectF(1, 4, holo.TICK_BOX, holo.TICK_BOX)
            holo.paint_tick_box(painter, box, self.isChecked(), enabled=self.isEnabled())
            if self.hasFocus():
                holo.paint_focus_ring(painter, box, holo.RADIUS_CHECKBOX)
            painter.setFont(holo.font("body"))
            painter.setPen(QColor(holo.INK_1))
            y = 4.0
            left = box.right() + 12
            for line in self._lines(self.width()):
                painter.drawText(QRectF(left, y, self.width() - left, holo.line_height("body")),
                                 int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), line)
                y += holo.line_height("body")
        finally:
            painter.end()


class TitleEdit(QLineEdit):
    """The start card's title box (``field`` fill, a ``line-input`` border, radius 8): one line, at most
    TITLE_CAP characters; left blank, the meeting is called "Meeting, Wed 22 Oct 2:05 PM"."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMaxLength(mt.TITLE_CAP)
        self.setMinimumHeight(38)
        self.setAccessibleName("Meeting title (optional)")
        self.setStyleSheet(f"QLineEdit {{ background: {holo.FIELD}; border: 1px solid rgba(60, 211, 238, 128);"
                           f" border-radius: {holo.RADIUS_BUTTON}px; padding: 0 12px; }}"
                           f" QLineEdit:focus {{ border: 1px solid {holo.HOLO}; }}")
        self.setFont(holo.font("body"))
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Text, QColor(holo.INK_1))
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(holo.INK_4))
        palette.setColor(QPalette.ColorRole.Highlight, holo.alpha(holo.HOLO, 0.35))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(holo.INK_1))
        self.setPalette(palette)


class _KindButton(QAbstractButton):
    """One side of the kind choice: a round mark (filled while chosen) and its words; 32 high, radius 8."""

    HEIGHT = 32
    MARK = 12

    def __init__(self, text: str, kind: str, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.setText(text)
        self.setAccessibleName(name)
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._focus_ring = False
        self.toggled.connect(lambda _on: self.update())

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        width = QFontMetricsF(holo.font("button_sm")).horizontalAdvance(self.text())
        return QSize(int(width) + 14 + self.MARK + 8 + 16, self.HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    def focusInEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._focus_ring = holo.keyboard_focus(event.reason(), self._focus_ring)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.reason() != Qt.FocusReason.ActiveWindowFocusReason:
            self._focus_ring = False
        super().focusOutEvent(event)
        self.update()

    def enterEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            if not self.isEnabled():
                painter.setOpacity(0.45)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            on = self.isChecked()
            fill = (holo.alpha(holo.HOLO, 0.12) if on else
                    holo.alpha(holo.INK_1, 0.05) if self.underMouse() else QColor(0, 0, 0, 0))
            painter.setPen(QPen(QColor(holo.HOLO) if on else holo.LINE, 1.0))
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, holo.RADIUS_BUTTON, holo.RADIUS_BUTTON)
            mark = QRectF(rect.left() + 13, rect.center().y() - self.MARK / 2, self.MARK, self.MARK)
            painter.setPen(QPen(QColor(holo.HOLO) if on else QColor(holo.INK_3), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(mark)
            if on:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(holo.HOLO))
                painter.drawEllipse(mark.center(), 3.0, 3.0)
            painter.setFont(holo.font("button_sm"))
            painter.setPen(QColor(holo.INK_1 if on else holo.INK_2))
            painter.drawText(QRectF(mark.right() + 8, rect.top(), rect.right() - mark.right() - 8, rect.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.text())
            if self.hasFocus() and self._focus_ring:
                holo.paint_focus_ring(painter, rect, holo.RADIUS_BUTTON)
        finally:
            painter.end()


class KindChoice(QWidget):
    """"In person" / "On a call": one is always chosen (the start card opens on the kind recorded last)."""

    kindChanged = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.in_person_button = _KindButton(IN_PERSON_TEXT, mt.KIND_IN_PERSON, IN_PERSON_NAME, self)
        self.call_button = _KindButton(ON_A_CALL_TEXT, mt.KIND_CALL, ON_A_CALL_NAME, self)
        self.call_button.setChecked(True)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(holo.BUTTON_GAP)
        for button in (self.in_person_button, self.call_button):
            button.toggled.connect(lambda on, b=button: self.kindChanged.emit(b.kind) if on else None)
            row.addWidget(button)
        row.addStretch(1)
        self.setAccessibleName("In person or on a call")

    def kind(self) -> str:
        return mt.KIND_IN_PERSON if self.in_person_button.isChecked() else mt.KIND_CALL

    def set_kind(self, kind: str) -> None:
        (self.in_person_button if mt.clean_kind(kind) == mt.KIND_IN_PERSON else self.call_button).setChecked(True)


class StartCard(QWidget):
    """The start card (amber: a decision waits). See the module docstring."""

    startClicked = Signal(object)        # a meetings.StartRequest (only while ticked)
    cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._linked: mt.MeetingInfo | None = None
        self._choices: list[mt.MeetingInfo] = []
        self._mic: tuple[str, bool] = ("", True)
        self._call: tuple[str, bool] = ("", True)
        self._problem = ""
        self._summaries = False
        self._filled = ""                    # the title the link filled in (it goes with an Unlink while untouched)
        self._ticked_at: datetime | None = None
        self._summary_at: datetime | None = None
        self._closing = False
        self._watched: QObject | None = None
        self._now = datetime.now
        self.menu: QMenu | None = None
        self.kicker = body_label(holo.role_text("label", CARD_KICKER), "label", holo.AMBER, wrap=False)
        self.title_edit = TitleEdit()
        self.kind_choice = KindChoice()
        self.kind_choice.kindChanged.connect(lambda _kind: self._show_devices())
        self.link_kicker = body_label("", "label", holo.INK_KICKER, wrap=False, name="meetingCardLink")
        self.link_button = holo.HoloButton(LINK_TEXT, holo.LINK_CYAN, size=holo.SIZE_SM)
        self.link_button.setAccessibleName("Link this recording to a calendar meeting")
        self.link_button.clicked.connect(self._menu)
        self.unlink_button = holo.HoloButton(UNLINK_TEXT, holo.LINK_CYAN, size=holo.SIZE_SM)
        self.unlink_button.setAccessibleName("Unlink the calendar meeting")
        self.unlink_button.clicked.connect(self.unlink)
        self.title_label = body_label("", "body", holo.INK_1, name="meetingCardTitle")
        self.meta_label = body_label("", "body_sm", holo.INK_3)
        self.mic_label = body_label("", "body_sm", holo.INK_2, name="meetingCardMic")
        self.call_label = body_label("", "body_sm", holo.INK_2, name="meetingCardCall")
        self.tick = ConsentTick()
        self.tick.toggled.connect(self._on_tick)
        self.summary_tick = ConsentTick(SUMMARY_TICK_TEXT)
        self.summary_tick.toggled.connect(self._on_summary_tick)
        self.note_label = body_label(CARD_NOTE, "body_sm", holo.INK_3)
        self.summary_note = body_label(SUMMARY_NOTE, "body_sm", holo.INK_3, name="meetingCardSummaryNote")
        self.stale_label = body_label(CONSENT_STALE_TEXT, "body_sm", holo.AMBER_TX, name="meetingCardStale")
        self.stale_label.hide()
        self.problem_label = body_label("", "body_sm", holo.ALERT_TX, name="meetingCardProblem")
        self.problem_label.hide()
        self.start_button = holo.HoloButton(START_TEXT, holo.AMBER_COMMIT)
        self.start_button.clicked.connect(self._on_start)
        self.cancel_button = holo.HoloButton(CANCEL_TEXT, holo.GHOST)
        self.cancel_button.clicked.connect(self._on_cancel)
        self._fresh = QTimer(self)
        self._fresh.setSingleShot(True)
        self._fresh.setInterval(CONSENT_FRESH_S * 1000)
        self._fresh.timeout.connect(self._expire_tick)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(holo.BUTTON_GAP)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        link_row = QHBoxLayout()
        link_row.setContentsMargins(0, 0, 0, 0)
        link_row.setSpacing(8)
        link_row.addWidget(self.link_kicker, 1, Qt.AlignmentFlag.AlignVCenter)
        link_row.addWidget(self.link_button, 0, Qt.AlignmentFlag.AlignVCenter)
        link_row.addWidget(self.unlink_button, 0, Qt.AlignmentFlag.AlignVCenter)
        column = QVBoxLayout(self)
        column.setContentsMargins(20, 16, 20, 16)
        column.setSpacing(6)
        column.addWidget(self.kicker)
        column.addWidget(self.title_edit)
        column.addSpacing(2)
        column.addWidget(self.kind_choice)
        column.addSpacing(6)
        column.addLayout(link_row)
        column.addWidget(self.title_label)
        column.addWidget(self.meta_label)
        column.addSpacing(6)
        column.addWidget(self.mic_label)
        column.addWidget(self.call_label)
        column.addSpacing(8)
        column.addWidget(self.tick)
        column.addWidget(self.note_label)
        column.addWidget(self.stale_label)
        column.addSpacing(4)
        column.addWidget(self.summary_tick)
        column.addWidget(self.summary_note)
        column.addWidget(self.problem_label)
        column.addSpacing(6)
        column.addLayout(buttons)
        self.setMaximumWidth(COLUMN_MAX)
        self.hide()
        self._show_link(None)

    def set_clock(self, now: Any) -> None:
        self._now = now

    def open_card(self, info: mt.MeetingInfo | None, mic: tuple[str, bool], call: tuple[str, bool], *,
                  problem: str = "", choices: Sequence[mt.MeetingInfo] = (), summaries: bool = False,
                  kind: str = mt.KIND_CALL, title: str = "") -> None:
        """Show the card linked to ``info`` (None: linked to no calendar meeting) with the devices it will use;
        ``choices`` the calendar meetings it may link instead; ``summaries``: the "summarise it" box shows (Ask
        Jarvis is on); ``kind`` the choice it starts on (the kind recorded last); ``title`` what the box holds.
        Both ticks are ALWAYS unticked here: never remembered from an earlier meeting."""
        self._mic, self._call = mic, call
        self._problem = problem
        self._summaries = bool(summaries)
        self._choices = [item for item in choices if isinstance(item, mt.MeetingInfo) and item.linked]
        self.problem_label.setText(problem)
        self.problem_label.setVisible(bool(problem))
        self.summary_tick.setVisible(self._summaries)
        self.summary_note.setVisible(self._summaries)
        self.title_edit.setPlaceholderText(TITLE_PLACEHOLDER.format(default=mt.default_title(self._now())))
        self.title_edit.setText(mt.clean_title(title))
        self._filled = ""
        self.kind_choice.set_kind(kind)
        self._show_devices()
        self._show_link(info if info is not None and info.linked else None)
        self.show()

    def _show_devices(self) -> None:
        """The devices the start will use: on a call "You" (the microphone) and "Others" (the call audio); in
        person "Room" (the microphone) and no call audio."""
        if self.kind() == mt.KIND_IN_PERSON:
            self.mic_label.setText(f"Room - microphone: {device_line(*self._mic)}")
            self.call_label.setText(CALL_NOT_RECORDED)
        else:
            self.mic_label.setText(f"You - microphone: {device_line(*self._mic)}")
            self.call_label.setText(f"Others - call audio: {device_line(*self._call)}")

    def _show_link(self, info: mt.MeetingInfo | None) -> None:
        """The card linked to ``info`` (None: to none); both ticks go back to unticked (a tick is for one meeting).
        Linking fills in the meeting's title only when the box is empty; a title the link filled in and nobody
        changed goes with it."""
        self._linked = info
        self.tick.setChecked(False)
        self.summary_tick.setChecked(False)
        self.stale_label.hide()
        typed = mt.clean_title(self.title_edit.text())
        if self._filled and typed == self._filled:
            self.title_edit.clear()
            typed = ""
        self._filled = ""
        if info is not None and not typed and mt.clean_title(info.title):
            self._filled = mt.clean_title(info.title)
            self.title_edit.setText(self._filled)
        if info is not None:
            self.link_kicker.setText(holo.role_text("label", LINKED_KICKER))
            self.title_label.setText(info.shown_title())
            meta = []
            if info.start is not None:
                meta.append(f"{_clock(info.start)} to {_clock(info.end)}" if info.end else _clock(info.start))
                if info.end is not None:
                    meta.append("stops by itself 15 minutes after the end")
            if info.guests:
                meta.append(f"with {mt.merge_names(info.guests)}")
            self.meta_label.setText(" · ".join(meta) if meta else "On your calendar.")
        else:
            self.link_kicker.setText(holo.role_text("label", UNLINKED_KICKER))
            self.title_label.setText("")
            self.meta_label.setText(UNLINKED_NOTE)
        self.title_label.setVisible(info is not None)
        self.unlink_button.setVisible(info is not None)
        self.link_button.setVisible(info is None and bool(self.link_choices()))
        self.summary_tick.set_words(SUMMARY_TICK_TEXT if info is not None and info.guests else SUMMARY_TICK_PLAIN)
        self._sync()

    def info(self) -> mt.MeetingInfo:
        """The calendar meeting the card is linked to (``MeetingInfo()``: none)."""
        return self._linked or mt.MeetingInfo()

    def linked(self) -> mt.MeetingInfo | None:
        return self._linked

    def kind(self) -> str:
        return self.kind_choice.kind()

    def set_kind(self, kind: str) -> None:
        self.kind_choice.set_kind(kind)

    def title_text(self) -> str:
        """The title in the box, as it will be kept ("" left blank)."""
        return mt.clean_title(self.title_edit.text())

    def device_texts(self) -> tuple[str, str]:
        return self.mic_label.text(), self.call_label.text()

    def link_choices(self) -> list[tuple[str, mt.MeetingInfo]]:
        """What "Link a calendar meeting" offers: the calendar meetings on now, then today's (not the linked one)."""
        current = self._linked
        items: list[tuple[str, mt.MeetingInfo]] = []
        for item in self._choices:
            if current is not None and (item.title, item.start, item.event_id) == (current.title, current.start,
                                                                                   current.event_id):
                continue
            when = (f"{_clock(item.start)} to {_clock(item.end)}" if item.start is not None and item.end is not None
                    else _clock(item.start))
            items.append((f"{item.shown_title()} - {when}" if when else item.shown_title(), item))
        return items

    def link_meeting(self, index: int) -> bool:
        """Link the ``index``-th of ``link_choices()`` (unticked again)."""
        items = self.link_choices()
        if not 0 <= index < len(items):
            return False
        self._show_link(items[index][1])
        return True

    def unlink(self) -> None:
        """Unlink: no calendar meeting (no guests, no end time; unticked again)."""
        if self._linked is not None:
            self._show_link(None)

    def _menu(self) -> None:
        menu = QMenu(self)
        for index, (text, _info) in enumerate(self.link_choices()):
            action = menu.addAction(text)
            action.triggered.connect(lambda _checked=False, i=index: self.link_meeting(i))
        self.menu = menu
        menu.popup(self.link_button.mapToGlobal(self.link_button.rect().bottomLeft()))

    def close_card(self) -> None:
        """The card goes and both ticks with it (Cancel, Start, another screen, the window hidden)."""
        if self._closing:
            return
        self._closing = True
        try:
            self.tick.setChecked(False)
            self.summary_tick.setChecked(False)
            self.stale_label.hide()
            self.hide()
        finally:
            self._closing = False

    def _on_tick(self, on: bool) -> None:
        if on:
            self._ticked_at = self._now()
            self.stale_label.hide()
            self._fresh.start()
        else:
            self._ticked_at = None
            self._fresh.stop()
        self._sync()

    def _on_summary_tick(self, on: bool) -> None:
        self._summary_at = self._now() if on else None

    def _expire_tick(self) -> None:
        """The consent tick is CONSENT_FRESH_S old: it unticks (tick it again for the call starting now)."""
        if self.tick.isChecked():
            self.tick.setChecked(False)
            self.stale_label.setVisible(self.isVisible())

    def _fresh_tick(self) -> bool:
        ticked = self._ticked_at
        if ticked is None:
            return False
        now = self._now()
        if (now.tzinfo is None) != (ticked.tzinfo is None):
            now, ticked = now.replace(tzinfo=None), ticked.replace(tzinfo=None)
        return 0 <= (now - ticked).total_seconds() <= CONSENT_FRESH_S

    def _sync(self, *_args: Any) -> None:
        ready = self.tick.isChecked() and not self._problem
        self.start_button.setEnabled(ready)
        self.start_button.setAccessibleDescription("" if ready else "Tick the consent box first")

    def _on_start(self) -> None:
        if not self.isVisible() or not self.tick.isChecked() or self._problem:
            return
        if not self._fresh_tick():
            self._expire_tick()
            self.stale_label.show()
            return
        assert self._ticked_at is not None
        summarise = self._summaries and self.summary_tick.isChecked()
        kind = self.kind()
        request = mt.StartRequest(self.info(), True, self._ticked_at, self._mic[0],
                                  self._call[0] if kind == mt.KIND_CALL else "", summarise=summarise,
                                  summarise_at=self._summary_at if summarise else None, title=self.title_text(),
                                  kind=kind)
        self.close_card()                     # the ticks go back to unticked: never remembered
        self.startClicked.emit(request)

    def _on_cancel(self) -> None:
        self.close_card()
        self.cancelled.emit()

    # ---- the card never outlives the screen or the window it was ticked on ------------------------------

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        window = self.window()
        if window is not self and window is not self._watched:
            if self._watched is not None:
                self._watched.removeEventFilter(self)
            window.installEventFilter(self)
            self._watched = window

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self.close_card()                     # another screen, the summary page, the window hidden or minimised

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if watched is self._watched and not self.isHidden():
            kind = event.type()
            minimized = (kind == QEvent.Type.WindowStateChange and isinstance(watched, QWidget)
                         and watched.isMinimized())
            if minimized or kind == QEvent.Type.Hide:
                self.close_card()
        return False

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            holo.paint_tile(painter, QRectF(self.rect()).adjusted(1, 1, -1, -1), holo.TILE_NEEDS_FOCUS)
        finally:
            painter.end()


def _clock(moment: datetime | None) -> str:
    if moment is None:
        return ""
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


# --------------------------------------------------------------------------
# REC: the header's badge, Home's, and the always-on-top pill
# --------------------------------------------------------------------------

def _paint_dot(painter: QPainter, center: QPointF, size: float = DOT) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(holo.alpha(holo.ALERT, 0.25))
    painter.drawEllipse(center, size, size)
    painter.setBrush(QColor(holo.ALERT))
    painter.drawEllipse(center, size / 2, size / 2)


class _RecMark(QWidget):
    """The alert dot and "REC 12:04" (mono readout, coral)."""

    def __init__(self, prefix: str, role: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._prefix, self._role = prefix, role
        self._elapsed = "0:00"
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def text(self) -> str:
        return f"{self._prefix} {self._elapsed}"

    def set_elapsed(self, text: str) -> None:
        if text != self._elapsed:
            self._elapsed = text
            self.updateGeometry()
            self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        metrics = QFontMetricsF(holo.font(self._role))
        width = DOT * 2 + 8 + metrics.horizontalAdvance(self.text()) + 4
        return QSize(int(width), int(max(20, metrics.height() + 4)))

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            _paint_dot(painter, QPointF(DOT, self.height() / 2))
            painter.setFont(holo.font(self._role))
            painter.setPen(QColor(holo.ALERT_TX))
            painter.drawText(QRectF(DOT * 2 + 6, 0, self.width() - DOT * 2 - 6, self.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.text())
        finally:
            painter.end()


class RecBadge(QWidget):
    """The header's REC (on every screen) or Home's larger one: the dot, the elapsed time and Stop."""

    stopClicked = Signal()

    def __init__(self, *, home: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mark = _RecMark("Recording" if home else "REC", "readout_lg" if home else "readout")
        self.warn_label = body_label("", "body_sm", holo.ALERT_TX, wrap=False, name="recWarning")
        self.warn_label.hide()
        self.stop_button = holo.HoloButton(STOP_TEXT, holo.DANGER_GHOST, size=holo.SIZE_SM)
        self.stop_button.setAccessibleName("Stop recording")
        self.stop_button.clicked.connect(self.stopClicked)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self.mark, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.warn_label, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.stop_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setAccessibleName("Recording")
        self._elapsed = "0:00"
        self.hide()

    def set_elapsed(self, text: str) -> None:
        self._elapsed = text
        self.mark.set_elapsed(text)
        self._describe()

    def set_warning(self, text: str) -> None:
        """Coral words beside REC while something is wrong with the recording ("No call audio"); "" for none."""
        if text != self.warn_label.text():
            self.warn_label.setText(text)
            self.warn_label.setVisible(bool(text))
            self._describe()

    def warning(self) -> str:
        return self.warn_label.text()

    def _describe(self) -> None:
        warning = self.warn_label.text()
        self.setAccessibleDescription(f"Jarvis is recording this meeting: {self._elapsed}"
                                      + (f". {warning}" if warning else ""))

    def text(self) -> str:
        return self.mark.text()


class RecPill(QWidget):
    """The always-on-top pill; see the module docstring."""

    stopClicked = Signal()

    def __init__(self) -> None:
        super().__init__(None, PILL_FLAGS)
        self.setObjectName("JarvisRecording")
        self.setWindowTitle("Jarvis - recording")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(PILL_SIZE)
        self._elapsed = "0:00"
        self._warning = ""
        self._held = False
        self.stop_button = holo.HoloButton(STOP_TEXT, holo.DANGER_GHOST, size=holo.SIZE_SM, parent=self)
        self.stop_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)       # a click never moves the focus here
        self.stop_button.setAccessibleName("Stop recording")
        self.stop_button.clicked.connect(self.stopClicked)
        self.setAccessibleName("Jarvis is recording")
        self._place_children()

    def text(self) -> str:
        return recording_text(self._elapsed)

    def warning(self) -> str:
        return self._warning

    def set_warning(self, warning: str) -> None:
        """A second, coral line under "Recording 12:04" ("No call audio"); "" for none (the pill is taller with it)."""
        if warning == self._warning:
            return
        self._warning = warning
        self.setFixedSize(PILL_SIZE if not warning else QSize(PILL_SIZE.width(), PILL_SIZE.height() + 16))
        self._place_children()
        self.setAccessibleDescription(self.text() + (f". {warning}" if warning else ""))
        self.update()

    def show_recording(self, elapsed: str, warning: str = "") -> None:
        """Show (or keep showing) the pill, top centre of the primary screen's work area, never the focus. Held
        (``set_held``), it only keeps its time."""
        self.set_elapsed(elapsed)
        self.set_warning(warning)
        if self._held:
            return
        self.place()
        handle = self.windowHandle()
        if handle is not None:
            handle.setProperty("_q_showWithoutActivating", True)
        if not self.isVisible():
            self.show()

    def set_held(self, held: bool) -> None:
        """While the desktop HUD shows, its REC chip says this and the pill (where the HUD's bar is) stays hidden;
        released, the next ``show_recording`` (each second while a meeting records) shows it again."""
        self._held = bool(held)
        if self._held:
            self.hide()

    def is_held(self) -> bool:
        return self._held

    def set_elapsed(self, elapsed: str) -> None:
        if elapsed != self._elapsed:
            self._elapsed = elapsed
            self.setAccessibleDescription(self.text() + (f". {self._warning}" if self._warning else ""))
            self.update()

    def place(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area: QRect = screen.availableGeometry()
        self.move(area.center().x() - self.width() // 2, area.top() + PILL_MARGIN)

    def dismiss(self) -> None:
        self.hide()

    def _place_children(self) -> None:
        size = self.stop_button.sizeHint()
        self.stop_button.setGeometry(self.width() - 6 + holo.HoloButton.MARGIN - size.width(),
                                     (self.height() - size.height()) // 2, size.width(), size.height())

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            radius = rect.height() / 2
            painter.setPen(QPen(holo.alpha(holo.ALERT, 0.65), 1.0))
            painter.setBrush(QColor(holo.RAISED))
            painter.drawRoundedRect(rect, radius, radius)
            _paint_dot(painter, QPointF(20, rect.center().y()))
            painter.setFont(holo.font("button"))
            painter.setPen(QColor(holo.INK_1))
            left = 34.0
            right = self.stop_button.geometry().left() + holo.HoloButton.MARGIN - 6
            if not self._warning:
                painter.drawText(QRectF(left, 0, right - left, self.height()),
                                 int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.text())
            else:
                half = self.height() / 2
                painter.drawText(QRectF(left, 4, right - left, half - 4),
                                 int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom), self.text())
                painter.setFont(holo.font("body_sm"))
                painter.setPen(QColor(holo.ALERT_TX))
                painter.drawText(QRectF(left, half, right - left, half - 4),
                                 int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop), self._warning)
        finally:
            painter.end()


# --------------------------------------------------------------------------
# The Meetings screen
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MeetingRowInfo:
    id: str
    when: str
    title: str = field(repr=False)
    length: str = ""
    status: str = mt.STATUS_RECORDED
    status_text: str = ""
    tone: str = TONE_QUIET
    note: str = field(default="", repr=False)
    can_summarise: bool = False          # Summarise shows (a transcript and no summary; Ask Jarvis on)
    summary_failed: bool = False


def device_note(changes: Sequence[Any]) -> str:
    """What the meeting record's device changes mean, in words: "the call audio was not recorded (its device could
    not be opened)", "... was recorded only once its device could be opened", "the microphone switched device during
    the meeting", "the call audio stopped for a moment" ("" for none)."""
    notes: list[str] = []
    for channel in ("microphone", "call audio"):
        kinds = [str(item.get("change") or "") for item in changes
                 if isinstance(item, dict) and item.get("channel") == channel]
        if not kinds:
            continue
        if "unavailable" in kinds:
            later = kinds[kinds.index("unavailable") + 1:]
            notes.append(f"the {channel} was recorded only once its device could be opened"
                         if any(kind in ("opened", "switched", "reopened") for kind in later) else
                         f"the {channel} was not recorded (its device could not be opened)")
            kinds = later[1:] if later and later[0] == "opened" else later
        if "switched" in kinds:
            notes.append(f"the {channel} switched device during the meeting")
        elif "reopened" in kinds:
            notes.append(f"the {channel} stopped for a moment")
    return " \u00b7 ".join(notes)


def row_info(record: mt.MeetingRecord, *, elapsed: str = "", summaries: bool = True,
             waiting: bool = False, call_missing: bool = False) -> MeetingRowInfo:
    """A meeting record as the list shows it (``elapsed`` for the one recording now; ``summaries``: Ask Jarvis is on,
    so a transcript can be summarised; ``waiting``: its summary waits for Jarvis to finish another request;
    ``call_missing``: it records, but the call audio can't be recorded right now)."""
    started = record.started_at
    when = f"{started:%a %d %b} {_clock(started)}" if started is not None else ""
    failed = record.status == mt.STATUS_TRANSCRIBED and bool(record.summary_error)
    tone = {mt.STATUS_RECORDING: TONE_RECORDING, mt.STATUS_TRANSCRIBING: TONE_WORKING,
            mt.STATUS_TRANSCRIBED: TONE_READY, mt.STATUS_SUMMARISING: TONE_WORKING,
            mt.STATUS_SUMMARY_READY: TONE_READY, mt.STATUS_FAILED: TONE_FAILED,
            mt.STATUS_DELETING: TONE_FAILED}.get(record.status, TONE_QUIET)
    if failed:
        tone = TONE_FAILED
    length = elapsed if record.status == mt.STATUS_RECORDING else mt.length_text(record.duration_s)
    note = mt.STOP_WORDS.get(record.stop_reason, "") if record.status != mt.STATUS_RECORDING else ""
    if record.status == mt.STATUS_RECORDING and call_missing:
        note = CALL_MISSING_ROW
    elif record.device_changes:
        words = device_note(record.device_changes)
        if words:
            note = (note + " \u00b7 " if note else "") + words
    status_text = record.status_text()
    if record.status == mt.STATUS_TRANSCRIBED and waiting:
        status_text = "Summary waits for Jarvis to finish another request"
    elif record.status == mt.STATUS_TRANSCRIBED and not summaries:
        note = (note + " \u00b7 " if note else "") + SUMMARY_OFF_NOTE
    return MeetingRowInfo(record.id, when, record.shown_title(), length, record.status, status_text, tone,
                          note, can_summarise=summaries and record.status == mt.STATUS_TRANSCRIBED and not waiting,
                          summary_failed=failed)


class MeetingRow(QWidget):
    """One meeting: date, title, length, status (and Retry when it failed), Delete (asked twice)."""

    deleteClicked = Signal(str)
    retryClicked = Signal(str)
    stopClicked = Signal()
    openClicked = Signal(str)
    summariseClicked = Signal(str)

    def __init__(self, info: MeetingRowInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.info = info
        self.when_label = body_label(holo.role_text("label", info.when), "label", holo.INK_KICKER, wrap=False)
        self.title_label = body_label(info.title, "tile_title", holo.INK_1, name="meetingTitle")
        self.length_label = body_label(info.length, "readout", holo.INK_3, wrap=False)
        self.status_label = body_label(info.status_text, "body_sm", _TONE_COLORS.get(info.tone, holo.INK_3),
                                       wrap=False, name="meetingStatus")
        self.note_label = body_label(info.note, "body_sm", holo.ALERT_TX if info.note == CALL_MISSING_ROW
                                     else holo.INK_4, name="meetingNote")
        self.note_label.setVisible(bool(info.note))
        self.retry_button = holo.HoloButton(RETRY_TEXT, holo.GHOST, size=holo.SIZE_SM)
        self.retry_button.clicked.connect(lambda: self.retryClicked.emit(self.info.id))
        self.retry_button.setVisible(info.status == mt.STATUS_FAILED)
        self.open_button = holo.HoloButton(OPEN_SUMMARY_TEXT, holo.GO, size=holo.SIZE_SM)
        self.open_button.setAccessibleName(f"{OPEN_SUMMARY_TEXT}: {info.title}")
        self.open_button.clicked.connect(lambda: self.openClicked.emit(self.info.id))
        self.open_button.setVisible(info.status == mt.STATUS_SUMMARY_READY)
        self.summarise_button = holo.HoloButton(RETRY_TEXT if info.summary_failed else SUMMARISE_TEXT, holo.GHOST,
                                                size=holo.SIZE_SM)
        self.summarise_button.setAccessibleName(f"{SUMMARISE_TEXT}: {info.title}")
        self.summarise_button.clicked.connect(lambda: self.summariseClicked.emit(self.info.id))
        self.summarise_button.setVisible(info.can_summarise)
        self.stop_button = holo.HoloButton(STOP_TEXT, holo.DANGER_GHOST, size=holo.SIZE_SM)
        self.stop_button.setAccessibleName("Stop recording")
        self.stop_button.clicked.connect(self.stopClicked)
        self.stop_button.setVisible(info.status == mt.STATUS_RECORDING)
        self.delete_button = holo.HoloButton(DELETE_TEXT, holo.DANGER_GHOST, size=holo.SIZE_SM)
        self.delete_button.setAccessibleName("Delete this meeting")
        self.delete_button.clicked.connect(self._ask)
        self.delete_button.setVisible(info.status != mt.STATUS_RECORDING)
        self.ask_label = body_label(DELETE_ASK, "body_sm", holo.ALERT_TX)
        self.sure_button = holo.HoloButton(DELETE_SURE_TEXT, holo.DANGER, size=holo.SIZE_SM)
        self.sure_button.clicked.connect(lambda: self.deleteClicked.emit(self.info.id))
        self.keep_button = holo.HoloButton(KEEP_TEXT, holo.GHOST, size=holo.SIZE_SM)
        self.keep_button.clicked.connect(self._keep)
        self._ask_box = QWidget()
        ask = QHBoxLayout(self._ask_box)
        ask.setContentsMargins(0, 0, 0, 0)
        ask.setSpacing(holo.BUTTON_GAP)
        ask.addWidget(self.ask_label, 1)
        ask.addWidget(self.sure_button)
        ask.addWidget(self.keep_button)
        self._ask_box.hide()
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(12)
        top.addWidget(self.when_label)
        top.addStretch(1)
        top.addWidget(self.length_label)
        middle = QHBoxLayout()
        middle.setContentsMargins(0, 0, 0, 0)
        middle.setSpacing(holo.BUTTON_GAP)
        middle.addWidget(self.status_label)
        middle.addStretch(1)
        for button in (self.retry_button, self.summarise_button, self.open_button, self.stop_button,
                       self.delete_button):
            middle.addWidget(button)
        column = QVBoxLayout(self)
        column.setContentsMargins(*PANEL_PAD)
        column.setSpacing(4)
        column.addLayout(top)
        column.addWidget(self.title_label)
        column.addLayout(middle)
        column.addWidget(self.note_label)
        column.addWidget(self._ask_box)
        self.setAccessibleName(f"{info.title}, {info.when}, {info.status_text}")

    def asking(self) -> bool:
        return not self._ask_box.isHidden()

    def set_call_missing(self, missing: bool) -> None:
        """While it records: the coral note that the call audio is not recorded right now (each second)."""
        if self.info.status != mt.STATUS_RECORDING:
            return
        text = CALL_MISSING_ROW if missing else ("" if self.info.note == CALL_MISSING_ROW else self.info.note)
        if text != self.note_label.text():
            self.note_label.setText(text)
            hud.set_label_color(self.note_label, holo.ALERT_TX if missing else holo.INK_4)
            self.note_label.setVisible(bool(text))

    def _ask(self) -> None:
        self._ask_box.show()
        self.delete_button.hide()

    def _keep(self) -> None:
        self._ask_box.hide()
        self.delete_button.show()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            failed = self.info.status == mt.STATUS_FAILED or self.info.summary_failed
            state = holo.TILE_ERROR if failed else holo.TILE_DEFAULT
            holo.paint_tile(painter, QRectF(self.rect()).adjusted(1, 1, -1, -1), state)
        finally:
            painter.end()


_TOP_WIDGETS = 3                  # the missing panel, the start card and the empty line come before the rows


class MeetingsStage(QWidget):
    """The Meetings screen; see the module docstring."""

    recordClicked = Signal()
    stopClicked = Signal()
    deleteClicked = Signal(str)
    retryClicked = Signal(str)
    downloadClicked = Signal()
    cancelDownloadClicked = Signal()
    startRequested = Signal(object)
    openSummaryClicked = Signal(str)
    summariseClicked = Signal(str)
    addBlockClicked = Signal(str, int)
    keepNoteClicked = Signal(str, int)
    dismissNoteClicked = Signal(str, int)
    summaryLinkClicked = Signal(str)
    pageChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[MeetingRow] = []
        self._recording = False
        self.head = SectionHead("meetings", MEETINGS_TITLE)
        self.record_button = holo.HoloButton(RECORD_TEXT, holo.GO, icon="record")
        self.record_button.clicked.connect(self.recordClicked)
        head_row = QHBoxLayout()
        head_row.setContentsMargins(0, 0, 0, 0)
        head_row.addWidget(self.head, 1)
        head_row.addWidget(self.record_button)
        self.missing_panel = PanelFrame()
        self.missing_kicker = body_label(holo.role_text("label", MISSING_KICKER), "label", holo.INK_KICKER,
                                         wrap=False)
        self.missing_label = body_label("", "body_sm", holo.INK_2, selectable=True, name="meetingsMissing")
        self.download_label = body_label("", "body_sm", holo.INK_3, name="meetingsDownload")
        self.download_button = holo.HoloButton(DOWNLOAD_BUTTON, holo.GHOST, size=holo.SIZE_SM)
        self.download_button.clicked.connect(self.downloadClicked)
        self.cancel_download_button = holo.HoloButton(CANCEL_DOWNLOAD, holo.GHOST, size=holo.SIZE_SM)
        self.cancel_download_button.clicked.connect(self.cancelDownloadClicked)
        self.cancel_download_button.hide()
        download_row = QHBoxLayout()
        download_row.setContentsMargins(0, 0, 0, 0)
        download_row.setSpacing(holo.BUTTON_GAP)
        download_row.addWidget(self.download_label, 1)
        download_row.addWidget(self.download_button)
        download_row.addWidget(self.cancel_download_button)
        missing = QVBoxLayout(self.missing_panel)
        missing.setContentsMargins(*PANEL_PAD)
        missing.setSpacing(6)
        missing.addWidget(self.missing_kicker)
        missing.addWidget(self.missing_label)
        missing.addLayout(download_row)
        self.missing_panel.hide()
        self.card = StartCard()
        self.card.startClicked.connect(self.startRequested)
        self.empty_label = body_label(EMPTY_TEXT, "body_sm", holo.INK_3, name="meetingsEmpty")
        # One scroll area holds what is missing, the start card and the meetings: nothing is squeezed in a short
        # window.
        self.scroll = QScrollArea()
        content = hud._transparent_scroll(self.scroll)
        self._list = QVBoxLayout(content)
        self._list.setContentsMargins(0, 0, 8, 0)
        self._list.setSpacing(holo.TILE_GAP)
        for widget in (self.missing_panel, self.card, self.empty_label):
            self._list.addWidget(widget)
        self._list.addStretch(1)
        # Part 2: a meeting's summary takes the list's place (All meetings brings the list back).
        self.summary = SummaryView()
        self.summary.backClicked.connect(self.show_list)
        self.summary.addBlockClicked.connect(lambda index: self.addBlockClicked.emit(self.showing_summary(), index))
        self.summary.keepClicked.connect(lambda index: self.keepNoteClicked.emit(self.showing_summary(), index))
        self.summary.dismissClicked.connect(lambda index: self.dismissNoteClicked.emit(self.showing_summary(), index))
        self.summary.linkClicked.connect(self.summaryLinkClicked)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.scroll)
        self.pages.addWidget(self.summary)
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(12)
        column.addLayout(head_row)
        column.addWidget(self.pages, 1)
        holder = QWidget()
        holder.setLayout(column)
        holder.setMaximumWidth(COLUMN_MAX + 120)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(holo.GUTTER_SIDE + 8, holo.GUTTER_TOP, holo.GUTTER_SIDE, 16)
        layout.addWidget(holder, 1000)
        layout.addStretch(1)

    # ---- API ---------------------------------------------------------------------------------

    def set_meetings(self, infos: Sequence[MeetingRowInfo]) -> None:
        asking = {row.info.id for row in self._rows if row.asking()}
        for row in self._rows:
            self._list.removeWidget(row)
            row.hide()
            row.deleteLater()
        self._rows = []
        for index, info in enumerate(infos):
            row = MeetingRow(info)
            row.deleteClicked.connect(self.deleteClicked)
            row.retryClicked.connect(self.retryClicked)
            row.stopClicked.connect(self.stopClicked)
            row.openClicked.connect(self.openSummaryClicked)
            row.summariseClicked.connect(self.summariseClicked)
            if info.id in asking and info.status != mt.STATUS_RECORDING:
                row._ask()
            self._list.insertWidget(_TOP_WIDGETS + index, row)
            self._rows.append(row)
        self.empty_label.setVisible(not infos)

    def rows(self) -> list[MeetingRow]:
        return list(self._rows)

    def row(self, meeting_id: str) -> MeetingRow | None:
        return next((row for row in self._rows if row.info.id == meeting_id), None)

    def set_missing(self, texts: Sequence[str], *, model_missing: bool, downloading: bool = False,
                    download_text: str = "") -> None:
        """Exactly what is missing (one line each); the Download button while the model is missing."""
        self.missing_label.setText("\n".join(texts))
        self.missing_panel.setVisible(bool(texts) or downloading)
        self.download_button.setVisible(model_missing and not downloading)
        self.cancel_download_button.setVisible(downloading)
        from .meeting_speech import DOWNLOAD_TEXT

        self.download_label.setText(download_text or (DOWNLOAD_TEXT if model_missing else ""))
        self.download_label.setVisible(model_missing or downloading)

    def missing_text(self) -> str:
        return self.missing_label.text() if self.missing_panel.isVisibleTo(self) else ""

    def set_recording(self, recording: bool) -> None:
        """While a meeting records, Record a meeting gives way (one at a time) and its card closes."""
        self._recording = bool(recording)
        self.record_button.setEnabled(not recording)
        if recording:
            self.card.close_card()

    # ---- part 2: a meeting's summary ------------------------------------------------------------------

    def show_summary(self, info: SummaryInfo) -> None:
        """The summary of one meeting in the list's place."""
        self.summary.set_summary(info)
        if self.pages.currentWidget() is not self.summary:
            self.pages.setCurrentWidget(self.summary)
        self.pageChanged.emit()

    def show_list(self) -> None:
        """Back to the list (All meetings, Esc); the summary's lent cards go back to NEEDS YOUR OK."""
        if self.pages.currentWidget() is self.list_page():
            return
        self.pages.setCurrentWidget(self.list_page())
        self.pageChanged.emit()

    def list_page(self) -> QWidget:
        return self.scroll

    def showing_summary(self) -> str:
        """The meeting whose summary shows ("" while the list shows)."""
        info = self.summary.info()
        return info.meeting_id if info is not None and self.pages.currentWidget() is self.summary else ""


# --------------------------------------------------------------------------
# Settings > Meetings
# --------------------------------------------------------------------------

class _DeviceRow(QWidget):
    picked = Signal(str)

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name_label = body_label(label, "body_sm", holo.INK_3, wrap=False)
        self.value_label = body_label("", "body", holo.INK_1, name="meetingsDevice")
        self.change_button = holo.HoloButton("Change", holo.GHOST, size=holo.SIZE_SM)
        self.change_button.setAccessibleName(f"Change the {label.lower()}")
        self.change_button.clicked.connect(self._menu)
        self._choices: list[str] = []
        self._picked = ""
        self.menu: QMenu | None = None
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        row.addWidget(self.name_label)
        row.addWidget(self.value_label, 1)
        row.addWidget(self.change_button)

    def set_state(self, picked: str, default: str, choices: Sequence[str]) -> None:
        self._picked, self._choices = picked, list(choices)
        if picked:
            missing = "" if picked in choices or not choices else " - not connected now: the default is used"
            self.value_label.setText(f"{picked}{missing}")
        else:
            self.value_label.setText(f"Follows the Windows default ({default or 'none found'})")

    def menu_items(self) -> list[str]:
        return [FOLLOW_DEFAULT, *self._choices]

    def choose(self, text: str) -> None:
        self.picked.emit("" if text == FOLLOW_DEFAULT else text)

    def _menu(self) -> None:
        menu = QMenu(self)
        for text in self.menu_items():
            action = menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(text == (self._picked or FOLLOW_DEFAULT))
            action.triggered.connect(lambda _checked=False, t=text: self.choose(t))
        self.menu = menu
        menu.popup(self.change_button.mapToGlobal(self.change_button.rect().bottomLeft()))


class MeetingsSettingsPanel(QWidget):
    """Settings > Meetings: the devices and the speech model (see the module docstring)."""

    microphonePicked = Signal(str)
    callAudioPicked = Signal(str)
    downloadClicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mic_row = _DeviceRow("Microphone (You)")
        self.mic_row.picked.connect(self.microphonePicked)
        self.call_row = _DeviceRow("Call audio (Others)")
        self.call_row.picked.connect(self.callAudioPicked)
        self.model_label = body_label("", "body_sm", holo.INK_2, name="meetingsModel")
        self.download_button = holo.HoloButton(DOWNLOAD_BUTTON, holo.GHOST, size=holo.SIZE_SM)
        self.download_button.clicked.connect(self.downloadClicked)
        model = QHBoxLayout()
        model.setContentsMargins(0, 0, 0, 0)
        model.setSpacing(12)
        model.addWidget(self.model_label, 1)
        model.addWidget(self.download_button)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        column.addWidget(self.mic_row)
        column.addWidget(self.call_row)
        column.addWidget(Rule())
        column.addLayout(model)

    def set_devices(self, mic: tuple[str, str, Sequence[str]], call: tuple[str, str, Sequence[str]]) -> None:
        """(picked, default name, choices) for each."""
        self.mic_row.set_state(*mic)
        self.call_row.set_state(*call)

    def set_model(self, text: str, *, can_download: bool) -> None:
        self.model_label.setText(text)
        self.download_button.setVisible(can_download)
