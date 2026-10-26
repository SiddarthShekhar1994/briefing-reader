"""Jarvis on your desktop: the result cards on the HUD and the window each one points at (SPEC sections 8.1, 8.4 and
10.3; owner decision D5; hard rules 4, 13, 14 and 17; Phase 1, part 9; DesktopB.dc.html's first demo).

``ResultCards`` shows a run's cards (desktop.results.Card, made on the desktop thread right after the run:
``DesktopRun.shown``) as hud_overlay.ResultCard windows - Tool, frameless, on top, never the focus, their buttons
NoFocus and acting on click - and keeps each card's OWN items for its clicks (verified review note: the resolved
target and its identity, never an id resolved again).

* A card for something that opened (D5: the best match is ALREADY OPEN) looks for the window it opened (desktop.
  winwatch: the app's exe - from the type's association for a file, the browser's for a web page, the Start menu's
  exe name for an app - a window not there before the request or the click (``snapshot()`` then), else the one in
  front; tried at once and a few times more within ``FIND_FOR_S``, then given up: bounded single shots, never a
  loop). Found: the card goes beside it (kept on screen, clear of the HUD and the other cards), a cyan bracket round
  it with the app's tag ("WORD") and a tether from it to the card - in the click-through DecorLayer (static: drawn
  again only when something changes) - and the window is followed (WinEvent hooks: a drag hides the bracket and the
  tether until it ends, a minimise until it is back, a move or a snap draws them again where it is; a close takes
  them away and the card stays). Physical pixels are mapped per monitor onto Qt's screens (winwatch.map_rect): a
  window over two screens gets a bracket on each; one that maps onto no screen gets none. File Explorer windows get
  no bracket in Phase 1 (P2.3 adds Explorer's).
* Not found, or a card with no window (a held step, what a find found): beside the HUD, under its bar and chips.
* A button's click comes out as ``clickRequested(entry, key)`` (desktop_ui carries it out on the desktop thread and
  hands what came of it back to ``apply_click``): an Open that opened replaces its card with the card of what opened;
  a step held again (a file that turned out to run macros) adds its amber card; a row whose item changed since goes;
  Not now (``DISMISS_KEY``) removes the card - nothing is carried out.
* ``clear()`` (a new request, leaving the HUD, Jarvis closing): every card, bracket, tether, hook and timer goes.

Logs carry counts, card kinds and event kinds only - never a name, a title, an exe or a path.

Public API
==========
``ResultCards(overlay, watch_factory=None, hour24=False)``: ``show_run(run, before=)``, ``apply_click(entry, key,
run, before=)``, ``remove(entry)``, ``clear()``, ``entries()``, ``snapshot()``, ``find_now()``, ``watch()``,
``decor()``; signals ``clickRequested(object, str)``, ``changed()``. ``CardEntry`` (``card``, ``window``, ``hwnd``,
``parts``, ``hidden``, ``found()``, ``following()``); ``content_for(card, hour24=)``; ``tether_points(window,
card)``; ``FIND_DELAYS_MS``, ``FIND_FOR_S``, ``MOVE_SETTLE_MS``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Sequence
from dataclasses import replace
from typing import Any

import shiboken6
from PySide6.QtCore import QObject, QPoint, QRect, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QScreen

from . import holo, hud_overlay
from .desktop import executor as desktop_executor
from .desktop import results as desktop_results
from .desktop import validate as dv
from .desktop import winwatch
from .hud_overlay import Bracket, CardButton, CardContent, CardRow, Tether

logger = logging.getLogger(__name__)

FIND_DELAYS_MS = (0, 200, 500, 1000, 2000, 3500, 5500, 8000, 10000)   # when a card looks for its window
FIND_FOR_S = FIND_DELAYS_MS[-1] / 1000                                  # then it gives up (SPEC 5.5: 10 s)
MOVE_SETTLE_MS = 60                       # a move or a snap outside a drag: drawn again once it settles
TAG_TOP_ROOM = 12                         # the bracket's tag needs this much room above it on its screen
TETHER_END = 40                           # the tether meets the card this far below its top (at its kicker)
TETHER_IN = 24                            # ... and leaves the window at least this far from its corners

_VARIANTS = {(desktop_results.CYAN, desktop_results.PRIMARY): holo.GO,
             (desktop_results.CYAN, desktop_results.COMMIT): holo.GO,
             (desktop_results.CYAN, desktop_results.SECONDARY): holo.GHOST,
             (desktop_results.CYAN, desktop_results.QUIET): holo.GHOST,
             (desktop_results.AMBER, desktop_results.PRIMARY): holo.AMBER_COMMIT,
             (desktop_results.AMBER, desktop_results.COMMIT): holo.AMBER_COMMIT,
             (desktop_results.AMBER, desktop_results.SECONDARY): holo.AMBER_GHOST,
             (desktop_results.AMBER, desktop_results.QUIET): holo.AMBER_GHOST}
_NOTE_TONES = {desktop_results.NOTE_DONE: hud_overlay.TONE_DONE, desktop_results.NOTE_INFO: hud_overlay.TONE_CYAN,
               desktop_results.NOTE_AMBER: hud_overlay.TONE_AMBER}


def content_for(card: desktop_results.Card, hour24: bool = False) -> CardContent:
    """A card's copy (desktop.results.card_text) as the ResultCard shows it: cyan, or amber for a NEEDS YOUR OK
    card; its buttons in their looks (the amber card's ghosts amber too)."""
    text = desktop_results.card_text(card, hour24)
    tone = hud_overlay.TONE_AMBER if text.tone == desktop_results.AMBER else hud_overlay.TONE_CYAN
    buttons = tuple(CardButton(key, words, _VARIANTS.get((text.tone, role), holo.GHOST), right)
                    for key, words, role, right in text.buttons)
    rows = tuple(CardRow(key, title, meta, action) for key, title, meta, action in text.rows)
    return CardContent(text.kicker, text.title, text.meta, buttons, text.tag, text.note,
                       _NOTE_TONES.get(text.note_tone, hud_overlay.TONE_CYAN), tone, rows, text.note_first)


def tether_points(window: QRect, card: QRect) -> tuple[QPoint, ...]:
    """A tether from ``window``'s edge facing ``card`` to the card's edge (both global logical rects; DesktopB's
    elbowed .tl paths: out of the window, along, and into the card at its kicker); none when they overlap."""
    if not window.isValid() or not card.isValid() or window.intersects(card):
        return ()

    def clamp(value: int, low: int, high: int) -> int:
        return max(low, min(value, high)) if low <= high else (low + high) // 2

    if card.left() > window.right() or card.right() < window.left():
        right = card.left() > window.right()
        end_y = card.top() + min(TETHER_END, card.height() // 2)
        start_y = clamp(end_y, window.top() + TETHER_IN, window.bottom() - TETHER_IN)
        start = QPoint(window.right() + 1 if right else window.left() - 1, start_y)
        end = QPoint(card.left() - 1 if right else card.right() + 1, end_y)
        if start_y == end_y:
            return (start, end)
        middle = (start.x() + end.x()) // 2
        return (start, QPoint(middle, start_y), QPoint(middle, end_y), end)
    below = card.top() > window.bottom()
    end_x = card.left() + min(TETHER_END, card.width() // 2)
    start_x = clamp(end_x, window.left() + TETHER_IN, window.right() - TETHER_IN)
    start = QPoint(start_x, window.bottom() + 1 if below else window.top() - 1)
    end = QPoint(end_x, card.top() - 1 if below else card.bottom() + 1)
    if start_x == end_x:
        return (start, end)
    middle = (start.y() + end.y()) // 2
    return (start, QPoint(start_x, middle), QPoint(end_x, middle), end)


class CardEntry:
    """One card on the HUD: its card (desktop.results.Card: its own items), its window, the window it points at
    (``hwnd``; 0 none) and where that is on Qt's screens (``parts``: (screen, logical rect) - none while it is not
    found, gone or maps onto no screen), whether its bracket is hidden (a drag, a minimise) and its follow."""

    def __init__(self, card: desktop_results.Card, window: hud_overlay.ResultCard, before: Collection[int]) -> None:
        self.card = card
        self.window = window
        self.before = frozenset(before)
        self.hwnd = 0
        self.parts: list[tuple[QScreen, QRect]] = []
        self.hidden = False
        self.follow: winwatch.Follow | None = None
        self.tries = 0
        self.finder: QTimer | None = None
        self.settle: QTimer | None = None

    def found(self) -> bool:
        return bool(self.hwnd)

    def following(self) -> bool:
        return self.follow is not None and self.follow.active()

    def __repr__(self) -> str:
        return f"CardEntry({self.card.kind}, {self.card.skill}, found={self.found()})"


class ResultCards(QObject):
    """See the module docstring."""

    clickRequested = Signal(object, str)        # the CardEntry, the button's key
    changed = Signal()                          # the cards or the decor changed

    def __init__(self, overlay: hud_overlay.HudOverlay, *,
                 watch_factory: Callable[[], winwatch.WindowWatch] | None = None, hour24: bool = False,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.overlay = overlay
        self._watch_factory = watch_factory or winwatch.WindowWatch
        self._watch: winwatch.WindowWatch | None = None
        self._hour24 = bool(hour24)
        self._entries: list[CardEntry] = []

    # ---- the cards ------------------------------------------------------------------------------------

    def watch(self) -> winwatch.WindowWatch:
        """The window watcher (made at first use: nothing is listed or hooked before a card needs it)."""
        if self._watch is None:
            self._watch = self._watch_factory()
        return self._watch

    def snapshot(self) -> frozenset[int]:
        """The top-level windows now: taken as a request or a click starts, so the window an open brings is known."""
        return self.watch().snapshot()

    def entries(self) -> list[CardEntry]:
        return list(self._entries)

    def show_run(self, run: Any, before: Collection[int] = frozenset()) -> int:
        """``run``'s cards (``run.shown``) on the HUD, each looking for its window; how many show."""
        cards = [card for card in getattr(run, "shown", ()) or () if isinstance(card, desktop_results.Card)]
        for card in cards:
            self._add(card, before)
        if cards:
            logger.info("Desktop %s: %d result card(s) (%s)", getattr(run, "run_id", "") or "-", len(cards),
                        ", ".join(card.kind for card in cards))
            self.changed.emit()
        return len(cards)

    def apply_click(self, entry: CardEntry, key: str, run: Any, before: Collection[int] = frozenset()) -> None:
        """What a click on ``entry``'s ``key`` came to (``run``: desktop.executor's run of it, with its cards): an
        open that opened replaces the card with its card; a step held again adds its card; a row that changed since
        goes; a held step whose item changed goes; Show in folder leaves the card as it is."""
        if entry not in self._entries:
            return
        results = getattr(run, "results", ()) or ()
        result = results[0] if results else None
        cards = [card for card in getattr(run, "shown", ()) or () if isinstance(card, desktop_results.Card)]
        status = getattr(result, "status", "")
        skill = getattr(result, "skill", "")
        index = desktop_results.row_index(key)
        if skill == dv.SHOW_IN_FOLDER:
            if status != desktop_executor.DONE and index >= 0:
                self._drop_row(entry, index)
            elif status != desktop_executor.DONE and entry.card.kind == desktop_results.WAITING:
                self.remove(entry)
            return
        if status == desktop_executor.DONE and cards:
            self._replace(entry, cards, before)
        elif status == desktop_executor.WAITING and cards:
            if entry.card.kind == desktop_results.WAITING and key == desktop_results.OPEN_KEY:
                self._replace(entry, cards, before)
            else:
                for card in cards:
                    self._add(card, before)
        elif index >= 0:
            self._drop_row(entry, index)
        elif entry.card.kind == desktop_results.WAITING:
            self.remove(entry)
        self.changed.emit()

    def remove(self, entry: CardEntry) -> None:
        """One card goes - its window, bracket, tether, hooks and timers."""
        if entry not in self._entries:
            return
        self._entries.remove(entry)
        self._let_go(entry)
        self.overlay.remove_card(entry.window)
        self._apply_decor()
        self.changed.emit()

    def clear(self) -> None:
        """Every card goes (a new request, leaving the HUD, Jarvis closing)."""
        if not self._entries:
            return
        entries, self._entries = self._entries, []
        for entry in entries:
            self._let_go(entry)
            self.overlay.remove_card(entry.window)
        self._apply_decor()
        if self._watch is not None:
            self._watch.stop()
        self.changed.emit()

    def _add(self, card: desktop_results.Card, before: Collection[int]) -> CardEntry:
        window = self.overlay.new_card()
        window.set_content(content_for(card, self._hour24))
        entry = CardEntry(card, window, before)
        window.buttonClicked.connect(lambda key, entry=entry: self._on_button(entry, key))
        self._entries.append(entry)
        self._place(entry)
        if card.kind == desktop_results.OPENED and card.exe:
            self._look(entry)
        return entry

    def _replace(self, entry: CardEntry, cards: Sequence[desktop_results.Card], before: Collection[int]) -> None:
        """``entry``'s card gives way to ``cards`` (the first takes its place; nothing of it is left behind)."""
        if entry in self._entries:
            self._entries.remove(entry)
            self._let_go(entry)
            self.overlay.remove_card(entry.window)
        for card in cards:
            self._add(card, before)
        self._apply_decor()

    def _drop_row(self, entry: CardEntry, index: int) -> None:
        card = entry.card
        if 0 <= index < len(card.rows):
            entry.card = replace(card, rows=card.rows[:index] + card.rows[index + 1:])
            entry.window.set_content(content_for(entry.card, self._hour24))
            self._place(entry)
            self._apply_decor()

    def _let_go(self, entry: CardEntry) -> None:
        for timer in (entry.finder, entry.settle):
            if timer is not None and shiboken6.isValid(timer):
                timer.stop()
                timer.deleteLater()
        entry.finder = entry.settle = None
        if entry.follow is not None:
            entry.follow.stop()
            entry.follow = None
        entry.hwnd = 0
        entry.parts = []

    def _on_button(self, entry: CardEntry, key: str) -> None:
        if entry in self._entries:
            self.clickRequested.emit(entry, key)

    # ---- the window it points at --------------------------------------------------------------------------

    def _look(self, entry: CardEntry) -> None:
        """Look for ``entry``'s window now, then at FIND_DELAYS_MS (bounded single shots) until found."""
        entry.tries = 0
        self._try_find(entry)

    def find_now(self) -> None:
        """Every card still looking for its window looks now (tests and the harness step it)."""
        for entry in list(self._entries):
            if entry.card.kind == desktop_results.OPENED and entry.card.exe and not entry.found() \
                    and entry.finder is not None:
                entry.finder.stop()
                self._try_find(entry)

    def _try_find(self, entry: CardEntry) -> None:
        if entry not in self._entries or entry.found():
            return
        entry.tries += 1
        hwnd = self.watch().find(exe=entry.card.exe, before=entry.before)
        if hwnd:
            self._found(entry, hwnd)
            return
        if entry.tries >= len(FIND_DELAYS_MS):
            logger.info("Desktop: no window found for a result card after %d tries; no bracket", entry.tries)
            return
        if entry.finder is None:
            entry.finder = QTimer(self)
            entry.finder.setSingleShot(True)
            entry.finder.timeout.connect(lambda entry=entry: self._try_find(entry))
        entry.finder.start(max(0, FIND_DELAYS_MS[entry.tries] - FIND_DELAYS_MS[entry.tries - 1]))

    def _found(self, entry: CardEntry, hwnd: int) -> None:
        entry.hwnd = int(hwnd)
        if entry.finder is not None:
            entry.finder.stop()
        entry.follow = self.watch().follow(entry.hwnd, lambda kind, entry=entry: self._on_window(entry, kind))
        self._refresh(entry)
        logger.info("Desktop: the window a result card points at was found (%s)",
                    "followed" if entry.follow is not None else "not followed")

    def _on_window(self, entry: CardEntry, kind: str) -> None:
        """A followed window's event (on the GUI thread)."""
        if entry not in self._entries:
            return
        if kind in (winwatch.DRAG, winwatch.MINIMISED):
            entry.hidden = True
            self._apply_decor()
        elif kind == winwatch.CLOSED:
            entry.follow = None
            entry.hwnd = 0
            entry.parts = []
            entry.hidden = False
            self._apply_decor()                          # the bracket and the tether go; the card stays
        elif kind == winwatch.MOVED:
            if entry.settle is None:
                entry.settle = QTimer(self)
                entry.settle.setSingleShot(True)
                entry.settle.timeout.connect(lambda entry=entry: self._refresh(entry))
            entry.settle.start(MOVE_SETTLE_MS)
        else:                                            # DROP, RESTORED, FRONT: drawn again where it is
            self._refresh(entry)
        self.changed.emit()

    def _refresh(self, entry: CardEntry) -> None:
        """The window's frame read again and mapped onto Qt's screens: the card beside it, the decor drawn anew."""
        if entry not in self._entries or not entry.hwnd:
            return
        rect = self.watch().bounds(entry.hwnd)
        entry.parts = self._map(rect) if rect is not None else []
        entry.hidden = not entry.parts
        self._place(entry)
        self._apply_decor()

    def _map(self, rect: winwatch.Rect) -> list[tuple[QScreen, QRect]]:
        screens = {screen.name(): screen for screen in QGuiApplication.screens()}
        areas = [winwatch.ScreenArea(name, _rect(screen.geometry()), float(screen.devicePixelRatio()))
                 for name, screen in screens.items()]
        parts = winwatch.map_rect(rect, self.watch().monitors(), areas)
        return [(screens[name], QRect(part.x, part.y, part.width, part.height)) for name, part in parts
                if name in screens]

    # ---- placing and the decor ---------------------------------------------------------------------------

    def _place(self, entry: CardEntry) -> None:
        """Beside its window (on that window's screen) when it has one; else under the HUD's bar and chips. Clear
        of the other cards; kept on screen."""
        others = [other.window.geometry() for other in self._entries if other is not entry]
        hud = self.overlay.hud
        if entry.parts and not entry.hidden:
            screen, rect = entry.parts[0]
            self.overlay.show_card(entry.window, rect, screen.availableGeometry(), others)
            return
        if entry.hwnd and entry.hidden:
            return                                       # mid-drag or minimised: it stays where it is meanwhile
        area = hud.area() if hud.area().isValid() else None
        anchor = hud.occupied_rect() if hud.occupied_rect().isValid() else QRect()
        if not anchor.isValid() and area is not None:
            anchor = QRect(area.center(), area.center())
        self.overlay.show_card(entry.window, anchor, area, others)

    def decor(self) -> tuple[list[Bracket], list[Tether]]:
        """The brackets (a tag on the first part's) and the tethers of the cards whose windows show."""
        brackets: list[Bracket] = []
        tethers: list[Tether] = []
        for entry in self._entries:
            if entry.hidden or not entry.parts:
                continue
            tag = entry.card.app.upper()
            for index, (screen, rect) in enumerate(entry.parts):
                brackets.append(Bracket(_bracket_rect(screen, rect), hud_overlay.TONE_CYAN, tag if index == 0 else ""))
            points = tether_points(entry.parts[0][1], entry.window.geometry())
            if points:
                tethers.append(Tether(points, hud_overlay.TONE_CYAN))
        return brackets, tethers

    def _apply_decor(self) -> None:
        brackets, tethers = self.decor()
        self.overlay.set_decor(brackets, tethers)


def _rect(rect: QRect) -> winwatch.Rect:
    return winwatch.Rect(rect.x(), rect.y(), rect.width(), rect.height())


def _bracket_rect(screen: QScreen, rect: QRect) -> QRect:
    """The bracket round a window's part, its top kept low enough for the tag to show on that screen (a maximised
    window's frame starts at the screen's top)."""
    top = screen.geometry().top() + TAG_TOP_ROOM
    if rect.top() < top < rect.bottom():
        return QRect(rect.left(), top, rect.width(), rect.bottom() - top + 1)
    return QRect(rect)
