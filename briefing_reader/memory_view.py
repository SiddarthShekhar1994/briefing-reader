"""The Memory screen in the hologram look (HOLOGRAM_SPEC 2, 5-7, 13, 15, 16.6; round 2, part 2b).

Public API
==========

``MemoryStage``
    The Memory screen: a frameless scroll area whose column (at most COLUMN_MAX wide, centred) is a
    ``tile`` panel with a ``line-faint`` border (``PanelFrame``). In it: the MEMORY head; the banner while
    memory.json can't be read or a write failed (coral: a failure, never amber); the focal tile
    (``suggestion_tile``, a SuggestionTile: the oldest pending suggestion as the amber needs-you tile, or the
    note just kept from it); the "Add a note..." field (``input`` and the round ``add_button``; Enter or the
    button emit ``added(text)``; why it was not kept shows under it, ``set_refusal``); 'Showing notes with
    "Ana"' with Show all (``set_filter``; ``filterCleared``); the notes, newest first (``set_rows``: one
    NoteRow each) or the empty line; and the status line at the bottom (``set_status``: where the notes are
    kept, who gets them, how many and how much is sent). The same API as the old hud.MemoryPage (the app's
    controller fills it; it never reads or writes a file) plus ``suggestions()``, ``suggestion_tile`` and
    ``show_kept(text, meta, when)``.

``SuggestionTile``
    The focal tile. Pending: the needs-you look (``tile-amber``, amber brackets), the kicker ``SUGGESTION``
    with its 7 px amber dot (``SUGGESTION - 1 OF 3`` while more wait), the question as its title
    ("Remember that Ana organises the weekly check-in?", the whole note, never elided), Dismiss (amber
    ghost, ``dismissRequested(id)``) and Keep (amber commit, ``keepRequested(id)``; off while memory.json
    can't be read). Kept (``set_kept``): the done look, the stamp ``KEPT - 10:42 AM``, the note as it was
    kept, its meta (``SUGGESTED - OCT 12``) and "Added to your notes". Nothing is kept without Keep.

``NoteRow``
    One note: its text (Sora 15, selectable), the readout meta (``TOLD - OCT 10``, ``SUGGESTED``,
    ``EDITED``; ``SENT WHEN RELEVANT`` past the 8 KB) and Edit (ghost) / Delete (danger ghost), which show while the
    pointer is on the row or the keyboard is on one of them (they keep their place, so nothing moves under
    the pointer, and Tab reaches them). Edit opens a field (Enter or Save saves, Esc or Cancel cancels);
    after Delete the row reads "Deleted." with Undo while the controller keeps it.

``PanelFrame``, ``SectionHead``, ``body_label``
    Shared with followups_view and settings_view: the quiet panel, a section head (glyph + label kicker)
    and a plain-text label in a type role and colour.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import shiboken6
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPalette, QPen
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QLineEdit, QScrollArea, QSizePolicy, QVBoxLayout,
                               QWidget)

from . import holo, hud

COLUMN_MAX = 640                 # the panel's width at most (body lines of about 72 characters)
PANEL_PAD = (16, 16, 16, 14)     # left, top, right, bottom
FIELD_HEIGHT = 44
MEMORY_TITLE = "MEMORY"
SUGGESTION_KICKER = "SUGGESTION"
KEPT_WORD = "KEPT"
KEEP_TEXT = "Keep"
DISMISS_TEXT = "Dismiss"
ADDED_TEXT = "Added to your notes"
ADD_NAME = "Add note"
SAVE_TEXT = "Save"
CANCEL_TEXT = "Cancel"
DOT = "\u00b7"
_DOT_ROOM = 15                   # the kicker's 7 px dot and its 8 px gap
_STAMP_ROOM = 24                 # the kept stamp's 16 px disc and its 8 px gap
# A note that starts with one of these words reads in lower case inside "Remember that ...?".
_LOWER_FIRST = frozenset({"a", "an", "the", "my", "our", "his", "her", "their", "its", "this", "that", "these",
                          "those", "every", "each", "all", "some", "no", "when", "if"})


def body_label(text: str, role: str, color: str | QColor, *, wrap: bool = True, selectable: bool = False,
               name: str = "") -> QLabel:
    """A plain-text label in the type role ``role`` (never read as HTML)."""
    label = hud.make_label(text, holo.font(role), color, wrap=wrap, name=name)
    if selectable:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def question_text(note: str) -> str:
    """"Remember that Ana organises the weekly check-in?" from the note "Ana organises the weekly
    check-in." (the note's own words; a first word like "The" in lower case)."""
    text = " ".join(str(note or "").split()).rstrip(" .!")
    first, _space, rest = text.partition(" ")
    if first[:1].isupper() and first.isalpha() and first.casefold() in _LOWER_FIRST:
        text = first[:1].lower() + first[1:] + (" " + rest if rest else "")
    return f"Remember that {text}?" if text else ""


class PanelFrame(QWidget):
    """The quiet panel (spec 5, information look): ``tile`` fill, a 1 px ``line-faint`` border, radius 10."""

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.setPen(QPen(holo.LINE_FAINT, 1.0))
            painter.setBrush(QColor(holo.TILE))
            painter.drawRoundedRect(rect, holo.RADIUS_TILE, holo.RADIUS_TILE)
        finally:
            painter.end()


class Rule(QWidget):
    """A 1 px ``line-faint`` hairline."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(1)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), holo.LINE_FAINT)
        finally:
            painter.end()


class _Glyph(QWidget):
    """A 16 px stroke glyph (decoration only)."""

    def __init__(self, icon: str, color: str = holo.INK_KICKER, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._icon, self._color = icon, color
        self.setFixedSize(16, 16)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            holo.paint_icon(painter, QRectF(self.rect()), self._icon, self._color, stroke=1.8)
        finally:
            painter.end()


class SectionHead(QWidget):
    """A section head: a 16 px glyph and a label kicker (``ink-kicker``); ``layout()`` takes more at its right."""

    def __init__(self, icon: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title_label = body_label(holo.role_text("label", text), "label", holo.INK_KICKER, wrap=False)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(_Glyph(icon), 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.title_label, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)
        self.setMinimumHeight(20)


def _holds_focus(widget: QWidget) -> bool:
    focused = QApplication.focusWidget()
    return focused is not None and (focused is widget or widget.isAncestorOf(focused))


def _style_line_edit(edit: QLineEdit, role: str) -> None:
    edit.setFont(holo.font(role))
    palette = edit.palette()
    palette.setColor(QPalette.ColorRole.Text, QColor(holo.INK_1))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(holo.INK_4))
    palette.setColor(QPalette.ColorRole.Highlight, holo.alpha(holo.HOLO, 0.35))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(holo.INK_1))
    edit.setPalette(palette)


class AddField(QWidget):
    """The "Add a note..." pill (``field`` fill, a ``line-input`` border, ``holo`` while the keyboard is in it)
    with the input and the round add button inside."""

    def __init__(self, placeholder: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(FIELD_HEIGHT)
        self.input = QLineEdit()
        self.input.setFrame(False)
        self.input.setPlaceholderText(placeholder)
        self.input.setMaxLength(400)   # longer than any note: a long one is refused with its own words
        self.input.setStyleSheet("QLineEdit { background: transparent; border: none; padding: 0; }")
        _style_line_edit(self.input, "input")
        self.input.installEventFilter(self)
        self.add_button = holo.HoloButton(ADD_NAME, holo.ICON, icon="plus", diameter=36)
        self.add_button.setToolTip(ADD_NAME)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self.input, 1, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.add_button, 0, Qt.AlignmentFlag.AlignVCenter)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if watched is self.input and event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut,
                                                      QEvent.Type.EnabledChange):
            self.update()
        return super().eventFilter(watched, event)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            if not self.input.isEnabled():
                painter.setOpacity(0.45)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            border = QColor(holo.HOLO) if self.input.hasFocus() else holo.LINE_INPUT
            painter.setPen(QPen(border, 1.0))
            painter.setBrush(QColor(holo.FIELD))
            painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        finally:
            painter.end()


class NoteEdit(QLineEdit):
    """A note's edit field (``field`` fill, a ``line-input`` border, radius 8): Enter saves
    (``submitted``), Esc cancels (``cancelled``)."""

    submitted = Signal()
    cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMaxLength(400)
        self.setMinimumHeight(38)
        self.setStyleSheet(f"QLineEdit {{ background: {holo.FIELD}; border: 1px solid rgba(60, 211, 238, 128);"
                           f" border-radius: {holo.RADIUS_BUTTON}px; padding: 0 12px; }}"
                           f" QLineEdit:focus {{ border: 1px solid {holo.HOLO}; }}")
        _style_line_edit(self, "body")

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            event.accept()
            self.cancelled.emit()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class RevealButton(holo.HoloButton):
    """A HoloButton that is drawn only while ``revealed`` or while the keyboard is on it; it keeps its place
    (and its place in the Tab order) when it is not drawn. ``focusChanged()`` tells its row."""

    focusChanged = Signal()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._revealed = False

    def set_revealed(self, revealed: bool) -> None:
        if bool(revealed) != self._revealed:
            self._revealed = bool(revealed)
            self.update()

    def revealed(self) -> bool:
        return self._revealed or self.hasFocus()

    def focusInEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().focusInEvent(event)
        self.focusChanged.emit()

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().focusOutEvent(event)
        self.focusChanged.emit()

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if self.revealed():
            super().paintEvent(event)


class NoteRow(QWidget):
    """One note; see the module docstring. When a control the keyboard is on goes away (Enter or Esc in the
    field, Save, Cancel, Delete, Undo), the row's control that takes its place gets the focus, never a control
    of another note. Each button's accessible name says which note."""

    editRequested = Signal(str)
    editSubmitted = Signal(str, str)
    editCancelled = Signal(str)
    deleteRequested = Signal(str)
    undoRequested = Signal(str)

    def __init__(self, info: hud.MemoryRowInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._info = info
        self._editing = False
        self._read_only = False
        self._refusal = ""
        self._hovered = False
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.text_label = hud._PlainWrapLabel(info.text, holo.font("body"), holo.INK_2)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.edit_field = NoteEdit()
        self.edit_field.setAccessibleName("Change the note")
        self.edit_field.submitted.connect(self._submit)
        self.edit_field.cancelled.connect(lambda: self.editCancelled.emit(self._info.id))
        self.cancel_button = holo.HoloButton(CANCEL_TEXT, holo.GHOST, size=holo.SIZE_SM)
        self.save_button = holo.HoloButton(SAVE_TEXT, holo.GO, size=holo.SIZE_SM)
        self.cancel_button.clicked.connect(lambda: self.editCancelled.emit(self._info.id))
        self.save_button.clicked.connect(self._submit)
        self._edit_box = QWidget()
        edit_actions = QHBoxLayout()
        edit_actions.setContentsMargins(0, 0, 0, 0)
        edit_actions.setSpacing(holo.BUTTON_GAP - 2 * holo.HoloButton.MARGIN)
        edit_actions.addStretch(1)
        edit_actions.addWidget(self.cancel_button)
        edit_actions.addWidget(self.save_button)
        edit_layout = QVBoxLayout(self._edit_box)
        edit_layout.setContentsMargins(0, 2, 0, 0)
        edit_layout.setSpacing(4)
        edit_layout.addWidget(self.edit_field)
        edit_layout.addLayout(edit_actions)
        self._edit_box.hide()
        self.meta_label = body_label(info.meta, "readout", holo.INK_4, wrap=False)
        self.not_sent_label = body_label(holo.role_text("label", hud.MEMORY_NOT_SENT_TEXT), "label", holo.INK_4,
                                         wrap=False)
        self.not_sent_label.setToolTip(hud.MEMORY_NOT_SENT_TIP)
        self.edit_button = RevealButton(hud.MEMORY_EDIT_TEXT, holo.GHOST, size=holo.SIZE_SM)
        self.delete_button = RevealButton(hud.MEMORY_DELETE_TEXT, holo.DANGER_GHOST, size=holo.SIZE_SM)
        self.undo_button = holo.HoloButton(hud.MEMORY_UNDO_TEXT, holo.LINK_CYAN)
        for button in (self.edit_button, self.delete_button):
            button.focusChanged.connect(self._sync_reveal_soon)
            policy = button.sizePolicy()
            policy.setRetainSizeWhenHidden(True)
            button.setSizePolicy(policy)
        self.edit_button.clicked.connect(lambda: self.editRequested.emit(self._info.id))
        self.delete_button.clicked.connect(lambda: self.deleteRequested.emit(self._info.id))
        self.undo_button.clicked.connect(lambda: self.undoRequested.emit(self._info.id))
        meta = QHBoxLayout()
        meta.setContentsMargins(0, 0, 0, 0)
        meta.setSpacing(6)
        meta.addWidget(self.meta_label, 0, Qt.AlignmentFlag.AlignVCenter)
        meta.addWidget(self.not_sent_label, 0, Qt.AlignmentFlag.AlignVCenter)
        meta.addStretch(1)
        for button in (self.edit_button, self.delete_button, self.undo_button):
            meta.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.refusal_label = body_label("", "body_sm", holo.INK_2, name="memoryRowRefusal")
        self.refusal_label.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 10, 2, 4)
        layout.setSpacing(2)
        layout.addWidget(self.text_label)
        layout.addWidget(self._edit_box)
        layout.addLayout(meta)
        layout.addWidget(self.refusal_label)
        self._apply()

    # ---- state ------------------------------------------------------------------------------

    def info(self) -> hud.MemoryRowInfo:
        return self._info

    def set_info(self, info: hud.MemoryRowInfo) -> None:
        if info.id != self._info.id:
            self._editing = False
        if info.id != self._info.id or info.deleted != self._info.deleted:
            self._refusal = ""
        self._info = info
        self._apply()

    def editing(self) -> bool:
        return self._editing

    def set_editing(self, editing: bool, *, focus: bool = True) -> None:
        """Open the field with the note's text, all selected (``focus``: and the keyboard on it), or close it.
        Opening a field that is open keeps what the owner typed."""
        editing = bool(editing) and not self._info.deleted and not self._read_only
        if editing != self._editing:
            if editing:
                self.edit_field.setText(self._info.text)
            self._editing = editing
            self._refusal = ""
            self._apply()
        if editing and focus:
            self.edit_field.setFocus(Qt.FocusReason.OtherFocusReason)
            self.edit_field.selectAll()

    def set_read_only(self, read_only: bool) -> None:
        self._read_only = bool(read_only)
        if self._read_only:
            self._editing = False
        self._apply()

    def set_refusal(self, text: str) -> None:
        """Why this note's Edit or Undo was not kept ("" hides it; it goes when the edit closes)."""
        self._refusal = text or ""
        self._apply()

    def refusal(self) -> str:
        return self._refusal

    def actions_revealed(self) -> bool:
        """Edit and Delete are drawn (the pointer is on the row or the keyboard on one of them)."""
        return self.edit_button.revealed() and not self.edit_button.isHidden()

    def _submit(self) -> None:
        self.editSubmitted.emit(self._info.id, self.edit_field.text())

    def _apply(self) -> None:
        info = self._info
        deleted = info.deleted
        self.text_label.setText(hud.MEMORY_DELETED_TEXT if deleted else info.text)
        self.text_label.setFont(holo.font("body_sm" if deleted else "body"))
        hud.set_label_color(self.text_label, holo.INK_3 if deleted else holo.INK_2)
        self.meta_label.setText(holo.role_text("readout", info.meta))
        self.meta_label.setVisible(not deleted)
        self.not_sent_label.setVisible(not deleted and not info.sent)
        self.refusal_label.setText(self._refusal)
        self.refusal_label.setVisible(bool(self._refusal))
        controls = {self.edit_field: self._editing, self.save_button: self._editing,
                    self.cancel_button: self._editing,
                    self.edit_button: not deleted and not self._editing and not self._read_only,
                    self.delete_button: not deleted and not self._editing and not self._read_only,
                    self.undo_button: deleted and not self._read_only}
        held = next((widget for widget in controls if widget.hasFocus()), None)
        # The new controls first: hiding the one the keyboard is on hands the focus to the next control in
        # the chain, which must not be another note's.
        if self._editing:
            self._edit_box.setVisible(True)
        for widget, visible in controls.items():
            if visible:
                widget.setVisible(True)
        self.text_label.setVisible(not self._editing)
        for widget, visible in controls.items():
            if not visible:
                widget.setVisible(False)
        if not self._editing:
            self._edit_box.setVisible(False)
        if held is not None and not controls[held]:
            target = next((widget for widget in (self.edit_button, self.undo_button, self.delete_button)
                           if controls[widget]), None)
            if target is not None:
                target.setFocus(Qt.FocusReason.OtherFocusReason)
        self.edit_button.setAccessibleName(f"{hud.MEMORY_EDIT_TEXT} note: {info.text}")
        self.delete_button.setAccessibleName(f"{hud.MEMORY_DELETE_TEXT} note: {info.text}")
        self.undo_button.setAccessibleName(f"{hud.MEMORY_UNDO_TEXT} delete")
        self.save_button.setAccessibleName(f"{SAVE_TEXT} note: {info.text}")
        self.cancel_button.setAccessibleName(f"{CANCEL_TEXT} the change: {info.text}")
        self.setAccessibleName(hud.MEMORY_DELETED_TEXT if deleted else f"Note: {info.text}")
        self._sync_reveal()

    # ---- hover / focus reveal ---------------------------------------------------------------

    def _sync_reveal_soon(self) -> None:
        QTimer.singleShot(0, self._sync_reveal_if_valid)

    def _sync_reveal_if_valid(self) -> None:
        if shiboken6.isValid(self):
            self._sync_reveal()

    def _sync_reveal(self) -> None:
        shown = self._hovered or self.edit_button.hasFocus() or self.delete_button.hasFocus()
        for button in (self.edit_button, self.delete_button):
            button.set_revealed(shown)

    def enterEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().enterEvent(event)
        self._hovered = True
        self._sync_reveal()

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().leaveEvent(event)
        self._hovered = False
        self._sync_reveal()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.fillRect(QRectF(0, 0, self.width(), 1), holo.LINE_FAINT)
        finally:
            painter.end()


class SuggestionTile(QWidget):
    """The focal tile; see the module docstring."""

    keepRequested = Signal(int)
    dismissRequested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._info: hud.SuggestionRowInfo | None = None
        self._place = (0, 0)
        self._kept: tuple[str, str, str] | None = None
        self._read_only = False
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.kicker_label = body_label("", "label", holo.AMBER, wrap=False)
        self.title_label = hud._PlainWrapLabel("", holo.font("tile_title"), holo.AMBER_TX)
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.meta_label = body_label("", "readout", holo.INK_4, wrap=False)
        self.dismiss_button = holo.HoloButton(DISMISS_TEXT, holo.AMBER_GHOST)
        self.keep_button = holo.HoloButton(KEEP_TEXT, holo.AMBER_COMMIT, icon="check")
        self.dismiss_button.clicked.connect(self._dismiss)
        self.keep_button.clicked.connect(self._keep)
        self.added_label = body_label(ADDED_TEXT, "body_sm", holo.INK_3, wrap=False)
        self._added_glyph = _Glyph("check", holo.OK)
        kicker = QHBoxLayout()
        kicker.setContentsMargins(_DOT_ROOM, 0, 0, 0)            # room for the painted dot / stamp
        kicker.setSpacing(0)
        kicker.addWidget(self.kicker_label, 1, Qt.AlignmentFlag.AlignVCenter)
        self._kicker_row = kicker
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(holo.BUTTON_GAP - 2 * holo.HoloButton.MARGIN)
        actions.addWidget(self.dismiss_button)
        actions.addWidget(self._added_glyph, 0, Qt.AlignmentFlag.AlignVCenter)
        actions.addSpacing(4)
        actions.addWidget(self.added_label, 0, Qt.AlignmentFlag.AlignVCenter)
        actions.addStretch(1)
        actions.addWidget(self.keep_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(holo.TILE_PAD[1], holo.TILE_PAD[0], holo.TILE_PAD[1], holo.TILE_PAD[0])
        layout.setSpacing(0)
        layout.addLayout(kicker)
        layout.addSpacing(holo.KICKER_GAP)
        layout.addWidget(self.title_label)
        layout.addSpacing(4)
        layout.addWidget(self.meta_label)
        layout.addSpacing(10)
        layout.addLayout(actions)
        self.hide()

    # ---- what it shows ------------------------------------------------------------------------

    def set_suggestion(self, info: hud.SuggestionRowInfo | None, place: int = 1, total: int = 1) -> None:
        """The pending suggestion (``place`` of ``total`` waiting); None: none waits."""
        self._info = info
        self._place = (place, total) if info is not None else (0, 0)
        self._apply()

    def set_kept(self, kept: tuple[str, str, str] | None) -> None:
        """(the note as kept, its meta, the time) - shown while no suggestion waits; None clears it."""
        self._kept = kept
        self._apply()

    def set_read_only(self, read_only: bool) -> None:
        self._read_only = bool(read_only)
        self.keep_button.setEnabled(not self._read_only)
        self.keep_button.setToolTip("memory.json can't be read" if self._read_only else "")

    def info(self) -> hud.SuggestionRowInfo | None:
        return self._info

    def state(self) -> str:
        """"pending", "kept" or "" (hidden)."""
        if self._info is not None:
            return "pending"
        return "kept" if self._kept is not None else ""

    def question(self) -> str:
        return question_text(self._info.text) if self._info is not None else ""

    def kicker(self) -> str:
        return self.kicker_label.text()

    def _apply(self) -> None:
        state = self.state()
        pending = state == "pending"
        if pending:
            assert self._info is not None
            place, total = self._place
            kicker = SUGGESTION_KICKER + (f" {DOT} {place} OF {total}" if total > 1 else "")
            self.kicker_label.setText(kicker)
            self._kicker_row.setContentsMargins(_DOT_ROOM, 0, 0, 0)
            hud.set_label_color(self.kicker_label, holo.AMBER)
            self.kicker_label.setFont(holo.font("label"))
            self.title_label.setText(self.question())
            hud.set_label_color(self.title_label, holo.AMBER_TX)
            self.setAccessibleName(f"Suggested note: {self._info.text}")
            self.keep_button.setAccessibleName(f"{KEEP_TEXT} suggested note: {self._info.text}")
            self.dismiss_button.setAccessibleName(f"{DISMISS_TEXT} suggested note: {self._info.text}")
        elif state == "kept":
            assert self._kept is not None
            text, meta, when = self._kept
            self.kicker_label.setText(f"{KEPT_WORD} {DOT} {when}".upper() if when else KEPT_WORD)
            self._kicker_row.setContentsMargins(_STAMP_ROOM, 0, 0, 0)
            hud.set_label_color(self.kicker_label, holo.OK)
            self.kicker_label.setFont(holo.font("readout"))
            self.title_label.setText(text)
            hud.set_label_color(self.title_label, holo.INK_3)
            self.meta_label.setText(holo.role_text("readout", meta))
            self.setAccessibleName(f"Kept: {text}")
        self.meta_label.setVisible(state == "kept" and bool(self.meta_label.text()))
        self.dismiss_button.setVisible(pending)
        self.keep_button.setVisible(pending)
        self._added_glyph.setVisible(state == "kept")
        self.added_label.setVisible(state == "kept")
        self.setVisible(bool(state))
        self.updateGeometry()
        self.update()

    def _keep(self) -> None:
        if self._info is not None and not self._read_only:
            self.keepRequested.emit(self._info.id)

    def _dismiss(self) -> None:
        if self._info is not None:
            self.dismissRequested.emit(self._info.id)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        state = self.state()
        if not state:
            return
        painter = QPainter(self)
        try:
            holo.paint_tile(painter, QRectF(self.rect()), holo.TILE_NEEDS if state == "pending" else holo.TILE_DONE)
            label = self.kicker_label.geometry()
            center = QPointF(holo.TILE_PAD[1] + 3.5, label.center().y() + 0.5)
            if state == "pending":
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(holo.AMBER))
                painter.drawEllipse(center, holo.NEW_DOT / 2, holo.NEW_DOT / 2)
            else:
                holo.paint_stamp(painter, QPointF(holo.TILE_PAD[1] + 8, label.center().y() + 0.5), holo.STAMP_OK,
                                 size=16)
        finally:
            painter.end()


class MemoryStage(QScrollArea):
    """The Memory screen; see the module docstring. Each note keeps its row widget across refreshes (an open
    edit keeps its draft and focus); Tab goes through the screen in the order it shows (the focal tile's
    buttons, the Add field, Show all, then each note's controls); a refused Edit or Undo says why under its
    note. The Add field stays usable while an Ask runs."""

    added = Signal(str)
    edited = Signal(str, str)
    deleted = Signal(str)
    undoDelete = Signal(str)
    suggestionSaved = Signal(int)          # Keep on the focal tile
    suggestionDismissed = Signal(int)
    filterCleared = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        content = hud._transparent_scroll(self)
        self.panel = PanelFrame()
        self.panel.setMaximumWidth(COLUMN_MAX)
        column = QHBoxLayout(content)
        column.setContentsMargins(holo.GUTTER_SIDE, holo.GUTTER_TOP, holo.GUTTER_SIDE, 16)
        column.setSpacing(0)
        column.addStretch(1)
        column.addWidget(self.panel, 1000)
        column.addStretch(1)
        self._layout = QVBoxLayout(self.panel)
        self._layout.setContentsMargins(*PANEL_PAD)
        self._layout.setSpacing(0)
        self.head = SectionHead("memory", MEMORY_TITLE)
        self.banner_label = body_label("", "body_sm", holo.ALERT_TX, name="memoryBanner")
        self.banner_label.setContentsMargins(0, 0, 0, 12)
        self.banner_label.hide()
        self.suggestion_tile = SuggestionTile()
        self.suggestion_tile.keepRequested.connect(self.suggestionSaved)
        self.suggestion_tile.dismissRequested.connect(self.suggestionDismissed)
        self.field = AddField(hud.MEMORY_ADD_PLACEHOLDER)
        self.input = self.field.input
        self.input.setAccessibleName("Add a note to Jarvis's memory")
        self.input.returnPressed.connect(self._submit)
        self.add_button = self.field.add_button
        self.add_button.clicked.connect(self._submit)
        self.refusal_label = body_label("", "body_sm", holo.INK_2, name="memoryRefusal")
        self.refusal_label.hide()
        self.filter_label = body_label("", "body_sm", holo.INK_3, name="memoryFilter")
        self.show_all_button = holo.HoloButton(hud.MEMORY_SHOW_ALL_TEXT, holo.LINK_CYAN)
        self.show_all_button.clicked.connect(self._clear_filter)
        self._filter_box = QWidget()
        filter_row = QHBoxLayout(self._filter_box)
        filter_row.setContentsMargins(6, 0, 0, 0)
        filter_row.setSpacing(4)
        filter_row.addWidget(self.filter_label, 1, Qt.AlignmentFlag.AlignVCenter)
        filter_row.addWidget(self.show_all_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self._filter_box.hide()
        self._notes_box = QWidget()
        self._notes_layout = QVBoxLayout(self._notes_box)
        self._notes_layout.setContentsMargins(0, 0, 0, 0)
        self._notes_layout.setSpacing(0)
        self.empty_label = body_label(hud.MEMORY_EMPTY_TEXT, "body_sm", holo.INK_3, name="memoryEmpty")
        self.status_label = body_label("", "body_sm", holo.INK_4, name="memoryStatus")
        self._status_rule = Rule()
        layout = self._layout
        layout.addWidget(self.head)
        layout.addSpacing(14)
        layout.addWidget(self.banner_label)
        layout.addWidget(self.suggestion_tile)
        self._tile_gap = QWidget()
        self._tile_gap.setFixedHeight(14)
        layout.addWidget(self._tile_gap)
        layout.addWidget(self.field)
        layout.addSpacing(8)
        for widget in (self.refusal_label, self._filter_box):
            layout.addWidget(widget)
        layout.addSpacing(4)
        layout.addWidget(self._notes_box)
        layout.addSpacing(6)
        layout.addWidget(self.empty_label)
        layout.addStretch(1)
        layout.addSpacing(16)
        layout.addWidget(self._status_rule)
        layout.addSpacing(12)
        layout.addWidget(self.status_label)
        self._rows: list[NoteRow] = []
        self._suggestions: tuple[hud.SuggestionRowInfo, ...] = ()
        self._kept: tuple[str, str, str] | None = None
        self._read_only = False
        self._filter = ""
        self._editing = ""
        self.set_suggestions(())
        self.set_rows(())

    # ---- what the controller shows ------------------------------------------------------------

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def status(self) -> str:
        return self.status_label.text()

    def set_banner(self, text: str) -> None:
        """The read-only banner or the write warning ("" hides it); a new one is scrolled into view."""
        new = bool(text) and (text != self.banner_label.text() or self.banner_label.isHidden())
        self.banner_label.setText(text)
        self.banner_label.setVisible(bool(text))
        if new:
            self.verticalScrollBar().setValue(0)

    def banner(self) -> str:
        return self.banner_label.text() if not self.banner_label.isHidden() else ""

    def set_read_only(self, read_only: bool) -> None:
        """memory.json can't be read: Add, Edit, Delete and Keep are off."""
        self._read_only = bool(read_only)
        self.input.setEnabled(not self._read_only)
        self.add_button.setEnabled(not self._read_only)
        for row in self._rows:
            row.set_read_only(self._read_only)
        self.suggestion_tile.set_read_only(self._read_only)
        self._sync_empty()
        if self._read_only:
            self._editing = ""

    def read_only(self) -> bool:
        return self._read_only

    def set_refusal(self, text: str, entry_id: str = "") -> None:
        """Why the last Add or Keep was not kept, under the Add field ("" hides it); with ``entry_id``, why
        that note's Edit or Undo was not kept, under the note (scrolled into view)."""
        row = self._row(entry_id) if entry_id else None
        if row is not None:
            row.set_refusal(text)
            if text:
                self.reveal(row.refusal_label)
            return
        self.refusal_label.setText(text)
        self.refusal_label.setVisible(bool(text))

    def refusal(self, entry_id: str = "") -> str:
        """The refusal under the Add field (or under the note ``entry_id``); "" when none shows."""
        if entry_id:
            row = self._row(entry_id)
            return row.refusal() if row is not None else ""
        return self.refusal_label.text() if not self.refusal_label.isHidden() else ""

    def clear_input(self) -> None:
        self.input.clear()

    def set_filter(self, words: str) -> None:
        """'Showing notes with "<words>"' and Show all ("" hides it); the controller passes the matching rows."""
        self._filter = " ".join(str(words or "").split())
        self.filter_label.setText(hud.MEMORY_FILTER_TEXT.format(words=self._filter) if self._filter else "")
        self._filter_box.setVisible(bool(self._filter))

    def filter_words(self) -> str:
        return self._filter

    def set_rows(self, rows: Sequence[hud.MemoryRowInfo]) -> None:
        """The notes, newest first. Each note keeps its own row widget whatever moves around it (a note added
        above, a Deleted placeholder that goes), so an edit in progress keeps what the owner typed, its cursor
        and its focus. When the row the keyboard is on goes, the row now in its place takes the focus (never a
        field outside Memory's rows)."""
        rows = tuple(rows)
        focused = next((index for index, row in enumerate(self._rows) if _holds_focus(row)), None)
        old: dict[str, NoteRow] = {}
        for row in self._rows:
            old.setdefault(row.info().id, row)
        kept: list[NoteRow] = []
        for info in rows:
            row = old.pop(info.id, None)
            if row is None:
                row = self._make_row(info)
            elif row.info() != info:
                row.set_info(info)
            kept.append(row)
        for index, row in enumerate(kept):   # in order (a moved widget stays shown: no focus change)
            if self._notes_layout.indexOf(row) != index:
                self._notes_layout.removeWidget(row)
                self._notes_layout.insertWidget(index, row)
        lost_focus = focused is not None and self._rows[focused] not in kept
        for row in old.values():
            self._notes_layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        self._rows = kept
        for row in self._rows:
            row.set_read_only(self._read_only)
            if row.info().id == self._editing and not row.editing():
                row.set_editing(True, focus=False)
            elif row.info().id != self._editing and row.editing():
                row.set_editing(False)
        if self._editing and not any(row.info().id == self._editing for row in self._rows):
            self._editing = ""
        if lost_focus and self._rows:
            assert focused is not None
            row = self._rows[min(focused, len(self._rows) - 1)]
            target = next((button for button in (row.edit_button, row.undo_button) if not button.isHidden()), None)
            if target is not None:
                target.setFocus(Qt.FocusReason.OtherFocusReason)
        self._notes_box.setVisible(bool(rows))
        self._sync_empty()
        self._chain_tab_order()

    def rows(self) -> list[NoteRow]:
        return list(self._rows)

    def row_infos(self) -> list[hud.MemoryRowInfo]:
        return [row.info() for row in self._rows]

    def set_suggestions(self, rows: Sequence[hud.SuggestionRowInfo]) -> None:
        """The pending suggestions, oldest first: the oldest is the focal tile (one decision at a time)."""
        self._suggestions = tuple(rows)
        if self._suggestions:
            self._kept = None
        first = self._suggestions[0] if self._suggestions else None
        tile = self.suggestion_tile
        tile.set_kept(self._kept)
        tile.set_suggestion(first, 1, len(self._suggestions))
        tile.set_read_only(self._read_only)
        self._tile_gap.setVisible(not tile.isHidden())
        self._chain_tab_order()

    def suggestions(self) -> list[hud.SuggestionRowInfo]:
        return list(self._suggestions)

    def show_kept(self, text: str, meta: str, when: str) -> None:
        """The note just kept from the focal tile: KEPT and the time, the note and its meta, while no other
        suggestion waits (it goes when the screen is left)."""
        self._kept = (text, meta, when)
        if not self._suggestions:
            self.suggestion_tile.set_kept(self._kept)
            self._tile_gap.setVisible(True)

    def kept(self) -> tuple[str, str, str] | None:
        return self._kept if self.suggestion_tile.state() == "kept" else None

    def _sync_empty(self) -> None:
        self.empty_label.setVisible(not self._rows and not self._filter and not self._read_only)

    def _chain_tab_order(self) -> None:
        """Tab goes through Memory in the order it shows: the focal tile's Dismiss and Keep, Add, Show all,
        then each note's field, Cancel, Save, Edit, Delete and Undo (rows made later would otherwise come last
        in the window's focus chain, after everything built after the screen)."""
        tile = self.suggestion_tile
        chain: list[QWidget] = [tile.dismiss_button, tile.keep_button, self.input, self.add_button,
                                self.show_all_button]
        for row in self._rows:
            chain += [row.edit_field, row.cancel_button, row.save_button, row.edit_button, row.delete_button,
                      row.undo_button]
        for first, second in zip(chain, chain[1:]):
            QWidget.setTabOrder(first, second)

    def edit_row(self, entry_id: str) -> None:
        """Open a note's inline edit field (Edit), scrolled into view."""
        if self._read_only:
            return
        self._editing = entry_id
        for row in self._rows:
            row.set_editing(row.info().id == entry_id)
            if row.info().id == entry_id:
                self.reveal(row.edit_field)

    def reveal(self, widget: QWidget) -> None:
        """Scroll ``widget`` (a row, its field or its refusal) into view once the layout has placed it."""
        def scroll() -> None:
            if not (shiboken6.isValid(self) and shiboken6.isValid(widget) and widget.isVisibleTo(self)):
                return
            holder: QWidget | None = widget
            while holder is not None and holder is not self:     # place every layout on the way down
                layout = holder.layout()
                if layout is not None:
                    layout.activate()
                holder = holder.parentWidget()
            self.widget().layout().activate()
            target: QWidget = widget
            parent = widget.parentWidget()
            while parent is not None and parent is not self.widget():
                if isinstance(parent, (NoteRow, SuggestionTile)):
                    target = parent
                    break
                parent = parent.parentWidget()
            self.ensureWidgetVisible(target, 0, 8)

        scroll()
        QTimer.singleShot(0, scroll)

    def editing(self) -> str:
        return self._editing

    def stop_editing(self) -> None:
        self._editing = ""
        for row in self._rows:
            row.set_editing(False)

    def deleted_row(self) -> NoteRow | None:
        return next((row for row in self._rows if row.info().deleted), None)

    # ---- the owner's clicks -------------------------------------------------------------------

    def _row(self, entry_id: str) -> NoteRow | None:
        return next((row for row in self._rows if row.info().id == entry_id), None)

    def _make_row(self, info: hud.MemoryRowInfo) -> NoteRow:
        row = NoteRow(info)
        row.set_read_only(self._read_only)
        row.editRequested.connect(self.edit_row)
        row.editSubmitted.connect(self.edited)
        row.editCancelled.connect(lambda _entry_id: self.stop_editing())
        row.deleteRequested.connect(self.deleted)
        row.undoRequested.connect(self.undoDelete)
        return row

    def _submit(self) -> None:
        text = " ".join(self.input.text().split())
        if text and not self._read_only:
            self.added.emit(text)

    def _clear_filter(self) -> None:
        self.set_filter("")
        self.filterCleared.emit()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        if self._kept is not None and not self._suggestions and not self.window().isMinimized():
            self._kept = None                       # seen: the next visit opens on the Add field
            self.suggestion_tile.set_kept(None)
            self._tile_gap.setVisible(False)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(COLUMN_MAX + 2 * holo.GUTTER_SIDE, 480)
