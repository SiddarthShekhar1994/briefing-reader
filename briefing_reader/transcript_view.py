"""The Transcript drawer's conversation in the hologram look (HOLOGRAM_SPEC 2, 6, 11.4, 14.11; round 2, part 3a).

Public API
==========

``TranscriptLog``
    The whole conversation (the old JARVIS tab) inside Home's Transcript drawer (home.Drawer: slides in from
    the right, 420 wide, Esc or its close button closes it and the focus goes back to the opener). The same
    API as hud.ConversationLog (``add``, ``update``, ``entry``, ``entries``, ``entry_widgets``, ``follow_from``,
    ``clear``, ``set_sub_filter``, ``linkClicked(link_id)``); only the entries look new. Long answers, the
    plan list and the brief live here in full.

``TranscriptEntry``
    One entry: the label ``YOU`` or ``JARVIS - 9:51 AM`` (JetBrains Mono 11, caps; JARVIS in ``holo``, YOU and
    the time in ``ink-4``), the text in body type (Sora 15 / 24, plain text, wrapped, never cut, selectable;
    ``ink-1`` for Jarvis, ``ink-2`` for the owner's words, ``alert-tx`` for a failure; never amber: amber is
    for a decision on a tile or a button), the quiet sub line (readouts are left out by the filter: they
    live in LIVE) and the link as a cyan text link (``link_button``: Undo, Plan my day, Prepare brief,
    Remember this, See every step ...). The old attribute names stay (``role_label``, ``time_label``,
    ``text_label``, ``sub_label``, ``link_button``) so every caller keeps working.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QHBoxLayout, QWidget

from . import holo, hud

DOT = "\u00b7"
_TONE_INK = {hud.TONE_IDLE: holo.INK_2, hud.TONE_WORKING: holo.INK_2, hud.TONE_DONE: holo.INK_1,
             hud.TONE_WARN: holo.INK_1, hud.TONE_ERROR: holo.ALERT_TX, hud.TONE_GOOD: holo.INK_1}
ENTRY_GAP = 18           # between two entries


class TranscriptEntry(hud._ConversationEntry):
    """One conversation entry in the hologram look; see the module docstring."""

    def __init__(self, entry_id: int, role: str, stamp: str, text: str, tone: str, sub: str, link_text: str,
                 link_id: str, sub_filter: Any = None) -> None:
        super().__init__(entry_id, role, stamp, text, tone, sub, link_text, link_id, sub_filter=sub_filter)
        jarvis = self.role == hud.ROLE_JARVIS
        label = holo.font("label")
        self.role_label.setFont(label)
        hud.set_label_color(self.role_label, holo.HOLO if jarvis else holo.INK_4)
        self.time_label.setFont(label)
        self.time_label.setText(f"{DOT} {stamp}")
        hud.set_label_color(self.time_label, holo.INK_4)
        self.time_label.setVisible(jarvis)          # spec 11.4: "YOU" / "JARVIS - 9:51 AM"
        header = self.layout().itemAt(0).layout()
        if header is not None:
            header.setSpacing(6)
        self.layout().setSpacing(6)
        self.text_label.setFont(holo.font("body"))
        self.sub_label.setFont(holo.font("readout"))
        hud.set_label_color(self.sub_label, holo.INK_4)
        old = self.link_button
        link = holo.HoloButton(link_text, holo.LINK_CYAN, size=holo.SIZE_SM, parent=self)
        link.clicked.connect(lambda: self.linkClicked.emit(self._link_id))
        row = self.layout().itemAt(self.layout().count() - 1).layout()
        if isinstance(row, QHBoxLayout):
            row.setContentsMargins(0, 0, 0, 0)
            row.replaceWidget(old, link)
        link.setVisible(bool(link_text))
        old.hide()
        old.deleteLater()
        self.link_button = link
        self._apply_ink()
        self._update_accessible()

    def _apply_ink(self) -> None:
        ink = _TONE_INK.get(self.tone(), holo.INK_1) if self.role == hud.ROLE_JARVIS else holo.INK_2
        hud.set_label_color(self.text_label, ink)

    def set_content(self, *, text: str | None = None, sub: str | None = None, tone: str | None = None,
                    link_text: str | None = None, link_id: str | None = None) -> None:
        super().set_content(text=text, sub=sub, tone=tone, link_text=link_text, link_id=link_id)
        if tone is not None:
            self._apply_ink()


class TranscriptLog(hud.ConversationLog):
    """The conversation in the Transcript drawer; see the module docstring."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.empty_label.setFont(holo.font("body_sm"))
        hud.set_label_color(self.empty_label, holo.INK_3)
        layout = self.widget().layout()
        layout.setSpacing(ENTRY_GAP)
        layout.setContentsMargins(12, 6, 12, 12)

    def _make_entry(self, *args: Any, **kwargs: Any) -> TranscriptEntry:
        return TranscriptEntry(*args, **kwargs)
