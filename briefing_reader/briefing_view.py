"""The Briefing screen in the hologram look (HOLOGRAM_SPEC 6, 13, 14, 16.1; round 2).

Public API
==========

``BriefingStage``
    The screen inside its four 18 px stage brackets, in one of these phases (``set_phase``):

    * ``READY`` (and ``LOADING`` / ``FAILED``): the focal point is the Play button (``play_button``, a 56 px
      cyan go button with a faint ring) under the compact orb (``orb``, an ``orb.OrbWidget`` at 120 px).
      Above it the kicker (``AM BRIEFING \u00b7 UPDATED 10:04 AM`` and a cyan ``NEW`` badge while unseen:
      ``set_kicker``, ``set_new``), Jarvis's line in greeting type with its cyan glow (``Your AM briefing is
      ready, sir.``; while loading or after a failure the message instead, ``set_message``) and the readout
      ``6 SECTIONS \u00b7 ABOUT 4 MIN`` (``set_summary``). One amber text link while proposals wait
      (``approvals_link``, ``set_proposals``).
    * ``PLAYING`` / ``PAUSED`` / ``ENDED``: the reading lens - the compact orb at the left (speaking while
      it plays), the label ``02 / 06 \u00b7 URGENT``, the previous line in ink-4, the line being read in lens
      type (Sora 22, ink-1) with a 2 px cyan bar and its 6 px underlay at its left (``speech``, a
      hud.SpeechLabel: ``set_line(text, previous)``); then the section track (``track``: one 4 px segment per
      section, played cyan 40 %, current cyan, upcoming ``line``, skipped dashed steel with ``NOT READ``;
      a click on a segment emits ``sectionClicked(index)``; the ``sections_button`` opens the list) and the
      transport row: Read everything (ghost), Pause / Play / Replay (56 px go), Skip section and Open in
      Notion (40 px icons). ``ENDED``: the label ``END \u00b7 BRIEFING READ`` and "That's the end of your
      briefing."

Only the orb moves, and only while it speaks. Nothing here plays or opens anything by itself: the buttons
are the reading view's (its signals reach the app).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import shiboken6
from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QAbstractButton, QSizePolicy, QWidget

from . import holo, hud
from . import home as home_mod
from . import orb as orb_mod

READY = "ready"
LOADING = "loading"
FAILED = "failed"
PLAYING = "playing"
PAUSED = "paused"
ENDED = "ended"
PHASES = (READY, LOADING, FAILED, PLAYING, PAUSED, ENDED)
END_LINE = "That's the end of your briefing."
END_LABEL = "BRIEFING READ"
NOT_READ = "NOT READ"
SECTIONS_TEXT = "Sections"
STAGE_BRACKET = 18
WORDS_PER_MINUTE = 150
LENS_MAX = 660
# The lens's words while a line is read (HOLOGRAM_SPEC 14.3, Briefing.dc.html): said, being said, to come.
WORD_COLORS = (holo.INK_1, holo.HOLO, holo.INK_3)
ROW_MAX = 860
TRACK_MAX = 820
# Transport glyphs by the primary button's text (the text stays its name: "Play", "Pause", ...).
_PRIMARY_ICONS = {"Pause": "pause", "Play": "play_lg", "Replay": "replay", "Retry": "replay"}


def greeting_for(title: str) -> str:
    """Jarvis's ready line: "Your AM briefing is ready, sir." (``title`` "AM briefing")."""
    words = (title or "").strip()
    if not words or words.casefold() == "briefing":
        return "Your briefing is ready, sir."
    return f"Your {words} is ready, sir."


def summary_text(sections: int, minutes: int) -> str:
    """The ready readout: "6 SECTIONS \u00b7 ABOUT 4 MIN" ("" without sections)."""
    if sections <= 0:
        return ""
    parts = [f"{sections} SECTION{'S' if sections != 1 else ''}"]
    if minutes > 0:
        parts.append(f"ABOUT {minutes} MIN")
    return f" {hud.MIDDLE_DOT} ".join(parts)


def estimate_minutes(words: int) -> int:
    """How long the voice takes for ``words`` words, in whole minutes (at least 1 when there are words)."""
    return max(1, math.ceil(words / WORDS_PER_MINUTE)) if words > 0 else 0


def lens_label(position: int, total: int, name: str) -> tuple[str, str]:
    """The lens's label as (number, name): ("02 / 06", "URGENT")."""
    width = max(2, len(str(total)))
    return f"{position:0{width}d} / {total:0{width}d}", name.upper()


class TransportButton(holo.HoloButton):
    """A Briefing transport button: the 56 px go button (its glyph follows its text: Play, Pause, Replay,
    Retry) or a 40 px icon button (Skip section, Open in Notion). The text stays its name (tooltip,
    screen readers)."""

    def __init__(self, text: str, variant: str, *, size: int = holo.SIZE_MD, icon: str | None = None,
                 diameter: int | None = None, parent: QWidget | None = None) -> None:
        super().__init__(text, variant, size=size, icon=icon, diameter=diameter, parent=parent)
        self.setAccessibleName(text)

    def glyph(self) -> str:
        return _PRIMARY_ICONS.get(self.text(), self._icon or "play_lg") if self._size == holo.SIZE_LG \
            else (self._icon or "")

    def _paint_content(self, painter: QPainter, rect: QRectF, label: QColor) -> None:
        name = self.glyph()
        if not name:
            return
        side = 22 if self._size == holo.SIZE_LG else 18
        box = QRectF(0, 0, side, side)
        box.moveCenter(rect.center())
        holo.paint_icon(painter, box, name, label, stroke=2.0)


class SectionsButton(holo.HoloButton):
    """``Sections`` (ghost, small, with a glyph): opens the section list. Checkable while the list shows;
    ``set_meta`` keeps the count for screen readers."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(SECTIONS_TEXT, holo.GHOST, size=holo.SIZE_SM, icon="read_all", parent=parent)
        self.setCheckable(True)
        self._meta = ""

    def set_meta(self, text: str) -> None:
        self._meta = text or ""
        self.setAccessibleName(f"{SECTIONS_TEXT}, {self._meta}" if self._meta else SECTIONS_TEXT)

    def meta(self) -> str:
        return self._meta


class SectionTrack(QWidget):
    """The slim segmented progress track: one segment per section (HOLOGRAM_SPEC 16.1)."""

    sectionClicked = Signal(int)
    BAR_Y = 10
    BAR_H = 4
    GAP = 4
    HEIGHT = 64

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._rows: tuple[hud.SectionRowInfo, ...] = ()
        self._hover = -1
        self.setAccessibleName("Briefing sections")

    def set_rows(self, rows: Sequence[hud.SectionRowInfo]) -> None:
        self._rows = tuple(rows)
        current = next((i for i, row in enumerate(self._rows) if row.state == hud.ROW_CURRENT), -1)
        if current >= 0:
            self.setAccessibleDescription(f"{self._rows[current].title}, now playing")
        self.update()

    def rows(self) -> tuple[hud.SectionRowInfo, ...]:
        return self._rows

    def segment_rect(self, index: int) -> QRectF:
        count = max(1, len(self._rows))
        width = (self.width() - (count - 1) * self.GAP) / count
        return QRectF(index * (width + self.GAP), 0, width, self.height())

    def segment_at(self, point: QPointF | QPoint) -> int:
        x = QPointF(point).x()
        for index in range(len(self._rows)):
            if self.segment_rect(index).left() <= x <= self.segment_rect(index).right():
                return index
        return -1

    def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        index = self.segment_at(event.position())
        clickable = 0 <= index < len(self._rows) and self._rows[index].clickable
        self.setCursor(Qt.CursorShape.PointingHandCursor if clickable else Qt.CursorShape.ArrowCursor)
        if index != self._hover:
            self._hover = index
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.segment_at(event.position())
            if 0 <= index < len(self._rows) and self._rows[index].clickable:
                self.sectionClicked.emit(index)
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            label_font = hud.mono_font(11, 500, 0.16)
            for index, row in enumerate(self._rows):
                rect = self.segment_rect(index)
                bar = QRectF(rect.left(), self.BAR_Y, rect.width(), self.BAR_H)
                state = row.state
                if state == hud.ROW_IGNORED:
                    pen = QPen(QColor(holo.STEEL), self.BAR_H)
                    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
                    pen.setDashPattern([6 / self.BAR_H, 4 / self.BAR_H])
                    painter.setPen(pen)
                    y = bar.center().y()
                    painter.drawLine(QPointF(bar.left(), y), QPointF(bar.right(), y))
                else:
                    color = {hud.ROW_PLAYED: holo.alpha(holo.HOLO, 0.4), hud.ROW_CURRENT: QColor(holo.HOLO)}.get(
                        state, holo.LINE)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(color)
                    painter.drawRoundedRect(bar, 2, 2)
                ink = {hud.ROW_CURRENT: holo.HOLO, hud.ROW_PLAYED: holo.INK_3}.get(state, holo.INK_4)
                if index == self._hover and row.clickable:
                    ink = holo.INK_1
                painter.setFont(label_font)
                painter.setPen(QColor(ink))
                text = row.title.upper()
                text_rect = QRectF(rect.left(), self.BAR_Y + self.BAR_H + 8, rect.width(), 14)
                painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                                 QFontMetricsF(label_font).elidedText(text, Qt.TextElideMode.ElideRight,
                                                                      max(1.0, rect.width())))
                if state == hud.ROW_IGNORED and rect.width() >= holo.status_badge_width(NOT_READ):
                    holo.paint_status_badge(painter, QPointF(rect.left(), text_rect.bottom() + 5 + 10), NOT_READ,
                                            holo.TONE_STEEL)
        finally:
            painter.end()


class BriefingStage(QWidget):
    """The Briefing screen; see the module docstring."""

    approvalsClicked = Signal()

    def __init__(self, speech: hud.SpeechLabel, play_button: TransportButton, skip_button: TransportButton,
                 everything_button: holo.HoloButton, notion_button: TransportButton,
                 sections_button: SectionsButton, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._phase = LOADING
        self._kicker = ""
        self._new = False
        self._title = ""
        self._message = ("", False)
        self._summary = ""
        self._status = ("", False)
        self._label = ("", "")
        self._previous = ""
        self._proposals = 0
        self._stale = ""
        self._word_ms = 0                 # the pace of the line being read (0: no line is being read)
        self._word_timer = QTimer(self)
        self._word_timer.timeout.connect(self._next_word)
        self.orb = orb_mod.OrbWidget(orb_mod.COMPACT, self)
        self.speech = speech
        self.speech.setParent(self)
        self.play_button = play_button
        self.skip_button = skip_button
        self.everything_button = everything_button
        self.notion_button = notion_button
        self.sections_button = sections_button
        for widget in (play_button, skip_button, everything_button, notion_button, sections_button):
            widget.setParent(self)
        self.track = SectionTrack(self)
        self.approvals_link = holo.HoloButton("", holo.LINK, parent=self)
        self.approvals_link.clicked.connect(self.approvalsClicked)
        self.approvals_link.hide()
        self._place_pending = False
        self.set_phase(LOADING)

    # ---- content ---------------------------------------------------------------------------

    def set_kicker(self, title: str, subtitle: str) -> None:
        """``AM BRIEFING \u00b7 UPDATED 10:04 AM``: the title and the "Updated ..." line."""
        self._title = title or ""
        parts = [part for part in (title, subtitle) if part]
        self._kicker = f" {hud.MIDDLE_DOT} ".join(parts).upper()
        self.update()

    def kicker(self) -> str:
        return self._kicker

    def set_new(self, new: bool) -> None:
        """The cyan NEW badge after the kicker (a fresh briefing nobody has seen; cyan, never amber)."""
        self._new = bool(new)
        self.update()

    def is_new(self) -> bool:
        return self._new

    def greeting(self) -> str:
        """The ready line, or the message while loading / after a failure."""
        if self._phase in (LOADING, FAILED) and self._message[0]:
            return self._message[0]
        return greeting_for(self._title)

    def set_message(self, text: str, error: bool = False) -> None:
        """While there is no briefing to play: the loading line or why it failed (in place of the greeting)."""
        self._message = (text or "", bool(error))
        self.update()

    def set_summary(self, text: str) -> None:
        self._summary = text or ""
        self.update()

    def summary(self) -> str:
        return self._summary

    def set_status(self, text: str, error: bool = False) -> None:
        """A short status under the readout ("Preparing audio...", a failure); "" for none."""
        self._status = (text or "", bool(error))
        if not self.reading():
            self._schedule_place()   # the ready view makes room for it
        self.update()

    def status(self) -> str:
        return self._status[0]

    def set_stale(self, text: str) -> None:
        """The briefing may be out of date ("May be stale - updated yesterday"; "" for none): it always
        shows, in the ready view and over the lens."""
        self._stale = text or ""
        self._schedule_place()
        self.update()

    def stale(self) -> str:
        return self._stale

    def notice(self) -> tuple[str, bool]:
        """The screen's one quiet line: the status (a failure in coral) and the stale warning."""
        text, error = self._status
        return (f" {hud.MIDDLE_DOT} ".join(part for part in (text, self._stale) if part), error)

    def set_proposals(self, count: int) -> None:
        """One amber text link while proposals wait ("2 proposals need your OK")."""
        self._proposals = max(0, int(count))
        if self._proposals:
            noun = "proposal needs" if self._proposals == 1 else "proposals need"
            self.approvals_link.setText(f"{self._proposals} {noun} your OK")
        self._sync_visibility()
        self._schedule_place()

    def set_rows(self, rows: Sequence[hud.SectionRowInfo], current: int | None) -> None:
        """The section track and the lens's label (``02 / 06 \u00b7 URGENT``)."""
        self.track.set_rows(rows)
        if current is not None and 0 <= current < len(rows):
            self._label = lens_label(current + 1, len(rows), rows[current].title)
        elif not rows:
            self._label = ("", "")
        self.update()

    def label(self) -> str:
        if self._phase == ENDED:
            return f"END {hud.MIDDLE_DOT} {END_LABEL}"
        number, name = self._label
        return f"{number} {hud.MIDDLE_DOT} {name}" if number else ""

    def set_line(self, text: str, previous: str) -> None:
        """The line being read (the lens) and the one before it in the same section ("" for none). Its
        words show in one colour until ``start_words`` paces them."""
        self._previous = previous or ""
        self._word_timer.stop()
        self._word_ms = 0
        self.speech.set_head(-1)
        self.speech.setText(text)
        self._schedule_place()
        self.update()

    def start_words(self, duration_ms: int = 0) -> None:
        """The line just set is being read: its words light up one by one, over ``duration_ms`` when the
        audio's length is known (else at the voice's estimated pace), while the briefing plays."""
        words = self.speech.word_count()
        if words <= 0:
            return
        low, high = home_mod.WORD_MS_RANGE
        if duration_ms > 0:
            self._word_ms = max(low, min(high, round(duration_ms / words)))
        else:
            self._word_ms = round(1000.0 / home_mod.SPOKEN_WPS)
        self.speech.set_head(0, WORD_COLORS)
        self._sync_words()

    def word_head(self) -> int:
        """The word being said in the lens (-1: none; the word count once the line is said)."""
        return self.speech.head()

    def _sync_words(self) -> None:
        reading = (self._phase == PLAYING and self._word_ms > 0
                   and 0 <= self.speech.head() < self.speech.word_count())
        if reading and not self._word_timer.isActive():
            self._word_timer.start(self._word_ms)
        elif not reading:
            self._word_timer.stop()

    def _next_word(self) -> None:
        head = self.speech.head() + 1
        self.speech.set_head(min(head, self.speech.word_count()))
        self._sync_words()

    def previous_line(self) -> str:
        return self._previous

    def set_phase(self, phase: str) -> None:
        self._phase = phase if phase in PHASES else READY
        if self._phase not in (PLAYING, PAUSED):   # ready, ended: the line in one colour
            self._word_ms = 0
            self.speech.set_head(-1)
        self._sync_words()
        self._sync_visibility()
        self._schedule_place()
        self.update()

    def phase(self) -> str:
        return self._phase

    def reading(self) -> bool:
        """The lens shows (playing, paused or ended)."""
        return self._phase in (PLAYING, PAUSED, ENDED)

    def _sync_visibility(self) -> None:
        reading = self.reading()
        for widget in (self.skip_button, self.everything_button, self.notion_button, self.sections_button,
                       self.track):
            widget.setVisible(reading)
        self.speech.setVisible(reading and self._phase != ENDED)
        self.approvals_link.setVisible(bool(self._proposals) and self._phase in (READY, ENDED))

    # ---- geometry --------------------------------------------------------------------------

    def _schedule_place(self) -> None:
        if not self._place_pending:
            self._place_pending = True
            QTimer.singleShot(0, self._place_later)

    def _place_later(self) -> None:
        self._place_pending = False
        if shiboken6.isValid(self):
            self._place()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._place()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._place()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(420, 360)

    def _move_orb(self, x: float, y: float) -> None:
        """The orb on whole multiples of 4 px in the window (whole device pixels at 125-175 %, as Home's)."""
        origin = self.mapTo(self.window(), QPoint(0, 0))

        def snap(local: float, base: int) -> int:
            return int(round((base + local) / 4.0)) * 4 - base

        self.orb.move(snap(x, origin.x()), snap(y, origin.y()))

    def _ready_layout(self) -> dict[str, QRectF]:
        """Where the ready view's parts go (a centred column): orb, kicker, greeting, readout, Play, link."""
        width, height = self.width(), self.height()
        side = self.orb.width()
        play = self.play_button.sizeHint()
        parts = [("orb", side, 0), ("kicker", 20, 30 - (side - orb_mod.COMPACT) / 2),
                 ("greeting", 48, 12), ("summary", 16, 8)]
        if self.notice()[0]:
            parts.append(("status", 20, 8))
        parts.append(("play", play.height(), 36 - holo.HoloButton.MARGIN))
        if self.approvals_link.isVisible():
            parts.append(("link", self.approvals_link.sizeHint().height(), 18))
        total = sum(h + gap for _name, h, gap in parts)
        y = max(8.0, (height - total) / 2 - 20)
        rects: dict[str, QRectF] = {}
        for name, h, gap in parts:
            y += gap
            rects[name] = QRectF(0, y, width, h)
            y += h
        return rects

    def _reading_layout(self) -> dict[str, QRectF]:
        """Where the reading view's parts go: the orb and the lens, the track, the transport row."""
        width, height = self.width(), self.height()
        pad = 24
        controls_h = self.play_button.sizeHint().height()
        controls_top = height - 16 - controls_h
        track_top = controls_top - 22 - SectionTrack.HEIGHT
        side = self.orb.width()
        row_w = min(ROW_MAX, width - 2 * pad)
        lens_w = min(LENS_MAX, row_w - side - 44)
        content_x = (width - (side + 44 + lens_w)) / 2
        line_h = holo.line_height("lens")
        lens_h = 14 + 16 + line_h + 10 + self._shown_lines(lens_w - 20) * line_h + 14 + 36
        notice = 20 + 12   # the quiet line's row over the lens (kept, so a status never moves the lens)
        top = max(pad + notice, notice + (track_top - 28 - notice - max(lens_h, side)) / 2 + 4)
        track_w = min(TRACK_MAX, width - 2 * pad)
        track_x = (width - track_w) / 2
        return {"orb": QRectF(content_x, top + (max(lens_h, side) - side) / 2, side, side),
                "lens": QRectF(content_x + side + 44, top + max(0.0, (side - lens_h) / 2), lens_w, lens_h),
                "track": QRectF(track_x, track_top, track_w, SectionTrack.HEIGHT),
                "controls": QRectF(0, controls_top, width, controls_h)}

    def _shown_lines(self, width: float) -> int:
        """How many lines the line being read takes in the lens (two at least, at most _lens_lines)."""
        text = self.speech.text()
        lines = len(_wrap_count(text, holo.font("lens"), width)) if text else 1
        return max(2, min(self._lens_lines(), lines))

    def _lens_lines(self) -> int:
        """How many lens lines the line being read may take here (2 to 4)."""
        room = (self.height() - 16 - 56 - 22 - SectionTrack.HEIGHT - 28 - 2 * 24 - 32
                - (14 + 16 + 33 + 10 + 14 + 36))
        return max(2, min(4, int(room // holo.line_height("lens"))))

    def _place(self) -> None:
        if self.reading():
            rects = self._reading_layout()
            orb = rects["orb"]
            self._move_orb(orb.left() - (self.orb.width() - orb.width()) / 2, orb.top())
            lens = rects["lens"]
            line_h = holo.line_height("lens")
            lines = self._lens_lines()
            if self.speech.width() != round(lens.width() - 20):
                self.speech.set_max_lines(lines)
            self.speech.setGeometry(round(lens.left() + 20), round(lens.top() + 14 + 16 + line_h + 10),
                                    round(lens.width() - 20), lines * line_h + 6)
            self.speech.set_max_lines(lines)
            track = rects["track"]
            sections = self.sections_button.sizeHint()
            self.sections_button.setGeometry(round(track.left()) - holo.HoloButton.MARGIN,
                                             round(track.top() + 2) - holo.HoloButton.MARGIN,
                                             sections.width(), sections.height())
            left = track.left() + sections.width() - 2 * holo.HoloButton.MARGIN + 16
            self.track.setGeometry(round(left), round(track.top()), max(10, round(track.right() - left)),
                                   SectionTrack.HEIGHT)
            controls = rects["controls"]
            play = self.play_button.sizeHint()
            cx = controls.center().x()
            self.play_button.setGeometry(round(cx - play.width() / 2), round(controls.top()), play.width(),
                                         play.height())
            gap = 20 - holo.HoloButton.MARGIN
            everything = self.everything_button.sizeHint()
            self.everything_button.setGeometry(round(cx - play.width() / 2 - gap - everything.width()),
                                               round(controls.center().y() - everything.height() / 2),
                                               everything.width(), everything.height())
            x = cx + play.width() / 2 + gap
            for button in (self.skip_button, self.notion_button):
                size = button.sizeHint()
                button.setGeometry(round(x), round(controls.center().y() - size.height() / 2), size.width(),
                                   size.height())
                x += size.width() + 8 - 2 * holo.HoloButton.MARGIN
            link = self.approvals_link.sizeHint()
            self.approvals_link.setGeometry(round(lens.left() + 20 - holo.HoloButton.MARGIN - 8),
                                            round(lens.bottom() - 36), link.width(), link.height())
        else:
            rects = self._ready_layout()
            orb = rects["orb"]
            self._move_orb((self.width() - self.orb.width()) / 2, orb.top())
            play = self.play_button.sizeHint()
            box = rects["play"]
            self.play_button.setGeometry(round((self.width() - play.width()) / 2), round(box.top()),
                                         play.width(), play.height())
            if "link" in rects:
                link = self.approvals_link.sizeHint()
                self.approvals_link.setGeometry(round((self.width() - link.width()) / 2), round(rects["link"].top()),
                                                link.width(), link.height())
        self.update()

    # ---- painting --------------------------------------------------------------------------

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            self._paint_brackets(painter)
            if self.reading():
                self._paint_lens(painter)
            else:
                self._paint_ready(painter)
        finally:
            painter.end()

    def _paint_brackets(self, painter: QPainter) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(holo.LINE_STRONG, 1.0))
        leg = STAGE_BRACKET
        for x, y, dx, dy in ((rect.left(), rect.top(), 1, 1), (rect.right(), rect.top(), -1, 1),
                             (rect.left(), rect.bottom(), 1, -1), (rect.right(), rect.bottom(), -1, -1)):
            painter.drawLine(QPointF(x, y), QPointF(x + dx * leg, y))
            painter.drawLine(QPointF(x, y), QPointF(x, y + dy * leg))
        painter.restore()

    def _paint_ready(self, painter: QPainter) -> None:
        rects = self._ready_layout()
        width = self.width()
        center = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        # The kicker and the NEW badge, centred together.
        label_font = holo.font("label")
        kicker = self._kicker
        badge = holo.status_badge_width("NEW") if self._new else 0.0
        text_w = QFontMetricsF(label_font).horizontalAdvance(kicker)
        total = text_w + (12 + badge if badge else 0)
        x = (width - total) / 2
        row = rects["kicker"]
        painter.setFont(label_font)
        painter.setPen(QColor(holo.INK_KICKER))
        painter.drawText(QRectF(x, row.top(), text_w + 2, row.height()),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), kicker)
        if badge:
            holo.paint_status_badge(painter, QPointF(x + text_w + 12, row.center().y()), "NEW", holo.TONE_CYAN)
        # Jarvis's line (greeting type) on its soft cyan glow; a failure's message in coral text.
        line = rects["greeting"]
        message, error = self._message if self._phase in (LOADING, FAILED) else ("", False)
        text = message or greeting_for(self._title)
        role = "greeting" if not message else "caption_sm"
        line_font = holo.font(role)
        line_w = min(width - 48, QFontMetricsF(line_font).horizontalAdvance(text) + 56)
        glow = QRadialGradient(QPointF(width / 2, line.center().y()), max(1.0, line_w * 0.7))
        glow.setColorAt(0.0, holo.alpha(holo.HOLO, 0.12))
        glow.setColorAt(1.0, holo.alpha(holo.HOLO, 0.0))
        painter.save()
        painter.translate(width / 2, line.center().y())
        painter.scale(1.0, max(0.2, line.height() * 1.4 / max(1.0, line_w * 1.4)))
        painter.translate(-width / 2, -line.center().y())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(QPointF(width / 2, line.center().y()), line_w * 0.7, line_w * 0.7)
        painter.restore()
        painter.setFont(line_font)
        painter.setPen(QColor(holo.ALERT_TX if error else (holo.INK_2 if message else holo.INK_1)))
        metrics = QFontMetricsF(line_font)
        painter.drawText(QRectF(24, line.top(), width - 48, line.height()), int(center),
                         metrics.elidedText(text, Qt.TextElideMode.ElideRight, width - 48))
        # The readout and the status.
        readout_font = holo.font("readout")
        painter.setFont(readout_font)
        painter.setPen(QColor(holo.INK_3))
        painter.drawText(rects["summary"], int(center), self._summary)
        if "status" in rects:
            self._paint_notice(painter, rects["status"])
        # The faint ring round Play (9 px outside it).
        play = QRectF(self.play_button.geometry()).adjusted(holo.HoloButton.MARGIN, holo.HoloButton.MARGIN,
                                                            -holo.HoloButton.MARGIN, -holo.HoloButton.MARGIN)
        painter.setPen(QPen(holo.alpha(holo.HOLO, 0.2), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(play.center(), play.width() / 2 + 9, play.height() / 2 + 9)

    def _paint_notice(self, painter: QPainter, rect: QRectF) -> None:
        """The quiet line (body-sm, ink-3; a failure in coral), centred in ``rect``."""
        text, error = self.notice()
        if not text:
            return
        font = holo.font("body_sm")
        painter.setFont(font)
        painter.setPen(QColor(holo.ALERT_TX if error else holo.INK_3))
        inner = rect.adjusted(24, 0, -24, 0)
        painter.drawText(inner, int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                         QFontMetricsF(font).elidedText(text, Qt.TextElideMode.ElideRight, max(1.0, inner.width())))

    def _paint_lens(self, painter: QPainter) -> None:
        rects = self._reading_layout()
        self._paint_notice(painter, QRectF(0, 24, self.width(), 20))
        lens = rects["lens"]
        line_h = holo.line_height("lens")
        x = lens.left() + 20
        label_font = holo.font("label")
        painter.setFont(label_font)
        if self._phase == ENDED:
            number, name = "END", END_LABEL
        else:
            number, name = self._label
        if number:
            metrics = QFontMetricsF(label_font)
            painter.setPen(QColor(holo.HOLO))
            painter.drawText(QRectF(x, lens.top(), lens.width(), 14),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), number)
            rest = f" {hud.MIDDLE_DOT} {name}"
            painter.setPen(QColor(holo.INK_KICKER))
            painter.drawText(QRectF(x + metrics.horizontalAdvance(number), lens.top(), lens.width(), 14),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), rest)
        lens_font = holo.font("lens")
        metrics = QFontMetricsF(lens_font)
        prev_top = lens.top() + 14 + 16
        painter.setFont(lens_font)
        previous = self.speech.text() if self._phase == ENDED else self._previous
        if previous:
            painter.setPen(QColor(holo.INK_4))
            painter.drawText(QRectF(x, prev_top, lens.width() - 20, line_h),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             metrics.elidedText(" ".join(previous.split()), Qt.TextElideMode.ElideRight,
                                                lens.width() - 20))
        current_top = prev_top + line_h + 10
        if self._phase == ENDED:
            current_h = float(line_h)
            painter.setPen(QColor(holo.INK_1))
            painter.drawText(QRectF(x, current_top, lens.width() - 20, line_h),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), END_LINE)
        else:
            shown = self.speech.shown_text()
            lines = max(1, len(_wrap_count(shown, lens_font, lens.width() - 20))) if shown else 1
            current_h = float(min(lines, self._lens_lines()) * line_h)
        # The 2 px cyan bar and its 6 px underlay at the line's left.
        bar = QRectF(lens.left() - 2, current_top + 3, 6, current_h - 6)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(holo.alpha(holo.HOLO, 0.15))
        painter.drawRoundedRect(bar, 3, 3)
        painter.setBrush(QColor(holo.HOLO))
        painter.drawRoundedRect(QRectF(lens.left(), current_top + 3, 2, current_h - 6), 1, 1)


def _wrap_count(text: str, font: Any, width: float) -> list[str]:
    """``text`` word-wrapped at ``width`` (only its line count matters: the bar's height)."""
    metrics = QFontMetricsF(font)
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split():
            trial = f"{current} {word}".strip()
            if metrics.horizontalAdvance(trial) <= width or not current:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines
