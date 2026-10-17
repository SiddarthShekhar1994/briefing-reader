"""The Dock (HOLOGRAM_SPEC section 11.1): the 64 px glyph rail on the window's left edge.

Slots, top to bottom: Home (the live mini orb), a hairline, Briefing, Approvals, Live, Memory,
Follow-ups, flexible space, Settings. Glyphs only; labels show as a tooltip right of the button
(after 400 ms of hover, at once on keyboard focus) with the shortcut (``Approvals  CTRL+3``). The
current slot carries a cyan highlight that slides between slots in 200 ms. Badges: amber counts
(something waits for the owner: Approvals, Follow-ups) and a cyan dot (new: Briefing, a Memory
suggestion, Live while Jarvis works or a Send is in its hidden hold; it never blinks). Count badges pop in when a
count goes from 0 to more; dots ping three times on arrival (neither in reduced motion).

The Dock is one tab stop (roving focus): Up / Down move between the visible slots, Home / End
jump, Enter / Space open. Every button's accessible name carries its signal ("Approvals, 2 waiting",
"Follow-ups, 2 waiting on you", "Briefing, new", "Memory, 1 suggestion", "Live, Jarvis is working",
"Live, Jarvis is sending"); the current one's description is "Current screen".

Public API
==========

Constants: slot keys ``HOME``, ``BRIEFING``, ``APPROVALS``, ``LIVE``, ``MEMORY``, ``FOLLOWUPS``,
``SETTINGS`` (``SLOTS`` in order), ``LABELS``, ``SHORTCUTS`` (Ctrl+1 .. Ctrl+6, Ctrl+,), ``GLYPHS``;
Live activity ``LIVE_IDLE``, ``LIVE_WORKING``, ``LIVE_HOLD``.

``Dock(parent=None)``
    Signal ``slotActivated(str)``: a slot was clicked, opened with Enter / Space or its shortcut
    (screen slots become current first; Settings does not). ``set_current(key, animate=True)``,
    ``current()``; ``set_slot_visible(key, bool)`` (Memory / Follow-ups when the bundle is off; a
    hidden current slot loses its highlight until ``set_current`` moves it), ``is_slot_visible``,
    ``visible_slots()``; signals: ``set_briefing_new(bool)``, ``set_approvals(n)``,
    ``set_live_activity(LIVE_*)``, ``set_memory_suggestions(n)`` (pending suggestions: "Memory, 1
    suggestion"), ``set_memory_new(bool)`` (a note was saved: "Memory, new"), ``set_followups(n)``;
    ``set_orb_look(base, muted=False, offline=False)`` for the Home slot's mini orb; ``button(key)``,
    ``mini_orb()``, ``accessible_name(key)``, ``highlight_rect()``, ``tooltip()`` (the shared
    ``DockTooltip``), ``install_shortcuts(widget) -> list[QShortcut]`` (window-wide shortcuts that
    open visible slots; the app decides whether to install them).
"""

from __future__ import annotations

from typing import Any

import shiboken6
from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import QAbstractButton, QVBoxLayout, QWidget

from . import holo
from . import orb as orb_mod

HOME = "home"
BRIEFING = "briefing"
APPROVALS = "approvals"
LIVE = "live"
MEMORY = "memory"
FOLLOWUPS = "followups"
SETTINGS = "settings"
SLOTS = (HOME, BRIEFING, APPROVALS, LIVE, MEMORY, FOLLOWUPS, SETTINGS)
LABELS = {HOME: "Home", BRIEFING: "Briefing", APPROVALS: "Approvals", LIVE: "Live", MEMORY: "Memory",
          FOLLOWUPS: "Follow-ups", SETTINGS: "Settings"}
SHORTCUTS = {HOME: "Ctrl+1", BRIEFING: "Ctrl+2", APPROVALS: "Ctrl+3", LIVE: "Ctrl+4", MEMORY: "Ctrl+5",
             FOLLOWUPS: "Ctrl+6", SETTINGS: "Ctrl+,"}
GLYPHS = {BRIEFING: "briefing", APPROVALS: "approvals", LIVE: "live", MEMORY: "memory", FOLLOWUPS: "followups",
          SETTINGS: "settings"}
LIVE_IDLE = "idle"
LIVE_WORKING = "working"
LIVE_HOLD = "hold"         # a Send's / Cancel's hidden hold (no countdown, no blink)

BUTTON = holo.DOCK_BUTTON
PAD_X = (holo.DOCK_WIDTH - BUTTON) // 2     # 10
PAD_Y = 16
GLYPH = 20


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


class DockButton(QAbstractButton):
    """One 44 x 44 slot: glyph (or the mini orb), hover fill, badges. The Dock paints the highlight."""

    def __init__(self, key: str, dock: Dock) -> None:
        super().__init__(dock)
        self._key = key
        self._dock = dock
        self._hover = 0.0
        self._current_mix = 0.0
        self._count = 0
        self._dot = False
        self._pop = 1.0
        self._ping = -1.0
        self._keyboard = False
        self.setFixedSize(BUTTON, BUTTON)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName(LABELS[key])
        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(holo.FADE_MS)
        self._hover_anim.valueChanged.connect(self._on_hover)
        self._pop_anim = QVariantAnimation(self)
        self._pop_anim.setStartValue(0.0)
        self._pop_anim.setEndValue(1.0)
        self._pop_anim.setDuration(holo.POP_MS)
        self._pop_anim.setEasingCurve(holo.qt_easing(holo.EASE_BACK))
        self._pop_anim.valueChanged.connect(self._on_pop)
        self._ping_anim = QVariantAnimation(self)
        self._ping_anim.setStartValue(0.0)
        self._ping_anim.setEndValue(1.0)
        self._ping_anim.setDuration(holo.PING_MS)
        self._ping_anim.setLoopCount(3)
        self._ping_anim.valueChanged.connect(self._on_ping)
        self._ping_anim.finished.connect(lambda: self._on_ping(-1.0))
        self.mini: orb_mod.MiniOrb | None = None
        if key == HOME:
            self.mini = orb_mod.MiniOrb(self)
            self.mini.move((BUTTON - orb_mod.MINI) // 2, (BUTTON - orb_mod.MINI) // 2)

    # ---- state -------------------------------------------------------------------

    def key(self) -> str:
        return self._key

    def count(self) -> int:
        return self._count

    def has_dot(self) -> bool:
        return self._dot

    def is_popping(self) -> bool:
        return self._pop_anim.state() == QVariantAnimation.State.Running

    def is_pinging(self) -> bool:
        return self._ping_anim.state() == QVariantAnimation.State.Running

    def has_focus_ring(self) -> bool:
        return self._keyboard and self.hasFocus()

    def set_count(self, count: int) -> None:
        count = max(0, int(count))
        if count == self._count:
            return
        arrived = self._count == 0 and count > 0
        self._count = count
        if arrived and not holo.reduced_motion() and self.isVisible():
            self._pop_anim.stop()
            self._pop_anim.start()
        self.update()

    def set_dot(self, on: bool, *, ping: bool = True) -> None:
        on = bool(on)
        if on == self._dot:
            return
        self._dot = on
        if on and ping and not holo.reduced_motion() and self.isVisible():
            self._ping_anim.stop()
            self._ping_anim.start()
        elif not on:
            self._ping_anim.stop()
            self._ping = -1.0
        self.update()

    def set_current_mix(self, value: float) -> None:
        if value != self._current_mix:
            self._current_mix = value
            self.update()

    def _on_hover(self, value: Any) -> None:
        self._hover = float(value)
        self.update()

    def _on_pop(self, value: Any) -> None:
        self._pop = float(value)
        self.update()

    def _on_ping(self, value: Any) -> None:
        self._ping = float(value)
        self.update()

    def _fade_hover(self, target: float) -> None:
        self._hover_anim.stop()
        if holo.reduced_motion():
            self._on_hover(target)
            return
        self._hover_anim.setStartValue(self._hover)
        self._hover_anim.setEndValue(target)
        self._hover_anim.start()

    # ---- events --------------------------------------------------------------------

    def enterEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().enterEvent(event)
        self._fade_hover(1.0)
        self._dock._hover_started(self)

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().leaveEvent(event)
        self._fade_hover(0.0)
        self._dock._hover_ended(self)

    def focusInEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._keyboard = holo.keyboard_focus(event.reason(), self._keyboard)
        super().focusInEvent(event)
        self._dock._focus_changed(self, True, self._keyboard)

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.reason() != Qt.FocusReason.ActiveWindowFocusReason:
            self._keyboard = False
        super().focusOutEvent(event)
        self._dock._focus_changed(self, False, False)

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if self._dock._key_pressed(self, event.key()):
            event.accept()
            return
        super().keyPressEvent(event)

    # ---- painting ------------------------------------------------------------------

    def glyph_color(self) -> QColor:
        rest = holo.mix(holo.INK_4, holo.HOLO, self._current_mix)
        return holo.mix(rest, holo.INK_1, self._hover)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = QRectF(self.rect())
            if self._hover > 0.0:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(holo.alpha(holo.INK_1, 0.06 * self._hover))
                painter.drawRoundedRect(rect, holo.RADIUS_DOCK_BUTTON, holo.RADIUS_DOCK_BUTTON)
            glyph = GLYPHS.get(self._key)
            if glyph:
                box = QRectF(0, 0, GLYPH, GLYPH)
                box.moveCenter(rect.center())
                holo.paint_icon(painter, box, glyph, self.glyph_color(), stroke=1.6)
            self._paint_badges(painter)
        finally:
            painter.end()

    def _paint_badges(self, painter: QPainter) -> None:
        if self._count > 0:
            holo.paint_count_badge(painter, QPointF(BUTTON - 4, 4), self._count, scale=self._pop)
        elif self._dot:
            center = QPointF(BUTTON - 8 - holo.NEW_DOT / 2, 8 + holo.NEW_DOT / 2)
            if self._ping >= 0.0:
                holo.paint_ping(painter, center, self._ping)
            holo.paint_new_dot(painter, center)


class DockTooltip(QWidget):
    """The shared Dock label: `raised` bubble, label + shortcut, fading in 180 ms from 6 px left."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._label = ""
        self._shortcut = ""
        self._reveal = 0.0
        self._anchor = QPoint()
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(holo.TOOLTIP_FADE_MS)
        self._anim.setEasingCurve(holo.qt_easing(holo.CSS_EASE))
        self._anim.valueChanged.connect(self._on_reveal)
        self.hide()

    def text(self) -> tuple[str, str]:
        return self._label, self._shortcut

    def _metrics(self) -> tuple[float, float]:
        label = QFontMetricsF(holo.font("tooltip")).horizontalAdvance(self._label)
        shortcut = QFontMetricsF(holo.font("shortcut")).horizontalAdvance(self._shortcut)
        return label, shortcut

    def show_at(self, anchor: QPoint, label: str, shortcut: str) -> None:
        """Show left-centred at ``anchor`` (parent coordinates); it fades in over 180 ms."""
        self._label, self._shortcut = label, shortcut.upper()
        label_w, shortcut_w = self._metrics()
        width = round(9 + label_w + 8 + shortcut_w + 9) + 2
        self.resize(width, 26)
        self._anchor = anchor
        self.move(anchor.x(), anchor.y() - self.height() // 2)
        self.raise_()
        self.show()
        self._anim.stop()
        if holo.reduced_motion():
            self._on_reveal(1.0)
            return
        self._anim.setStartValue(self._reveal)
        self._anim.setEndValue(1.0)
        self._anim.start()

    def hide_tip(self) -> None:
        self._anim.stop()
        self._reveal = 0.0
        self.hide()

    def reveal(self) -> float:
        return self._reveal

    def _on_reveal(self, value: Any) -> None:
        self._reveal = float(value)
        self.update()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        if self._reveal <= 0.0:
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setOpacity(self._reveal)
            painter.translate(-6.0 * (1.0 - self._reveal), 0.0)
            label_w, shortcut_w = self._metrics()
            bubble = QRectF(0.5, 0.5, 9 + label_w + 8 + shortcut_w + 9, self.height() - 1.0)
            painter.setPen(QPen(holo.LINE_STRONG, 1.0))
            painter.setBrush(QColor(holo.RAISED))
            painter.drawRoundedRect(bubble, holo.RADIUS_TOOLTIP, holo.RADIUS_TOOLTIP)
            painter.setFont(holo.font("tooltip"))
            painter.setPen(QColor(holo.INK_1))
            align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            painter.drawText(QRectF(9.5, 0, label_w + 2, bubble.height()), align, self._label)
            painter.setFont(holo.font("shortcut"))
            painter.setPen(QColor(holo.INK_4))
            painter.drawText(QRectF(9.5 + label_w + 8, 0, shortcut_w + 2, bubble.height()), align, self._shortcut)
        finally:
            painter.end()


class _Hairline(QWidget):
    """The 24 px `line-strong` hairline under Home (4 px margins: 10 px from its neighbours)."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFixedSize(BUTTON, 9)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.fillRect(QRectF((BUTTON - 24) / 2, 4, 24, 1), holo.LINE_STRONG)
        painter.end()


class Dock(QWidget):
    """The glyph rail; see the module docstring."""

    slotActivated = Signal(str)
    WIDTH = holo.DOCK_WIDTH

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(self.WIDTH)
        self.setAccessibleName("Jarvis sections")
        self._current = HOME
        self._previous: str | None = None
        self._slide = 1.0
        self._roving = HOME
        self._briefing_new = False
        self._live = LIVE_IDLE
        self._memory = 0
        self._memory_new = False
        self._tip: DockTooltip | None = None
        self._tip_for: DockButton | None = None
        self._buttons: dict[str, DockButton] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(PAD_X, PAD_Y, PAD_X, PAD_Y)
        layout.setSpacing(holo.DOCK_GAP)
        for key in SLOTS:
            if key == SETTINGS:
                layout.addStretch(1)
            button = DockButton(key, self)
            button.clicked.connect(lambda _checked=False, k=key: self._activate(k))
            self._buttons[key] = button
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignHCenter)
            if key == HOME:
                layout.addWidget(_Hairline(self), 0, Qt.AlignmentFlag.AlignHCenter)
        self._slide_anim = QVariantAnimation(self)
        self._slide_anim.setStartValue(0.0)
        self._slide_anim.setEndValue(1.0)
        self._slide_anim.setDuration(holo.DOCK_SLIDE_MS)
        self._slide_anim.setEasingCurve(holo.qt_easing(holo.EASE_STD))
        self._slide_anim.valueChanged.connect(self._on_slide)
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(holo.TOOLTIP_DELAY_MS)
        self._hover_timer.timeout.connect(self._show_hover_tip)
        self._hover_target: DockButton | None = None
        self._sync_roving()
        self._sync_mix()
        for key in SLOTS:
            self._update_name(key)

    # ---- slots and current -------------------------------------------------------------

    def button(self, key: str) -> DockButton:
        return self._buttons[key]

    def mini_orb(self) -> orb_mod.MiniOrb:
        mini = self._buttons[HOME].mini
        assert mini is not None
        return mini

    def current(self) -> str:
        return self._current

    def set_current(self, key: str, *, animate: bool = True) -> None:
        if key not in self._buttons or key == self._current:
            return
        previous = self._current
        self._current = key
        self._previous = previous
        visible = self._buttons[previous].isVisibleTo(self) and self._buttons[key].isVisibleTo(self)
        self._slide_anim.stop()
        if animate and visible and self.isVisible() and not holo.reduced_motion():
            self._slide = 0.0
            self._slide_anim.start()
        else:
            self._slide = 1.0
        if not self._buttons[self._roving].hasFocus():
            self._roving = key if self.is_slot_visible(key) else self._roving
            self._sync_roving()
        self._sync_mix()
        self._update_name(previous)
        self._update_name(key)
        self.update()

    def is_sliding(self) -> bool:
        return self._slide_anim.state() == QVariantAnimation.State.Running

    def _on_slide(self, value: Any) -> None:
        self._slide = float(value)
        self._sync_mix()
        self.update()

    def _sync_mix(self) -> None:
        for key, button in self._buttons.items():
            if key == self._current:
                button.set_current_mix(self._slide)
            elif key == self._previous and self._slide < 1.0:
                button.set_current_mix(1.0 - self._slide)
            else:
                button.set_current_mix(0.0)

    def set_slot_visible(self, key: str, visible: bool) -> None:
        button = self._buttons[key]
        if key == HOME or button.isHidden() != bool(visible):
            return                      # Home is always there; nothing changes
        focused = button.hasFocus()
        button.setVisible(visible)
        if not visible:
            if self._tip_for is button:
                self._hide_tip()
            if self._roving == key:
                self._roving = self._current if self.is_slot_visible(self._current) else HOME
                self._sync_roving()
                if focused:
                    self._buttons[self._roving].setFocus(Qt.FocusReason.TabFocusReason)
        self._slide_anim.stop()
        self._slide = 1.0
        self._sync_mix()
        self.update()

    def is_slot_visible(self, key: str) -> bool:
        return not self._buttons[key].isHidden()

    def visible_slots(self) -> list[str]:
        return [key for key in SLOTS if self.is_slot_visible(key)]

    def slot_rect(self, key: str) -> QRectF:
        return QRectF(self._buttons[key].geometry())

    def highlight_rect(self) -> QRectF | None:
        """Where the current-slot highlight is painted now (None when the current slot is hidden)."""
        if not self.is_slot_visible(self._current):
            return None
        target = self.slot_rect(self._current)
        if self._slide >= 1.0 or self._previous is None or not self.is_slot_visible(self._previous):
            return target
        start = self.slot_rect(self._previous)
        k = self._slide
        return QRectF(target.x(), start.y() + (target.y() - start.y()) * k, target.width(), target.height())

    # ---- signals -------------------------------------------------------------------------

    def set_briefing_new(self, new: bool) -> None:
        self._briefing_new = bool(new)
        self._buttons[BRIEFING].set_dot(self._briefing_new)
        self._update_name(BRIEFING)

    def set_approvals(self, count: int) -> None:
        self._buttons[APPROVALS].set_count(count)
        self._update_name(APPROVALS)

    def set_followups(self, count: int) -> None:
        self._buttons[FOLLOWUPS].set_count(count)
        self._update_name(FOLLOWUPS)

    def set_memory_suggestions(self, count: int) -> None:
        """Suggestions waiting for the owner's Save or Dismiss (the cyan dot while there are any)."""
        self._memory = max(0, int(count))
        self._sync_memory()

    def set_memory_new(self, new: bool) -> None:
        """A note was saved since Memory was last open (the same cyan dot; no suggestion is claimed)."""
        self._memory_new = bool(new)
        self._sync_memory()

    def _sync_memory(self) -> None:
        self._buttons[MEMORY].set_dot(self._memory > 0 or self._memory_new)
        self._update_name(MEMORY)

    def set_live_activity(self, activity: str) -> None:
        self._live = activity if activity in (LIVE_IDLE, LIVE_WORKING, LIVE_HOLD) else LIVE_IDLE
        self._buttons[LIVE].set_dot(self._live != LIVE_IDLE, ping=False)
        self._buttons[LIVE].update()
        self._update_name(LIVE)

    def live_activity(self) -> str:
        return self._live

    def set_orb_look(self, base: str, *, muted: bool = False, offline: bool = False) -> None:
        self.mini_orb().set_look(base, muted=muted, offline=offline)
        self._update_name(HOME)

    def blinking(self) -> bool:
        """Nothing in the Dock blinks (a Send's hidden hold is a plain dot, like working)."""
        return False

    # ---- accessible names ------------------------------------------------------------------

    def accessible_name(self, key: str) -> str:
        label = LABELS[key]
        if key == BRIEFING and self._briefing_new:
            return f"{label}, new"
        if key == APPROVALS and self._buttons[key].count():
            return f"{label}, {self._buttons[key].count()} waiting"
        if key == FOLLOWUPS and self._buttons[key].count():
            return f"{label}, {self._buttons[key].count()} waiting on you"
        if key == MEMORY and self._memory:
            return f"{label}, {self._memory} {_plural(self._memory, 'suggestion', 'suggestions')}"
        if key == MEMORY and self._memory_new:
            return f"{label}, new"
        if key == LIVE and self._live == LIVE_WORKING:
            return f"{label}, Jarvis is working"
        if key == LIVE and self._live == LIVE_HOLD:
            return f"{label}, Jarvis is sending"
        return label

    def _update_name(self, key: str) -> None:
        button = self._buttons[key]
        button.setAccessibleName(self.accessible_name(key))
        parts = []
        if key == self._current:
            parts.append("Current screen")
        if key == HOME and button.mini is not None:
            parts.append(button.mini.accessibleDescription())
        parts.append(f"Shortcut {SHORTCUTS[key]}")
        button.setAccessibleDescription(". ".join(parts))

    # ---- activation, keyboard, focus ---------------------------------------------------------

    def _activate(self, key: str) -> None:
        if not self.is_slot_visible(key):
            return
        if key != SETTINGS:
            self.set_current(key)
        self.slotActivated.emit(key)

    def activate(self, key: str) -> None:
        """Open ``key`` as a click would (no-op for a hidden slot)."""
        self._activate(key)

    def _sync_roving(self) -> None:
        if not self.is_slot_visible(self._roving):
            self._roving = self._current if self.is_slot_visible(self._current) else HOME
        for key, button in self._buttons.items():
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus if key == self._roving else Qt.FocusPolicy.NoFocus)

    def roving(self) -> str:
        return self._roving

    def _move_focus(self, key: str) -> None:
        self._roving = key
        self._sync_roving()
        self._buttons[key].setFocus(Qt.FocusReason.TabFocusReason)     # arrow keys are keyboard focus

    def _key_pressed(self, button: DockButton, key: int) -> bool:
        order = self.visible_slots()
        if not order:
            return False
        index = order.index(button.key()) if button.key() in order else 0
        if key == Qt.Key.Key_Up:
            target = order[max(0, index - 1)]
        elif key == Qt.Key.Key_Down:
            target = order[min(len(order) - 1, index + 1)]
        elif key == Qt.Key.Key_Home:
            target = order[0]
        elif key == Qt.Key.Key_End:
            target = order[-1]
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._activate(button.key())
            return True
        else:
            return False
        if target != button.key():
            self._move_focus(target)
        return True

    def _focus_changed(self, button: DockButton, focused: bool, keyboard: bool) -> None:
        if focused:
            self._roving = button.key()
            self._sync_roving()
            if keyboard:
                self._show_tip(button)
        elif self._tip_for is button and self._hover_target is not button:
            self._hide_tip()
        self.update()

    # ---- tooltip -----------------------------------------------------------------------------

    def tooltip(self) -> DockTooltip | None:
        return self._tip

    def tooltip_for(self) -> str | None:
        """The slot whose tooltip is showing, if any."""
        if self._tip is None or not self._tip.isVisible() or self._tip_for is None:
            return None
        return self._tip_for.key()

    def _hover_started(self, button: DockButton) -> None:
        self._hover_target = button
        if self._tip_for is not None and self._tip is not None and self._tip.isVisible():
            self._show_tip(button)        # moving along the rail: no new delay
        else:
            self._hover_timer.start()

    def _hover_ended(self, button: DockButton) -> None:
        if self._hover_target is button:
            self._hover_target = None
            self._hover_timer.stop()
            if self._tip_for is button and not button.hasFocus():
                self._hide_tip()

    def _show_hover_tip(self) -> None:
        if self._hover_target is not None and shiboken6.isValid(self._hover_target):
            self._show_tip(self._hover_target)

    def _show_tip(self, button: DockButton) -> None:
        host = self.window()
        if self._tip is None or self._tip.parent() is not host:
            if self._tip is not None:
                self._tip.deleteLater()
            self._tip = DockTooltip(host)
        anchor = button.mapTo(host, QPoint(button.width() + 10, button.height() // 2))
        self._tip_for = button
        self._tip.show_at(anchor, LABELS[button.key()], SHORTCUTS[button.key()])

    def _hide_tip(self) -> None:
        self._tip_for = None
        if self._tip is not None:
            self._tip.hide_tip()

    # ---- shortcuts -----------------------------------------------------------------------------

    def install_shortcuts(self, widget: QWidget) -> list[QShortcut]:
        """Window-wide shortcuts (Ctrl+1 .. Ctrl+6, Ctrl+,) on ``widget`` that open visible slots."""
        shortcuts = []
        for key in SLOTS:
            shortcut = QShortcut(QKeySequence(SHORTCUTS[key]), widget)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(lambda k=key: self._activate(k))
            shortcuts.append(shortcut)
        return shortcuts

    # ---- Qt ------------------------------------------------------------------------------------

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(self.WIDTH, 2 * PAD_Y + 8 * BUTTON)

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self._hide_tip()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor(holo.DOCK))
            painter.fillRect(QRectF(self.width() - 1, 0, 1, self.height()), holo.LINE_FAINT)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            highlight = self.highlight_rect()
            if highlight is not None:
                painter.setPen(QPen(holo.LINE_STRONG, 1.0))
                painter.setBrush(holo.alpha(holo.HOLO, 0.12))
                painter.drawRoundedRect(highlight.adjusted(0.5, 0.5, -0.5, -0.5), holo.RADIUS_DOCK_BUTTON,
                                        holo.RADIUS_DOCK_BUTTON)
            for button in self._buttons.values():
                if button.has_focus_ring() and button.isVisible():
                    holo.paint_focus_ring(painter, QRectF(button.geometry()), holo.RADIUS_DOCK_BUTTON)
        finally:
            painter.end()
