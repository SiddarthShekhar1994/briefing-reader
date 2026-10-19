"""Meeting recording, part 1: what the owner sees (hologram look, British spelling; amber only when a decision
waits - the start card - and coral for the recording itself).

Nothing here records, reads or downloads anything: every widget shows what the app hands it and every click only
emits a signal. The app (meetings_ui.MeetingsController) starts a recording only from the start card's Start, and
the card builds that request only while its consent tick is ticked.

Public API
==========
``ConsentTick``: the unticked box "Everyone in this meeting has agreed to be recorded" (the NEW RECIPIENT tick's
    look). ``StartCard``: ``open_card(info, mic, call, problem="")`` always opens UNTICKED (never remembered); Start
    stays disabled until the tick; ``startClicked(StartRequest)``, ``cancelled``; ``device_texts()``.
``RecBadge(home=False)``: the alert dot, "REC 12:04" and Stop (the header on every screen; Home's larger one).
    ``set_elapsed(text)``; ``stopClicked``.
``RecPill``: Jarvis's own always-on-top pill "Recording 12:04" with Stop; a tool window that never takes the focus
    (the toast's flags) and stays while Jarvis is minimised or hidden. ``show_recording(text)``, ``set_elapsed``,
    ``dismiss()``, ``text()``; ``stopClicked``.
``MeetingRowInfo`` / ``MeetingsStage``: the Meetings screen - Record a meeting, what is missing (exact add-ons and
    the model, with Download), the start card, then the meetings (date, title, length, status: Recording /
    Writing it down 40% / Transcript ready / Failed + Retry) with Delete (asked twice: audio, transcript and
    summary go at once). Signals ``recordClicked``, ``stopClicked``, ``deleteClicked(id)``, ``retryClicked(id)``,
    ``downloadClicked``, ``cancelDownloadClicked``, ``startRequested(StartRequest)``.
``MeetingsSettingsPanel``: Settings > Meetings - the microphone and the call-audio device (Follow the Windows
    default, or one picked from a menu), the speech model with Download; ``microphonePicked(str)``,
    ``callAudioPicked(str)`` ("" = follow the default), ``downloadClicked``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QHBoxLayout, QMenu, QScrollArea, QSizePolicy, QVBoxLayout,
                               QWidget)

from . import holo, hud
from . import meetings as mt
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
DOWNLOAD_BUTTON = "Download"
CANCEL_DOWNLOAD = "Stop the download"
CARD_KICKER = "RECORD A MEETING"
CARD_NOTE = ("Jarvis records only after you click Start. The audio stays on this PC: kept 7 days, the transcript "
             "30 days.")
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
    """The consent box: unticked until the owner ticks it (a click, or Space with the focus on it)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(False)
        self.setText(mt.CONSENT_TEXT)
        self.setAccessibleName(mt.CONSENT_TEXT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.toggled.connect(lambda _on: self.update())

    def _lines(self, width: float) -> list[str]:
        from .offer_view import wrap_lines

        return wrap_lines(self.text(), holo.font("body"), max(80.0, width - holo.TICK_BOX - 12), 3)

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


class StartCard(QWidget):
    """The start card (amber: a decision waits). See the module docstring."""

    startClicked = Signal(object)        # a meetings.StartRequest (only while ticked)
    cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._info = mt.MeetingInfo()
        self._devices = ("", "")
        self._problem = ""
        self._now = datetime.now
        self.kicker = body_label(holo.role_text("label", CARD_KICKER), "label", holo.AMBER, wrap=False)
        self.title_label = body_label("", "tile_title", holo.INK_1, name="meetingCardTitle")
        self.meta_label = body_label("", "body_sm", holo.INK_3)
        self.mic_label = body_label("", "body_sm", holo.INK_2, name="meetingCardMic")
        self.call_label = body_label("", "body_sm", holo.INK_2, name="meetingCardCall")
        self.tick = ConsentTick()
        self.tick.toggled.connect(self._sync)
        self.note_label = body_label(CARD_NOTE, "body_sm", holo.INK_3)
        self.problem_label = body_label("", "body_sm", holo.ALERT_TX, name="meetingCardProblem")
        self.problem_label.hide()
        self.start_button = holo.HoloButton(START_TEXT, holo.AMBER_COMMIT)
        self.start_button.clicked.connect(self._on_start)
        self.cancel_button = holo.HoloButton(CANCEL_TEXT, holo.GHOST)
        self.cancel_button.clicked.connect(self._on_cancel)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(holo.BUTTON_GAP)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        column = QVBoxLayout(self)
        column.setContentsMargins(20, 16, 20, 16)
        column.setSpacing(6)
        for widget in (self.kicker, self.title_label, self.meta_label):
            column.addWidget(widget)
        column.addSpacing(6)
        column.addWidget(self.mic_label)
        column.addWidget(self.call_label)
        column.addSpacing(8)
        column.addWidget(self.tick)
        column.addWidget(self.note_label)
        column.addWidget(self.problem_label)
        column.addSpacing(6)
        column.addLayout(buttons)
        self.setMaximumWidth(COLUMN_MAX)
        self.hide()
        self._sync()

    def set_clock(self, now: Any) -> None:
        self._now = now

    def open_card(self, info: mt.MeetingInfo | None, mic: tuple[str, bool], call: tuple[str, bool], *,
                  problem: str = "") -> None:
        """Show the card for ``info`` (None: an unscheduled call) with the two devices it will use. The consent
        tick is ALWAYS unticked here: it is never remembered from an earlier meeting."""
        self._info = info or mt.MeetingInfo()
        self._devices = (mic[0], call[0])
        self._problem = problem
        self.tick.setChecked(False)
        self.title_label.setText(self._info.shown_title())
        meta = []
        if self._info.start is not None:
            meta.append(f"{_clock(self._info.start)} to {_clock(self._info.end)}" if self._info.end
                        else _clock(self._info.start))
            meta.append("stops by itself 15 minutes after the end")
        if self._info.guests:
            meta.append(f"with {mt.merge_names(self._info.guests)}")
        self.meta_label.setText(" \u00b7 ".join(meta) if meta else "Not on your calendar; stop it yourself.")
        self.mic_label.setText(f"You - microphone: {device_line(*mic)}")
        self.call_label.setText(f"Others - call audio: {device_line(*call)}")
        self.problem_label.setText(problem)
        self.problem_label.setVisible(bool(problem))
        self._sync()
        self.show()

    def info(self) -> mt.MeetingInfo:
        return self._info

    def device_texts(self) -> tuple[str, str]:
        return self.mic_label.text(), self.call_label.text()

    def close_card(self) -> None:
        self.tick.setChecked(False)
        self.hide()

    def _sync(self, *_args: Any) -> None:
        ready = self.tick.isChecked() and not self._problem
        self.start_button.setEnabled(ready)
        self.start_button.setAccessibleDescription("" if ready else "Tick the consent box first")

    def _on_start(self) -> None:
        if not self.isVisible() or not self.tick.isChecked() or self._problem:
            return
        request = mt.StartRequest(self._info, True, self._now(), self._devices[0], self._devices[1])
        self.close_card()                     # the tick goes back to unticked: never remembered
        self.startClicked.emit(request)

    def _on_cancel(self) -> None:
        self.close_card()
        self.cancelled.emit()

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
        self.stop_button = holo.HoloButton(STOP_TEXT, holo.DANGER_GHOST, size=holo.SIZE_SM)
        self.stop_button.setAccessibleName("Stop recording")
        self.stop_button.clicked.connect(self.stopClicked)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self.mark, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.stop_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setAccessibleName("Recording")
        self.hide()

    def set_elapsed(self, text: str) -> None:
        self.mark.set_elapsed(text)
        self.setAccessibleDescription(f"Jarvis is recording this meeting: {text}")

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
        self.stop_button = holo.HoloButton(STOP_TEXT, holo.DANGER_GHOST, size=holo.SIZE_SM, parent=self)
        self.stop_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)       # a click never moves the focus here
        self.stop_button.setAccessibleName("Stop recording")
        self.stop_button.clicked.connect(self.stopClicked)
        self.setAccessibleName("Jarvis is recording")
        self._place_children()

    def text(self) -> str:
        return recording_text(self._elapsed)

    def show_recording(self, elapsed: str) -> None:
        """Show (or keep showing) the pill, top centre of the primary screen's work area, never the focus."""
        self.set_elapsed(elapsed)
        self.place()
        handle = self.windowHandle()
        if handle is not None:
            handle.setProperty("_q_showWithoutActivating", True)
        if not self.isVisible():
            self.show()

    def set_elapsed(self, elapsed: str) -> None:
        if elapsed != self._elapsed:
            self._elapsed = elapsed
            self.setAccessibleDescription(self.text())
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
            painter.drawText(QRectF(left, 0, right - left, self.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.text())
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


def row_info(record: mt.MeetingRecord, *, elapsed: str = "") -> MeetingRowInfo:
    """A meeting record as the list shows it (``elapsed`` for the one recording now)."""
    started = record.started_at
    when = f"{started:%a %d %b} {_clock(started)}" if started is not None else ""
    tone = {mt.STATUS_RECORDING: TONE_RECORDING, mt.STATUS_TRANSCRIBING: TONE_WORKING,
            mt.STATUS_TRANSCRIBED: TONE_READY, mt.STATUS_SUMMARY_READY: TONE_READY,
            mt.STATUS_FAILED: TONE_FAILED}.get(record.status, TONE_QUIET)
    length = elapsed if record.status == mt.STATUS_RECORDING else mt.length_text(record.duration_s)
    note = mt.STOP_WORDS.get(record.stop_reason, "") if record.status != mt.STATUS_RECORDING else ""
    if record.device_changes:
        note = (note + " \u00b7 " if note else "") + "the call audio switched device during the meeting"
    return MeetingRowInfo(record.id, when, record.shown_title(), length, record.status, record.status_text(), tone,
                          note)


class MeetingRow(QWidget):
    """One meeting: date, title, length, status (and Retry when it failed), Delete (asked twice)."""

    deleteClicked = Signal(str)
    retryClicked = Signal(str)
    stopClicked = Signal()

    def __init__(self, info: MeetingRowInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.info = info
        self.when_label = body_label(holo.role_text("label", info.when), "label", holo.INK_KICKER, wrap=False)
        self.title_label = body_label(info.title, "tile_title", holo.INK_1, name="meetingTitle")
        self.length_label = body_label(info.length, "readout", holo.INK_3, wrap=False)
        self.status_label = body_label(info.status_text, "body_sm", _TONE_COLORS.get(info.tone, holo.INK_3),
                                       wrap=False, name="meetingStatus")
        self.note_label = body_label(info.note, "body_sm", holo.INK_4)
        self.note_label.setVisible(bool(info.note))
        self.retry_button = holo.HoloButton(RETRY_TEXT, holo.GHOST, size=holo.SIZE_SM)
        self.retry_button.clicked.connect(lambda: self.retryClicked.emit(self.info.id))
        self.retry_button.setVisible(info.status == mt.STATUS_FAILED)
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
        for button in (self.retry_button, self.stop_button, self.delete_button):
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

    def _ask(self) -> None:
        self._ask_box.show()
        self.delete_button.hide()

    def _keep(self) -> None:
        self._ask_box.hide()
        self.delete_button.show()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            state = holo.TILE_ERROR if self.info.status == mt.STATUS_FAILED else holo.TILE_DEFAULT
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
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(12)
        column.addLayout(head_row)
        column.addWidget(self.scroll, 1)
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
