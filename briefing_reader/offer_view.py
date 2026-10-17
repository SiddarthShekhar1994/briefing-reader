"""An offer on Home in the hologram look (HOLOGRAM_SPEC 5, 6, 14.5, 16.0, 16.4 / 16.5 moment 1; round 2, part 3a).

Public API
==========

``OfferInfo``
    One open offer as Home shows it: ``offer_id``, ``kind`` ("plan" / "prep"), ``link_id`` (the same link
    the Transcript entry carries: a click on the commit runs exactly what a click on that link runs), the
    ``kicker`` (``PLAN MY DAY``, ``12:30 PM - IN 15 MIN``), the ``title``, the ``meta`` lines (plain words,
    never a technical readout), the ``commit`` text (``Plan my day`` / ``Prepare brief``), the spoken
    ``question`` (the caption's resting line while it waits) and the Transcript ``entry`` id.

``OfferTile``
    The ONE amber tile: the needs-you look (``tile-amber``, amber brackets, the kicker's 7 px amber dot that
    pings three times on arrival), the title in two lines at most, the meta lines, then Not now (amber
    ghost) and the commit (amber). ``set_offer(info)`` (None hides it); ``commitClicked(link_id)``,
    ``notNowClicked(offer_id)``. The tile itself never runs or reads anything: the commit only hands its
    link to the app, which checks everything again before anything is read (the offer never acts).

``wrap_lines(text, font, width, lines)``
    ``text`` word-wrapped in at most ``lines`` lines of ``width`` px, the last one elided (shared with
    plan_view and brief_view).
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import shiboken6
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import holo

TILE_MIN = 220
TILE_MAX = 290
NOT_NOW_TEXT = "Not now"          # spec 6: offers decline with Not now (never "Not today")
PLAN_KICKER = "PLAN MY DAY"
PLAN_TITLE = "Blocks around your meetings"
PLAN_META = "Nothing goes on your calendar until you approve each block."
PREP_META = "Nothing is read until you click."
TITLE_LINES = 2
META_LINES = 3
_DOT_ROOM = 15                    # the kicker's 7 px dot and its 8 px gap
_ACTION_GAP = 12                  # between the meta lines and the buttons
_PINGS = 3


def wrap_lines(text: str, font: QFont, width: float, lines: int) -> list[str]:
    """``text`` in at most ``lines`` word-wrapped lines of ``width`` px, the last one elided."""
    words = " ".join(str(text or "").split()).split(" ")
    metrics = QFontMetricsF(font)
    width = max(1.0, width)
    out: list[str] = []
    current = ""
    for index, word in enumerate(words):
        trial = f"{current} {word}".strip()
        if metrics.horizontalAdvance(trial) <= width or not current:
            current = trial
            continue
        out.append(current)
        current = word
        if len(out) == lines - 1:
            current = " ".join(words[index:])
            break
    if current:
        out.append(current)
    out = out[:max(1, lines)]
    if out:
        out[-1] = metrics.elidedText(out[-1], Qt.TextElideMode.ElideRight, width)
    return [line for line in out if line] or []


@dataclass(frozen=True)
class OfferInfo:
    offer_id: int
    kind: str
    link_id: str
    kicker: str
    title: str
    commit: str
    meta: tuple[str, ...] = ()
    question: str = field(default="", repr=False)
    entry: int = 0

    def __repr__(self) -> str:
        return f"OfferInfo({self.offer_id}, {self.kind})"


class OfferTile(QWidget):
    """The offer's amber tile; see the module docstring."""

    commitClicked = Signal(str)
    notNowClicked = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._info: OfferInfo | None = None
        self._ping_start = 0.0
        self._ping = QTimer(self)
        self._ping.setInterval(33)
        self._ping.timeout.connect(self._on_ping)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.not_now_button = holo.HoloButton(NOT_NOW_TEXT, holo.AMBER_GHOST, parent=self)
        self.commit_button = holo.HoloButton("", holo.AMBER_COMMIT, parent=self)
        self.not_now_button.clicked.connect(self._not_now)
        self.commit_button.clicked.connect(self._commit)
        self.hide()

    # ---- what it shows -------------------------------------------------------------------------

    def set_offer(self, info: OfferInfo | None) -> None:
        """The offer to show (None hides the tile). A new offer pings its dot three times."""
        fresh = info is not None and (self._info is None or self._info.offer_id != info.offer_id)
        self._info = info
        if info is None:
            self._ping.stop()
            self.hide()
            return
        self.commit_button.setText(info.commit)
        self.commit_button.setAccessibleName(f"{info.commit}: {info.title}" if info.title else info.commit)
        self.not_now_button.setAccessibleName(f"{NOT_NOW_TEXT}: {info.title}" if info.title else NOT_NOW_TEXT)
        what = info.commit.lower() if info.commit else "an offer"
        self.setAccessibleName(f"Offer from Jarvis: {what}")
        self.setAccessibleDescription(" ".join(part for part in (info.kicker, info.title, *info.meta) if part))
        self.setToolTip(info.title if info.title else "")
        if fresh and not holo.reduced_motion():
            self._ping_start = time.monotonic()
            self._ping.start()
        self.updateGeometry()
        self._place_buttons()
        self.show()
        self.update()

    def info(self) -> OfferInfo | None:
        return self._info

    def texts(self, width: int | None = None) -> tuple[str, list[str], list[str]]:
        """(kicker, title lines, meta lines) as drawn at ``width``."""
        info = self._info
        if info is None:
            return "", [], []
        inner = (width if width is not None else self.width()) - 2 * holo.TILE_PAD[1]
        kicker = QFontMetricsF(holo.font("label")).elidedText(holo.role_text("label", info.kicker),
                                                              Qt.TextElideMode.ElideRight, inner - _DOT_ROOM)
        title = wrap_lines(info.title, holo.font("tile_title"), inner, TITLE_LINES)
        meta: list[str] = []
        for line in info.meta:
            room = META_LINES - len(meta)
            if room <= 0:
                break
            meta += wrap_lines(line, holo.font("body_sm"), inner, room)
        return kicker, title, meta

    # ---- geometry ------------------------------------------------------------------------------

    def height_for(self, width: int) -> int:
        _kicker, title, meta = self.texts(width)
        pad_v = holo.TILE_PAD[0]
        height = pad_v + holo.line_height("label") + holo.KICKER_GAP
        height += len(title) * holo.line_height("tile_title")
        if meta:
            height += 4 + len(meta) * holo.line_height("body_sm")
        button = self.commit_button.sizeHint().height()
        height += _ACTION_GAP + button + pad_v - holo.HoloButton.MARGIN
        return height

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        return self.height_for(width)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(TILE_MAX, self.height_for(TILE_MAX))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(TILE_MIN, self.height_for(TILE_MIN))

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._place_buttons()

    def _place_buttons(self) -> None:
        margin = holo.HoloButton.MARGIN
        pad_h, pad_v = holo.TILE_PAD[1], holo.TILE_PAD[0]
        commit = self.commit_button.sizeHint()
        later = self.not_now_button.sizeHint()
        y = self.height() - pad_v + margin - commit.height()
        right = self.width() - pad_h + margin
        self.commit_button.setGeometry(right - commit.width(), y, commit.width(), commit.height())
        left = pad_h - margin
        width = min(later.width(), max(0, right - commit.width() - holo.BUTTON_GAP + 2 * margin - left))
        self.not_now_button.setGeometry(left, y, width, later.height())

    # ---- actions -------------------------------------------------------------------------------

    def _commit(self) -> None:
        if self._info is not None and self._info.link_id:
            self.commitClicked.emit(self._info.link_id)

    def _not_now(self) -> None:
        if self._info is not None:
            self.notNowClicked.emit(self._info.offer_id)

    # ---- paint ---------------------------------------------------------------------------------

    def _ping_progress(self) -> float:
        cycle = holo.PING_MS / 1000.0
        elapsed = time.monotonic() - self._ping_start
        return -1.0 if elapsed >= cycle * _PINGS else (elapsed % cycle) / cycle

    def _on_ping(self) -> None:
        if not shiboken6.isValid(self):
            return
        if self._ping_progress() < 0.0 or not self.isVisible():
            self._ping.stop()
        self.update(QRectF(0, 0, 60, 60).toRect())

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._ping.stop()
        super().hideEvent(event)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        if self._info is None:
            return
        kicker, title, meta = self.texts()
        painter = QPainter(self)
        try:
            holo.paint_tile(painter, QRectF(self.rect()), holo.TILE_NEEDS)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            pad_h, pad_v = holo.TILE_PAD[1], holo.TILE_PAD[0]
            y = float(pad_v)
            label_h = holo.line_height("label")
            dot = QPointF(pad_h + holo.NEW_DOT / 2, y + label_h / 2)
            if self._ping.isActive():
                holo.paint_ping(painter, dot, self._ping_progress(), color=holo.AMBER)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(holo.AMBER))
            painter.drawEllipse(dot, holo.NEW_DOT / 2, holo.NEW_DOT / 2)
            painter.setFont(holo.font("label"))
            painter.setPen(QColor(holo.AMBER))
            inner = self.width() - 2 * pad_h
            painter.drawText(QRectF(pad_h + _DOT_ROOM, y, inner - _DOT_ROOM, label_h),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), kicker)
            y += label_h + holo.KICKER_GAP
            painter.setFont(holo.font("tile_title"))
            painter.setPen(QColor(holo.AMBER_TX))
            line = holo.line_height("tile_title")
            for text in title:
                painter.drawText(QRectF(pad_h, y, inner, line),
                                 int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
                y += line
            if meta:
                y += 4
                painter.setFont(holo.font("body_sm"))
                painter.setPen(QColor(holo.AMBER_TX2))
                line = holo.line_height("body_sm")
                for text in meta:
                    painter.drawText(QRectF(pad_h, y, inner, line),
                                     int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
                    y += line
        finally:
            painter.end()


def tile_width(room: float) -> int:
    """The tile's width for ``room`` px beside the orb: 220-290 (spec 16.0)."""
    return int(max(TILE_MIN, min(TILE_MAX, math.floor(room))))


def newest_waiting(offers: Sequence[OfferInfo], dismissed: set[int]) -> list[OfferInfo]:
    """The offers that still wait on Home (not put away with Not now), oldest first."""
    return [offer for offer in offers if offer.offer_id not in dismissed]
