"""The Follow-ups screen in the hologram look (HOLOGRAM_SPEC 2, 5-7, 13, 15, 16.6; round 2, part 2b).

Public API
==========

``FollowupsStage``
    The Follow-ups screen: a frameless scroll area whose column (at most COLUMN_MAX wide, centred) is the
    quiet panel of memory_view. In it: the FOLLOW-UPS head with the "..." icon button
    (``followups_menu_button``; ``followupsMenuRequested()``: the app's menu with the actions it already has,
    Rescan, Show hidden and Turn off on this PC); the header readout (``followups_meta``, its text as the app
    gives it, shown in capitals: ``2 WAITING - 1 OWED - UPDATED 10:41 AM``, ``SCANNING...``, ``OFF``); the fix
    lines (each with its link: ``followupsLinkClicked(link_id)``); the groups WAITING ON YOU (an amber label,
    rows with a 3 px amber bar) and OWED TO YOU (an ``ink-kicker`` label, a 3 px ``holo`` bar), each showing
    its first ``shown`` rows with "+N more" / "Show fewer" under it (``followup_more(kind)``); and the empty
    line. ``set_followups(info)`` (a hud.FollowupsInfo; None: nothing shows) fills it.

    One row is expanded, the focal row: the oldest WAITING ON YOU row (the app lists them oldest first)
    until the owner opens or closes a row himself (a click, Enter or Space on its head; ``toggle(row_id)``).
    Its actions: Draft a reply / Draft a nudge (cyan go) - or Show the draft (ghost) while a draft for its
    thread waits - ``draftRequested(row_id)``; Tell Jarvis what to say... ``wordsRequested``; Hide (ghost)
    ``hideRequested`` (the app asks for how long); Open in Gmail (an icon) ``openRequested``. A row whose
    thread is being checked after a reply (``REPLIED - CHECKING``) does not open. A row's whole menu (the
    right button, the Menu key or Shift+F10): ``followupActivated(row_id)``. Row tags are steel badges
    (``DRAFT WAITING``, ``REPLIED - CHECKING``); the age is a readout at the right (``20H``, ``3D``).

``FollowupTile``
    One row (``info()``, ``head``, ``draft_button``, ``words_button``, ``hide_button``, ``open_button``,
    ``expanded()``).

Nothing here drafts, sends, hides or opens anything: the buttons only say which row; the app does what it
did before (a draft is only a draft, on its normal approval card with the undo countdown; Open in Gmail opens
the thread link the app builds). Texts from mail are painted as plain text, never as HTML.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from . import holo, hud
from .memory_view import COLUMN_MAX, PANEL_PAD, PanelFrame, SectionHead, body_label

FOLLOWUPS_TITLE = "FOLLOW-UPS"
DRAFT_TEXT = {hud.FOLLOWUP_WAITING: "Draft a reply", hud.FOLLOWUP_OWED: "Draft a nudge"}
SHOW_DRAFT_TEXT = "Show the draft"
WORDS_TEXT = "Tell Jarvis what to say..."
HIDE_TEXT = "Hide"
OPEN_TEXT = "Open in Gmail"
MENU_NAME = "Follow-ups menu"
MENU_TIP = "Follow-ups: rescan, hidden rows, turn off on this PC"
CHECKING_TIP = "Checking the thread after your reply"
MORE_TEXT = "+{count} more"
FEWER_TEXT = "Show fewer"
_BAR = {hud.FOLLOWUP_WAITING: holo.AMBER, hud.FOLLOWUP_OWED: holo.HOLO}
_GROUP_COLORS = {hud.FOLLOWUP_WAITING: holo.AMBER, hud.FOLLOWUP_OWED: holo.INK_KICKER}
_HEAD_PAD = (16, 10, 12, 10)          # left, top, right, bottom


def _who_font() -> QFont:
    return hud.display_font(15, 500, 0.01)


class FollowupHead(QAbstractButton):
    """A row's head: who (Chakra 15), the tag badge and the age at the right, the subject under it (Sora 13,
    ``ink-3``, elided). A click, Enter or Space opens or closes the row (``clicked``)."""

    HEIGHT = _HEAD_PAD[1] + 22 + 2 + 20 + _HEAD_PAD[3]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._info = hud.FollowupRowInfo("", hud.FOLLOWUP_WAITING, "")
        self._expanded = False
        self._focus_ring = False
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def set_info(self, info: hud.FollowupRowInfo, expanded: bool) -> None:
        self._info, self._expanded = info, bool(expanded)
        words = [info.who, info.age, info.state_text, info.subject]
        self.setAccessibleName(", ".join(word for word in words if word))
        self.setAccessibleDescription("expanded" if self._expanded else "collapsed")
        self.setToolTip(hud.plain_tooltip(info.tooltip or info.subject))
        self.setCursor(Qt.CursorShape.ArrowCursor if info.state == hud.FOLLOWUP_CHECKING
                       else Qt.CursorShape.PointingHandCursor)
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(260, self.HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(160, self.HEIGHT)

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            return
        super().keyPressEvent(event)

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
        info = self._info
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = QRectF(self.rect())
            if self.underMouse() and not self._expanded and info.state != hud.FOLLOWUP_CHECKING:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(holo.alpha(holo.INK_1, 0.04))
                painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
            left, top = _HEAD_PAD[0], _HEAD_PAD[1]
            right = rect.width() - _HEAD_PAD[2]
            age_font = holo.font("readout")
            age = holo.role_text("readout", info.age)
            age_w = QFontMetricsF(age_font).horizontalAdvance(age)
            line_mid = top + 11
            painter.setFont(age_font)
            painter.setPen(QColor(holo.INK_3))
            painter.drawText(QRectF(right - age_w - 1, top, age_w + 2, 22),
                             int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), age)
            right -= age_w + (8 if age else 0)
            if info.state_text:
                tag = info.state_text
                room = right - left - 60
                if room > 40:
                    width = holo.status_badge_width(tag)
                    if width > room:
                        tag = QFontMetricsF(holo.font("badge")).elidedText(
                            holo.role_text("badge", tag), Qt.TextElideMode.ElideRight, room - 14)
                        width = holo.status_badge_width(tag)
                    holo.paint_status_badge(painter, QPointF(right - width, line_mid), tag, holo.TONE_STEEL)
                    right -= width + 8
            who_font = _who_font()
            metrics = QFontMetricsF(who_font)
            painter.setFont(who_font)
            painter.setPen(QColor(holo.INK_1 if info.state != hud.FOLLOWUP_CHECKING else holo.INK_3))
            painter.drawText(QRectF(left, top, max(0.0, right - left), 22),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             metrics.elidedText(info.who, Qt.TextElideMode.ElideRight, max(0.0, right - left)))
            sub_font = holo.font("body_sm")
            width = rect.width() - _HEAD_PAD[2] - left
            painter.setFont(sub_font)
            painter.setPen(QColor(holo.INK_3))
            painter.drawText(QRectF(left, top + 24, max(0.0, width), 20),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             QFontMetricsF(sub_font).elidedText(info.subject, Qt.TextElideMode.ElideRight,
                                                                max(0.0, width)))
            if self.hasFocus() and self._focus_ring:
                holo.paint_focus_ring(painter, rect.adjusted(3, 3, -3, -3), 8)
        finally:
            painter.end()


class FollowupTile(QWidget):
    """One follow-up row; see the module docstring."""

    toggled = Signal(str)
    activated = Signal(str)            # the row's whole menu
    draftRequested = Signal(str)
    wordsRequested = Signal(str)
    hideRequested = Signal(str)
    openRequested = Signal(str)

    def __init__(self, info: hud.FollowupRowInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._info = info
        self._expanded = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.head = FollowupHead()
        self.head.clicked.connect(lambda: self.toggled.emit(self._info.row_id))
        self.head.customContextMenuRequested.connect(lambda _pos: self.activated.emit(self._info.row_id))
        self.draft_button = holo.HoloButton(DRAFT_TEXT[hud.FOLLOWUP_WAITING], holo.GO, size=holo.SIZE_SM,
                                            icon="edit")
        self.words_button = holo.HoloButton(WORDS_TEXT, holo.GHOST, size=holo.SIZE_SM)
        self.hide_button = holo.HoloButton(HIDE_TEXT, holo.GHOST, size=holo.SIZE_SM)
        self.open_button = holo.HoloButton(OPEN_TEXT, holo.ICON, icon="popout", diameter=32)
        self.open_button.setToolTip(OPEN_TEXT)
        self.draft_button.clicked.connect(lambda: self.draftRequested.emit(self._info.row_id))
        self.words_button.clicked.connect(lambda: self.wordsRequested.emit(self._info.row_id))
        self.hide_button.clicked.connect(lambda: self.hideRequested.emit(self._info.row_id))
        self.open_button.clicked.connect(lambda: self.openRequested.emit(self._info.row_id))
        self._actions_holder = QWidget()
        actions = QHBoxLayout(self._actions_holder)
        actions.setContentsMargins(_HEAD_PAD[0] - holo.HoloButton.MARGIN, 0, 6, 8)
        actions.setSpacing(holo.BUTTON_GAP - 2 * holo.HoloButton.MARGIN)
        for button in (self.draft_button, self.words_button, self.hide_button):
            actions.addWidget(button)
        actions.addStretch(1)
        actions.addWidget(self.open_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.head)
        layout.addWidget(self._actions_holder)
        self.set_info(info, False)

    def info(self) -> hud.FollowupRowInfo:
        return self._info

    def expanded(self) -> bool:
        return self._expanded

    def checking(self) -> bool:
        return self._info.state == hud.FOLLOWUP_CHECKING

    def set_info(self, info: hud.FollowupRowInfo, expanded: bool | None = None) -> None:
        self._info = info
        if expanded is not None:
            self._expanded = bool(expanded) and not self.checking()
        waiting_draft = info.state == hud.FOLLOWUP_DRAFT_WAITING
        self.draft_button.setText(SHOW_DRAFT_TEXT if waiting_draft else DRAFT_TEXT.get(info.kind,
                                                                                       DRAFT_TEXT["waiting"]))
        self.draft_button.set_variant(holo.GHOST if waiting_draft else holo.GO)
        self.draft_button.set_icon_name(None if waiting_draft else "edit")
        self.draft_button.updateGeometry()
        who = info.who or "this thread"
        for button, word in ((self.draft_button, self.draft_button.text()), (self.words_button, WORDS_TEXT),
                             (self.hide_button, HIDE_TEXT), (self.open_button, OPEN_TEXT)):
            button.setAccessibleName(f"{word}: {who}")
        self.head.set_info(info, self._expanded)
        self._actions_holder.setVisible(self._expanded)
        self.updateGeometry()
        self.update()

    def set_expanded(self, expanded: bool) -> None:
        self.set_info(self._info, expanded)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            if self._expanded:
                painter.setPen(QPen(holo.LINE, 1.0))
                painter.setBrush(QColor(holo.TILE_FOCUS))
                painter.drawRoundedRect(rect, 8, 8)
            bar = holo.STEEL if self.checking() else _BAR.get(self._info.kind, holo.HOLO)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(bar))
            painter.drawRoundedRect(QRectF(0, 10, 3, max(0.0, self.height() - 20)), 1.5, 1.5)
        finally:
            painter.end()


class FollowupsStage(QScrollArea):
    """The Follow-ups screen; see the module docstring."""

    followupActivated = Signal(str)
    followupsLinkClicked = Signal(str)
    followupsMenuRequested = Signal()
    draftRequested = Signal(str)
    wordsRequested = Signal(str)
    hideRequested = Signal(str)
    openRequested = Signal(str)

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
        self.head = SectionHead("followups", FOLLOWUPS_TITLE)
        self.followups_menu_button = holo.HoloButton(MENU_NAME, holo.ICON, icon="more", diameter=32)
        self.followups_menu_button.setToolTip(MENU_TIP)
        self.followups_menu_button.clicked.connect(self.followupsMenuRequested.emit)
        self.head.layout().addWidget(self.followups_menu_button, 0, Qt.AlignmentFlag.AlignVCenter)
        meta_font = holo.font("label")
        meta_font.setCapitalization(QFont.Capitalization.AllUppercase)
        self.followups_meta = hud.make_label("", meta_font, holo.INK_4, wrap=True, name="followupsMeta")
        self._notes_box = QWidget()
        self._notes = QVBoxLayout(self._notes_box)
        self._notes.setContentsMargins(0, 6, 0, 0)
        self._notes.setSpacing(2)
        self.followups_empty = body_label("", "body_sm", holo.INK_3, name="followupsEmpty")
        self.followups_empty.hide()
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(*PANEL_PAD)
        layout.setSpacing(0)
        layout.addWidget(self.head)
        layout.addSpacing(2)
        layout.addWidget(self.followups_meta)
        layout.addWidget(self._notes_box)
        self._rows: dict[str, list[FollowupTile]] = {hud.FOLLOWUP_WAITING: [], hud.FOLLOWUP_OWED: []}
        self._labels: dict[str, QLabel] = {}
        self._layouts: dict[str, QVBoxLayout] = {}
        self._more: dict[str, holo.HoloButton] = {}
        self._groups: dict[str, QWidget] = {}
        self._show_all = {hud.FOLLOWUP_WAITING: False, hud.FOLLOWUP_OWED: False}
        for kind, label_text in ((hud.FOLLOWUP_WAITING, "WAITING ON YOU"), (hud.FOLLOWUP_OWED, "OWED TO YOU")):
            group = QWidget()
            box = QVBoxLayout(group)
            box.setContentsMargins(0, 18, 0, 0)
            box.setSpacing(0)
            label = body_label(label_text, "label", _GROUP_COLORS[kind], wrap=False)
            rows = QVBoxLayout()
            rows.setContentsMargins(0, 4, 0, 0)
            rows.setSpacing(8)
            more = holo.HoloButton("", holo.LINK_CYAN)
            more.clicked.connect(lambda _checked=False, which=kind: self._toggle_more(which))
            box.addWidget(label)
            box.addLayout(rows)
            box.addWidget(more, 0, Qt.AlignmentFlag.AlignLeft)
            layout.addWidget(group)
            self._groups[kind], self._labels[kind], self._layouts[kind], self._more[kind] = group, label, rows, more
        layout.addSpacing(12)
        layout.addWidget(self.followups_empty)
        layout.addStretch(1)
        self._info: hud.FollowupsInfo | None = None
        self._expanded: str | None = None      # the row the owner opened
        self._auto = True                      # the oldest WAITING ON YOU row is the open one
        self.set_followups(None)

    # ---- what the app shows -----------------------------------------------------------------

    def set_followups(self, info: hud.FollowupsInfo | None) -> None:
        """The section (None: nothing; the screen's Dock slot is hidden then)."""
        self._info = info
        self.panel.setVisible(info is not None)
        if info is None:
            for kind in self._rows:
                self._sync_rows(kind, ())
                self._groups[kind].hide()
            self._sync_notes(())
            self.followups_meta.setText("")
            self.followups_empty.hide()
            return
        self.followups_meta.setText(info.meta)
        hud.set_label_color(self.followups_meta, holo.INK_4 if info.meta_color == hud.TEXT_DIM else holo.INK_2)
        self.followups_meta.setVisible(bool(info.meta))
        self._sync_notes(info.notes)
        all_rows = info.waiting + info.owed
        ids = {row.row_id for row in all_rows}
        if self._expanded is not None and self._expanded not in ids:
            self._expanded, self._auto = None, True          # the row the owner opened went
        open_id = self.expanded_id()
        any_rows = False
        for kind, rows, label_text in ((hud.FOLLOWUP_WAITING, info.waiting, info.waiting_label),
                                       (hud.FOLLOWUP_OWED, info.owed, info.owed_label)):
            shown_count = len(rows) if self._show_all[kind] else max(1, int(info.shown))
            shown = rows[:shown_count]
            if open_id and not any(row.row_id == open_id for row in shown):
                shown = tuple(shown) + tuple(row for row in rows if row.row_id == open_id)
            self._sync_rows(kind, shown)
            self._labels[kind].setText(holo.role_text("label", label_text))
            self._groups[kind].setVisible(bool(rows))
            more = self._more[kind]
            hidden = len(rows) - shown_count
            more.setVisible(hidden > 0 or (self._show_all[kind] and len(rows) > max(1, int(info.shown))))
            more.setText(FEWER_TEXT if self._show_all[kind] else MORE_TEXT.format(count=max(0, hidden)))
            more.updateGeometry()
            any_rows = any_rows or bool(rows)
        self.followups_empty.setText(info.empty_text)
        self.followups_empty.setVisible(not any_rows and bool(info.empty_text))
        self._chain_tab_order()

    def followups_visible(self) -> bool:
        return self._info is not None

    def followup_rows(self) -> list[FollowupTile]:
        return [row for kind in (hud.FOLLOWUP_WAITING, hud.FOLLOWUP_OWED) for row in self._rows[kind]]

    def followup_row(self, row_id: str) -> FollowupTile | None:
        return next((row for row in self.followup_rows() if row.info().row_id == row_id), None)

    def followup_notes(self) -> list[str]:
        return [label.text() for label in self._note_labels()]

    def followup_more(self, kind: str) -> holo.HoloButton:
        return self._more[kind]

    def followups_section(self) -> QWidget:
        return self.panel

    def reveal_followups(self) -> None:
        """The section's top in view."""
        self.verticalScrollBar().setValue(0)

    def expanded_id(self) -> str:
        """The open row: the one the owner opened, else (until he closes one) the oldest WAITING ON YOU row
        that is not being checked; "" when none is open."""
        info = self._info
        if info is None:
            return ""
        rows = {row.row_id: row for row in info.waiting + info.owed}
        if not self._auto:
            row = rows.get(self._expanded or "")
            return row.row_id if row is not None and row.state != hud.FOLLOWUP_CHECKING else ""
        first = next((row for row in info.waiting if row.state != hud.FOLLOWUP_CHECKING), None)
        return first.row_id if first is not None else ""

    def toggle(self, row_id: str) -> None:
        """Open ``row_id`` (closing the open one) or close it when it is the open one (the owner's click)."""
        row = self.followup_row(row_id)
        if row is None or row.checking():
            return
        self._auto = False
        self._expanded = None if self.expanded_id() == row_id else row_id
        self._sync_expanded()

    # ---- inside -------------------------------------------------------------------------------

    def _sync_expanded(self) -> None:
        open_id = self.expanded_id()
        for row in self.followup_rows():
            row.set_expanded(row.info().row_id == open_id)

    def _sync_rows(self, kind: str, infos: Sequence[hud.FollowupRowInfo]) -> None:
        rows, layout = self._rows[kind], self._layouts[kind]
        open_id = self.expanded_id()
        while len(rows) > len(infos):
            row = rows.pop()
            layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        for index, info in enumerate(infos):
            expanded = info.row_id == open_id
            if index < len(rows):
                if rows[index].info() != info or rows[index].expanded() != expanded:
                    rows[index].set_info(info, expanded)
                continue
            row = FollowupTile(info)
            row.set_expanded(expanded)
            row.toggled.connect(self.toggle)
            row.activated.connect(self.followupActivated)
            row.draftRequested.connect(self._draft)
            row.wordsRequested.connect(self._words)
            row.hideRequested.connect(self._hide)
            row.openRequested.connect(self.openRequested)
            layout.addWidget(row)
            rows.append(row)

    def _guarded(self, row_id: str) -> bool:
        row = self.followup_row(row_id)
        return row is not None and not row.checking()

    def _draft(self, row_id: str) -> None:
        if self._guarded(row_id):
            self.draftRequested.emit(row_id)

    def _words(self, row_id: str) -> None:
        if self._guarded(row_id):
            self.wordsRequested.emit(row_id)

    def _hide(self, row_id: str) -> None:
        if self._guarded(row_id):
            self.hideRequested.emit(row_id)

    def _note_labels(self) -> list[QLabel]:
        labels = []
        for index in range(self._notes.count()):
            widget = self._notes.itemAt(index).widget()
            label = widget.findChild(QLabel) if widget is not None else None
            if label is not None:
                labels.append(label)
        return labels

    def _sync_notes(self, notes: Sequence[tuple[str, str, str]]) -> None:
        while self._notes.count():
            item = self._notes.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        for text, link_text, link_id in notes:
            line = QWidget()
            row = QHBoxLayout(line)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            row.addWidget(body_label(text, "body_sm", holo.INK_2), 1, Qt.AlignmentFlag.AlignVCenter)
            if link_text and link_id:
                button = holo.HoloButton(link_text, holo.LINK_CYAN)
                button.clicked.connect(lambda _checked=False, target=link_id: self.followupsLinkClicked.emit(target))
                row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
            self._notes.addWidget(line)
        self._notes_box.setVisible(bool(notes))

    def _toggle_more(self, kind: str) -> None:
        self._show_all[kind] = not self._show_all[kind]
        if self._info is not None:
            self.set_followups(self._info)

    def _chain_tab_order(self) -> None:
        """Tab goes down the screen: the menu, the fix lines' links, then each row's head and (open) actions."""
        chain: list[QWidget] = [self.followups_menu_button]
        for kind in (hud.FOLLOWUP_WAITING, hud.FOLLOWUP_OWED):
            for row in self._rows[kind]:
                chain += [row.head, row.draft_button, row.words_button, row.hide_button, row.open_button]
            chain.append(self._more[kind])
        for first, second in zip(chain, chain[1:]):
            QWidget.setTabOrder(first, second)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(COLUMN_MAX + 2 * holo.GUTTER_SIDE, 480)
