"""Meetings part 2: one meeting's summary on the Meetings screen (hologram look, British spelling; amber only where a
decision waits - the draft cards themselves, a pending note's Keep / Dismiss).

Nothing here sends, adds, keeps or reads anything: every widget shows what the app hands it and every click only
emits a signal. The follow-up drafts and the Add block cards are the very approval cards of NEEDS YOUR OK, lent by
the app while the summary shows (``hold(card)`` / ``release(card)`` / ``held()``), so Send, the NEW RECIPIENT tick on
the card, the own-address refusal, Sent with its hidden hold and Undo, Add and its Undo work exactly as there.

Public API
==========
``SummaryAction(who, what, due, mine, block, card_id)``: one action item; for the owner's own (``mine``) ``block`` is
    BLOCK_NONE (an Add block button: a click proposes a block card - nothing is added until that card's own Add),
    BLOCK_PROPOSED (its card shows under ACTION ITEMS), BLOCK_ADDED / BLOCK_DISMISSED (that card was decided: its
    own Add, or Dismiss; no button - the card keeps its Undo while it is listed) or BLOCK_NO_TIME (no free half hour
    was found).
``SummaryFollowUp(to, subject, state, card_id)``: one follow-up draft and where its card stands.
``SummaryNote(text, state)``: a suggested note: NOTE_PENDING (Keep / Dismiss: Memory's own), NOTE_KEPT,
    NOTE_DISMISSED, NOTE_OFF (Memory can't keep notes now).
``SummaryInfo``: what the view shows (memory only; never logged or spoken).
``SummaryView``: ``set_summary(info)``, ``card_ids()``, ``hold`` / ``release`` / ``held``, ``texts()``,
    ``sections()``; signals ``backClicked()``, ``addBlockClicked(int)``, ``keepClicked(int)``,
    ``dismissClicked(int)``, ``linkClicked(str)`` (See every step: "live:<task>").
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import shiboken6
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from . import holo, hud
from .memory_view import COLUMN_MAX, body_label

KICKER = "MEETING SUMMARY"
BACK_TEXT = "All meetings"
DECISIONS_TITLE = "DECISIONS"
ACTIONS_TITLE = "ACTION ITEMS"
QUESTIONS_TITLE = "OPEN QUESTIONS"
FOLLOWUPS_TITLE = "FOLLOW-UP DRAFTS"
NOTES_TITLE = "WORTH REMEMBERING?"
NONE_TEXT = "None noted."
ADD_BLOCK_TEXT = "Add block"
BLOCK_PROPOSED_TEXT = "A block is proposed below - add it there"
BLOCK_ADDED_TEXT = "Block added to your calendar"
BLOCK_DISMISSED_TEXT = "Block dismissed"
BLOCK_NO_TIME_TEXT = "No free half hour found in the next days"
DRAFTS_NOTE = "Drafts only: nothing is sent until you click Send on a card."
NO_DRAFTS_TEXT = "No follow-up drafts."
KEEP_TEXT = "Keep"
DISMISS_TEXT = "Dismiss"
KEPT_TEXT = "Kept in your notes"
DISMISSED_TEXT = "Dismissed"
MEMORY_OFF_TEXT = "Memory is off on this PC: nothing can be kept"
SEE_STEPS_TEXT = "See every step"
FOOT_TEXT = ("Written by your Claude Code from the transcript on this PC. Check it before you act: speech to text "
             "makes mistakes.")
DOT = "\u00b7"

BLOCK_NONE, BLOCK_PROPOSED, BLOCK_NO_TIME = "none", "proposed", "no_time"
BLOCK_ADDED, BLOCK_DISMISSED = "added", "dismissed"
_BLOCK_CARDS = (BLOCK_PROPOSED, BLOCK_ADDED, BLOCK_DISMISSED)     # states whose card the view may hold
NOTE_PENDING, NOTE_KEPT, NOTE_DISMISSED, NOTE_OFF = "pending", "kept", "dismissed", "off"


@dataclass(frozen=True)
class SummaryAction:
    who: str = field(repr=False)              # "You" or the guest's name
    what: str = field(repr=False)
    due: str = field(default="", repr=False)  # "due Wed 22 Oct" ("" for none)
    mine: bool = False
    block: str = BLOCK_NONE
    card_id: str = ""


@dataclass(frozen=True)
class SummaryFollowUp:
    to: str = field(repr=False)
    subject: str = field(repr=False)
    state: str = field(default="", repr=False)   # "" while its card shows; else "Sent", "Dismissed", ...
    card_id: str = ""


@dataclass(frozen=True)
class SummaryNote:
    text: str = field(repr=False)
    state: str = NOTE_PENDING


@dataclass(frozen=True)
class SummaryInfo:
    meeting_id: str
    title: str = field(repr=False)
    meta: str = field(default="", repr=False)                  # "MON 19 OCT 2:00 PM . 46:12"
    guests: str = field(default="", repr=False)                # "with Ana Lima and Ben Ode"
    decisions: tuple[str, ...] = field(default=(), repr=False)
    actions: tuple[SummaryAction, ...] = field(default=(), repr=False)
    questions: tuple[str, ...] = field(default=(), repr=False)
    follow_ups: tuple[SummaryFollowUp, ...] = field(default=(), repr=False)
    notes: tuple[SummaryNote, ...] = field(default=(), repr=False)
    note: str = field(default="", repr=False)                  # "2 items left out (...)"
    steps_link: str = ""

    def __repr__(self) -> str:
        return (f"SummaryInfo({self.meeting_id}, {len(self.decisions)} decision(s), {len(self.actions)} action(s), "
                f"{len(self.follow_ups)} follow-up(s), {len(self.notes)} note(s))")


def _label(text: str, role: str, color: str, *, wrap: bool = True, selectable: bool = False) -> QLabel:
    label = body_label(text, role, color, wrap=wrap, selectable=selectable)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    return label


class _Panel(QWidget):
    """The summary's focused tile (painted behind its labels)."""

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            holo.paint_tile(painter, QRectF(self.rect()), holo.TILE_FOCUSED)
        finally:
            painter.end()


class _ActionRow(QWidget):
    """One action item: "You - Send the Q3 numbers . due Wed 22 Oct", and for the owner's own an Add block button."""

    addClicked = Signal(int)

    def __init__(self, index: int, action: SummaryAction, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.index = index
        self.action = action
        text = f"{action.who} - {action.what}" + (f" {DOT} {action.due}" if action.due else "")
        self.text_label = _label(text, "body", holo.INK_1 if action.mine else holo.INK_2, selectable=True)
        self.add_button = holo.HoloButton(ADD_BLOCK_TEXT, holo.GHOST, size=holo.SIZE_SM, icon="calendar")
        self.add_button.setAccessibleName(f"{ADD_BLOCK_TEXT}: {action.what}")
        self.add_button.clicked.connect(lambda: self.addClicked.emit(self.index))
        self.add_button.setVisible(action.mine and action.block == BLOCK_NONE)
        state = {BLOCK_PROPOSED: BLOCK_PROPOSED_TEXT, BLOCK_NO_TIME: BLOCK_NO_TIME_TEXT, BLOCK_ADDED: BLOCK_ADDED_TEXT,
                 BLOCK_DISMISSED: BLOCK_DISMISSED_TEXT}.get(action.block, "")
        self.state_label = _label(state, "body_sm", holo.INK_3)
        self.state_label.setVisible(action.mine and bool(state))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        row.addWidget(self.text_label, 1)
        row.addWidget(self.add_button, 0, Qt.AlignmentFlag.AlignTop)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        column.addLayout(row)
        column.addWidget(self.state_label)


class _NoteRow(QWidget):
    """One suggested note with Memory's Dismiss and Keep (nothing is kept without Keep)."""

    keepClicked = Signal(int)
    dismissClicked = Signal(int)

    def __init__(self, index: int, note: SummaryNote, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.index = index
        self.note = note
        self.text_label = _label(note.text, "body", holo.AMBER_TX if note.state == NOTE_PENDING else holo.INK_2,
                                 selectable=True)
        self.dismiss_button = holo.HoloButton(DISMISS_TEXT, holo.AMBER_GHOST, size=holo.SIZE_SM)
        self.keep_button = holo.HoloButton(KEEP_TEXT, holo.AMBER_COMMIT, size=holo.SIZE_SM, icon="check")
        self.dismiss_button.setAccessibleName(f"{DISMISS_TEXT} suggested note: {note.text}")
        self.keep_button.setAccessibleName(f"{KEEP_TEXT} suggested note: {note.text}")
        self.dismiss_button.clicked.connect(lambda: self.dismissClicked.emit(self.index))
        self.keep_button.clicked.connect(lambda: self.keepClicked.emit(self.index))
        pending = note.state == NOTE_PENDING
        self.dismiss_button.setVisible(pending)
        self.keep_button.setVisible(pending)
        state = {NOTE_KEPT: KEPT_TEXT, NOTE_DISMISSED: DISMISSED_TEXT, NOTE_OFF: MEMORY_OFF_TEXT}.get(note.state, "")
        self.state_label = _label(state, "body_sm", holo.OK if note.state == NOTE_KEPT else holo.INK_3, wrap=False)
        self.state_label.setVisible(bool(state))
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(holo.BUTTON_GAP - 2 * holo.HoloButton.MARGIN)
        buttons.addWidget(self.state_label)
        buttons.addStretch(1)
        buttons.addWidget(self.dismiss_button)
        buttons.addWidget(self.keep_button)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        column.addWidget(self.text_label)
        column.addLayout(buttons)


class SummaryView(QScrollArea):
    """A meeting's summary; see the module docstring."""

    backClicked = Signal()
    addBlockClicked = Signal(int)
    keepClicked = Signal(int)
    dismissClicked = Signal(int)
    linkClicked = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        content = hud._transparent_scroll(self)
        self.setAccessibleName("Meeting summary")
        self._info: SummaryInfo | None = None
        self._held: list[Any] = []
        self._rows: list[QWidget] = []
        self.column = QWidget()
        self.column.setMaximumWidth(COLUMN_MAX)
        self.column.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.back_button = holo.HoloButton(BACK_TEXT, holo.LINK_CYAN, size=holo.SIZE_SM)
        self.back_button.clicked.connect(self.backClicked)
        self.panel = _Panel()
        self._panel_layout = QVBoxLayout(self.panel)
        self._panel_layout.setContentsMargins(22, 18, 22, 18)
        self._panel_layout.setSpacing(6)
        self.blocks_box = QWidget()
        self._blocks_layout = QVBoxLayout(self.blocks_box)
        self._blocks_layout.setContentsMargins(0, 0, 0, 0)
        self._blocks_layout.setSpacing(hud.ActionList.CARD_GAP)
        self.drafts_head = _label(holo.role_text("label", FOLLOWUPS_TITLE), "label", holo.INK_KICKER, wrap=False)
        self.drafts_label = _label("", "body_sm", holo.INK_3, selectable=True)
        self.cards_box = QWidget()
        self._cards_layout = QVBoxLayout(self.cards_box)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setSpacing(hud.ActionList.CARD_GAP)
        self.notes_box = QWidget()
        self._notes_layout = QVBoxLayout(self.notes_box)
        self._notes_layout.setContentsMargins(0, 0, 0, 0)
        self._notes_layout.setSpacing(10)
        self.steps_button = holo.HoloButton(SEE_STEPS_TEXT, holo.LINK_CYAN, size=holo.SIZE_SM)
        self.steps_button.clicked.connect(self._on_steps)
        self.foot_label = _label(FOOT_TEXT, "body_sm", holo.INK_4)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.back_button)
        top.addStretch(1)
        steps = QHBoxLayout()
        steps.setContentsMargins(0, 0, 0, 0)
        steps.addWidget(self.steps_button)
        steps.addStretch(1)
        inner = QVBoxLayout(self.column)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(12)
        inner.addLayout(top)
        inner.addWidget(self.panel)
        inner.addWidget(self.blocks_box)
        inner.addWidget(self.drafts_head)
        inner.addWidget(self.drafts_label)
        inner.addWidget(self.cards_box)
        inner.addWidget(self.notes_box)
        inner.addLayout(steps)
        inner.addWidget(self.foot_label)
        outer = QHBoxLayout(content)
        outer.setContentsMargins(0, 0, 8, 16)
        outer.addWidget(self.column, 100)
        outer.addStretch(1)

    # ---- what it shows ------------------------------------------------------------------------

    def info(self) -> SummaryInfo | None:
        return self._info

    def set_summary(self, info: SummaryInfo | None) -> None:
        for widget in self._rows:
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        self._rows = []
        self._info = info
        self.steps_button.setVisible(bool(info is not None and info.steps_link))
        if info is None:
            return
        add = self._add
        add(_label(KICKER, "label", holo.HOLO, wrap=False))
        add(_label(info.title, "screen_title", holo.INK_1, selectable=True))
        if info.meta:
            add(_label(holo.role_text("readout", info.meta), "readout", holo.INK_KICKER))
        if info.guests:
            add(_label(info.guests, "body_sm", holo.INK_3, selectable=True))
        if info.note:
            add(_label(info.note, "body_sm", holo.INK_3))
        for title, lines in ((DECISIONS_TITLE, info.decisions),):
            self._section(title)
            for line in lines or (NONE_TEXT,):
                add(_label(line, "body", holo.INK_2 if lines else holo.INK_4, selectable=bool(lines)))
        self._section(ACTIONS_TITLE)
        if not info.actions:
            add(_label(NONE_TEXT, "body", holo.INK_4))
        for index, action in enumerate(info.actions):
            row = _ActionRow(index, action)
            row.addClicked.connect(self.addBlockClicked)
            add(row)
        self._section(QUESTIONS_TITLE)
        for line in info.questions or (NONE_TEXT,):
            add(_label(line, "body", holo.INK_2 if info.questions else holo.INK_4, selectable=bool(info.questions)))
        drafts = [f"To {item.to} - {item.subject}" + (f" {DOT} {item.state}" if item.state else "")
                  for item in info.follow_ups]
        self.drafts_label.setText("\n".join([DRAFTS_NOTE, *drafts]) if drafts else NO_DRAFTS_TEXT)
        for index, note in enumerate(info.notes):
            if index == 0:
                head = _label(holo.role_text("label", NOTES_TITLE), "label", holo.AMBER if any(
                    item.state == NOTE_PENDING for item in info.notes) else holo.INK_KICKER, wrap=False)
                self._notes_layout.addWidget(head)
                self._rows.append(head)
            row = _NoteRow(index, note)
            row.keepClicked.connect(self.keepClicked)
            row.dismissClicked.connect(self.dismissClicked)
            self._notes_layout.addWidget(row)
            self._rows.append(row)
        self.notes_box.setVisible(bool(info.notes))
        self.verticalScrollBar().setValue(0)

    def _add(self, widget: QWidget) -> None:
        self._panel_layout.addWidget(widget)
        self._rows.append(widget)

    def _section(self, title: str) -> None:
        spacer = QWidget()
        spacer.setFixedHeight(8)
        self._add(spacer)
        self._add(_label(holo.role_text("label", title), "label", holo.INK_KICKER, wrap=False))

    def card_ids(self) -> tuple[str, ...]:
        """The approval cards this view shows (the follow-up drafts', then the proposed blocks')."""
        info = self._info
        if info is None:
            return ()
        ids = [item.card_id for item in info.follow_ups if item.card_id and not item.state]
        ids += [item.card_id for item in info.actions if item.card_id and item.block in _BLOCK_CARDS]
        return tuple(ids)

    def sections(self) -> list[str]:
        """The section labels shown, in order."""
        titles = (DECISIONS_TITLE, ACTIONS_TITLE, QUESTIONS_TITLE, NOTES_TITLE)
        found = [row.text() for row in self._rows if isinstance(row, QLabel) and row.text() in titles]
        return found[:3] + [FOLLOWUPS_TITLE] + found[3:]

    def action_rows(self) -> list[_ActionRow]:
        return [row for row in self._rows if isinstance(row, _ActionRow)]

    def note_rows(self) -> list[_NoteRow]:
        return [row for row in self._rows if isinstance(row, _NoteRow)]

    def texts(self) -> list[str]:
        """Every text the view shows, in order (tests, harnesses)."""
        out: list[str] = []
        for row in self._rows:
            if isinstance(row, QLabel):
                out.append(row.text())
            elif isinstance(row, _ActionRow):
                out.append(row.text_label.text())
            elif isinstance(row, _NoteRow):
                out.append(row.note.text)
        out.append(self.drafts_label.text())
        return [text for text in out if text]

    # ---- the lent approval cards --------------------------------------------------------------

    def hold(self, card: QWidget, *, block: bool = False) -> None:
        """Show an approval card here (the app lends it from NEEDS YOUR OK): an Add block card (``block``) under
        the action items, a draft with the follow-ups."""
        if card in self._held:
            return
        self._held.append(card)
        (self._blocks_layout if block else self._cards_layout).addWidget(card)
        card.show()

    def block_ids(self) -> tuple[str, ...]:
        """The proposed blocks' cards (shown under the action items)."""
        info = self._info
        if info is None:
            return ()
        return tuple(item.card_id for item in info.actions if item.card_id and item.block in _BLOCK_CARDS)

    def release(self, card: QWidget) -> None:
        if card in self._held:
            self._held.remove(card)
            if shiboken6.isValid(card):
                self._blocks_layout.removeWidget(card)
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
            self.backClicked.emit()
            return
        super().keyPressEvent(event)


def note_text(count: int, notes_left: int = 0) -> str:
    """"2 items left out (not you or an invited guest, or a repeat)" ("" for none)."""
    parts = []
    if count:
        parts.append(f"{count} item{'' if count == 1 else 's'} left out (not you or an invited guest, or a repeat)")
    if notes_left:
        parts.append(f"{notes_left} suggested note{'' if notes_left == 1 else 's'} left out")
    return "; ".join(parts)


def guests_text(names: Sequence[str]) -> str:
    """"with Ana Lima and Ben Ode" ("" for none)."""
    clean = [" ".join(str(name).split()) for name in names if str(name).strip()]
    if not clean:
        return ""
    if len(clean) == 1:
        return f"with {clean[0]}"
    return "with " + ", ".join(clean[:-1]) + " and " + clean[-1]
