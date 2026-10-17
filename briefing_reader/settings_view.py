"""The Settings screen in the hologram look, read only (HOLOGRAM_SPEC 2-7, 15, 16.8; round 2, part 2b).

Public API
==========

``SettingsStage``
    Left: the SETTINGS head and the group list (``GROUPS``: Voice, Briefing times, Ask, Accounts, Quiet
    hours, LIVE and privacy; a click, or Up / Down on the list, opens a group: ``select_group(key)``,
    ``current_group()``). Right, the focal panel: the open group's name, its settings as they are now in
    plain words (``set_config(config)``: the config Jarvis loaded, never a file read here; ``rows()``), a
    line on what it covers, how to change them (``text_label``: the config.toml tables, then a restart;
    nothing on this screen changes a setting), ``config_path`` with Open config.toml (``config_button``,
    ``openConfig``) and ``data_path`` with Open the data folder (``data_button``, ``openDataFolder``;
    ``set_paths(config_path, data_dir)``). Accounts show only their names and whether they are connected
    (``set_connected({alias: True | False})``, from the app's own sign-in check; "Not checked yet" before it).

``settings_rows(config, group, connected)``
    The (label, value) pairs a group shows (used by the panel and the tests).

Read only by design: nothing here writes config.toml or .env, and no secret, token, client id, page id, file
path of a sign-in or e-mail address is ever shown (Accounts: the alias and connected yes / no only).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from . import holo, hud
from .memory_view import COLUMN_MAX, PANEL_PAD, PanelFrame, Rule, SectionHead, body_label

SETTINGS_TITLE = "SETTINGS"
GROUPS: tuple[tuple[str, str], ...] = (("voice", "Voice"), ("times", "Briefing times"), ("ask", "Ask"),
                                       ("accounts", "Accounts"), ("quiet", "Quiet hours"),
                                       ("live", "LIVE and privacy"))
GROUP_TABLES: dict[str, tuple[str, ...]] = {
    "voice": ("voice", "assistant"), "times": ("schedule", "polling", "display"),
    "ask": ("ask", "research", "hotkey", "actions"), "accounts": ("accounts",), "quiet": ("productivity",),
    "live": ("live", "productivity"),
}
GROUP_NOTES: dict[str, str] = {
    "voice": "How Jarvis sounds and when he speaks.",
    "times": "The scheduled tasks keep their own times; these are used when no task gives one.",
    "ask": "Ask Jarvis uses your own signed-in Claude Code.",
    "accounts": "Each account's sign-in stays on this PC.",
    "quiet": "Quiet hours keep offers silent.",
    "live": "What LIVE shows stays in memory and is never saved.",
}
HOW_TEXT = ("To change these, edit {tables} in config.toml, then restart Jarvis. Nothing on this screen changes "
            "a setting.")
DATA_TEXT = "What Jarvis keeps on this PC (memory, decisions, voice cache, logs) is in the data folder."
OPEN_CONFIG_TEXT = "Open config.toml"
OPEN_DATA_TEXT = "Open the data folder"
NOT_CHECKED = "Not checked yet"
LIST_WIDTH = 220
_LIVE_OPENS = {"tab": "On the Live screen", "window": "In its own window", "off": "Only when you open it"}


def _yes(value: Any) -> str:
    return "Yes" if value else "No"


def _on(value: Any) -> str:
    return "On" if value else "Off"


def _percent(value: Any, more: str, less: str) -> str:
    """"+0%" -> "Normal", "+10%" -> "10% faster", "-5%" -> "5% slower"."""
    text = str(value or "").strip()
    try:
        number = int(text.rstrip("%"))
    except ValueError:
        return text or "Normal"
    if number == 0:
        return "Normal"
    return f"{abs(number)}% {more if number > 0 else less}"


def _clock(value: Any, hour24: bool) -> str:
    """"22:00" -> "10:00 PM" (or "22:00" with the 24-hour clock); anything else as it is."""
    text = str(value or "").strip()
    hours, _colon, minutes = text.partition(":")
    if not (hours.isdigit() and minutes.isdigit()):
        return text
    hour, minute = int(hours), int(minutes)
    if hour24:
        return f"{hour:02d}:{minute:02d}"
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def _combo(value: Any) -> str:
    """"ctrl+alt+j" -> "Ctrl+Alt+J"."""
    return "+".join(part[:1].upper() + part[1:] for part in str(value or "").split("+") if part)


def _every(seconds: Any) -> str:
    try:
        count = int(seconds)
    except (TypeError, ValueError):
        return "now and then"
    if count % 60 == 0:
        minutes = count // 60
        return "every minute" if minutes == 1 else f"every {minutes} minutes"
    return f"every {count} seconds"


def settings_rows(config: Any, group: str, connected: Mapping[str, bool] | None = None) -> list[tuple[str, str]]:
    """What ``group`` shows: (label, value) pairs from the loaded ``config``, in plain words. Never a secret,
    a token, a client id, a page id, a sign-in file or an e-mail address."""
    if config is None:
        return []
    hour24 = bool(getattr(getattr(config, "display", None), "hour24", False))
    assistant = getattr(config, "assistant", None)
    prod = getattr(config, "productivity", None)
    prod_on = bool(getattr(prod, "enabled", False))
    if group == "voice":
        voice = getattr(config, "voice", None)
        offline = str(getattr(voice, "offline_voice", "") or "")
        address = str(getattr(assistant, "address", "") or "")
        return [("Voice", str(getattr(voice, "voice", "") or "Default")),
                ("Speed", _percent(getattr(voice, "rate", ""), "faster", "slower")),
                ("Volume", _percent(getattr(voice, "volume", ""), "louder", "quieter")),
                ("When that voice is out of reach", f"Windows voice: {offline}" if offline
                 else "Windows' default voice"),
                ("Speaks on his own", "Yes" if getattr(assistant, "speak", True) else "No - text only"),
                ("Speaks his answers", _yes(getattr(assistant, "speak_replies", True))),
                ("Says what happened after an approval", _yes(getattr(assistant, "speak_results", True))),
                ("Greets you when opened", _yes(getattr(assistant, "greet", True))),
                ("Calls you", f'"{address}"' if address else "Nothing")]
    if group == "times":
        schedule = getattr(config, "schedule", None)
        polling = getattr(config, "polling", None)
        return [("Morning briefing", _clock(getattr(schedule, "am", ""), hour24)),
                ("Evening briefing", _clock(getattr(schedule, "pm", ""), hour24)),
                ("Waits for a late briefing", f"Up to {getattr(polling, 'timeout_minutes', 15)} minutes, "
                 f"checking {_every(getattr(polling, 'interval_seconds', 60))}"),
                ("When a briefing arrives", 'Asks "Hear it now?" first' if getattr(assistant, "scheduled_prompt",
                                                                                 False) else "Says it is ready"),
                ("Clock on screen", "24-hour (13:05)" if hour24 else "12-hour (1:05 PM)")]
    if group == "ask":
        ask = getattr(config, "ask", None)
        research = getattr(config, "research", None)
        hotkey = getattr(config, "hotkey", None)
        actions = getattr(config, "actions", None)
        enabled = bool(getattr(ask, "enabled", False))
        research_on = enabled and bool(getattr(research, "enabled", False))
        research_words = ("On" if getattr(research, "planner_may_ask", True) else "Only when you ask for it") \
            if research_on else "Off"
        return [("Ask Jarvis", _on(enabled)),
                ("Model", str(getattr(ask, "model", "") or "")),
                ("Reads the mail a request is about", _yes(getattr(ask, "read_mail", True))),
                ("Web research", research_words),
                ("Requests", f"Up to {getattr(ask, 'max_per_hour', 20)} an hour and "
                 f"{getattr(ask, 'max_per_day', 60)} a day"),
                ("Keyboard shortcut", _combo(getattr(hotkey, "combo", "")) if getattr(hotkey, "enabled", False)
                 else "Off"),
                ("Time to undo a send", f"{getattr(actions, 'undo_seconds', 10)} seconds")]
    if group == "accounts":
        connected = connected or {}
        aliases = list(getattr(config, "accounts", {}) or {})
        words = {True: "Connected", False: "Not connected"}
        return [(str(alias), words.get(connected.get(alias), NOT_CHECKED)) for alias in aliases]
    if group == "quiet":
        if not prod_on:
            return [("Quiet hours", "Not used - offers are off on this PC")]
        return [("Quiet hours", f"{_clock(getattr(prod, 'quiet_from', ''), hour24)} to "
                                f"{_clock(getattr(prod, 'quiet_until', ''), hour24)}"),
                ("Then", "Offers show as text only; nothing is said out loud")]
    if group == "live":
        live = getattr(config, "live", None)
        live_on = bool(getattr(live, "enabled", True))
        rows = [("LIVE", "On - every step on screen, never saved" if live_on else "Off")]
        if live_on:
            rows += [("Opens", _LIVE_OPENS.get(str(getattr(live, "auto_open", "tab")), "On the Live screen")),
                     ("Exact texts", "Shown on screen" if getattr(live, "text", True) else "Their sizes only"),
                     ("Pictures", "Drawn on this PC" if getattr(live, "pictures", True) else "Off"),
                     ("Pictures of web pages", "On - the only part that uses the network"
                      if getattr(live, "page_snapshots", True) else "Off")]
        memory_on = prod_on and bool(getattr(prod, "memory", False))
        rows.append(("Memory", "On - kept only on this PC, never given to web research" if memory_on else "Off"))
        if memory_on:
            rows.append(("Memory suggestions", "On - nothing is kept until you press Keep"
                         if getattr(prod, "memory_suggestions", False) else "Off"))
        rows.append(("Follow-ups", "On - message headers only" if prod_on and getattr(prod, "followups", False)
                     else "Off"))
        return rows
    return []


def _tables(group: str) -> str:
    names = [f"[{name}]" for name in GROUP_TABLES.get(group, ())]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


class GroupButton(QAbstractButton):
    """One group in the list: 40 px, radius 8; the open one on ``tile-focus`` with a 3 px ``holo`` bar."""

    HEIGHT = 40

    def __init__(self, key: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self.setText(text)
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAccessibleName(f"{text} settings")
        self._focus_ring = False

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        width = QFontMetricsF(holo.font("button", 500)).horizontalAdvance(self.text())
        return QSize(int(width) + 40, self.HEIGHT)

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
            rect = QRectF(self.rect()).adjusted(3.5, 2.5, -3.5, -2.5)
            on = self.isChecked()
            if on:
                painter.setPen(QPen(holo.LINE, 1.0))
                painter.setBrush(QColor(holo.TILE_FOCUS))
                painter.drawRoundedRect(rect, holo.RADIUS_BUTTON, holo.RADIUS_BUTTON)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(holo.HOLO))
                painter.drawRoundedRect(QRectF(rect.left(), rect.top() + 9, 3, rect.height() - 18), 1.5, 1.5)
            elif self.underMouse():
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(holo.alpha(holo.INK_1, 0.05))
                painter.drawRoundedRect(rect, holo.RADIUS_BUTTON, holo.RADIUS_BUTTON)
            painter.setFont(holo.font("button", 500))
            painter.setPen(QColor(holo.INK_1 if on else holo.INK_2))
            painter.drawText(rect.adjusted(14, 0, -8, 0),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.text())
            if self.hasFocus() and self._focus_ring:
                holo.paint_focus_ring(painter, rect, holo.RADIUS_BUTTON)
        finally:
            painter.end()


class SettingsStage(QWidget):
    """The Settings screen; see the module docstring."""

    openConfig = Signal()
    openDataFolder = Signal()
    groupChanged = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config: Any = None
        self._connected: dict[str, bool] = {}
        self._group = GROUPS[0][0]
        self.head = SectionHead("settings", SETTINGS_TITLE)
        self.group_buttons: dict[str, GroupButton] = {}
        side = QVBoxLayout()
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(2)
        side.addWidget(self.head)
        side.addSpacing(12)
        for key, text in GROUPS:
            button = GroupButton(key, text)
            button.clicked.connect(lambda _checked=False, which=key: self.select_group(which))
            button.installEventFilter(self)
            self.group_buttons[key] = button
            side.addWidget(button)
        side.addStretch(1)
        self._side = QWidget()
        self._side.setLayout(side)
        self._side.setFixedWidth(LIST_WIDTH)
        # The open group (the focal panel), in a scroll area for short windows.
        self.scroll = QScrollArea()
        content = hud._transparent_scroll(self.scroll)
        self.panel = PanelFrame()
        self.panel.setMaximumWidth(COLUMN_MAX)
        holder = QHBoxLayout(content)
        holder.setContentsMargins(0, 0, 0, 0)
        holder.addWidget(self.panel, 1000)
        holder.addStretch(1)
        self.title_label = body_label("", "screen_title", holo.INK_1, wrap=False)
        self.note_label = body_label("", "body_sm", holo.INK_3, name="settingsNote")
        self._grid_box = QWidget()
        self._grid = QGridLayout(self._grid_box)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(16)
        self._grid.setVerticalSpacing(8)
        self._grid.setColumnStretch(0, 2)
        self._grid.setColumnStretch(1, 3)
        self.text_label = body_label("", "body_sm", holo.INK_2, selectable=True, name="settingsHow")
        self.config_path = body_label("", "body_sm", holo.INK_4, selectable=True, name="settingsConfigPath")
        self.config_button = holo.HoloButton(OPEN_CONFIG_TEXT, holo.GHOST)
        self.config_button.clicked.connect(self.openConfig)
        self.data_label = body_label(DATA_TEXT, "body_sm", holo.INK_2, selectable=True, name="settingsData")
        self.data_path = body_label("", "body_sm", holo.INK_4, selectable=True, name="settingsDataPath")
        self.data_button = holo.HoloButton(OPEN_DATA_TEXT, holo.GHOST)
        self.data_button.clicked.connect(self.openDataFolder)
        column = QVBoxLayout(self.panel)
        column.setContentsMargins(PANEL_PAD[0] + 4, PANEL_PAD[1] + 4, PANEL_PAD[2] + 4, PANEL_PAD[3] + 4)
        column.setSpacing(0)
        column.addWidget(self.title_label)
        column.addSpacing(6)
        column.addWidget(self.note_label)
        column.addSpacing(14)
        column.addWidget(self._grid_box)
        column.addSpacing(16)
        column.addWidget(Rule())
        column.addSpacing(12)
        column.addWidget(self.text_label)
        column.addSpacing(4)
        column.addWidget(self.config_path)
        column.addSpacing(6)
        column.addWidget(self.config_button, 0, Qt.AlignmentFlag.AlignLeft)
        column.addSpacing(14)
        column.addWidget(self.data_label)
        column.addSpacing(4)
        column.addWidget(self.data_path)
        column.addSpacing(6)
        column.addWidget(self.data_button, 0, Qt.AlignmentFlag.AlignLeft)
        column.addStretch(1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(holo.GUTTER_SIDE + 8, holo.GUTTER_TOP, holo.GUTTER_SIDE, 16)
        layout.setSpacing(24)
        layout.addWidget(self._side)
        layout.addWidget(self.scroll, 1)
        self._value_labels: list[QLabel] = []
        self.select_group(self._group)

    # ---- API --------------------------------------------------------------------------------

    def set_paths(self, config_path: str, data_dir: str) -> None:
        self.config_path.setText(config_path)
        self.data_path.setText(data_dir)

    def set_config(self, config: Any) -> None:
        """The loaded config (read only; never written back)."""
        self._config = config
        self._fill()

    def set_connected(self, connected: Mapping[str, bool]) -> None:
        """Which accounts are signed in (the app's own check); only yes / no is shown."""
        self._connected = {str(alias): bool(value) for alias, value in dict(connected or {}).items()}
        if self._group == "accounts":
            self._fill()

    def select_group(self, key: str) -> None:
        if key not in self.group_buttons:
            return
        changed = key != self._group
        self._group = key
        self.group_buttons[key].setChecked(True)
        self._fill()
        self.scroll.verticalScrollBar().setValue(0)
        if changed:
            self.groupChanged.emit(key)

    def current_group(self) -> str:
        return self._group

    def rows(self) -> list[tuple[str, str]]:
        """What the open group shows now."""
        return settings_rows(self._config, self._group, self._connected)

    def shown_texts(self) -> list[str]:
        """Every text the panel shows (tests: nothing secret)."""
        labels = [self.title_label, self.note_label, self.text_label, self.config_path, self.data_label,
                  self.data_path, *self._value_labels]
        labels += [widget for widget in self._grid_box.findChildren(QLabel)]
        return [label.text() for label in labels]

    # ---- inside -------------------------------------------------------------------------------

    def _fill(self) -> None:
        key = self._group
        self.title_label.setText(dict(GROUPS)[key])
        self.note_label.setText(GROUP_NOTES.get(key, ""))
        self.text_label.setText(HOW_TEXT.format(tables=_tables(key)))
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self._value_labels = []
        rows = self.rows()
        for index, (label, value) in enumerate(rows):
            name = body_label(label, "body_sm", holo.INK_3)
            name.setContentsMargins(0, 2, 0, 0)                  # on the value's first line
            shown = body_label(value, "body", holo.INK_1, selectable=True)
            shown.setAccessibleName(f"{label}: {value}")
            self._grid.addWidget(name, index, 0, Qt.AlignmentFlag.AlignTop)
            self._grid.addWidget(shown, index, 1, Qt.AlignmentFlag.AlignTop)
            self._value_labels.append(shown)
        self._grid_box.setVisible(bool(rows))

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802 - Qt override
        if isinstance(watched, GroupButton) and event.type() == event.Type.KeyPress:
            keys = [key for key, _text in GROUPS]
            step = {Qt.Key.Key_Down: 1, Qt.Key.Key_Up: -1}.get(event.key(), 0)
            if step:
                index = (keys.index(watched.key) + step) % len(keys)
                self.select_group(keys[index])
                self.group_buttons[keys[index]].setFocus(Qt.FocusReason.TabFocusReason)
                return True
        return super().eventFilter(watched, event)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(LIST_WIDTH + 320, 300)
