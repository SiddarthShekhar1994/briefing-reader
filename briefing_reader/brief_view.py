"""The meeting brief on Home in the hologram look (HOLOGRAM_SPEC 2, 5, 7, 9, 15, 16.5 moment 2; round 2, part 3a).

Public API
==========

``BriefPerson`` / ``BriefInfo``
    What the brief shows (memory only, never logged or spoken): the meeting's ``title``, ``when`` ("tomorrow
    2:00-3:00 PM") and account ``alias``; WHO'S COMING (``people``: each name, the response ``badge``
    ("ORGANISER", "ACCEPTED", "NO ANSWER YET"), its ``tone`` (cyan, or steel for no answer / declined) and the
    last thread line Jarvis wrote from the headers he read ('"Budget numbers", Oct 8' - the subject, written
    by someone else, stays in quotes - or why there is none: "no email in the last 14 days")); LAST THREADS,
    STILL OPEN, POINTS TO RAISE and FROM YOUR NOTES as lines; the ``note`` (why no mail was read); the
    reply cards' ``card_ids``, the ``steps_link`` ("live:<task>", "" without LIVE) and the follow-up ``nudges``
    (OWES YOU AN ANSWER: each line with its Draft a nudge text link - the Transcript's own link, which only
    drafts a card). No technical readout.

``BriefStage``
    Home's brief moment: a scroll area whose column (at most 640 wide, centred) holds the brief panel (the
    focused tile: ``tile-focus``, all four brackets), then the reply card(s) under it - the very approval
    cards of NEEDS YOUR OK, lent by the app while this shows (``hold(card)`` / ``release(card)`` /
    ``held()``), so Send, the NEW RECIPIENT tick on the card, Edit, Deny, Sent and its Undo work
    exactly as there - then See every step. ``set_brief(info)``; ``linkClicked(link_id)``;
    ``closeRequested()`` (the close button or Esc). It never sends, reads or saves anything itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import shiboken6
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from . import holo, hud

COLUMN_MAX = 640
KICKER = "PREP BRIEF"
WHO_TITLE = "WHO'S COMING"
THREADS_TITLE = "LAST THREADS"
OPEN_TITLE = "STILL OPEN"
POINTS_TITLE = "POINTS TO RAISE"
NOTES_TITLE = "FROM YOUR NOTES"
NUDGES_TITLE = "OWES YOU AN ANSWER"
NUDGE_TEXT = "Draft a nudge"
SEE_STEPS_TEXT = "See every step"
CLOSE_NAME = "Close the brief"
DOT = "\u00b7"
TONE_CYAN = holo.TONE_CYAN
TONE_STEEL = holo.TONE_STEEL
_QUIET = ("no answer", "declined")


@dataclass(frozen=True)
class BriefPerson:
    name: str = field(repr=False)
    badge: str = field(repr=False)
    tone: str = TONE_CYAN
    line: str = field(default="", repr=False)
    quiet: bool = False                   # no thread line of his or hers: the line is Jarvis's why

    def __repr__(self) -> str:
        return f"BriefPerson({self.tone})"


@dataclass(frozen=True)
class BriefInfo:
    title: str = field(repr=False)
    when: str = field(default="", repr=False)
    alias: str = ""
    people: tuple[BriefPerson, ...] = ()
    threads: tuple[str, ...] = field(default=(), repr=False)
    open: tuple[str, ...] = field(default=(), repr=False)
    points: tuple[str, ...] = field(default=(), repr=False)
    notes: tuple[str, ...] = field(default=(), repr=False)
    note: str = field(default="", repr=False)
    card_ids: tuple[str, ...] = ()
    steps_link: str = ""
    nudges: tuple[tuple[str, str], ...] = field(default=(), repr=False)    # (text, its Draft a nudge link id)

    def __repr__(self) -> str:
        return (f"BriefInfo({len(self.people)} people, {len(self.threads)} thread(s), {len(self.open)} open, "
                f"{len(self.points)} point(s), {len(self.nudges)} nudge(s), {len(self.card_ids)} card(s))")


def badge_of(words: str) -> tuple[str, str]:
    """(badge, tone) from Jarvis's response words ("organizer, accepted" -> "ORGANISER", cyan: British
    spelling on screen; "no answer yet" -> steel)."""
    text = " ".join(str(words or "").split())
    first = "organiser" if text.startswith("organizer") else text
    tone = TONE_STEEL if any(word in text.casefold() for word in _QUIET) else TONE_CYAN
    return first.upper(), tone


def initials(name: str) -> str:
    parts = [part for part in str(name or "").replace("@", " ").replace(".", " ").split() if part[:1].isalnum()]
    if not parts:
        return "?"
    return (parts[0][:1] + (parts[-1][:1] if len(parts) > 1 else "")).upper()


def _label(text: str, role: str, color: str, *, wrap: bool = True, selectable: bool = False) -> QLabel:
    label = hud.make_label(text, holo.font(role), color, wrap=wrap)
    label.setTextFormat(Qt.TextFormat.PlainText)
    if selectable:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setCursor(Qt.CursorShape.IBeamCursor)
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    return label


class _Ring(QWidget):
    """A 44 px initial ring (decoration: the row says the name)."""

    def __init__(self, text: str, quiet: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text, self._quiet = text, quiet
        self.setFixedSize(44, 44)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            color = holo.STEEL if self._quiet else holo.HOLO
            painter.setPen(QPen(holo.alpha(color, 0.55), 1.5))
            painter.setBrush(QColor(holo.TILE_FOCUS))
            painter.drawEllipse(QRectF(self.rect()).adjusted(1, 1, -1, -1))
            font = holo.font("readout")
            painter.setFont(font)
            painter.setPen(QColor(holo.INK_3 if self._quiet else holo.HOLO))
            painter.drawText(QRectF(self.rect()), int(Qt.AlignmentFlag.AlignCenter), self._text)
        finally:
            painter.end()


class _NameLine(QWidget):
    """The name (Chakra 15) and the response badge, painted (elided, never cut)."""

    def __init__(self, name: str, badge: str, tone: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._name, self._badge, self._tone = name, badge, tone
        self.setFixedHeight(24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(260, 24)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(80, 24)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            font = holo.font("button")
            font.setPixelSize(15)
            badge_w = holo.status_badge_width(self._badge) if self._badge else 0.0
            metrics = QFontMetricsF(font)
            room = max(1.0, self.width() - badge_w - 10)
            name = metrics.elidedText(self._name, Qt.TextElideMode.ElideRight, room)
            painter.setFont(font)
            painter.setPen(QColor(holo.INK_1))
            width = metrics.horizontalAdvance(name)
            painter.drawText(QRectF(0, 0, width + 2, self.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), name)
            if self._badge:
                holo.paint_status_badge(painter, QPointF(width + 10, self.height() / 2), self._badge, self._tone)
        finally:
            painter.end()


class PersonRow(QWidget):
    """One guest: the initial ring, the name and badge, the last thread line."""

    def __init__(self, person: BriefPerson, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.person = person
        steel = person.tone == TONE_STEEL
        self.ring = _Ring(initials(person.name), steel)
        self.name_line = _NameLine(person.name, person.badge, person.tone)
        self.line_label = _label(person.line, "body", holo.INK_4 if person.quiet else holo.INK_2, selectable=True)
        self.line_label.setVisible(bool(person.line))
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        column.addWidget(self.name_line)
        column.addWidget(self.line_label)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        row.addWidget(self.ring, 0, Qt.AlignmentFlag.AlignTop)
        row.addLayout(column, 1)
        self.setAccessibleName(f"{person.name}, {person.badge.lower()}" + (f": {person.line}" if person.line else ""))


class BriefPanel(QWidget):
    """The brief's focused tile (painted behind its labels)."""

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            holo.paint_tile(painter, QRectF(self.rect()), holo.TILE_FOCUSED)
        finally:
            painter.end()


class BriefStage(QScrollArea):
    """Home's brief moment; see the module docstring."""

    linkClicked = Signal(str)
    closeRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        content = hud._transparent_scroll(self)
        self.setAccessibleName("Meeting brief")
        self._info: BriefInfo | None = None
        self._held: list[Any] = []
        self.column = QWidget()
        self.column.setMaximumWidth(COLUMN_MAX)
        self.column.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.close_button = holo.HoloButton(CLOSE_NAME, holo.ICON, icon="close", diameter=34)
        self.close_button.setToolTip(CLOSE_NAME)
        self.close_button.clicked.connect(self.closeRequested)
        self.panel = BriefPanel()
        self._panel_layout = QVBoxLayout(self.panel)
        self._panel_layout.setContentsMargins(22, 18, 22, 18)
        self._panel_layout.setSpacing(6)
        self.cards_box = QWidget()
        self._cards_layout = QVBoxLayout(self.cards_box)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setSpacing(hud.ActionList.CARD_GAP)
        self.steps_button = holo.HoloButton(SEE_STEPS_TEXT, holo.LINK_CYAN, size=holo.SIZE_SM)
        self.steps_button.clicked.connect(self._on_steps)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addStretch(1)
        top.addWidget(self.close_button)
        steps = QHBoxLayout()
        steps.setContentsMargins(0, 0, 0, 0)
        steps.addWidget(self.steps_button)
        steps.addStretch(1)
        inner = QVBoxLayout(self.column)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(14)
        inner.addLayout(top)
        inner.addWidget(self.panel)
        inner.addWidget(self.cards_box)
        inner.addLayout(steps)
        outer = QHBoxLayout(content)
        outer.setContentsMargins(8, 8, 8, 16)
        outer.addStretch(1)
        outer.addWidget(self.column, 100)
        outer.addStretch(1)
        self._rows: list[QWidget] = []
        self.hide()

    # ---- what it shows ------------------------------------------------------------------------

    def set_brief(self, info: BriefInfo | None) -> None:
        for widget in self._rows:
            self._panel_layout.removeWidget(widget)
            widget.hide()
            widget.deleteLater()
        self._rows = []
        self._info = info
        self.steps_button.setVisible(bool(info is not None and info.steps_link))
        if info is None:
            return
        add = self._add
        add(_label(KICKER, "label", holo.HOLO, wrap=False))
        add(_label(info.title, "screen_title", holo.INK_1, selectable=True))
        meta = f" {DOT} ".join(part for part in (info.when, info.alias) if part)
        if meta:
            add(_label(holo.role_text("readout", meta), "readout", holo.INK_KICKER))
        if info.note:
            add(_label(info.note, "body_sm", holo.INK_3))
        if info.people:
            self._section(WHO_TITLE)
            for person in info.people:
                add(PersonRow(person))
        for title, lines in ((THREADS_TITLE, info.threads), (OPEN_TITLE, info.open), (POINTS_TITLE, info.points),
                             (NOTES_TITLE, info.notes)):
            if lines:
                self._section(title)
                for line in lines:
                    add(_label(line, "body", holo.INK_2, selectable=True))
        if info.nudges:
            self._section(NUDGES_TITLE)
            for text, link_id in info.nudges:
                add(_label(text, "body", holo.INK_2, selectable=True))
                button = holo.HoloButton(NUDGE_TEXT, holo.LINK_CYAN, size=holo.SIZE_SM)
                button.setAccessibleName(f"{NUDGE_TEXT}: {text}")
                button.clicked.connect(lambda _checked=False, link=link_id: self.linkClicked.emit(link))
                add(button)
        self.verticalScrollBar().setValue(0)

    def _add(self, widget: QWidget) -> None:
        self._panel_layout.addWidget(widget)
        self._rows.append(widget)

    def _section(self, title: str) -> None:
        spacer = QWidget()
        spacer.setFixedHeight(8)
        self._add(spacer)
        self._add(_label(holo.role_text("label", title), "label", holo.INK_KICKER, wrap=False))

    def info(self) -> BriefInfo | None:
        return self._info

    def card_ids(self) -> tuple[str, ...]:
        return self._info.card_ids if self._info is not None else ()

    def sections(self) -> list[str]:
        """The section labels shown, in order (WHO'S COMING, LAST THREADS, ...)."""
        titles = (WHO_TITLE, THREADS_TITLE, OPEN_TITLE, POINTS_TITLE, NOTES_TITLE, NUDGES_TITLE)
        return [row.text() for row in self._rows if isinstance(row, QLabel) and row.text() in titles]

    def people_rows(self) -> list[PersonRow]:
        return [row for row in self._rows if isinstance(row, PersonRow)]

    def texts(self) -> list[str]:
        """Every text the panel shows (labels, names, badges, lines), in order."""
        out: list[str] = []
        for row in self._rows:
            if isinstance(row, QLabel):
                out.append(row.text())
            elif isinstance(row, PersonRow):
                out += [row.person.name, row.person.badge, row.person.line]
        return [text for text in out if text]

    # ---- the lent reply cards -----------------------------------------------------------------

    def hold(self, card: QWidget) -> None:
        """Show an approval card under the brief (the app lends it from NEEDS YOUR OK)."""
        if card in self._held:
            return
        self._held.append(card)
        self._cards_layout.addWidget(card)
        card.show()

    def release(self, card: QWidget) -> None:
        if card in self._held:
            self._held.remove(card)
            if shiboken6.isValid(card):
                self._cards_layout.removeWidget(card)

    def held(self) -> list[Any]:
        self._held = [card for card in self._held if shiboken6.isValid(card)]
        return list(self._held)

    def _on_steps(self) -> None:
        if self._info is not None and self._info.steps_link:
            self.linkClicked.emit(self._info.steps_link)

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self.closeRequested.emit()
            return
        super().keyPressEvent(event)


def people_of(who: Sequence[tuple[str, str]], threads: Sequence[tuple[str, str, str, str]],
              missing: Sequence[tuple[str, str]]) -> tuple[BriefPerson, ...]:
    """WHO'S COMING from a brief's own parts (ask.prep.PrepBrief.who / threads / missing): each guest's
    badge and the line of his or her last thread ('"Budget numbers", Oct 8'), else why there is none."""
    lines = {name: label for name, label, _summary, _waiting in threads if name}
    why = {name: reason for name, reason in missing}
    people = []
    for name, words in who:
        badge, tone = badge_of(words)
        if name in lines:
            people.append(BriefPerson(name, badge, tone, lines[name]))
        else:
            people.append(BriefPerson(name, badge, tone, why.get(name, ""), quiet=True))
    return tuple(people)
