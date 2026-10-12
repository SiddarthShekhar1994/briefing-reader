"""Jarvis HUD widget kit: theme tokens, fonts, painting helpers and widgets.

The look follows the user's "Jarvis HUD - sci-fi" artboard: a near-black
ground with a faint cyan grid, chamfered (cut-corner) panels, cyan accents,
amber for anything that needs the user's OK, and three typefaces: Chakra Petch
(wordmark), Sora (prose) and JetBrains Mono (labels, meta, numbers). Every
widget paints itself with QPainter; nothing here knows about briefings,
Notion or Google, so ``ui.py`` composes the views from these parts. The
imports from the package are the Qt-free clock formatter
(``text_prep.clock_parts`` / ``format_time``), so every screen writes times
the same way, the LIVE view's Qt-free event types and words (``live``) and its
pictures as plain data (``pictures``).

Public API
==========

Tokens and fonts
    Colour constants (hex strings): ``GROUND``, ``TEXT_BRIGHT``, ``TEXT_BODY``,
    ``TEXT_SOFT``, ``TEXT_MUTED``, ``TEXT_DIM``, ``TEXT_SUB``, ``TEXT_TIME``,
    ``ACCENT`` (+ ``_HOVER``/``_PRESSED``/``_INK``), ``AMBER`` (+ ``_HOVER``/
    ``_PRESSED``/``_INK``/``_TEXT``/``_SUB``/``_META``), ``GREEN``, ``RED``,
    ``RED_LINE``, ``PINK``, ``SLATE``, ``OFF``; QColors ``PANEL_GROUND`` and
    ``AMBER_GROUND``; ``rgba(color, alpha) -> QColor``.
    ``load_fonts(fonts_dir) -> dict[str, str]`` registers the bundled TTFs
    (call once after QApplication exists; missing files are logged and the
    font helpers fall back to Bahnschrift / Segoe UI / Cascadia Mono, Consolas).
    ``display_font(px, weight, spacing)``, ``body_font(...)``, ``mono_font(...)``
    return QFonts; ``spacing`` is CSS letter-spacing in em.
    ``apply_theme(app)``: Fusion, HUD palette, Sora 13 px and a stylesheet for
    tooltips, scroll bars, menus and transparent text browsers/scroll areas.
    ``make_label(text, font, color, wrap=, name=)`` plain-text QLabel;
    ``set_label_color(label, color)``; ``SpeechLabel`` (QLabel with a real
    line height and optional ``max_lines`` elision; ``text()`` returns the
    plain text that was set, ``shown_text()`` what is displayed).

Painting helpers
    Corner flags ``CUT_TL``, ``CUT_TR``, ``CUT_BR``, ``CUT_BL``, ``CUT_DIAGONAL``
    (top-left + bottom-right, the default), ``CUT_TOP``.
    ``chamfer_path(rect, cut, corners) -> QPainterPath``.
    ``paint_ground(painter, rect, glow_center=None, glow_radii=None)``: ground
    colour, 48 px grid and the soft radial cyan glow.
    ``glow_pixmap(path, color, blur, dpr=1.0, key=None) -> (QPixmap, QPointF)``:
    a cached, blurred copy of ``path`` (CSS box-shadow ``0 0 <blur>px``).
    ``paint_row_highlight(painter, rect, color=ACCENT, strength=1.0)``: the
    agenda-row look (3 px glowing bar on the left + left-to-right gradient),
    shared by ``SectionList`` and the transcript highlight.
    ``paint_dash_bar(painter, rect, fraction, color)``: segmented telemetry bar.

Widgets
    ``HudWindowFrame(QWidget)``: frameless, translucent, always-on-top window
        with chamfered outer corners, painted ground (``set_glow_anchor``), a
        ``header`` (HeaderBar; its close button calls ``close()``, its minimize
        button ``showMinimized()``), ``body`` / ``body_layout`` for the
        content, optional edge resizing (``set_resizable``). It has a taskbar
        button (a top-level window with no owner, never a tool window), which
        restores it when minimized and minimizes it when it is in front.
    ``HeaderBar``: logo, JARVIS wordmark, subtitle, ServiceChips
        (``set_service(name, status, tooltip)``), date + clock updated each
        minute (``set_clock(callable)``, ``clock_texts()``; "1:52 PM" with a
        small "PM", or "13:52" after ``set_hour24(True)``), drag-to-move,
        ``minimize_button`` (accessible name "Minimize") ->
        ``minimizeRequested`` and ``close_button`` (accessible name "Close")
        -> ``closeRequested``, side by side at the right end.
        Narrow bars drop the subtitle, then the date, then tighten the chips
        and the room around them (``compact_level()``), so the prompt still
        fits. ``set_service_visible(name, bool)`` hides a chip without its room
        (Ask Jarvis's ``claude`` chip on the narrow prompt).
    ``ServiceChip``: statuses ``STATUS_OK`` / ``STATUS_WARN`` / ``STATUS_ERROR``
        / ``STATUS_OFF``.
    ``ChamferPanel(title, meta, variant=PANEL_CYAN|PANEL_AMBER, cut, corners)``:
        ``body_layout`` (QVBoxLayout below the title row), ``set_title``,
        ``set_meta``.
    ``MainPanel``: the transcript panel (top corners cut 10 px) with a header
        strip holding ``steps`` (StepStrip) and ``live`` (LiveMarker)
        (``strip_layout`` takes more widgets), and ``body_layout`` for the
        text browser.
    ``HudButton(text, variant, compact=False)``: QPushButton painted per
        variant ``PRIMARY``, ``SECONDARY``, ``DENY``, ``APPROVE``, ``LINK``;
        never default/autoDefault, Tab focus only, ring on keyboard focus,
        ``set_selected`` (secondary), ``set_variant``, ``set_link_color``.
    ``Orb(size=168, state=ORB_STANDBY)``: ``set_state``, ``state()``,
        ``set_phase(seconds or None)`` (freeze for screenshots),
        ``set_animated(bool)``, ``is_animating()``. ~30 fps only while visible
        and not minimized. States in ``ORB_STATES`` (colour, label).
    ``StateLabel``: blinking square + state text (``set_state(state, text=None)``).
    ``TelemetryBar(label, value, fraction, color)``: ``set_value`` (with an
        optional ``short`` form shown when the value does not fit), ``shown_value()``.
    ``SectionList`` (scroll area of ``SectionRow`` buttons): ``set_rows`` with
        ``SectionRowInfo``, ``set_row_state``, ``row(i)``, ``ensure_visible``,
        ``content_height()``, ``rowClicked(int)``. Row states ``ROW_PLAYED``,
        ``ROW_CURRENT``, ``ROW_UPCOMING``, ``ROW_IGNORED``.
    ``AgendaPanel(title)``: the TODAY / TOMORROW agenda (``AgendaRow`` from
        ``AgendaRowInfo``: a glowing 3 px bar, mono time ("12:30 PM" drawn as
        "12:30" over a small "PM"), title, meta; states
        ``AGENDA_UPCOMING`` cyan, ``AGENDA_NOW`` green, ``AGENDA_PAST`` dim) or
        one message line with an optional link (``set_message``,
        ``linkClicked``), then DEADLINES (``DeadlineRow`` from
        ``DeadlineRowInfo``; the due label is red / amber / grey for
        ``URGENCY_HIGH`` / ``URGENCY_MEDIUM`` / ``URGENCY_LOW``). It scrolls
        inside the panel.
    ``ChipButton(text)``: a small checkable chamfered toggle with a count and
        a caret (``set_meta``), e.g. SECTIONS in the transcript strip.
    ``Popover(title, meta)``: a frameless popup around ``panel`` (a
        ChamferPanel); ``open_below(anchor, width, height)``, ``closed``.
    ``StepStrip``: ``set_steps([(label, STEP_DONE|STEP_ACTIVE|STEP_TODO|STEP_DENIED)])``.
    ``LiveMarker``: blinking red dot + LIVE; ``set_live(bool)`` (keeps its space).
    ``ActionCard(action_id, kind, title, detail, actionable=True, *,
        approve_text="Approve", body="", title_lines=0, open_text="",
        copy_text="", edit_text="", sign_in_text="", check_line=False)``:
        ``set_status(CARD_*, message="", link="", link_text="")`` (``CARD_DONE``
        reads DONE and ``CARD_SENT`` its message ("ACCEPTED") in green;
        ``CARD_COUNTDOWN`` shows Undo on the left and the message ("SENDING IN
        9 S") on the right; ``CARD_UNKNOWN`` keeps Deny / the right button and
        shows the reason under them in amber; ``link_text`` names the result's
        link, "Open" by default), ``set_approve_text(text)`` ("Retry",
        "Sign in", "Done": a card built with ``check_line=True`` is as wide
        as any of them from the start; on a narrow card Deny / Approve may
        lose some side padding, never their text), ``set_check_line(text,
        tooltip="", warn=False)`` (a two-line line under the detail, reserved
        when the card is built with ``check_line=True``; ``warn`` draws it in
        amber; ``check_warns()``), ``set_texts`` and ``set_body`` (they may
        make the card taller, never shorter), ``set_copy_text(text)`` (show,
        rename or hide Copy after an edit), ``set_edit_enabled(bool)``,
        ``set_sign_in_visible(bool)`` (a card built with ``sign_in_text``),
        ``set_note(text)``, ``note()``, ``result_text()``, ``title()``,
        ``body()``, ``show_copied()``, signals ``approveClicked(str)``,
        ``denyClicked(str)``, ``openClicked(str)`` (the link),
        ``undoClicked(str)``, and ``sourceClicked(str)`` / ``copyClicked(str)``
        / ``editClicked(str)`` / ``signInClicked(str)`` (the action id, from the
        tools row's Open / Copy / Edit / Sign in links). Deny / Approve
        and the result (WORKING..., ADDED + Open, DENIED, ...) share one
        fixed-height slot, so a status change never changes the card's height
        and the cards below never move; the FAILED / UNKNOWN reason and the
        note sit under the buttons and may only make a card taller. The body
        preview (three lines), the check line and the tools row sit above the
        slot and never change height by themselves (the Sign in link keeps its
        place while hidden). ``set_locked(bool,
        tooltip)`` dims Deny / Approve (a click on them emits
        ``lockedClicked(str)``); the tools row is never locked. ``COPIED_TEXT``
        / ``COPIED_MS`` are the Copy feedback; ``plain_tooltip(text)`` makes a
        tooltip that keeps line breaks and is never read as HTML (every
        tooltip that can hold page or calendar text uses it).
        A Reply / Email card is built with ``mail=True`` (``body_lines``,
        ``body_links``, ``approve_alternatives``): ``set_recipients(sender, to,
        cc)`` shows FROM and the TO / CC chips (``RecipientChips`` of
        ``RecipientChip(address, state)``; ``RECIPIENT_NEW`` is red with a NEW
        RECIPIENT badge, ``RECIPIENT_CONFIRMED`` reads NEW \u00b7 CONFIRMED),
        and the drafted text keeps its line breaks, up to ``body_lines`` lines,
        its links highlighted (``mail_text()``).
        ``EditDialog(action_id, kind, kind_label, title, ...)``: the window-modal
        Edit dialog of an RSVP / Move / Cancel card, or of a Reply / Email
        (``EDIT_MAIL_KINDS``: FROM, To / Cc rows with Remove, Add and a "Send to"
        tick per new recipient, the subject and the message) (``open()``;
        ``saved(id, values)``, ``show_error(text)``, ``values()``). ``ActionList``:
        scrollable cards ``ActionList.CARD_GAP`` px apart with an empty-state
        line (``add_card``, ``card(id)``, ``cards()``, ``clear()``,
        ``set_empty_text``); ``add_group_header(title, count)`` puts a
        ``GroupHeader`` ("ASK \u00b7 2") before the cards added after it
        (``group_headers()``).
    ``CommandBar``: Ask Jarvis's typed bar: a chamfered field (at most
        ``COMMAND_MAX_LENGTH`` characters, plain text, ``COMMAND_PLACEHOLDER``)
        with its Ask button, a status line (``set_status(text, TONE_*)``, two
        lines, the whole text on hover) and a meta line with an optional link
        (``set_meta``, ``set_link(text, tooltip)``). Signals ``submitted(str)``
        (Enter or Ask, whitespace collapsed, never while running or empty),
        ``cancelRequested`` (Cancel or Esc while ``set_running(True)``),
        ``linkClicked`` and ``statusDismissed`` (Esc in an empty idle field);
        ``set_compact(True)`` for narrow or short screens; ``focus_input()``.
        Its height never changes with its texts.
    ``ActivityLog``: ``add(tag, message, sub="", when=None)`` newest first,
        at most ``ActivityLog.MAX_ENTRIES``; tags ``TAG_RUN``, ``TAG_DONE``,
        ``TAG_WAIT``, ``TAG_STOP``, ``TAG_ASK``; ``entries()``; ``set_hour24(bool)``.
    ``TabStrip(live=False)``: the JARVIS / BRIEFING folder tabs above the
        centre panel (``TAB_JARVIS``, ``TAB_BRIEFING``; ``currentChanged(int)``,
        ``set_current``, ``current()``, ``set_badge(bool)`` = the amber NEW
        pill on BRIEFING, ``set_unread(bool)`` = a dot on JARVIS,
        ``set_compact(bool)``, automatic below ``max(TabStrip.COMPACT_BELOW,
        natural_width())`` px, the natural width counting every badge so a dot
        never flips it); each tab is a ``TabButton`` (Left / Right move between
        the shown tabs). ``live=True`` adds LIVE (``TAB_LIVE``, accessible
        name "Live steps"): ``set_live_visible(bool)`` (hidden, it is never
        current and Left / Right skip it), ``set_activity(state)`` its dot
        (``ACTIVITY_UNREAD`` steady, ``ACTIVITY_WORKING`` blinking on the
        shared blink clock; none while LIVE is current), ``activity()``.
    ``LiveLog(clock=time.monotonic)``: the LIVE view (live.py's stream): one
        chamfered card per task (the pinned "Background reads" first, then
        oldest -> newest), its header a button (tag pill, the title in at most
        two lines, the status pill with the running time or "SENDING IN 7 S",
        the meta line; a compact task is one line until opened) and its steps
        as button rows (status glyph, title, summary, status word over the
        elapsed time; click, Space or Enter shows / hides the details: fields
        in two columns from ``LIVE_FIELDS_MEDIUM_PX`` up (the label column
        narrower below ``LIVE_FIELDS_WIDE_PX``), items, notes, and
        collapsible read-only text blocks made on their first open, at most
        ``LIVE_TEXT_MAX_PX`` tall, "written by other people" on untrusted
        ones). ``apply(live.LiveChanges)`` redoes only tasks whose revision
        changed; ``clear()``, ``set_hour24``, ``task_widget(id)``,
        ``step_widget(id)``, ``task_ids()``, ``reveal(task_id)``,
        ``following()``, ``scroll_to_newest()``, ``tick()``, ``ticking()``;
        ``linkClicked(str)`` (a result link or an internal id such as
        "tab:jarvis"). Plain text only (painted, PlainText labels, a read-only
        QPlainTextEdit); it follows the newest step unless the owner scrolled
        in the last 4 s (what came meanwhile is followed when the hold ends),
        keeps an approved card's payload in view during its undo countdown
        (a card just approved comes into view whatever was scrolled), shows its
        empty-state text while only the pinned row is there, and keeps its
        column at most ``LIVE_CONTENT_MAX_PX`` wide; its timer ticks every
        250 ms during a countdown, every second while a step runs, never while
        hidden, minimized or idle; Tab walks the rows in the order they are
        shown. A step with a picture shows it first in its details (a
        thumbnail button: Enter, Space or a click opens it larger);
        ``open_picture(step_id)`` shows a READY picture in the viewer,
        ``picture_viewer()`` (None until one was opened); during a countdown a
        short view shows the payload picture's changed part. A field's
        "picture:<step id>" link (a research's check pointing at a source's
        picture) is followed here (``follow_link``): that picture larger (its
        close gives the focus back to the link, scrolled into view), or that
        step (``reveal_step``) while its picture is not READY (the keyboard
        focus the link had goes along, to that step's picture).
    LIVE pictures (pictures.py's data, drawn in the HUD theme at the size
        shown): ``picture_height(picture, width, stamp=, max_height=)``,
        ``paint_picture(painter, rect, picture, stamp=, max_height=)``,
        ``picture_pixmap(picture, width, dpr=, stamp=, max_height=)`` (a picture
        taller than ``max_height`` is cut with a fade and "Enter: see all"),
        ``picture_focus(...)`` (where a calendar change's changed blocks are),
        ``week_label`` / ``week_labels`` (the week strip's day labels, one form
        for all), ``search_address_lines(hit, width)`` (a search result's
        address line: its site first, cut in the middle when too wide),
        ``picture_state_text(picture)``; the caps
        ``LIVE_PICTURE_MAX_W``, ``LIVE_PICTURE_MAIL_MAX_PX``,
        ``LIVE_PICTURE_DAY_MAX_PX``, ``LIVE_PICTURE_WEEK_MAX_PX``,
        ``LIVE_PICTURE_PAGE_MAX_PX``, ``LIVE_PICTURE_SEARCH_MAX_PX`` (a web
        search drawn as a compact results page), ``LIVE_PICTURE_PENDING_PX``.
        ``PictureViewer(parent)``: a picture larger inside Jarvis (non-modal;
        ``show_picture(step_id, picture, stamp, title)``, ``refresh``,
        ``step_id()``, ``picture()``, ``closed(step_id)``; Esc / Enter / Space /
        Ctrl+W close it; at most ``PICTURE_VIEWER_SCREEN`` of its screen, an
        email or calendar picture at most ``PICTURE_VIEWER_MODEL_W`` wide, a page
        at most ``PICTURE_VIEWER_PAGE_W``); it keeps nothing once closed.
    ``LiveStatus``: the LIVE page's tag in the strip (``set_state(LIVE_IDLE |
        LIVE_WORKING | LIVE_COUNTDOWN, text=None, since=, deadline=)``,
        ``text()``: "IDLE", "WORKING 0:12", "SENDING IN 7 S"); it blinks only
        while working or counting down. ``paint_status_glyph(painter, rect,
        status, opacity=1.0)`` draws live.STATUS_*'s glyph (running square, ok
        check, warn triangle, blocked octagon, failed cross, cancelled dash);
        ``live_status_color(status)``.
    ``ConversationLog``: Jarvis's conversation, oldest first, never elided
        (``add(role, text, when=, tone=, sub=, link_text=, link_id=) -> id``,
        ``update(id, ...)``, ``entries()``, ``clear()``, ``set_hour24``;
        roles ``ROLE_JARVIS`` / ``ROLE_YOU``; ``linkClicked(link_id)``); it
        follows the newest entry unless the owner scrolled in the last 4 s.
    ``HudChip(text, color)``: small chamfered tag (e.g. "2 NEED YOUR OK").
    ``FlowLayout(h_spacing, v_spacing)``: wrapping row for buttons, so a
        narrow column gets two rows instead of a wider window.

Notes for integration:
* Every clickable thing is a real QAbstractButton with Tab focus (HudButton,
  SectionRow, the close button), so a Space shortcut filter that checks
  ``isinstance(watched, QAbstractButton)`` covers them all. Focus rings show
  for keyboard focus only (Tab, Backtab, shortcuts), like CSS :focus-visible.
* PRIMARY / APPROVE glows are QGraphicsDropShadowEffects: a child cannot paint
  outside its parent, so leave ~8 px between such a button and the edge of
  the widget that holds it, or that side of the glow is cut off.
* Timers (orb animation, blinking, clock) only run while their widget is shown.
* Widths: the regular reading controls (Pause, Skip section, Read everything,
  Open in Notion, Done) need ~570 px in one row (~505 compact); put them in a
  FlowLayout or give the center column that much room.
"""

from __future__ import annotations

import dataclasses
import html
import logging
import math
import re
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import shiboken6
from PySide6.QtCore import (
    QEvent,
    QLineF,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QConicalGradient,
    QFont,
    QFontDatabase,
    QFontInfo,
    QFontMetricsF,
    QImage,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
    QRadialGradient,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextLayout,
    QTextOption,
    QTransform,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QGraphicsBlurEffect,
    QGraphicsDropShadowEffect,
    QGraphicsScene,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from . import live as live_events
from . import pictures as live_pictures
from .text_prep import clock_parts, format_time

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------

GROUND = "#020509"
TEXT_BRIGHT = "#eaffff"
TEXT_BODY = "#d9f6ff"
TEXT_SOFT = "#a9d8e6"
TEXT_MUTED = "#7fb3c4"
TEXT_DIM = "#5f8a99"
TEXT_SUB = "#6f9aab"          # activity sub-lines
TEXT_TIME = "#7a8692"         # activity timestamps (artboard #6b7682, lifted for contrast)
BUTTON_TEXT = "#e6edf3"

ACCENT = "#22d3ee"
ACCENT_HOVER = "#67e8f9"
ACCENT_PRESSED = "#06b6d4"
ACCENT_INK = "#021016"        # text on accent fills

AMBER = "#fbbf24"
AMBER_HOVER = "#fcd34d"
AMBER_PRESSED = "#f59e0b"
AMBER_INK = "#1a1203"
AMBER_TEXT = "#fff7e6"
AMBER_SUB = "#b8a67a"
AMBER_META = "#a38a52"

GREEN = "#86efac"
RED = "#fca5a5"
RED_LINE = "#f87171"
PINK = "#f5d0fe"
LAVENDER = "#c4b5fd"         # LIVE's PLAN tag (the productivity bundle's day plan)
PEACH = "#fdba74"            # LIVE's PREP tag (the productivity bundle's meeting brief)
SLATE = "#64748b"
OFF = "#475569"
STEP_DIM = "#3a4450"

GRID_STEP = 48
MIDDLE_DOT = chr(0xB7)


def rgba(color: str | QColor, alpha: float) -> QColor:
    """``color`` with alpha ``alpha`` (0..1)."""
    result = QColor(color)
    result.setAlphaF(max(0.0, min(1.0, alpha)))
    return result


PANEL_GROUND = rgba("#030a10", 0.94)
AMBER_GROUND = rgba("#0c0903", 0.94)

# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------

DISPLAY_FAMILY = "Chakra Petch"
BODY_FAMILY = "Sora"
MONO_FAMILY = "JetBrains Mono"
FONT_FILES = (
    "ChakraPetch-Medium.ttf",
    "ChakraPetch-SemiBold.ttf",
    "Sora[wght].ttf",
    "JetBrainsMono[wght].ttf",
)
_FALLBACKS: dict[str, tuple[str, ...]] = {
    DISPLAY_FAMILY: ("Bahnschrift", "Segoe UI"),
    BODY_FAMILY: ("Segoe UI",),
    MONO_FAMILY: ("Cascadia Mono", "Consolas"),
}
_loaded_font_dirs: set[str] = set()


def load_fonts(fonts_dir: Path) -> dict[str, str]:
    """Register the bundled TTFs; returns {wanted family: family that will be used}.

    Never raises: a missing or broken file is logged and the font helpers fall
    back to the next family in their list. Loading the same folder twice is a no-op.
    """
    folder = Path(fonts_dir)
    key = str(folder.resolve())
    if key not in _loaded_font_dirs:
        _loaded_font_dirs.add(key)
        for name in FONT_FILES:
            _add_font_file(folder / name)
    available = set(QFontDatabase.families())
    used = {}
    for family, fallbacks in _FALLBACKS.items():
        choice = next((name for name in (family, *fallbacks) if name in available), "")
        if choice != family:
            logger.warning("Font %s is not available; using %s", family, choice or "the default font")
        used[family] = choice or family
    return used


def _add_font_file(path: Path) -> None:
    if not path.is_file():
        logger.warning("Font file missing: %s", path)
        return
    try:
        font_id = QFontDatabase.addApplicationFont(str(path))
    except Exception as exc:  # noqa: BLE001 - a font must never stop the app
        logger.warning("Could not load font %s: %s", path.name, exc)
        return
    if font_id < 0:
        logger.warning("Could not load font %s", path.name)
    else:
        logger.debug("Loaded font %s: %s", path.name, QFontDatabase.applicationFontFamilies(font_id))


def _font(family: str, px: int, weight: int, spacing: float) -> QFont:
    font = QFont()
    font.setFamilies([family, *_FALLBACKS.get(family, ())])
    font.setPixelSize(px)
    font.setWeight(QFont.Weight(weight))
    if spacing:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, px * spacing)
    return font


def display_font(px: int = 20, weight: int = 600, spacing: float = 0.0) -> QFont:
    """Chakra Petch (wordmark, display headings); ``spacing`` in em."""
    return _font(DISPLAY_FAMILY, px, weight, spacing)


def body_font(px: int = 14, weight: int = 400, spacing: float = 0.0) -> QFont:
    """Sora (speech line, body text, titles)."""
    return _font(BODY_FAMILY, px, weight, spacing)


def mono_font(px: int = 11, weight: int = 400, spacing: float = 0.0) -> QFont:
    """JetBrains Mono (labels, meta, numbers, activity)."""
    return _font(MONO_FAMILY, px, weight, spacing)


def _qss_color(color: str | QColor) -> str:
    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {c.alpha()})"


_STYLESHEET = """
QToolTip {
    color: @TEXT_BODY; background-color: #06121a; border: 1px solid @TIP_BORDER;
    padding: 4px 6px; font-family: "JetBrains Mono", "Cascadia Mono", Consolas; font-size: 11px;
}
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px 1px 2px 0px; }
QScrollBar::handle:vertical { background: @HANDLE; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: @HANDLE_HOVER; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 0px 2px 1px 2px; }
QScrollBar::handle:horizontal { background: @HANDLE; min-width: 28px; }
QScrollBar::handle:horizontal:hover { background: @HANDLE_HOVER; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QMenu { color: @TEXT_BODY; background-color: #050d14; border: 1px solid @TIP_BORDER; padding: 4px; }
QMenu::item { padding: 6px 18px; }
QMenu::item:selected { background-color: @MENU_SELECTED; color: @TEXT_BRIGHT; }
QMenu::separator { height: 1px; background: @TIP_BORDER; margin: 4px 6px; }
QTextBrowser {
    background: transparent; border: none; color: @TEXT_BODY;
    selection-background-color: @SELECTION; selection-color: @TEXT_BRIGHT;
}
QScrollArea#hudScroll { background: transparent; border: none; }
"""


def stylesheet() -> str:
    """The application stylesheet (tooltips, scroll bars, menus, text browsers)."""
    tokens = {
        "TEXT_BODY": TEXT_BODY, "TEXT_BRIGHT": TEXT_BRIGHT,
        "TIP_BORDER": _qss_color(rgba(ACCENT, 0.35)), "HANDLE": _qss_color(rgba(ACCENT, 0.22)),
        "HANDLE_HOVER": _qss_color(rgba(ACCENT, 0.45)), "MENU_SELECTED": _qss_color(rgba(ACCENT, 0.16)),
        "SELECTION": _qss_color(rgba(ACCENT, 0.32)),
    }
    text = _STYLESHEET
    for name in sorted(tokens, key=len, reverse=True):
        text = text.replace("@" + name, tokens[name])
    return text


def hud_palette() -> QPalette:
    role = QPalette.ColorRole
    colors = {
        role.Window: GROUND, role.WindowText: TEXT_BODY, role.Base: "#030a10", role.AlternateBase: GROUND,
        role.ToolTipBase: "#06121a", role.ToolTipText: TEXT_BODY, role.PlaceholderText: TEXT_DIM,
        role.Text: TEXT_BODY, role.Button: "#06121a", role.ButtonText: TEXT_BODY, role.BrightText: TEXT_BRIGHT,
        role.Highlight: "#0f4b59", role.HighlightedText: TEXT_BRIGHT, role.Link: ACCENT,
        role.LinkVisited: ACCENT, role.Light: "#12303a", role.Midlight: "#0c222a", role.Mid: "#0a1a21",
        role.Dark: "#01030a", role.Shadow: "#000000",
    }
    palette = QPalette()
    for color_role, color in colors.items():
        palette.setColor(color_role, QColor(color))
    for color_role in (role.WindowText, role.Text, role.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, color_role, QColor(TEXT_DIM))
    return palette


def apply_theme(app: QApplication) -> None:
    """Fusion style, the HUD palette, Sora 13 px and the HUD stylesheet."""
    app.setStyle("Fusion")
    app.setPalette(hud_palette())
    app.setFont(body_font(13))
    app.setStyleSheet(stylesheet())


def make_label(text: str = "", font: QFont | None = None, color: str | QColor = TEXT_BODY, *,
               wrap: bool = False, name: str = "") -> QLabel:
    """A plain-text label (page text is never interpreted as HTML)."""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)
    if name:
        label.setObjectName(name)
    if font is not None:
        label.setFont(font)
    set_label_color(label, color)
    return label


def set_label_color(label: QWidget, color: str | QColor) -> None:
    """Text colour through a widget stylesheet (wins over any app stylesheet rule)."""
    label.setStyleSheet(f"color: {_qss_color(color)}; background: transparent;")


def plain_tooltip(text: str) -> str:
    """``text`` as a tooltip that keeps its line breaks and is never read as HTML.

    QToolTip guesses the format (Qt.AutoText), so page text with "<img src=...>" given to
    setToolTip as it is would be rendered, and the image file fetched, on a mere hover.
    """
    return f'<p style="white-space:pre-wrap">{html.escape(text)}</p>' if text else ""


class SpeechLabel(QLabel):
    """Word-wrapped prose with a CSS-like line height (Sora Light 20 px by default).

    The text is escaped and shown as rich text only to get the line height;
    ``text()`` returns exactly what was passed to ``setText``. With
    ``max_lines`` the text is cut to that many lines at the current width,
    ending in an ellipsis (the full text becomes the tooltip).
    """

    def __init__(self, text: str = "", *, font: QFont | None = None, color: str | QColor = TEXT_BRIGHT,
                 line_height: float = 1.4, max_lines: int = 0, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._plain = ""
        self._line_height = line_height
        self._max_lines = max_lines
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setFont(font or body_font(20, 300))
        set_label_color(self, color)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - mirrors QLabel
        self._plain = text or ""
        self.setAccessibleName(self._plain)
        self._render()

    def text(self) -> str:  # noqa: D102 - mirrors QLabel
        return self._plain

    def shown_text(self) -> str:
        """The text as displayed (elided when it needs more than ``max_lines``)."""
        return self._elided(self._plain) if self._max_lines else self._plain

    def set_max_lines(self, max_lines: int) -> None:
        self._max_lines = max_lines
        self._render()

    def _line_percent(self) -> int:
        # CSS line-height is relative to the font size; Qt's percentage is
        # relative to the font's own line height (ascent + descent).
        natural = QFontMetricsF(self.font()).height()
        pixels = QFontInfo(self.font()).pixelSize()
        return round(100 * self._line_height * pixels / natural) if natural > 0 else 100

    def _render(self) -> None:
        shown = self.shown_text()
        self.setToolTip(plain_tooltip(self._plain) if shown != self._plain else "")
        body = html.escape(shown).replace("\n", "<br>")
        super().setText(f'<div style="line-height:{self._line_percent()}%;">{body}</div>')

    def _elided(self, text: str) -> str:
        # A little slack against rounding in QLabel's own wrap.
        return _elide_to_lines(text, self.font(), self.contentsRect().width() - 2, self._max_lines)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if self._max_lines and event.oldSize().width() != event.size().width():
            self._render()


def _elide_to_lines(text: str, font: QFont, width: int, max_lines: int) -> str:
    """``text`` cut to ``max_lines`` word-wrapped lines of ``width`` px, ending in an ellipsis."""
    if width <= 0 or not text or max_lines <= 0:
        return text
    flat = text.replace("\n", " ")
    layout = QTextLayout(flat, font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WordWrap)
    layout.setTextOption(option)
    starts: list[int] = []
    layout.beginLayout()
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        starts.append(line.textStart())
    layout.endLayout()
    if len(starts) <= max_lines:
        return text
    start = starts[max_lines - 1]
    tail = QFontMetricsF(font).elidedText(flat[start:], Qt.TextElideMode.ElideRight, width)
    return flat[:start] + tail


# --------------------------------------------------------------------------
# Painting helpers
# --------------------------------------------------------------------------

CUT_TL = 1
CUT_TR = 2
CUT_BR = 4
CUT_BL = 8
CUT_DIAGONAL = CUT_TL | CUT_BR
CUT_TOP = CUT_TL | CUT_TR


def chamfer_path(rect: QRectF, cut: float, corners: int = CUT_DIAGONAL) -> QPainterPath:
    """``rect`` with the chosen corners cut at 45 degrees by ``cut`` px (like the CSS clip-path)."""
    c = max(0.0, min(float(cut), rect.width() / 2, rect.height() / 2))
    left, top, right, bottom = rect.left(), rect.top(), rect.right(), rect.bottom()
    corner_points = (
        (CUT_TL, (left, top), ((left, top + c), (left + c, top))),
        (CUT_TR, (right, top), ((right - c, top), (right, top + c))),
        (CUT_BR, (right, bottom), ((right, bottom - c), (right - c, bottom))),
        (CUT_BL, (left, bottom), ((left + c, bottom), (left, bottom - c))),
    )
    points: list[QPointF] = []
    for flag, square, cut_points in corner_points:
        for x, y in (cut_points if corners & flag and c > 0 else (square,)):
            points.append(QPointF(x, y))
    path = QPainterPath()
    path.addPolygon(QPolygonF(points))
    path.closeSubpath()
    return path


def paint_ground(painter: QPainter, rect: QRectF, glow_center: QPointF | None = None,
                 glow_radii: tuple[float, float] | None = None) -> None:
    """The HUD ground: colour, a 48 px grid of faint lines and a soft radial cyan glow.

    Grid lines sit at multiples of 48 px from ``rect``'s top-left. Without
    ``glow_center`` the glow sits at 50 % / 42 % like the artboard; the default
    radii are 53 % of the width and height (760 x 480 on 1440 x 900).
    """
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.fillRect(rect, QColor(GROUND))
    line = rgba(ACCENT, 0.03)
    x = rect.left()
    while x < rect.right():
        painter.fillRect(QRectF(x, rect.top(), 1, rect.height()), line)
        x += GRID_STEP
    y = rect.top()
    while y < rect.bottom():
        painter.fillRect(QRectF(rect.left(), y, rect.width(), 1), line)
        y += GRID_STEP
    center = glow_center or QPointF(rect.left() + rect.width() * 0.5, rect.top() + rect.height() * 0.42)
    rx, ry = glow_radii or (rect.width() * 0.528, rect.height() * 0.533)
    if rx > 0 and ry > 0:
        gradient = QRadialGradient(center, rx)
        gradient.setColorAt(0.0, rgba(ACCENT, 0.10))
        gradient.setColorAt(0.7, rgba(ACCENT, 0.0))
        gradient.setColorAt(1.0, rgba(ACCENT, 0.0))
        squash = QTransform()
        squash.translate(center.x(), center.y())
        squash.scale(1.0, ry / rx)
        squash.translate(-center.x(), -center.y())
        brush = QBrush(gradient)
        brush.setTransform(squash)
        painter.fillRect(rect, brush)
    painter.restore()


_GLOW_CACHE: OrderedDict[tuple, tuple[QPixmap, QPointF]] = OrderedDict()
_GLOW_CACHE_MAX = 128


def _path_key(path: QPainterPath) -> tuple:
    elements = (path.elementAt(i) for i in range(path.elementCount()))
    return tuple((round(e.x, 2), round(e.y, 2), int(e.type.value)) for e in elements)


def _blur(image: QImage, radius: float) -> QImage:
    """Gaussian-like blur through QGraphicsBlurEffect (only used for cached glows)."""
    # Everything is owned by the scene (the effect via its QObject parent), so
    # deleting the scene frees it all once, whatever order Python drops the names in.
    scene = QGraphicsScene()
    effect = QGraphicsBlurEffect(scene)
    effect.setBlurRadius(radius)
    effect.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
    item = scene.addPixmap(QPixmap.fromImage(image))
    item.setGraphicsEffect(effect)
    out = QImage(image.size(), QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    try:
        scene.render(painter, QRectF(out.rect()), QRectF(0, 0, image.width(), image.height()))
    finally:
        painter.end()
    return out


def glow_pixmap(path: QPainterPath, color: str | QColor, blur: float, *, dpr: float = 1.0,
                key: Any = None) -> tuple[QPixmap, QPointF]:
    """A blurred, filled copy of ``path`` and the top-left point to draw it at.

    Mimics CSS ``box-shadow: 0 0 <blur>px <color>`` / ``text-shadow``. Results
    are cached (LRU) by ``key`` (default: the path's geometry).
    """
    color = QColor(color)
    cache_key = (key if key is not None else _path_key(path), color.rgba(), round(blur, 2), round(dpr, 3))
    cached = _GLOW_CACHE.get(cache_key)
    if cached is not None:
        _GLOW_CACHE.move_to_end(cache_key)
        return cached
    pad = math.ceil(blur * 1.6) + 2
    bounds = path.boundingRect()
    origin = QPointF(math.floor(bounds.left()) - pad, math.floor(bounds.top()) - pad)
    width = math.ceil(bounds.right()) + pad - origin.x()
    height = math.ceil(bounds.bottom()) + pad - origin.y()
    image = QImage(max(1, round(width * dpr)), max(1, round(height * dpr)),
                   QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(dpr, dpr)
        painter.translate(-origin)
        painter.fillPath(path, color)
    finally:
        painter.end()
    pixmap = QPixmap.fromImage(_blur(image, blur * dpr) if blur > 0 else image)
    pixmap.setDevicePixelRatio(dpr)
    _GLOW_CACHE[cache_key] = (pixmap, origin)
    while len(_GLOW_CACHE) > _GLOW_CACHE_MAX:
        _GLOW_CACHE.popitem(last=False)
    return pixmap, origin


def _draw_glow(painter: QPainter, path: QPainterPath, color: str | QColor, blur: float, *,
               dpr: float, key: Any = None) -> None:
    pixmap, origin = glow_pixmap(path, color, blur, dpr=dpr, key=key)
    painter.drawPixmap(origin, pixmap)


def paint_row_highlight(painter: QPainter, rect: QRectF, color: str | QColor = ACCENT, *,
                        strength: float = 1.0, bar_width: float = 3.0, glow: bool = True,
                        dpr: float = 1.0) -> None:
    """The artboard's agenda row: a glowing ``bar_width`` px bar on the left and a gradient.

    ``strength`` scales the gradient (1.0 = the current item, ~0.4 = a quiet row).
    """
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    gradient = QLinearGradient(rect.topLeft(), rect.topRight())
    gradient.setColorAt(0.0, rgba(color, 0.12 * strength))
    gradient.setColorAt(1.0, rgba(color, 0.0))
    painter.fillRect(rect, gradient)
    bar = QRectF(rect.left(), rect.top(), bar_width, rect.height())
    if glow:
        path = QPainterPath()
        path.addRect(QRectF(0, 0, bar.width(), bar.height()))
        painter.translate(bar.topLeft())
        _draw_glow(painter, path, rgba(color, 0.85), 10, dpr=dpr,
                   key=("bar", round(bar.width(), 1), round(bar.height(), 1)))
        painter.translate(-bar.topLeft())
    painter.fillRect(bar, QColor(color))
    painter.restore()


def paint_dash_bar(painter: QPainter, rect: QRectF, fraction: float, color: str | QColor) -> None:
    """Segmented bar: 6 px dashes, 2 px gaps; a faint track and ``fraction`` filled in ``color``."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    track = rgba(ACCENT, 0.12)
    fill_until = rect.left() + rect.width() * max(0.0, min(1.0, fraction))
    fill = QColor(color)
    x = rect.left()
    while x < rect.right():
        dash_end = min(x + 6, rect.right())
        painter.fillRect(QRectF(x, rect.top(), dash_end - x, rect.height()), track)
        if x < fill_until:
            painter.fillRect(QRectF(x, rect.top(), min(dash_end, fill_until) - x, rect.height()), fill)
        x += 8
    painter.restore()


def _text_advance(font: QFont, text: str) -> float:
    return QFontMetricsF(font).horizontalAdvance(text)


# Times with the widest text on each clock: two-digit hours, both halves of the day.
_WIDEST_TIMES = (datetime(2000, 1, 1, 0, 0), datetime(2000, 1, 1, 12, 59), datetime(2000, 1, 1, 23, 59))


def _clock_width(font: QFont, hour24: bool, part: int | None = None) -> float:
    """The advance of the widest time on that clock: the whole text ("12:59 PM" / "23:59"),
    or one part of it (0: the digits, 1: "AM" / "PM", "" on the 24-hour clock)."""
    texts = (format_time(moment, hour24=hour24) if part is None else clock_parts(moment, hour24=hour24)[part]
             for moment in _WIDEST_TIMES)
    return max(_text_advance(font, text) for text in texts)


def _line_height(font: QFont) -> float:
    return QFontMetricsF(font).height()


_KEYBOARD_FOCUS = (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                   Qt.FocusReason.ShortcutFocusReason)


def _focus_ring_after(reason: Qt.FocusReason, previous: bool) -> bool:
    """CSS :focus-visible: a ring for keyboard focus, none for mouse or programmatic focus.

    Re-activating the window keeps whatever the focus looked like before.
    """
    if reason in _KEYBOARD_FOCUS:
        return True
    if reason == Qt.FocusReason.ActiveWindowFocusReason:
        return previous
    return False


# --------------------------------------------------------------------------
# Shared blink clock (state squares, LIVE dot)
# --------------------------------------------------------------------------

class _BlinkClock(QObject):
    """One 250 ms timer for every blinking widget, running only while one is shown.

    The phase comes from the monotonic clock, so all blinkers stay in step.
    """

    def __init__(self) -> None:
        super().__init__()
        # Weak: a widget deleted while shown (no hide event) must not be kept alive here.
        self._widgets: weakref.WeakSet[QWidget] = weakref.WeakSet()
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._tick)
        self._last_on: bool | None = None

    @staticmethod
    def is_on() -> bool:
        return (time.monotonic() % 1.0) < 0.5

    def register(self, widget: QWidget) -> None:
        self._widgets.add(widget)
        try:
            if not self._timer.isActive():
                self._timer.start()
        except RuntimeError:   # the timer is already gone while Qt shuts down
            pass

    def unregister(self, widget: QWidget) -> None:
        self._widgets.discard(widget)
        try:
            if not self._widgets:
                self._timer.stop()
        except RuntimeError:   # widgets are hidden during shutdown, after the timer died
            pass

    def active(self) -> bool:
        try:
            return self._timer.isActive()
        except RuntimeError:
            return False

    def _tick(self) -> None:
        on = self.is_on()
        if on == self._last_on:
            return
        self._last_on = on
        for widget in list(self._widgets):
            if shiboken6.isValid(widget):
                widget.update()
            else:
                self.unregister(widget)


_blink_clock_instance: _BlinkClock | None = None


def _blink_clock() -> _BlinkClock:
    global _blink_clock_instance
    if _blink_clock_instance is None:
        _blink_clock_instance = _BlinkClock()
    return _blink_clock_instance


class _Blinking(QWidget):
    """Base for widgets with a 1 s on/off blink (CSS ``steps(1)``: 100 % / 15 % opacity)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._blinking = True
        self._frozen_on: bool | None = None

    def set_blinking(self, blinking: bool) -> None:
        self._blinking = blinking
        self._sync_blink()
        self.update()

    def freeze_blink(self, on: bool | None) -> None:
        """Pin the blink phase (screenshots); None resumes."""
        self._frozen_on = on
        self._sync_blink()
        self.update()

    def blink_opacity(self) -> float:
        if not self._blinking:
            return 1.0
        on = self._frozen_on if self._frozen_on is not None else _BlinkClock.is_on()
        return 1.0 if on else 0.15

    def _sync_blink(self) -> None:
        clock = _blink_clock()
        if self._blinking and self._frozen_on is None and self.isVisible():
            clock.register(self)
        else:
            clock.unregister(self)

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._sync_blink()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        _blink_clock().unregister(self)


# --------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------

PRIMARY = "primary"
SECONDARY = "secondary"
DENY = "deny"
APPROVE = "approve"
LINK = "link"
BUTTON_VARIANTS = (PRIMARY, SECONDARY, DENY, APPROVE, LINK)


@dataclass(frozen=True)
class _ButtonSpec:
    font: Callable[[], QFont]
    height: int
    padding: int
    cut: int


_BUTTON_SPECS: dict[tuple[str, bool], _ButtonSpec] = {
    (PRIMARY, False): _ButtonSpec(lambda: mono_font(12, 700, 0.14), 40, 18, 8),
    (PRIMARY, True): _ButtonSpec(lambda: mono_font(11, 700, 0.14), 36, 14, 6),
    (SECONDARY, False): _ButtonSpec(lambda: body_font(13, 400, 0.02), 40, 16, 8),
    (SECONDARY, True): _ButtonSpec(lambda: body_font(12, 400, 0.02), 36, 12, 6),
    (DENY, False): _ButtonSpec(lambda: mono_font(12, 400, 0.12), 40, 16, 8),
    (DENY, True): _ButtonSpec(lambda: mono_font(11, 400, 0.12), 36, 12, 6),
    (APPROVE, False): _ButtonSpec(lambda: mono_font(12, 700, 0.14), 40, 18, 8),
    (APPROVE, True): _ButtonSpec(lambda: mono_font(11, 700, 0.14), 36, 14, 6),
    (LINK, False): _ButtonSpec(lambda: mono_font(11, 400, 0.12), 24, 2, 0),
    (LINK, True): _ButtonSpec(lambda: mono_font(10, 400, 0.12), 22, 2, 0),
}
_GLOWS = {PRIMARY: (ACCENT, 0.55, 22.0), APPROVE: (AMBER, 0.5, 16.0)}


class HudButton(QPushButton):
    """A chamfered HUD button; looks are chosen by ``variant`` (see BUTTON_VARIANTS).

    Never a default button, focus only via Tab, pointing-hand cursor. The glow
    of PRIMARY/APPROVE is a drop-shadow effect, so it may extend outside the
    button; the keyboard focus ring is drawn inside the button's rect. The HUD
    font is kept in ``self._font`` and used for painting and sizing, so an app
    stylesheet cannot change it.
    """

    def __init__(self, text: str = "", variant: str = SECONDARY, *, compact: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setAutoDefault(False)
        self.setDefault(False)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._variant = variant
        self._compact = compact
        self._selected = False
        self._focus_visible = False
        self._link_color = QColor(TEXT_MUTED)
        self._spec = _BUTTON_SPECS[(variant, compact)]
        self._font = self._spec.font()
        self._apply_variant()

    # ---- configuration --------------------------------------------------------

    def variant(self) -> str:
        return self._variant

    def set_variant(self, variant: str) -> None:
        if variant != self._variant:
            self._variant = variant
            self._apply_variant()

    def set_selected(self, selected: bool) -> None:
        """Secondary buttons only: accent border and brighter text."""
        if selected != self._selected:
            self._selected = selected
            self.update()

    def is_selected(self) -> bool:
        return self._selected

    def set_link_color(self, color: str | QColor) -> None:
        self._link_color = QColor(color)
        self.update()

    def _apply_variant(self) -> None:
        self._spec = _BUTTON_SPECS[(self._variant, self._compact)]
        self._font = self._spec.font()
        self.setFont(self._font)
        glow = _GLOWS.get(self._variant)
        if glow is None:
            self.setGraphicsEffect(None)
        else:
            color, alpha, blur = glow
            effect = QGraphicsDropShadowEffect(self)
            effect.setOffset(0, 0)
            effect.setBlurRadius(blur)
            effect.setColor(rgba(color, alpha))
            effect.setEnabled(self.isEnabled())
            self.setGraphicsEffect(effect)
        self.updateGeometry()
        self.update()

    # ---- size -----------------------------------------------------------------

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        text = self.text().replace("&&", "\0").replace("&", "").replace("\0", "&")
        width = math.ceil(_text_advance(self._font, text)) + 2 * self._spec.padding
        return QSize(max(width, self._spec.height), self._spec.height)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    # ---- state tracking -----------------------------------------------------------

    def show_focus_ring(self) -> None:
        """Draw the keyboard focus ring now, though the focus came by program: the app moved the
        focus here for someone using the keyboard, who must see where the next key lands (R1)."""
        self._focus_visible = True
        self.update()

    def focusInEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._focus_visible = _focus_ring_after(event.reason(), self._focus_visible)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().focusOutEvent(event)
        self.update()

    def enterEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().leaveEvent(event)
        self.update()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt override
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange:
            effect = self.graphicsEffect()
            if effect is not None:
                effect.setEnabled(self.isEnabled())

    # ---- painting ---------------------------------------------------------------

    def _colors(self) -> tuple[QColor | None, QColor | None, QColor]:
        """(fill, border, text) for the current variant and state."""
        enabled, down = self.isEnabled(), self.isDown()
        hover = enabled and self.underMouse() and not down
        variant = self._variant
        if variant == PRIMARY:
            if not enabled:
                return rgba(ACCENT, 0.16), None, rgba(TEXT_BRIGHT, 0.38)
            fill = ACCENT_PRESSED if down else ACCENT_HOVER if hover else ACCENT
            return QColor(fill), None, QColor(ACCENT_INK)
        if variant == APPROVE:
            if not enabled:
                return rgba(AMBER, 0.16), None, rgba(AMBER_TEXT, 0.38)
            fill = AMBER_PRESSED if down else AMBER_HOVER if hover else AMBER
            return QColor(fill), None, QColor(AMBER_INK)
        if variant == DENY:
            if not enabled:
                return None, rgba(RED_LINE, 0.22), rgba(RED, 0.4)
            fill = rgba(RED_LINE, 0.18 if down else 0.10) if (down or hover) else None
            return fill, rgba(RED_LINE, 0.8 if hover or down else 0.55), QColor(RED)
        if variant == LINK:
            if not enabled:
                return None, None, QColor(TEXT_DIM)
            return None, None, QColor(TEXT_BRIGHT) if hover or down else QColor(self._link_color)
        if not enabled:   # SECONDARY
            return None, rgba("#ffffff", 0.06), QColor(TEXT_DIM)
        fill = rgba("#ffffff", 0.10 if down else 0.06) if (down or hover or self._selected) else None
        if self._selected:
            border = QColor(ACCENT)
        else:
            border = rgba(ACCENT, 0.5) if hover or down else rgba("#ffffff", 0.12)
        text = BUTTON_TEXT if hover or down or self._selected else TEXT_SOFT
        return fill, border, QColor(text)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect())
            fill, border, text_color = self._colors()
            cut = self._spec.cut
            if fill is not None:
                painter.fillPath(chamfer_path(rect, cut), fill)
            if border is not None:
                painter.setPen(QPen(border, 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(chamfer_path(rect.adjusted(0.5, 0.5, -0.5, -0.5), cut))
            font = QFont(self._font)
            focused = self.hasFocus() and self._focus_visible
            if self._variant == LINK and ((self.isEnabled() and self.underMouse()) or focused):
                font.setUnderline(True)
            painter.setFont(font)
            painter.setPen(text_color)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextShowMnemonic, self.text())
            if focused:
                self._paint_focus(painter, rect)
        finally:
            painter.end()

    def _paint_focus(self, painter: QPainter, rect: QRectF) -> None:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self._variant in (PRIMARY, APPROVE) and self.isEnabled():
            ink = ACCENT_INK if self._variant == PRIMARY else AMBER_INK
            painter.setPen(QPen(QColor(ink), 2))
            painter.drawPath(chamfer_path(rect.adjusted(3, 3, -3, -3), max(0, self._spec.cut - 2)))
        elif self._variant != LINK:
            painter.setPen(QPen(QColor(ACCENT), 2))
            painter.drawPath(chamfer_path(rect.adjusted(1, 1, -1, -1), self._spec.cut))


class _HeaderButton(QPushButton):
    """A 30 px window button of the header: a stroked glyph, a chamfered fill in the button's own
    colour on hover / press, and the focus ring after Tab. Subclasses set the name, the colour and
    ``_draw_glyph``."""

    _NAME = ""
    _LINE = ACCENT        # hover / press fill
    _ACTIVE = ACCENT      # glyph while hovered or pressed (TEXT_MUTED otherwise)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAutoDefault(False)
        self.setDefault(False)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAccessibleName(self._NAME)
        self.setToolTip(self._NAME)
        self.setFixedSize(30, 30)
        self._focus_visible = False

    def focusInEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._focus_visible = _focus_ring_after(event.reason(), self._focus_visible)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().focusOutEvent(event)
        self.update()

    def enterEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().leaveEvent(event)
        self.update()

    def _draw_glyph(self, painter: QPainter, center: QPointF) -> None:
        raise NotImplementedError

    def _glyph_color(self, active: bool) -> str:
        return self._ACTIVE if active else TEXT_MUTED

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect())
            active = self.underMouse() or self.isDown()
            if active:
                painter.fillPath(chamfer_path(rect, 6), rgba(self._LINE, 0.18 if self.isDown() else 0.12))
            pen = QPen(QColor(self._glyph_color(active)), 1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            self._draw_glyph(painter, rect.center())
            if self.hasFocus() and self._focus_visible:
                painter.setPen(QPen(QColor(ACCENT), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(chamfer_path(rect.adjusted(1, 1, -1, -1), 6))
        finally:
            painter.end()


class _CloseButton(_HeaderButton):
    """The header's close button: an X drawn as two strokes, red on hover."""

    _NAME = "Close"
    _LINE = RED_LINE
    _ACTIVE = RED

    def _draw_glyph(self, painter: QPainter, center: QPointF) -> None:
        c, r = center, 5.0
        painter.drawLine(QPointF(c.x() - r, c.y() - r), QPointF(c.x() + r, c.y() + r))
        painter.drawLine(QPointF(c.x() - r, c.y() + r), QPointF(c.x() + r, c.y() - r))


class _MinimizeButton(_HeaderButton):
    """The header's minimize button, left of the close button: one bar as wide as the X, cyan on
    hover (red stays the close button's)."""

    _NAME = "Minimize"

    def _draw_glyph(self, painter: QPainter, center: QPointF) -> None:
        y = math.floor(center.y()) + 0.5   # on a pixel row at 100 %, so the bar stays crisp
        painter.drawLine(QPointF(center.x() - 5.0, y), QPointF(center.x() + 5.0, y))


SPEAKER_ON_TIP = "Mute Jarvis's voice (the briefing still plays when you press Play)"
SPEAKER_MUTED_TIP = "Jarvis is muted - click to let him speak"


class _SpeakerButton(_HeaderButton):
    """The header's speaker button, left of minimize: Jarvis's own voice on (a speaker with two
    arcs, cyan) or muted (the speaker struck through, amber). It only switches what Jarvis says on
    his own; the briefing still plays when you press Play. ``muted()`` / ``set_muted``."""

    _NAME = SPEAKER_ON_TIP
    _LINE = ACCENT

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._muted = False
        self.set_muted(False)

    def muted(self) -> bool:
        return self._muted

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        tip = SPEAKER_MUTED_TIP if self._muted else SPEAKER_ON_TIP
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.update()

    def _glyph_color(self, active: bool) -> str:
        return AMBER if self._muted else ACCENT

    def _draw_glyph(self, painter: QPainter, center: QPointF) -> None:
        x, y = center.x() - 3.0, center.y()   # the speaker sits left of centre, the arcs to its right
        body = QPolygonF([QPointF(x - 4.5, y - 2.5), QPointF(x - 1.5, y - 2.5), QPointF(x + 2.5, y - 6.0),
                          QPointF(x + 2.5, y + 6.0), QPointF(x - 1.5, y + 2.5), QPointF(x - 4.5, y + 2.5)])
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolygon(body)
        if self._muted:   # struck through: one slash across the speaker and where its arcs were
            painter.drawLine(QPointF(x - 6.5, y - 7.5), QPointF(x + 11.0, y + 7.5))
            return
        for radius in (4.5, 8.0):
            arc = QRectF(x + 2.5 - radius, y - radius, 2 * radius, 2 * radius)
            painter.drawArc(arc, -50 * 16, 100 * 16)


class FlowLayout(QLayout):
    """Left-to-right layout that wraps onto further rows when narrow (Qt's flow layout).

    Meant for button rows that must never force their window wider; it reports
    heightForWidth, so the rows below move down when it wraps.
    """

    def __init__(self, parent: QWidget | None = None, h_spacing: int = 6, v_spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 - Qt override
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 - Qt override
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 - Qt override
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802 - Qt override
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 - Qt override
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def _visible(self) -> list[QLayoutItem]:
        return [item for item in self._items if not item.isEmpty()]

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        items = self._visible()
        margins = self.contentsMargins()
        width = sum(item.sizeHint().width() for item in items) + self._h_spacing * max(0, len(items) - 1)
        height = max((item.sizeHint().height() for item in items), default=0)
        return QSize(width + margins.left() + margins.right(), height + margins.top() + margins.bottom())

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt override
        size = QSize()
        for item in self._visible():
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _arrange(self, rect: QRect, *, apply: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, row_height = area.x(), area.y(), 0
        for item in self._visible():
            hint = item.sizeHint()
            if x > area.x() and x + hint.width() > area.x() + area.width():
                x, y, row_height = area.x(), y + row_height + self._v_spacing, 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._h_spacing
            row_height = max(row_height, hint.height())
        return y + row_height - rect.y() + margins.bottom()


# --------------------------------------------------------------------------
# Small painted pieces
# --------------------------------------------------------------------------

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_ERROR = "error"
STATUS_OFF = "off"
STATUS_COLORS = {STATUS_OK: GREEN, STATUS_WARN: AMBER, STATUS_ERROR: RED, STATUS_OFF: OFF}
_STATUS_WORDS = {STATUS_OK: "ok", STATUS_WARN: "attention", STATUS_ERROR: "error", STATUS_OFF: "off"}


class ServiceChip(QWidget):
    """Header chip: 2 px left border and dot in the status colour, then the service name."""

    def __init__(self, name: str, status: str = STATUS_OFF, tooltip: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._name = name
        self._status = status
        self._tight = False
        self._font = mono_font(11, 400, 0.04)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.set_status(status, tooltip)

    def set_tight(self, tight: bool) -> None:
        """Narrower padding (used by a crowded header)."""
        if tight != self._tight:
            self._tight = tight
            self.updateGeometry()
            self.update()

    def _padding(self, tight: bool) -> tuple[int, int]:
        return (7, 5) if tight else (10, 6)   # (outer padding, dot-to-text gap)

    def width_for(self, tight: bool) -> int:
        pad, gap = self._padding(tight)
        return math.ceil(2 + pad + 6 + gap + _text_advance(self._font, self._name) + pad)

    def name(self) -> str:
        return self._name

    def status(self) -> str:
        return self._status

    def set_status(self, status: str, tooltip: str = "") -> None:
        self._status = status if status in STATUS_COLORS else STATUS_OFF
        self.setToolTip(tooltip)
        self.setAccessibleName(f"{self._name}: {_STATUS_WORDS[self._status]}")
        self.setAccessibleDescription(tooltip)
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(self.width_for(self._tight), math.ceil(_line_height(self._font)) + 10)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(self.width_for(True), self.sizeHint().height())

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        pad, gap = self._padding(self._tight)
        painter = QPainter(self)
        try:
            rect = QRectF(self.rect())
            color = QColor(STATUS_COLORS[self._status])
            painter.fillRect(rect, rgba(ACCENT, 0.05))
            painter.fillRect(QRectF(0, 0, 2, rect.height()), color)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(2 + pad + 3, rect.height() / 2), 3, 3)
            painter.setFont(self._font)
            painter.setPen(QColor(TEXT_SOFT))
            painter.drawText(rect.adjusted(2 + pad + 6 + gap, 0, 0, 0),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._name)
        finally:
            painter.end()


class HudChip(QWidget):
    """A small chamfered tag in mono caps, e.g. "2 NEED YOUR OK" (amber by default)."""

    def __init__(self, text: str = "", color: str | QColor = AMBER, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text
        self._color = QColor(color)
        self._font = mono_font(10, 500, 0.14)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(text)

    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text
        self.setAccessibleName(text)
        self.updateGeometry()
        self.update()

    def set_color(self, color: str | QColor) -> None:
        self._color = QColor(color)
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        width = _text_advance(self._font, self._text.upper()) + 20
        return QSize(math.ceil(width), math.ceil(_line_height(self._font)) + 8)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect())
            painter.fillPath(chamfer_path(rect, 5), rgba(self._color, 0.10))
            painter.setPen(QPen(rgba(self._color, 0.5), 1))
            painter.drawPath(chamfer_path(rect.adjusted(0.5, 0.5, -0.5, -0.5), 5))
            painter.setFont(self._font)
            painter.setPen(self._color)
            painter.drawText(rect.adjusted(1, 0, 0, 0), Qt.AlignmentFlag.AlignCenter, self._text.upper())
        finally:
            painter.end()


class LiveMarker(_Blinking):
    """Red bordered "LIVE" tag with a blinking dot; hidden (space kept) when not live."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._font = mono_font(11, 400, 0.2)
        policy = QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        policy.setRetainSizeWhenHidden(True)
        self.setSizePolicy(policy)
        self.setAccessibleName("Live")

    def set_live(self, live: bool) -> None:
        self.setVisible(live)

    def is_live(self) -> bool:
        return not self.isHidden()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        width = 10 + 7 + 8 + _text_advance(self._font, "LIVE") + 10
        return QSize(math.ceil(width), math.ceil(_line_height(self._font)) + 8 + 2)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect())
            painter.setPen(QPen(rgba(RED_LINE, 0.6), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(rgba(RED, self.blink_opacity()))
            painter.drawEllipse(QPointF(10 + 3.5, rect.height() / 2), 3.5, 3.5)
            painter.setFont(self._font)
            painter.setPen(QColor(RED))
            painter.drawText(rect.adjusted(10 + 7 + 8, 0, 0, 0),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "LIVE")
        finally:
            painter.end()


# --------------------------------------------------------------------------
# LIVE view: status glyphs and the strip's status tag
# --------------------------------------------------------------------------

# The colour of each LIVE status (live.STATUS_*): words, glyphs and pills.
LIVE_STATUS_COLORS = {
    live_events.STATUS_RUNNING: ACCENT, live_events.STATUS_OK: GREEN, live_events.STATUS_WARN: AMBER,
    live_events.STATUS_BLOCKED: AMBER, live_events.STATUS_FAILED: RED, live_events.STATUS_CANCELLED: TEXT_DIM,
}


def live_status_color(status: str) -> str:
    return LIVE_STATUS_COLORS.get(status, TEXT_DIM)


def paint_status_glyph(painter: QPainter, rect: QRectF, status: str, opacity: float = 1.0) -> None:
    """The LIVE view's status glyph, centred in ``rect`` (square, about 10 px): RUNNING a filled
    accent square (``opacity`` blinks it), OK a green check, WARN an amber outline triangle with a
    bar, BLOCKED an amber filled octagon with an ink bar, FAILED a red cross, CANCELLED a dim dash.
    Anything else draws nothing."""
    side = min(rect.width(), rect.height())
    if side <= 0:
        return
    box = QRectF(rect.center().x() - side / 2, rect.center().y() - side / 2, side, side)

    def at(fx: float, fy: float) -> QPointF:
        return QPointF(box.left() + fx * side, box.top() + fy * side)

    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen_width = max(1.5, side / 5)
        if status == live_events.STATUS_RUNNING:
            inner = side * 0.7
            painter.fillRect(QRectF(box.center().x() - inner / 2, box.center().y() - inner / 2, inner, inner),
                             rgba(ACCENT, opacity))
        elif status == live_events.STATUS_OK:
            pen = QPen(QColor(GREEN), pen_width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawPolyline(QPolygonF([at(0.12, 0.52), at(0.4, 0.8), at(0.9, 0.2)]))
        elif status == live_events.STATUS_WARN:
            pen = QPen(QColor(AMBER), max(1.2, side / 8))
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawPolygon(QPolygonF([at(0.5, 0.04), at(0.97, 0.92), at(0.03, 0.92)]))
            painter.drawLine(at(0.5, 0.38), at(0.5, 0.62))
            painter.drawPoint(at(0.5, 0.77))
        elif status == live_events.STATUS_BLOCKED:
            c = 0.3
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(AMBER))
            painter.drawPolygon(QPolygonF([at(c, 0), at(1 - c, 0), at(1, c), at(1, 1 - c), at(1 - c, 1),
                                           at(c, 1), at(0, 1 - c), at(0, c)]))
            pen = QPen(QColor(AMBER_INK), max(1.5, side / 6))
            painter.setPen(pen)
            painter.drawLine(at(0.25, 0.5), at(0.75, 0.5))
        elif status == live_events.STATUS_FAILED:
            pen = QPen(QColor(RED), pen_width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(at(0.15, 0.15), at(0.85, 0.85))
            painter.drawLine(at(0.15, 0.85), at(0.85, 0.15))
        elif status == live_events.STATUS_CANCELLED:
            pen = QPen(QColor(TEXT_DIM), pen_width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(at(0.15, 0.5), at(0.85, 0.5))
    finally:
        painter.restore()


LIVE_IDLE = "idle"
LIVE_WORKING = "working"
LIVE_COUNTDOWN = "countdown"
LIVE_STATES = (LIVE_IDLE, LIVE_WORKING, LIVE_COUNTDOWN)
_LIVE_STATE_COLORS = {LIVE_IDLE: TEXT_DIM, LIVE_WORKING: ACCENT, LIVE_COUNTDOWN: AMBER}
# The widest text of each state (a mono font: the width only depends on the number of characters),
# so the tag keeps its width while it counts.
_LIVE_STATE_TEMPLATES = {LIVE_IDLE: "IDLE", LIVE_WORKING: "WORKING 00:00", LIVE_COUNTDOWN: " IN 10 S"}


class LiveStatus(_Blinking):
    """The LIVE page's tag in the centre panel's strip, where the briefing's LiveMarker sits on the
    other tabs (same height): "IDLE" (dim, no blink), "WORKING 0:12" (accent, a blinking square,
    the time counting from ``since``) or "SENDING IN 7 S" (amber, counting down to ``deadline``).
    It blinks (and so repaints its time) only while working or counting down."""

    def __init__(self, parent: QWidget | None = None, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(parent)
        self._font = mono_font(11, 400, 0.12)
        self._clock = clock
        self._state = LIVE_IDLE
        self._word = "IDLE"
        self._since: float | None = None
        self._deadline: float | None = None
        policy = QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        policy.setRetainSizeWhenHidden(False)
        self.setSizePolicy(policy)
        self.set_blinking(False)
        self.setAccessibleName("Live: idle")

    def state(self) -> str:
        return self._state

    def set_state(self, state: str, text: str | None = None, *, since: float | None = None,
                  deadline: float | None = None) -> None:
        """``state`` LIVE_IDLE / LIVE_WORKING / LIVE_COUNTDOWN; ``text`` the word ("WORKING",
        "SENDING", "ADDING"; the state's own by default)."""
        state = state if state in LIVE_STATES else LIVE_IDLE
        default = {LIVE_IDLE: "IDLE", LIVE_WORKING: "WORKING", LIVE_COUNTDOWN: "SENDING"}[state]
        word = (text or default).upper()
        changed = (state, word, since, deadline) != (self._state, self._word, self._since, self._deadline)
        self._state, self._word = state, word
        self._since = since if state == LIVE_WORKING else None
        self._deadline = deadline if state == LIVE_COUNTDOWN else None
        if changed:
            self.set_blinking(state != LIVE_IDLE)
            self.setAccessibleName(f"Live: {self.text().lower()}")
            self.updateGeometry()
            self.update()

    def text(self) -> str:
        """What the tag says now ("WORKING 0:12", "SENDING IN 7 S", "IDLE")."""
        now = self._clock()
        if self._state == LIVE_WORKING and self._since is not None:
            return f"{self._word} {live_events.elapsed_text(now - self._since)}"
        if self._state == LIVE_COUNTDOWN and self._deadline is not None:
            return f"{self._word} IN {live_events.countdown_seconds(self._deadline, now)} S"
        return self._word

    def color(self) -> str:
        return _LIVE_STATE_COLORS[self._state]

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        template = _LIVE_STATE_TEMPLATES[self._state]
        if self._state == LIVE_COUNTDOWN:
            template = self._word + template
        widest = max(_text_advance(self._font, template), _text_advance(self._font, self.text()))
        return QSize(math.ceil(8 + 7 + 7 + widest + 8), math.ceil(_line_height(self._font)) + 8 + 2)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect())
            color = self.color()
            painter.setPen(QPen(rgba(color, 0.5 if self._state != LIVE_IDLE else 0.3), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
            square = QRectF(8, round(rect.height() / 2 - 3.5), 7, 7)
            painter.fillRect(square, rgba(color, self.blink_opacity() if self._state != LIVE_IDLE else 0.6))
            painter.setFont(self._font)
            painter.setPen(QColor(color))
            text_rect = rect.adjusted(8 + 7 + 7, 0, -2, 0)
            text = QFontMetricsF(self._font).elidedText(self.text(), Qt.TextElideMode.ElideRight, text_rect.width())
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        finally:
            painter.end()


# --------------------------------------------------------------------------
# Orb and state label
# --------------------------------------------------------------------------

ORB_WORKING = "working"
ORB_WAITING = "waiting"
ORB_STANDBY = "standby"
ORB_SPEAKING = "speaking"
ORB_LISTENING = "listening"
ORB_STATES: dict[str, tuple[str, str]] = {
    ORB_WORKING: (ACCENT, "WORKING"),
    ORB_WAITING: (AMBER, "WAITING ON YOU"),
    ORB_STANDBY: (SLATE, "STANDBY"),
    ORB_SPEAKING: (PINK, "SPEAKING"),
    ORB_LISTENING: (GREEN, "LISTENING"),
}
_ORB_BASE = 168.0
_TICKS = tuple(
    QLineF(77 * math.sin(math.radians(a)), -77 * math.cos(math.radians(a)),
           84 * math.sin(math.radians(a)), -84 * math.cos(math.radians(a)))
    for a in (k * 10 + 0.3 for k in range(36))
)
_ORIGIN = QPointF(0, 0)
_ARC_RECT = QRectF(-72, -72, 144, 144)
_CROSSHAIR = (QLineF(-62, 0, 62, 0), QLineF(0, -62, 0, 62))


_SWEEP_ALPHA = 0x55 / 255      # the artboard's stateGlow = state colour + "55"


def _ease_in_out(x: float) -> float:
    return x * x * (3.0 - 2.0 * x)


def orb_color(state: str) -> str:
    return ORB_STATES.get(state, ORB_STATES[ORB_STANDBY])[0]


@dataclass(frozen=True)
class _OrbStyle:
    """Pens, brushes and colours for one orb state, built once (the orb paints ~30 times a second)."""

    color: QColor
    tick_pen: QPen
    arc_pen: QPen
    hairline: QPen
    sweep: QBrush
    glow: tuple[QColor, QColor, QColor, QColor]
    core_white: QColor
    core_dark: QColor


_ORB_STYLES: dict[str, _OrbStyle] = {}


def _orb_style(state: str) -> _OrbStyle:
    style = _ORB_STYLES.get(state)
    if style is not None:
        return style
    color = QColor(orb_color(state))
    tick_pen = QPen(rgba(ACCENT, 0.42), 1.0)
    tick_pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    arc_pen = QPen(rgba(color, 0.7), 2.0)
    arc_pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    hairline = QPen(rgba(ACCENT, 0.25), 1.0)
    hairline.setCosmetic(True)
    # CSS conic-gradient(from 0deg, glow 0deg, transparent 70deg): bright at the
    # top, fading clockwise. Qt's conical gradient runs counter-clockwise from
    # 3 o'clock, so start at 90 degrees and put the colour at the end. The
    # leading edge gets a ~4 degree ramp; a hard edge renders as steps.
    sweep = QConicalGradient(QPointF(0, 0), 90)
    sweep.setColorAt(0.0, rgba(color, _SWEEP_ALPHA * 0.5))
    sweep.setColorAt(0.006, rgba(color, 0.0))
    sweep.setColorAt(1 - 70 / 360, rgba(color, 0.0))
    sweep.setColorAt(0.994, rgba(color, _SWEEP_ALPHA))
    sweep.setColorAt(1.0, rgba(color, _SWEEP_ALPHA * 0.5))
    glow = (rgba(color, _SWEEP_ALPHA), rgba(color, _SWEEP_ALPHA * 0.6), rgba(color, _SWEEP_ALPHA * 0.18),
            rgba(color, 0.0))
    style = _OrbStyle(color, tick_pen, arc_pen, hairline, QBrush(sweep), glow,
                      QColor("#ffffff"), QColor(2, 6, 12, 230))
    _ORB_STYLES[state] = style
    return style


class Orb(QWidget):
    """The animated status orb (tick ring, counter arc, radar sweep, pulsing core).

    One ~30 fps timer drives it, and only while the orb is visible and its
    window is not minimized. ``set_phase(t)`` freezes the animation at ``t``
    seconds (deterministic screenshots).
    """

    FRAME_MS = 33

    def __init__(self, size: int = 168, state: str = ORB_STANDBY, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._state = state if state in ORB_STATES else ORB_STANDBY
        self._t0 = time.monotonic()
        self._frozen: float | None = None
        self._animated = True
        self._timer = QTimer(self)
        # Windows rounds a coarse 33 ms timer up to its 15.6 ms tick (about 21 fps);
        # a precise one keeps ~30 fps at a similar cost (no multimedia timer above 20 ms).
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self.update)
        self.setAccessibleName("Status")
        self.setAccessibleDescription(ORB_STATES[self._state][1])

    # ---- API ------------------------------------------------------------------

    def state(self) -> str:
        return self._state

    def set_state(self, state: str) -> None:
        state = state if state in ORB_STATES else ORB_STANDBY
        if state != self._state:
            self._state = state
            self.setAccessibleDescription(ORB_STATES[state][1])
            self.update()

    def set_phase(self, seconds: float | None) -> None:
        self._frozen = seconds
        self._sync_timer()
        self.update()

    def set_animated(self, animated: bool) -> None:
        self._animated = animated
        self._sync_timer()

    def is_animating(self) -> bool:
        return self._timer.isActive()

    # ---- timer control ------------------------------------------------------

    def _should_run(self) -> bool:
        window = self.window()
        return (self._animated and self._frozen is None and self.isVisible()
                and not (window is not None and window.isMinimized()))

    def _sync_timer(self) -> None:
        if self._should_run():
            if not self._timer.isActive():
                self._timer.start()
        elif self._timer.isActive():
            self._timer.stop()

    def _watch_window(self) -> None:
        # Minimizing does not hide child widgets, so listen to the window's state
        # changes. No reference to the window is kept (Qt drops the filter when
        # either object dies, and installing it twice keeps one).
        window = self.window()
        if window is not self:
            window.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if event.type() == QEvent.Type.WindowStateChange and watched is self.window():
            QTimer.singleShot(0, self._sync_timer)
        return False

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._watch_window()
        self._sync_timer()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self._sync_timer()

    # ---- painting ---------------------------------------------------------------

    def _elapsed(self) -> float:
        return self._frozen if self._frozen is not None else time.monotonic() - self._t0

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        t = self._elapsed()
        style = _orb_style(self._state)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.translate(self.width() / 2, self.height() / 2)
            scale = self.width() / _ORB_BASE
            painter.scale(scale, scale)
            self._paint_rings(painter, t, style)
            self._paint_core(painter, t, style)
        finally:
            painter.end()

    @staticmethod
    def _paint_rings(painter: QPainter, t: float, style: _OrbStyle) -> None:
        # DOM order of the artboard: ticks, arc, inner circle, sweep, crosshair.
        base = painter.transform()
        painter.rotate((t / 24.0 * 360.0) % 360.0)
        painter.setPen(style.tick_pen)
        painter.drawLines(_TICKS)
        painter.setTransform(base)
        painter.rotate(-(t / 16.0 * 360.0) % 360.0)
        painter.setPen(style.arc_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(_ARC_RECT, 45 * 16, 90 * 16)
        painter.setTransform(base)
        painter.setPen(style.hairline)
        painter.drawEllipse(_ORIGIN, 61.5, 61.5)
        painter.rotate((t / 3.2 * 360.0) % 360.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(style.sweep)
        painter.drawEllipse(_ORIGIN, 62, 62)
        painter.setTransform(base)
        painter.setPen(style.hairline)
        painter.drawLines(_CROSSHAIR)

    @staticmethod
    def _paint_core(painter: QPainter, t: float, style: _OrbStyle) -> None:
        u = (t % 1.8) / 1.8
        k = _ease_in_out(u * 2) if u < 0.5 else 1.0 - _ease_in_out((u - 0.5) * 2)
        grow = 1.0 + 0.1 * k
        painter.setOpacity(0.7 + 0.3 * k)
        radius = 23.0 * grow
        glow_radius = radius + 24.0
        edge = radius / glow_radius
        full, edge_color, tail, clear = style.glow
        glow = QRadialGradient(_ORIGIN, glow_radius)
        glow.setColorAt(0.0, full)
        glow.setColorAt(edge * 0.8, full)
        glow.setColorAt(edge, edge_color)
        glow.setColorAt(edge + (1 - edge) * 0.45, tail)
        glow.setColorAt(1.0, clear)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(_ORIGIN, glow_radius, glow_radius)
        # radial-gradient(circle at 45% 38%, #fff 0%, state 32%, rgba(2,6,12,.9) 80%), farthest corner
        core = QRadialGradient(QPointF(-2.3 * grow, -5.52 * grow), 38.1 * grow)
        core.setColorAt(0.0, style.core_white)
        core.setColorAt(0.32, style.color)
        core.setColorAt(0.8, style.core_dark)
        painter.setBrush(core)
        painter.drawEllipse(_ORIGIN, radius, radius)
        painter.setOpacity(1.0)


class StateLabel(_Blinking):
    """Blinking square + state text in mono caps, coloured like the orb state."""

    def __init__(self, state: str = ORB_STANDBY, text: str | None = None, *,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._font = mono_font(12, 400, 0.2)
        self._state = ORB_STANDBY
        self._text = ""
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.set_state(state, text)

    def state(self) -> str:
        return self._state

    def text(self) -> str:
        return self._text

    def set_state(self, state: str, text: str | None = None) -> None:
        self._state = state if state in ORB_STATES else ORB_STANDBY
        self._text = text if text is not None else ORB_STATES[self._state][1]
        self.setAccessibleName(self._text)
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        width = 7 + 10 + _text_advance(self._font, self._text)
        return QSize(math.ceil(width), math.ceil(_line_height(self._font)) + 2)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(40, self.sizeHint().height())

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            color = QColor(orb_color(self._state))
            rect = QRectF(self.rect())
            square = QRectF(0, round(rect.height() / 2 - 3.5), 7, 7)
            painter.fillRect(square, rgba(color, self.blink_opacity()))
            painter.setFont(self._font)
            painter.setPen(color)
            text_rect = rect.adjusted(7 + 10, 0, 0, 0)
            text = QFontMetricsF(self._font).elidedText(self._text, Qt.TextElideMode.ElideRight, text_rect.width())
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        finally:
            painter.end()


# --------------------------------------------------------------------------
# Header bar
# --------------------------------------------------------------------------

_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_HEADER_HEIGHT = 46
_HEADER_PAD = 14


def header_date(now: datetime) -> str:
    """"SUN 04 OCT" (English, independent of the locale)."""
    return f"{_DAYS[now.weekday()]} {now.day:02d} {_MONTHS[now.month - 1]}"


class _Brand(QWidget):
    """Rotated-square logo, JARVIS wordmark (with glow) and the mono subtitle."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._word_font = display_font(20, 600, 0.34)
        self._sub_font = mono_font(11, 400, 0.1)
        self._subtitle = f"assistant {MIDDLE_DOT} desktop"
        self._show_subtitle = True
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_subtitle(self, text: str) -> None:
        self._subtitle = text
        self.updateGeometry()
        self.update()

    def subtitle(self) -> str:
        return self._subtitle

    def set_subtitle_visible(self, visible: bool) -> None:
        if visible != self._show_subtitle:
            self._show_subtitle = visible
            self.updateGeometry()
            self.update()

    def subtitle_visible(self) -> bool:
        return self._show_subtitle

    def _word_x(self) -> float:
        return _HEADER_PAD + 14 + 16

    def _sub_x(self) -> float:
        return self._word_x() + _text_advance(self._word_font, "JARVIS") + 16

    def width_for(self, subtitle: bool) -> int:
        if subtitle and self._subtitle:
            return math.ceil(self._sub_x() + _text_advance(self._sub_font, self._subtitle) + 4)
        return math.ceil(self._sub_x() - 12)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(self.width_for(self._show_subtitle), _HEADER_HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(self.width_for(False), _HEADER_HEIGHT)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            dpr = self.devicePixelRatioF()
            mid = self.height() / 2
            self._paint_logo(painter, QPointF(_HEADER_PAD + 7, mid), dpr)
            metrics = QFontMetricsF(self._word_font)
            baseline = round(mid + (metrics.ascent() - metrics.descent()) / 2)
            origin = QPointF(self._word_x(), baseline)
            word = QPainterPath()
            word.addText(origin, self._word_font, "JARVIS")
            _draw_glow(painter, word, rgba(ACCENT, 0.45), 10, dpr=dpr,
                       key=("wordmark", origin.x(), origin.y()))
            painter.setFont(self._word_font)
            painter.setPen(QColor(TEXT_BRIGHT))
            painter.drawText(origin, "JARVIS")
            if self._show_subtitle:
                sub_metrics = QFontMetricsF(self._sub_font)
                sub_baseline = round(mid + (sub_metrics.ascent() - sub_metrics.descent()) / 2)
                painter.setFont(self._sub_font)
                painter.setPen(QColor(TEXT_DIM))
                painter.drawText(QPointF(self._sub_x(), sub_baseline), self._subtitle)
        finally:
            painter.end()

    @staticmethod
    def _paint_logo(painter: QPainter, center: QPointF, dpr: float) -> None:
        painter.save()
        painter.translate(center)
        painter.rotate(45)
        square = QPainterPath()
        square.addRect(QRectF(-7, -7, 14, 14))
        outside = QPainterPath()
        outside.addRect(QRectF(-30, -30, 60, 60))
        painter.save()
        painter.setClipPath(outside.subtracted(square))
        _draw_glow(painter, square, rgba(ACCENT, 0.9), 12, dpr=dpr, key="logo")
        painter.restore()
        painter.setPen(QPen(QColor(ACCENT), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(-6, -6, 12, 12))
        painter.restore()


class _Clock(QWidget):
    """Date (mono 11 dim) and time (mono 24 bright with glow), baseline aligned.

    The 12-hour clock ends in a small "AM" / "PM" on the same baseline. The
    widget is as wide as the widest time, so the header does not move when the
    hour gains a digit; the time keeps to the right edge and the date to it.
    """

    _MERIDIEM_GAP = 4
    _END_ROOM = 6             # after the digits: room for their glow
    _MERIDIEM_END_ROOM = 2    # after "AM" / "PM", which has no glow

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._date_font = mono_font(11, 400, 0.12)
        self._time_font = mono_font(24, 400)
        self._meridiem_font = mono_font(11, 500, 0.06)
        self._hour24 = False
        self._date = ""
        self._time = ""            # as read: "1:05 PM" (or "13:05")
        self._digits = ""          # as drawn: "1:05" big, then "PM" small
        self._meridiem = ""
        self._show_date = True
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_hour24(self, hour24: bool) -> None:
        """Takes effect at the next set_now."""
        if hour24 != self._hour24:
            self._hour24 = hour24
            self.updateGeometry()

    def hour24(self) -> bool:
        return self._hour24

    def set_date_visible(self, visible: bool) -> None:
        if visible != self._show_date:
            self._show_date = visible
            self.updateGeometry()
            self.update()

    def date_visible(self) -> bool:
        return self._show_date

    def set_now(self, now: datetime) -> None:
        date, clock = header_date(now), format_time(now, hour24=self._hour24)
        if (date, clock) != (self._date, self._time):
            self._date, self._time = date, clock
            self._digits, self._meridiem = clock_parts(now, hour24=self._hour24)
            self.setAccessibleName(f"{date} {clock}")
            self.updateGeometry()
            self.update()

    def texts(self) -> tuple[str, str]:
        return self._date, self._time

    def _date_width(self) -> float:
        return _text_advance(self._date_font, self._date or "WED 00 SEP")

    def _time_width(self) -> float:
        """The widest digits and, on the 12-hour clock, the gap and the wider of "AM" / "PM",
        with the room after them."""
        digits = _clock_width(self._time_font, self._hour24, 0)
        meridiem = _clock_width(self._meridiem_font, self._hour24, 1)
        if not meridiem:
            return digits + self._END_ROOM
        return digits + self._MERIDIEM_GAP + meridiem + self._MERIDIEM_END_ROOM

    def width_for(self, date: bool) -> int:
        return math.ceil((self._date_width() + 12 if date else 0) + self._time_width())

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(self.width_for(self._show_date), _HEADER_HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(self.width_for(False), _HEADER_HEIGHT)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            dpr = self.devicePixelRatioF()
            metrics = QFontMetricsF(self._time_font)
            baseline = round(self.height() / 2 + (metrics.ascent() - metrics.descent()) / 2)
            right = self.width() - (self._MERIDIEM_END_ROOM if self._meridiem else self._END_ROOM)
            meridiem_x = right - _text_advance(self._meridiem_font, self._meridiem)
            time_x = (meridiem_x - (self._MERIDIEM_GAP if self._meridiem else 0)
                      - _text_advance(self._time_font, self._digits))
            if self._show_date:
                painter.setFont(self._date_font)
                painter.setPen(QColor(TEXT_DIM))
                painter.drawText(QPointF(time_x - 12 - self._date_width(), baseline), self._date)
            origin = QPointF(time_x, baseline)
            path = QPainterPath()
            path.addText(origin, self._time_font, self._digits)
            _draw_glow(painter, path, rgba(ACCENT, 0.6), 14, dpr=dpr,
                       key=("clock", self._digits, round(origin.x(), 1), origin.y()))
            painter.setFont(self._time_font)
            painter.setPen(QColor(TEXT_BRIGHT))
            painter.drawText(origin, self._digits)
            if self._meridiem:
                painter.setFont(self._meridiem_font)
                painter.setPen(QColor(TEXT_SOFT))
                painter.drawText(QPointF(meridiem_x, baseline), self._meridiem)
        finally:
            painter.end()


class HeaderBar(QWidget):
    """The 46 px title bar of a frameless HUD window.

    Dragging anywhere but the minimize and close buttons moves the window
    (double-click does nothing). The clock updates on each minute boundary while
    the bar is shown.
    When the bar is narrow it drops detail in steps (``compact_level()``):
    1 hides the subtitle, 2 also the date, 3 also tightens the chips. A
    12-hour clock is wider, so it has a level 4 that also moves the chips up to
    the wordmark and the window buttons closer to the clock (the prompt), and
    it keeps at least 12 px from the chips, so its digits and their glow never
    crowd the last one.
    """

    closeRequested = Signal()
    minimizeRequested = Signal()
    muteToggled = Signal(bool)   # the speaker button: True = Jarvis's voice muted

    _MAX_LEVEL = 4           # 3 on the 24-hour clock
    _RIGHT_MARGIN = 8
    _CHIP_MARGINS = ((16, 16), (16, 16), (16, 16), (8, 8), (0, 8))   # (left, right) of the chips, per level
    _CLOSE_GAPS = (10, 10, 10, 10, 6)                                  # between the clock and the buttons, per level
    _BUTTON_GAP = 2                                                    # between minimize and close
    _SPEAKER_GAP = 6                                                   # between the speaker and minimize
    _MERIDIEM_ROOM = 12      # at least this much between the chips and a 12-hour clock

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(_HEADER_HEIGHT)
        self._brand = _Brand()
        self._clock = _Clock()
        self._now: Callable[[], datetime] = datetime.now
        self._chips: dict[str, ServiceChip] = {}
        self._level = 0
        self._chips_box = QWidget()
        self._chip_row = QHBoxLayout(self._chips_box)
        self._chip_row.setSpacing(4)
        self._chip_row.setContentsMargins(16, 0, 16, 0)
        self.speaker_button = _SpeakerButton()   # shown by set_speaker_visible (the assistant screen)
        self.speaker_button.hide()
        self.speaker_button.clicked.connect(self._on_speaker_clicked)
        self.minimize_button = _MinimizeButton()
        self.minimize_button.clicked.connect(self._on_minimize_clicked)
        self.close_button = _CloseButton()
        self.close_button.clicked.connect(self._on_close_clicked)
        self._drag_offset: QPoint | None = None
        self._clock_timer = QTimer(self)
        self._clock_timer.setSingleShot(True)
        self._clock_timer.timeout.connect(self._tick)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, self._RIGHT_MARGIN, 1)
        layout.setSpacing(0)
        layout.addWidget(self._brand)
        layout.addStretch(1)
        layout.addWidget(self._chips_box)
        layout.addStretch(1)
        layout.addWidget(self._clock)
        self._close_gap = QSpacerItem(self._CLOSE_GAPS[0], 0, QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
        layout.addSpacerItem(self._close_gap)
        layout.addWidget(self.speaker_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self._speaker_gap = QSpacerItem(0, 0, QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
        layout.addSpacerItem(self._speaker_gap)
        layout.addWidget(self.minimize_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addSpacing(self._BUTTON_GAP)
        layout.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self._clock.set_now(self._now())
        self._sync_chips_box()

    def _sync_chips_box(self) -> None:
        """No chips shown (the LIVE pop-out's header): their row and its margins take no room, as
        _required_width counts them."""
        shown = bool(self._shown_chips())
        if self._chips_box.isHidden() == shown:
            self._chips_box.setVisible(shown)

    def _on_close_clicked(self) -> None:
        self.closeRequested.emit()

    def _on_minimize_clicked(self) -> None:
        self.minimizeRequested.emit()

    def _on_speaker_clicked(self) -> None:
        muted = not self.speaker_button.muted()
        self.speaker_button.set_muted(muted)
        self.muteToggled.emit(muted)

    # ---- the speaker button ---------------------------------------------------------

    def set_speaker_visible(self, visible: bool) -> None:
        """Show the speaker button (left of minimize, 6 px apart) or take it out of the bar; the bar
        fits as if it were not there while hidden."""
        if self.speaker_button.isHidden() == (not visible):
            return
        self.speaker_button.setVisible(visible)
        self._speaker_gap.changeSize(self._SPEAKER_GAP if visible else 0, 0, QSizePolicy.Policy.Fixed,
                                     QSizePolicy.Policy.Minimum)
        self.layout().invalidate()
        self.updateGeometry()
        self._fit(force=True)

    def speaker_visible(self) -> bool:
        return not self.speaker_button.isHidden()

    def set_muted(self, muted: bool) -> None:
        """The speaker button's state (it does not emit ``muteToggled``)."""
        self.speaker_button.set_muted(muted)

    def muted(self) -> bool:
        return self.speaker_button.muted()

    # ---- content ----------------------------------------------------------------

    def set_subtitle(self, text: str) -> None:
        self._brand.set_subtitle(text)
        self._fit()

    def subtitle(self) -> str:
        return self._brand.subtitle()

    def set_service(self, name: str, status: str, tooltip: str = "") -> ServiceChip:
        """Create or update the chip called ``name`` (chips keep their creation order)."""
        chip = self._chips.get(name)
        if chip is None:
            chip = ServiceChip(name, status, tooltip)
            chip.set_tight(self._level >= 3)
            self._chips[name] = chip
            self._chip_row.addWidget(chip)
            self._sync_chips_box()
            self.updateGeometry()
            self._fit()
        else:
            chip.set_status(status, tooltip)
        return chip

    def set_service_visible(self, name: str, visible: bool) -> None:
        """Show or hide the chip called ``name`` (nothing when there is none); a hidden chip takes
        no room, so the bar fits as if it were not there (e.g. ``claude`` on the narrow prompt)."""
        chip = self._chips.get(name)
        if chip is None or chip.isHidden() == (not visible):
            return
        chip.setVisible(visible)
        self._sync_chips_box()
        self.updateGeometry()
        self._fit(force=True)

    def _shown_chips(self) -> list[ServiceChip]:
        return [chip for chip in self._chips.values() if not chip.isHidden()]

    # ---- narrow widths ---------------------------------------------------------------

    def compact_level(self) -> int:
        return self._level

    def _max_level(self) -> int:
        return self._MAX_LEVEL - 1 if self._clock.hour24() else self._MAX_LEVEL

    def _chip_margins(self, level: int) -> tuple[int, int]:
        left, right = self._CHIP_MARGINS[level]
        return left, right if self._clock.hour24() else max(right, self._MERIDIEM_ROOM)

    def _required_width(self, level: int) -> int:
        tight = level >= 3
        chips = [chip.width_for(tight) for chip in self._shown_chips()]
        chips_width = sum(chips) + 4 * max(0, len(chips) - 1) + (sum(self._chip_margins(level)) if chips else 0)
        return (self._brand.width_for(level == 0) + chips_width + self._clock.width_for(level <= 1)
                + self._CLOSE_GAPS[level] + self._speaker_width() + self._buttons_width() + self._RIGHT_MARGIN)

    def _speaker_width(self) -> int:
        """The speaker button and its gap to minimize while it is shown, else 0."""
        if self.speaker_button.isHidden():
            return 0
        return self.speaker_button.width() + self._SPEAKER_GAP

    def _buttons_width(self) -> int:
        """Minimize, the gap and close."""
        return self.minimize_button.width() + self._BUTTON_GAP + self.close_button.width()

    def _fit(self, force: bool = False) -> None:
        """Pick the least compact level that fits (``force``: re-apply it after the clock changed)."""
        width = self.width()
        level = next((lvl for lvl in range(self._max_level() + 1) if self._required_width(lvl) <= width),
                     self._max_level())
        if level == self._level and not force:
            return
        self._level = level
        self._brand.set_subtitle_visible(level == 0)
        self._clock.set_date_visible(level <= 1)
        for chip in self._chips.values():
            chip.set_tight(level >= 3)
        left, right = self._chip_margins(level)
        self._chip_row.setContentsMargins(left, 0, right, 0)
        if self._close_gap.sizeHint().width() != self._CLOSE_GAPS[level]:
            self._close_gap.changeSize(self._CLOSE_GAPS[level], 0, QSizePolicy.Policy.Fixed,
                                       QSizePolicy.Policy.Minimum)
            self.layout().invalidate()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(self._required_width(self._max_level()), _HEADER_HEIGHT)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._fit()

    def service_chip(self, name: str) -> ServiceChip | None:
        return self._chips.get(name)

    def set_clock(self, now: Callable[[], datetime]) -> None:
        """Replace the time source (tests, screenshots) and refresh."""
        self._now = now
        self._tick()

    def set_hour24(self, hour24: bool) -> None:
        """Show the time as "13:52" (True) or "1:52 PM" (False, the default)."""
        self._clock.set_hour24(hour24)
        self._tick()
        self.updateGeometry()
        self._fit(force=True)

    def clock_texts(self) -> tuple[str, str]:
        """("SUN 04 OCT", "1:52 PM") as read (the "PM" is drawn small); "13:52" on the 24-hour clock."""
        return self._clock.texts()

    def clock_running(self) -> bool:
        return self._clock_timer.isActive()

    def _tick(self) -> None:
        now = self._now()
        self._clock.set_now(now)
        if self.isVisible():
            ms_left = (60 - now.second) * 1000 - now.microsecond // 1000
            self._clock_timer.start(max(250, ms_left + 50))

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._tick()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self._clock_timer.stop()

    # ---- painting and dragging --------------------------------------------------------

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.fillRect(QRectF(0, self.height() - 1, self.width(), 1), rgba(ACCENT, 0.14))
        finally:
            painter.end()

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        event.accept()
        handle = self.window().windowHandle()
        if handle is not None and handle.startSystemMove():
            return
        self._drag_offset = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()

    def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        event.accept()   # no maximize / restore on double-click


# --------------------------------------------------------------------------
# Panels
# --------------------------------------------------------------------------

PANEL_CYAN = "cyan"
PANEL_AMBER = "amber"


class ChamferPanel(QWidget):
    """A panel with a 1 px border, cut corners, a mono title row and a body layout."""

    def __init__(self, title: str = "", meta: str = "", *, variant: str = PANEL_CYAN, cut: int = 8,
                 corners: int = CUT_DIAGONAL, border_alpha: float | None = None,
                 padding: tuple[int, int, int, int] = (16, 14, 16, 14), spacing: int = 10,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cut = cut
        self._corners = corners
        amber = variant == PANEL_AMBER
        alpha = border_alpha if border_alpha is not None else (0.35 if amber else 0.22)
        self._border = rgba(AMBER if amber else ACCENT, alpha)
        self._ground = AMBER_GROUND if amber else PANEL_GROUND
        self.title_label = make_label(title.upper(), mono_font(11, 400, 0.16), AMBER if amber else ACCENT)
        self.meta_label = make_label(meta, mono_font(11, 400, 0.16), AMBER_META if amber else TEXT_DIM)
        self.meta_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._header = QWidget()
        header = QHBoxLayout(self._header)
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.title_label)
        header.addStretch(1)
        header.addWidget(self.meta_label)
        self._header.setVisible(bool(title or meta))
        self.body_layout = QVBoxLayout(self)
        self.body_layout.setContentsMargins(*padding)
        self.body_layout.setSpacing(spacing)
        self.body_layout.addWidget(self._header)

    def set_title(self, text: str) -> None:
        self.title_label.setText(text.upper())
        self._header.setVisible(bool(text or self.meta_label.text()))

    def set_meta(self, text: str, color: str | QColor | None = None) -> None:
        """Right-hand meta text, shown as given (the artboard mixes "1 PENDING" and "live")."""
        self.meta_label.setText(text)
        if color is not None:
            set_label_color(self.meta_label, color)
        self._header.setVisible(bool(text or self.title_label.text()))

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect())
            painter.fillPath(chamfer_path(rect, self._cut, self._corners), self._border)
            painter.fillPath(chamfer_path(rect.adjusted(1, 1, -1, -1), self._cut, self._corners), self._ground)
        finally:
            painter.end()


STEP_DONE = "done"
STEP_ACTIVE = "active"
STEP_TODO = "todo"
STEP_DENIED = "denied"
_STEP_STYLE: dict[str, tuple[str, QColor, QColor, str]] = {
    # state: (dot, bottom border, background, text)
    STEP_DONE: (GREEN, rgba(GREEN, 0.3), rgba(GREEN, 0.06), "#c3ccd6"),
    STEP_ACTIVE: (ACCENT, QColor(ACCENT), rgba(ACCENT, 0x22 / 255), BUTTON_TEXT),
    STEP_TODO: (STEP_DIM, rgba("#ffffff", 0.08), rgba("#ffffff", 0.0), "#8592a0"),
    STEP_DENIED: (RED, rgba("#ffffff", 0.08), rgba("#ffffff", 0.0), RED),
}


class StepStrip(QWidget):
    """A row of step chips (dot + label, 2 px bottom border), e.g. neighbouring sections.

    Chips that do not fit are left out; when not even the first one fits, it
    is drawn narrower with its label elided.
    """

    MAX_LABEL_PX = 150
    _MIN_CUT_CHIP = 36 + 28    # chip padding and dot + room for a few letters

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._font = mono_font(11, 400, 0.04)
        self._steps: list[tuple[str, str]] = []
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def set_steps(self, steps: Sequence[tuple[str, str]]) -> None:
        self._steps = [(label, state if state in _STEP_STYLE else STEP_TODO) for label, state in steps]
        self.setAccessibleName(", ".join(f"{label} ({state})" for label, state in self._steps))
        self.updateGeometry()
        self.update()

    def steps(self) -> list[tuple[str, str]]:
        return list(self._steps)

    def _chip_widths(self) -> list[float]:
        metrics = QFontMetricsF(self._font)
        return [12 + 6 + 6 + min(metrics.horizontalAdvance(label), self.MAX_LABEL_PX) + 12
                for label, _ in self._steps]

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(math.ceil(sum(self._chip_widths())), math.ceil(_line_height(self._font)) + 12 + 2)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(0, self.sizeHint().height())

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setFont(self._font)
            metrics = QFontMetricsF(self._font)
            height = float(self.height())
            x = 0.0
            for index, ((label, state), width) in enumerate(zip(self._steps, self._chip_widths())):
                room = self.width() - x
                if width > room + 0.5:
                    # Never a half chip; only a lone first chip is cut down (its label elided).
                    if index > 0 or room < self._MIN_CUT_CHIP:
                        break
                    width = room
                dot, border, ground, text = _STEP_STYLE[state]
                painter.fillRect(QRectF(x, 0, width, height), ground)
                painter.fillRect(QRectF(x, height - 2, width, 2), border)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(dot))
                painter.drawEllipse(QPointF(x + 12 + 3, (height - 2) / 2), 3, 3)
                painter.setPen(QColor(text))
                shown = metrics.elidedText(label, Qt.TextElideMode.ElideRight, min(self.MAX_LABEL_PX, width - 36))
                painter.drawText(QRectF(x + 12 + 12, 0, width - 24, height - 2),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, shown)
                x += width
        finally:
            painter.end()


class MainPanel(QWidget):
    """The transcript panel.

    Top corners cut 10 px, gradient ground with a soft inner glow, a header
    strip (``steps`` on the left, ``live`` on the right; ``strip_layout``
    takes more widgets at its right end) and ``body_layout``.
    """

    def __init__(self, cut: int = 10, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cut = cut
        self._cache: QPixmap | None = None
        self._cache_key: tuple = ()
        self.steps = StepStrip()
        self.live = LiveMarker()
        self.strip = QWidget()
        self.strip_layout = QHBoxLayout(self.strip)
        self.strip_layout.setContentsMargins(20, 12, 20, 12)
        self.strip_layout.setSpacing(10)
        self.strip_layout.addWidget(self.steps, 1, Qt.AlignmentFlag.AlignVCenter)
        self.strip_layout.addWidget(self.live, 0, Qt.AlignmentFlag.AlignVCenter)
        self.body_layout = QVBoxLayout()
        self.body_layout.setContentsMargins(8, 4, 4, 8)
        self.body_layout.setSpacing(0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        layout.addWidget(self.strip)
        layout.addLayout(self.body_layout, 1)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._cache = None

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        dpr = self.devicePixelRatioF()
        key = (self.width(), self.height(), dpr, self.strip.geometry().getRect())
        if self._cache is None or key != self._cache_key:
            self._cache = self._render_background(dpr)
            self._cache_key = key
        painter = QPainter(self)
        try:
            painter.drawPixmap(0, 0, self._cache)
        finally:
            painter.end()

    def _render_background(self, dpr: float) -> QPixmap:
        size = self.size()
        image = QImage(max(1, round(size.width() * dpr)), max(1, round(size.height() * dpr)),
                       QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(0, 0, size.width(), size.height())
            painter.fillPath(chamfer_path(rect, self._cut, CUT_TOP), rgba(ACCENT, 0.30))
            inner_rect = rect.adjusted(1, 1, -1, -1)
            inner = chamfer_path(inner_rect, self._cut, CUT_TOP)
            ground = QLinearGradient(inner_rect.topLeft(), inner_rect.bottomLeft())
            ground.setColorAt(0.0, QColor(4, 14, 22, 247))
            ground.setColorAt(1.0, QColor(2, 6, 11, 247))
            painter.fillPath(inner, ground)
            painter.setClipPath(inner)
            self._paint_inner_glow(painter, inner_rect)
            strip = QRectF(self.strip.geometry())
            painter.fillRect(strip, rgba(ACCENT, 0.04))
            painter.fillRect(QRectF(strip.left(), strip.bottom() - 1, strip.width(), 1), rgba(ACCENT, 0.16))
        finally:
            painter.end()
        return QPixmap.fromImage(image)

    @staticmethod
    def _paint_inner_glow(painter: QPainter, rect: QRectF) -> None:
        depth = 56.0
        edges = (
            (rect.topLeft(), QPointF(rect.left(), rect.top() + depth),
             QRectF(rect.left(), rect.top(), rect.width(), depth)),
            (rect.bottomLeft(), QPointF(rect.left(), rect.bottom() - depth),
             QRectF(rect.left(), rect.bottom() - depth, rect.width(), depth)),
            (rect.topLeft(), QPointF(rect.left() + depth, rect.top()),
             QRectF(rect.left(), rect.top(), depth, rect.height())),
            (rect.topRight(), QPointF(rect.right() - depth, rect.top()),
             QRectF(rect.right() - depth, rect.top(), depth, rect.height())),
        )
        for start, end, area in edges:
            gradient = QLinearGradient(start, end)
            gradient.setColorAt(0.0, rgba(ACCENT, 0.07))
            gradient.setColorAt(1.0, rgba(ACCENT, 0.0))
            painter.fillRect(area, gradient)


# --------------------------------------------------------------------------
# Telemetry
# --------------------------------------------------------------------------

class TelemetryBar(QWidget):
    """Label left, value right (mono 11) and a 4 px segmented bar under them.

    A value given with a ``short`` form ("Mon 11:31 PM" for "yesterday
    11:31 PM") shows that form while the full value does not fit beside the
    label with ``_MIN_GAP`` to spare, so a narrow column never crowds them.
    """

    _MIN_GAP = 8     # between the label and a value that has a short form

    def __init__(self, label: str, value: str = "", fraction: float = 0.0, color: str | QColor = ACCENT,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._font = mono_font(11)
        self._label = label
        self._value = value
        self._short = ""
        self._fraction = max(0.0, min(1.0, fraction))
        self._color = QColor(color)
        self._value_color = QColor(TEXT_BRIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._update_accessible()

    def set_label(self, label: str) -> None:
        self._label = label
        self._update_accessible()
        self.update()

    def set_value(self, value: str, fraction: float | None = None, color: str | QColor | None = None,
                  short: str = "", value_color: str | QColor | None = None) -> None:
        """``color`` is the bar's; ``value_color`` the value text's (default TEXT_BRIGHT)."""
        self._value = value
        self._short = short
        if fraction is not None:
            self._fraction = max(0.0, min(1.0, fraction))
        if color is not None:
            self._color = QColor(color)
        self._value_color = QColor(value_color if value_color is not None else TEXT_BRIGHT)
        self._update_accessible()
        self.update()

    def value_color(self) -> QColor:
        return QColor(self._value_color)

    def label(self) -> str:
        return self._label

    def value(self) -> str:
        return self._value

    def shown_value(self) -> str:
        """The value as painted at the bar's current width: ``value()`` or its short form."""
        room = self.width() - _text_advance(self._font, self._label) - self._MIN_GAP
        if self._short and _text_advance(self._font, self._value) > room:
            return self._short
        return self._value

    def fraction(self) -> float:
        return self._fraction

    def _update_accessible(self) -> None:
        self.setAccessibleName(f"{self._label}: {self._value}")

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(200, math.ceil(_line_height(self._font)) + 4 + 4)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(80, self.sizeHint().height())

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            text_rect = QRectF(0, 0, self.width(), _line_height(self._font))
            painter.setFont(self._font)
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)
            painter.setPen(self._value_color)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             self.shown_value())
            paint_dash_bar(painter, QRectF(0, self.height() - 4, self.width(), 4), self._fraction, self._color)
        finally:
            painter.end()


# --------------------------------------------------------------------------
# Section list
# --------------------------------------------------------------------------

ROW_PLAYED = "played"
ROW_CURRENT = "current"
ROW_UPCOMING = "upcoming"
ROW_IGNORED = "ignored"
_ROW_STYLE: dict[str, tuple[str, str, str, float, bool]] = {
    # state: (bar, title colour, meta colour, gradient strength, bar glow)
    ROW_PLAYED: (GREEN, TEXT_SOFT, TEXT_MUTED, 0.35, True),
    ROW_CURRENT: (ACCENT, TEXT_BRIGHT, TEXT_SOFT, 1.0, True),
    ROW_UPCOMING: ("#3f5a66", TEXT_BODY, TEXT_MUTED, 0.35, False),
    ROW_IGNORED: (SLATE, TEXT_DIM, TEXT_DIM, 0.0, False),
}


@dataclass(frozen=True)
class SectionRowInfo:
    """One row of a SectionList. ``number`` defaults to the 1-based position ("01").

    ``flag`` (e.g. "now") follows the meta only when the whole line fits, so a
    narrow row never cuts the meta short for it.
    """

    title: str
    meta: str = ""
    state: str = ROW_UPCOMING
    clickable: bool = True
    number: str = ""
    flag: str = ""


class SectionRow(QAbstractButton):
    """A clickable agenda-style row: glowing state bar, number, title and meta."""

    HEIGHT = 46
    _TEXT_LEFT = 21 + 26 + 4    # bar, number column, gap
    _TEXT_RIGHT = 8

    def __init__(self, info: SectionRowInfo, number: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._title_font = body_font(14)
        self._meta_font = mono_font(11)
        self._number_font = mono_font(12)
        self._number = number
        self._info = info
        self._focus_visible = False
        self.set_info(info)

    def info(self) -> SectionRowInfo:
        return self._info

    def focusInEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._focus_visible = _focus_ring_after(event.reason(), self._focus_visible)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().focusOutEvent(event)
        self.update()

    def set_info(self, info: SectionRowInfo) -> None:
        self._info = info
        self.setEnabled(info.clickable)
        self.setCursor(Qt.CursorShape.PointingHandCursor if info.clickable else Qt.CursorShape.ArrowCursor)
        self.setText(info.title)
        self.setAccessibleName(f"{info.title}, {info.state}" + (f", {info.meta}" if info.meta else ""))
        self.setToolTip(plain_tooltip(info.title))   # page text: never read as HTML
        self.update()

    def enterEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().leaveEvent(event)
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(220, self.HEIGHT)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        bar, title_color, meta_color, strength, glow = _ROW_STYLE.get(self._info.state, _ROW_STYLE[ROW_UPCOMING])
        hover = self.isEnabled() and self.underMouse()
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect())
            gradient = QLinearGradient(rect.topLeft(), rect.topRight())
            gradient.setColorAt(0.0, rgba(ACCENT, 0.07 * strength + (0.05 if hover else 0.0)))
            gradient.setColorAt(1.0, rgba(ACCENT, 0.0))
            painter.fillRect(rect, gradient)
            bar_rect = QRectF(8, round((rect.height() - 30) / 2), 3, 30)
            if glow:
                path = QPainterPath()
                path.addRect(bar_rect)
                _draw_glow(painter, path, QColor(bar), 10, dpr=self.devicePixelRatioF(),
                           key=("rowbar", bar, bar_rect.top()))
            painter.fillRect(bar_rect, QColor(bar))
            painter.setFont(self._number_font)
            painter.setPen(QColor(TEXT_BRIGHT if self._info.state == ROW_CURRENT else TEXT_MUTED))
            painter.drawText(QRectF(21, 0, 26, rect.height()),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             self._info.number or self._number)
            self._paint_texts(painter, rect, title_color, meta_color)
            if self.hasFocus() and self._focus_visible:
                painter.setPen(QPen(QColor(ACCENT), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
        finally:
            painter.end()

    def _paint_texts(self, painter: QPainter, rect: QRectF, title_color: str, meta_color: str) -> None:
        left = self._TEXT_LEFT
        width = rect.width() - left - self._TEXT_RIGHT
        title_metrics = QFontMetricsF(self._title_font)
        meta_metrics = QFontMetricsF(self._meta_font)
        has_meta = bool(self._info.meta or self._info.flag)
        block = title_metrics.height() + (meta_metrics.height() if has_meta else 0)
        top = round((rect.height() - block) / 2)
        painter.setFont(self._title_font)
        painter.setPen(QColor(title_color))
        title = title_metrics.elidedText(self._info.title, Qt.TextElideMode.ElideRight, width)
        painter.drawText(QPointF(left, top + title_metrics.ascent()), title)
        if has_meta:
            painter.setFont(self._meta_font)
            painter.setPen(QColor(meta_color))
            meta = self._meta_text(meta_metrics, width)
            painter.drawText(QPointF(left, top + title_metrics.height() + meta_metrics.ascent()), meta)

    def meta_text(self) -> str:
        """The meta line as painted at the row's current width."""
        return self._meta_text(QFontMetricsF(self._meta_font), self.width() - self._TEXT_LEFT - self._TEXT_RIGHT)

    def _meta_text(self, metrics: QFontMetricsF, width: float) -> str:
        meta, flag = self._info.meta, self._info.flag
        if flag:
            joined = f"{meta} {MIDDLE_DOT} {flag}" if meta else flag
            if metrics.horizontalAdvance(joined) <= width:
                return joined
        return metrics.elidedText(meta, Qt.TextElideMode.ElideRight, width)


class _ScrollContent(QWidget):
    """Scroll-area content whose height follows heightForWidth only.

    QScrollArea also enforces the content's minimumSizeHint, which for
    word-wrapped labels is measured at their narrowest width and so is too tall.
    """

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(0, 0)


def _transparent_scroll(area: QScrollArea, content: QWidget | None = None) -> QWidget:
    """Make ``area`` frameless and see-through; returns its (transparent) content widget.

    The default content is a _ScrollContent (height from heightForWidth only);
    rows of fixed height need a plain QWidget, whose layout minimum makes the
    area scroll instead of squeezing the rows together.
    """
    area.setObjectName("hudScroll")
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.viewport().setAutoFillBackground(False)
    area.setStyleSheet("QScrollArea { background: transparent; border: none; }")
    content = content if content is not None else _ScrollContent()
    content.setObjectName("hudScrollContent")
    content.setAutoFillBackground(False)
    content.setStyleSheet("#hudScrollContent { background: transparent; }")
    area.setWidget(content)
    return content


class SectionList(QScrollArea):
    """Scrollable list of SectionRows; ``rowClicked(index)`` when a clickable row is clicked."""

    rowClicked = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        content = _transparent_scroll(self, QWidget())
        self._layout = QVBoxLayout(content)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._layout.addStretch(1)
        self._rows: list[SectionRow] = []

    def set_rows(self, rows: Sequence[SectionRowInfo]) -> None:
        """Show ``rows``; existing row widgets are reused, so focus and scroll survive updates."""
        while len(self._rows) > len(rows):
            row = self._rows.pop()
            row.hide()
            row.deleteLater()
        for index, info in enumerate(rows):
            if index < len(self._rows):
                self._rows[index].set_info(info)
                continue
            row = SectionRow(info, f"{index + 1:02d}")
            row.clicked.connect(self._on_row_clicked)
            self._layout.insertWidget(index, row)
            self._rows.append(row)

    def _on_row_clicked(self) -> None:
        row = self.sender()
        if row in self._rows:
            self.rowClicked.emit(self._rows.index(row))

    def set_row_state(self, index: int, state: str, meta: str | None = None) -> None:
        if 0 <= index < len(self._rows):
            info = self._rows[index].info()
            self._rows[index].set_info(SectionRowInfo(info.title, info.meta if meta is None else meta,
                                                      state, info.clickable, info.number, info.flag))

    def row(self, index: int) -> SectionRow:
        return self._rows[index]

    def row_count(self) -> int:
        return len(self._rows)

    def content_height(self) -> int:
        """Height of all rows with their gaps (what the list needs to show everything)."""
        count = len(self._rows)
        return count * SectionRow.HEIGHT + max(0, count - 1) * self._layout.spacing()

    def ensure_visible(self, index: int) -> None:
        if 0 <= index < len(self._rows):
            self.ensureWidgetVisible(self._rows[index], 0, 12)


# --------------------------------------------------------------------------
# Agenda and deadlines
# --------------------------------------------------------------------------

AGENDA_PAST = "past"
AGENDA_NOW = "now"
AGENDA_UPCOMING = "upcoming"
URGENCY_HIGH = "high"
URGENCY_MEDIUM = "medium"
URGENCY_LOW = "low"
DEADLINE_LATER = "#c3ccd6"    # the artboard's colour for a deadline that is not close yet
_AGENDA_STYLE: dict[str, tuple[str, float, bool, str, str, str]] = {
    # state: (bar, gradient strength, bar glow, time colour, title colour, meta colour)
    AGENDA_UPCOMING: (ACCENT, 1.0, True, TEXT_BRIGHT, TEXT_BODY, TEXT_MUTED),
    AGENDA_NOW: (GREEN, 1.4, True, TEXT_BRIGHT, TEXT_BRIGHT, GREEN),
    AGENDA_PAST: ("#3f5a66", 0.35, False, TEXT_DIM, TEXT_MUTED, TEXT_DIM),
}
URGENCY_COLORS = {URGENCY_HIGH: RED, URGENCY_MEDIUM: AMBER, URGENCY_LOW: DEADLINE_LATER}


@dataclass(frozen=True)
class AgendaRowInfo:
    """One agenda row: the time ("9:00 AM", "09:00", "ALL DAY"), title, meta and an AGENDA_* state."""

    time: str
    title: str
    meta: str = ""
    state: str = AGENDA_UPCOMING


@dataclass(frozen=True)
class DeadlineRowInfo:
    """One deadline row: title, source (drawn in capitals), due text and an URGENCY_* urgency."""

    title: str
    source: str = ""
    due: str = ""
    urgency: str = URGENCY_LOW


def _elide(metrics: QFontMetricsF, text: str, width: float) -> str:
    return metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(0.0, width))


def _split_meridiem(text: str) -> tuple[str, str]:
    """("12:30", "PM") for "12:30 PM"; (text, "") for anything else ("09:00", "ALL DAY")."""
    digits, _, meridiem = text.rpartition(" ")
    return (digits, meridiem) if digits and meridiem in ("AM", "PM") else (text, "")


class AgendaRow(QWidget):
    """The artboard's agenda row: a glowing 3 px bar, the time in mono, the title and a meta line.

    Upcoming rows have a cyan bar, the current one a green bar, past ones a
    dim bar without glow. Long titles and metas are cut with an ellipsis
    (the tooltip has them in full). A 12-hour time ("12:30 PM") is drawn as
    "12:30" on the title's line with a small "PM" under it on the meta line,
    so it fits the same column as "09:00". Such a row keeps its title on that
    line even without a meta, and so does every row of a list with 12-hour
    times (``set_two_line_times``, "ALL DAY" included), so the column keeps
    one rhythm.
    """

    HEIGHT = 44
    TIME_WIDTH = 44
    _PAD = 8
    _GAP = 10

    def __init__(self, info: AgendaRowInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._time_font = mono_font(12)
        self._time_small = mono_font(10)
        self._meridiem_font = mono_font(10, 400, 0.06)
        self._title_font = body_font(14)
        self._meta_font = mono_font(11)
        self._info = info
        self._two_line_times = False
        self.set_info(info)

    def info(self) -> AgendaRowInfo:
        return self._info

    def set_two_line_times(self, two_line: bool) -> None:
        """True while the list shows 12-hour times: keep the time and title on the first line, as those rows do."""
        if two_line != self._two_line_times:
            self._two_line_times = two_line
            self.update()

    def _two_line(self) -> bool:
        return self._two_line_times or bool(_split_meridiem(self._info.time)[1])

    def set_info(self, info: AgendaRowInfo) -> None:
        self._info = info
        text = f"{info.time} {info.title}" + (f", {info.meta}" if info.meta else "")
        self.setAccessibleName(f"{text}, {info.state}")
        # An event's title is anyone's text (an invitation), so it is never read as HTML.
        self.setToolTip(plain_tooltip(info.title + (f"\n{info.meta}" if info.meta else "")))
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(220, self.HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(120, self.HEIGHT)

    def text_left(self) -> int:
        return self._PAD + 3 + self._GAP + self.TIME_WIDTH + self._GAP

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        state = self._info.state if self._info.state in _AGENDA_STYLE else AGENDA_UPCOMING
        bar, strength, glow, time_color, title_color, meta_color = _AGENDA_STYLE[state]
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect())
            tint = GREEN if state == AGENDA_NOW else ACCENT
            gradient = QLinearGradient(rect.topLeft(), rect.topRight())
            gradient.setColorAt(0.0, rgba(tint, 0.07 * strength))
            gradient.setColorAt(1.0, rgba(tint, 0.0))
            painter.fillRect(rect, gradient)
            bar_rect = QRectF(self._PAD, round((rect.height() - 30) / 2), 3, 30)
            if glow:
                path = QPainterPath()
                path.addRect(bar_rect)
                _draw_glow(painter, path, QColor(bar), 10, dpr=self.devicePixelRatioF(),
                           key=("agendabar", bar, bar_rect.top()))
            painter.fillRect(bar_rect, QColor(bar))
            self._paint_time(painter, rect, time_color, meta_color)
            self._paint_texts(painter, rect, title_color, meta_color)
        finally:
            painter.end()

    def _text_top(self, rect: QRectF, with_meta: bool) -> int:
        """Top of the title line (and the meta line under it), centred in the row."""
        block = _line_height(self._title_font) + (_line_height(self._meta_font) if with_meta else 0)
        return round((rect.height() - block) / 2)

    def _paint_time(self, painter: QPainter, rect: QRectF, color: str, meridiem_color: str) -> None:
        text, meridiem = _split_meridiem(self._info.time)
        font = self._time_font if _text_advance(self._time_font, text) <= self.TIME_WIDTH else self._time_small
        painter.setFont(font)
        painter.setPen(QColor(color))
        left = self._PAD + 3 + self._GAP
        shown = _elide(QFontMetricsF(font), text, self.TIME_WIDTH + self._GAP - 2)
        if not self._two_line():
            painter.drawText(QRectF(left, 0, self.TIME_WIDTH + self._GAP, rect.height()),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, shown)
            return
        # On the lines of a row with a meta, in every row, so the times line up down the column.
        top = self._text_top(rect, True)
        title_metrics = QFontMetricsF(self._title_font)
        painter.drawText(QPointF(left, top + title_metrics.ascent()), shown)
        if not meridiem:
            return
        painter.setFont(self._meridiem_font)
        painter.setPen(QColor(meridiem_color))
        meta_ascent = QFontMetricsF(self._meta_font).ascent()
        painter.drawText(QPointF(left, top + title_metrics.height() + meta_ascent), meridiem)

    def _paint_texts(self, painter: QPainter, rect: QRectF, title_color: str, meta_color: str) -> None:
        left = self.text_left()
        width = rect.width() - left - 6
        title_metrics = QFontMetricsF(self._title_font)
        meta_metrics = QFontMetricsF(self._meta_font)
        top = self._text_top(rect, bool(self._info.meta) or self._two_line())
        painter.setFont(self._title_font)
        painter.setPen(QColor(title_color))
        painter.drawText(QPointF(left, top + title_metrics.ascent()),
                         _elide(title_metrics, self._info.title, width))
        if self._info.meta:
            painter.setFont(self._meta_font)
            painter.setPen(QColor(meta_color))
            painter.drawText(QPointF(left, top + title_metrics.height() + meta_metrics.ascent()),
                             _elide(meta_metrics, self._info.meta, width))


class DeadlineRow(QWidget):
    """A deadline: title over its source (mono capitals), the due label on the right, a dashed rule.

    The due label is red, amber or soft grey by urgency. A label too wide
    for the row ("TONIGHT 11:59 PM" in a narrow column) is drawn smaller,
    on two lines when it has a space.
    """

    HEIGHT = 44
    _GAP = 10
    _DUE_SHARE = 0.42      # the due label may take this much of the row's width

    def __init__(self, info: DeadlineRowInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._title_font = body_font(13)
        self._source_font = mono_font(10, 400, 0.06)
        self._due_font = mono_font(15, 500)
        self._due_small = mono_font(11, 500, 0.04)
        self._info = info
        self.set_info(info)

    def info(self) -> DeadlineRowInfo:
        return self._info

    def set_info(self, info: DeadlineRowInfo) -> None:
        self._info = info
        parts = [info.title, info.source, info.due]
        self.setAccessibleName(", ".join(part for part in parts if part))
        self.setToolTip(plain_tooltip(info.title + (f"\n{info.source}" if info.source else "")
                                      + (f"\nDue: {info.due}" if info.due else "")))
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(220, self.HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(120, self.HEIGHT)

    def due_lines(self, width: float | None = None) -> tuple[list[str], QFont]:
        """The due label as drawn at ``width`` (default: the row's): its lines and font."""
        width = float(self.width() if width is None else width)
        due = self._info.due
        if _text_advance(self._due_font, due) <= width * self._DUE_SHARE:
            return [due], self._due_font
        lines = due.split(" ", 1) if " " in due else [due]
        return lines, self._due_small

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(0, 0, self.width(), self.height() - 1)
            lines, font = self.due_lines()
            metrics = QFontMetricsF(font)
            due_width = min(max((metrics.horizontalAdvance(line) for line in lines), default=0.0),
                            rect.width() * 0.6)
            self._paint_due(painter, rect, lines, font, due_width)
            self._paint_texts(painter, rect, rect.width() - due_width - (self._GAP if due_width else 0))
            pen = QPen(rgba(ACCENT, 0.16), 1)
            pen.setDashPattern([4.0, 3.0])
            painter.setPen(pen)
            painter.drawLine(QPointF(0, self.height() - 0.5), QPointF(self.width(), self.height() - 0.5))
        finally:
            painter.end()

    def _paint_due(self, painter: QPainter, rect: QRectF, lines: list[str], font: QFont, width: float) -> None:
        if not lines or not lines[0]:
            return
        metrics = QFontMetricsF(font)
        painter.setFont(font)
        painter.setPen(QColor(URGENCY_COLORS.get(self._info.urgency, DEADLINE_LATER)))
        top = (rect.height() - metrics.height() * len(lines)) / 2
        for number, line in enumerate(lines):
            baseline = top + metrics.height() * number + metrics.ascent()
            shown = _elide(metrics, line, width)
            painter.drawText(QPointF(rect.right() - metrics.horizontalAdvance(shown), baseline), shown)

    def _paint_texts(self, painter: QPainter, rect: QRectF, width: float) -> None:
        title_metrics = QFontMetricsF(self._title_font)
        source_metrics = QFontMetricsF(self._source_font)
        source = self._info.source.upper()
        block = title_metrics.height() + (source_metrics.height() if source else 0)
        top = round((rect.height() - block) / 2)
        painter.setFont(self._title_font)
        painter.setPen(QColor(TEXT_BODY))
        painter.drawText(QPointF(0, top + title_metrics.ascent()), _elide(title_metrics, self._info.title, width))
        if source:
            painter.setFont(self._source_font)
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(QPointF(0, top + title_metrics.height() + source_metrics.ascent()),
                             _elide(source_metrics, source, width))


class AgendaPanel(ChamferPanel):
    """TODAY / TOMORROW agenda rows followed by the DEADLINES list, scrolling inside the panel.

    ``set_day(title, meta)`` names the day; ``set_events(rows)`` shows
    AgendaRows, or ``set_message(text, color, link, tooltip)`` shows one line
    instead, with an optional link button (``link_button``, ``linkClicked``).
    ``set_deadlines(rows, empty_text)`` and ``set_deadlines_meta(text)`` fill
    the DEADLINES part. Row widgets are reused, so a refresh does not flicker.
    """

    linkClicked = Signal()
    EVENT_GAP = 4

    def __init__(self, title: str = "Today", parent: QWidget | None = None) -> None:
        super().__init__(title, "", border_alpha=0.18, parent=parent)
        caps = QFont(self.meta_label.font())
        caps.setCapitalization(QFont.Capitalization.AllUppercase)
        self.meta_label.setFont(caps)
        self._scroll = QScrollArea()
        content = _transparent_scroll(self._scroll, QWidget())
        self._event_rows: list[AgendaRow] = []
        self._deadline_rows: list[DeadlineRow] = []
        self._events_layout = QVBoxLayout()
        self._events_layout.setContentsMargins(0, 0, 0, 0)
        self._events_layout.setSpacing(self.EVENT_GAP)
        self._message_box = self._make_message_box()
        self._deadline_layout = QVBoxLayout()
        self._deadline_layout.setContentsMargins(0, 0, 0, 0)
        self._deadline_layout.setSpacing(0)
        self.deadlines_empty = make_label("", mono_font(11, 400, 0.02), TEXT_DIM, wrap=True)
        self.deadlines_empty.hide()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 6, 0)   # keeps the rows off the scroll bar
        layout.setSpacing(0)
        layout.addLayout(self._events_layout)
        layout.addWidget(self._message_box)
        layout.addSpacing(12)
        layout.addWidget(self._make_deadlines_header())
        layout.addSpacing(2)
        layout.addLayout(self._deadline_layout)
        layout.addWidget(self.deadlines_empty)
        layout.addStretch(1)
        self.body_layout.addWidget(self._scroll, 1)

    def _make_message_box(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(2)
        self.message_label = make_label("", mono_font(11, 400, 0.02), TEXT_SOFT, wrap=True)
        self.link_button = HudButton("Connect", LINK)
        self.link_button.set_link_color(ACCENT)
        self.link_button.clicked.connect(self._on_link)
        self.link_button.hide()
        layout.addWidget(self.message_label)
        layout.addWidget(self.link_button, 0, Qt.AlignmentFlag.AlignLeft)
        box.hide()
        return box

    def _make_deadlines_header(self) -> QWidget:
        header = QWidget()
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        self.deadlines_title = make_label("DEADLINES", mono_font(11, 400, 0.16), ACCENT)
        caps = mono_font(11, 400, 0.16)
        caps.setCapitalization(QFont.Capitalization.AllUppercase)
        self.deadlines_meta = make_label("", caps, TEXT_DIM)
        self.deadlines_meta.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.deadlines_title)
        row.addStretch(1)
        row.addWidget(self.deadlines_meta)
        return header

    def _on_link(self) -> None:
        self.linkClicked.emit()

    # ---- narrow columns -------------------------------------------------------------

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._fit_metas()

    def _fit_metas(self) -> None:
        """Hide a meta that does not fit beside its title, so it never widens the panel
        (a wider scroll content would cut the wrapped lines off at the right)."""
        margins = self.body_layout.contentsMargins()
        width = self.width() - margins.left() - margins.right()
        pairs = ((self.title_label, self.meta_label, width),
                 (self.deadlines_title, self.deadlines_meta, width - 6 - 10))   # content margin, scroll bar
        for title, meta, room in pairs:
            fits = title.sizeHint().width() + 8 + meta.sizeHint().width() <= room
            meta.setVisible(bool(meta.text()) and fits)

    # ---- content ------------------------------------------------------------------

    def set_meta(self, text: str, color: str | QColor | None = None) -> None:
        super().set_meta(text, color)
        self._fit_metas()

    def set_day(self, title: str, meta: str = "") -> None:
        self.set_title(title)
        self.set_meta(meta)

    def set_events(self, rows: Sequence[AgendaRowInfo]) -> None:
        """Show these agenda rows (and no message line)."""
        _reuse_rows(self._event_rows, self._events_layout, rows, AgendaRow)
        two_line = any(_split_meridiem(row.time)[1] for row in rows)
        for row in self._event_rows:
            row.set_two_line_times(two_line)
        self._message_box.hide()

    def set_message(self, text: str, color: str | QColor = TEXT_SOFT, *, link: str = "",
                    tooltip: str = "") -> None:
        """One line instead of the agenda rows; ``link`` adds a link button under it."""
        _reuse_rows(self._event_rows, self._events_layout, (), AgendaRow)
        self.message_label.setText(text)
        self.message_label.setToolTip(tooltip)
        set_label_color(self.message_label, color)
        self.link_button.setText(link or "Connect")
        self.link_button.setVisible(bool(link))
        self.link_button.updateGeometry()
        self._message_box.setVisible(bool(text or link))

    def set_deadlines(self, rows: Sequence[DeadlineRowInfo], empty_text: str = "") -> None:
        _reuse_rows(self._deadline_rows, self._deadline_layout, rows, DeadlineRow)
        self.deadlines_empty.setText(empty_text)
        self.deadlines_empty.setVisible(not rows and bool(empty_text))

    def set_deadlines_meta(self, text: str) -> None:
        self.deadlines_meta.setText(text)
        self._fit_metas()

    # ---- for tests ---------------------------------------------------------------

    def events(self) -> list[AgendaRowInfo]:
        return [row.info() for row in self._event_rows]

    def event_rows(self) -> list[AgendaRow]:
        return list(self._event_rows)

    def deadlines(self) -> list[DeadlineRowInfo]:
        return [row.info() for row in self._deadline_rows]

    def deadline_rows(self) -> list[DeadlineRow]:
        return list(self._deadline_rows)

    def message(self) -> str:
        """The message line ("" while agenda rows or nothing are shown)."""
        return self.message_label.text() if not self._message_box.isHidden() else ""

    def scroll_area(self) -> QScrollArea:
        return self._scroll


def _reuse_rows(rows: list, layout: QVBoxLayout, infos: Sequence[Any],
                factory: Callable[[Any], QWidget]) -> None:
    """Make ``rows`` show ``infos``: widgets are updated in place, added or removed as needed."""
    while len(rows) > len(infos):
        row = rows.pop()
        layout.removeWidget(row)
        row.hide()
        row.deleteLater()
    for index, info in enumerate(infos):
        if index < len(rows):
            if rows[index].info() != info:
                rows[index].set_info(info)
            continue
        row = factory(info)
        layout.addWidget(row)
        rows.append(row)


# --------------------------------------------------------------------------
# Chip button and popover
# --------------------------------------------------------------------------

class ChipButton(QAbstractButton):
    """A small chamfered toggle in mono capitals with an optional count and a caret ("SECTIONS 8").

    Checkable: it shows "open" (bright border, caret up) while checked, so it
    can stand for a popover. Tab focus only, with a ring for keyboard focus.
    """

    HEIGHT = 26
    _PAD = 10
    _CARET = 8

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._font = mono_font(11, 400, 0.14)
        self._meta_font = mono_font(11)
        self._meta = ""
        self._focus_visible = False
        self.setAccessibleName(text)

    def set_meta(self, text: str) -> None:
        if text != self._meta:
            self._meta = text
            self.setAccessibleName(f"{self.text()} {text}".strip())
            self.updateGeometry()
            self.update()

    def meta(self) -> str:
        return self._meta

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        width = self._PAD + _text_advance(self._font, self.text().upper())
        if self._meta:
            width += 8 + _text_advance(self._meta_font, self._meta)
        width += 8 + self._CARET + self._PAD
        return QSize(math.ceil(width), self.HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    def focusInEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._focus_visible = _focus_ring_after(event.reason(), self._focus_visible)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().focusOutEvent(event)
        self.update()

    def enterEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        opened, hover = self.isChecked() or self.isDown(), self.underMouse() and self.isEnabled()
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect())
            painter.fillPath(chamfer_path(rect, 5), rgba(ACCENT, 0.14 if opened else 0.08 if hover else 0.04))
            border = rgba(ACCENT, 0.9 if opened else 0.6 if hover else 0.3)
            painter.setPen(QPen(border, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(chamfer_path(rect.adjusted(0.5, 0.5, -0.5, -0.5), 5))
            text_color = QColor(TEXT_BRIGHT if opened or hover else TEXT_SOFT)
            if not self.isEnabled():
                text_color = QColor(TEXT_DIM)
            x = float(self._PAD)
            painter.setFont(self._font)
            painter.setPen(text_color)
            label = self.text().upper()
            painter.drawText(QRectF(x, 0, rect.width(), rect.height()),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
            x += _text_advance(self._font, label)
            if self._meta:
                x += 8
                painter.setFont(self._meta_font)
                painter.setPen(QColor(ACCENT if opened else TEXT_DIM))
                painter.drawText(QRectF(x, 0, rect.width(), rect.height()),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._meta)
            self._paint_caret(painter, rect.width() - self._PAD - self._CARET / 2, rect.height() / 2,
                              opened, text_color)
            if self.hasFocus() and self._focus_visible:
                painter.setPen(QPen(QColor(ACCENT), 2))
                painter.drawPath(chamfer_path(rect.adjusted(1, 1, -1, -1), 5))
        finally:
            painter.end()

    def _paint_caret(self, painter: QPainter, cx: float, cy: float, up: bool, color: QColor) -> None:
        half, rise = self._CARET / 2, 2.0 if up else -2.0
        pen = QPen(color, 1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPolyline(QPolygonF([QPointF(cx - half, cy + rise), QPointF(cx, cy - rise),
                                        QPointF(cx + half, cy + rise)]))


class Popover(QWidget):
    """A frameless popup holding ``panel`` (a ChamferPanel), opened under an anchor widget.

    It closes on a click outside it, on Escape and on ``close()``; ``closed``
    is emitted whenever it hides. A click on the anchor itself only closes
    it (that press is not replayed to the anchor), so a toggle button that
    opened it does not open it again at once. Create it together with its
    window, before that is shown: a popup created later was seen to close
    again right after its first show (offscreen platform).
    """

    closed = Signal()
    MARGIN = 10      # room around the panel for its shadow
    _GAP = 6         # between the anchor and the panel

    def __init__(self, title: str = "", meta: str = "", parent: QWidget | None = None) -> None:
        flags = (Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.NoDropShadowWindowHint)
        super().__init__(parent, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._anchor: weakref.ref[QWidget] | None = None   # weak: the anchor is a sibling widget
        self.panel = ChamferPanel(title, meta, border_alpha=0.4)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(self.MARGIN, self.MARGIN, self.MARGIN, self.MARGIN)
        layout.setSpacing(0)
        layout.addWidget(self.panel)

    def anchor(self) -> QWidget | None:
        anchor = self._anchor() if self._anchor is not None else None
        return anchor if anchor is not None and shiboken6.isValid(anchor) else None

    def open_below(self, anchor: QWidget, width: int, height: int) -> None:
        """Show the panel ``width`` x ``height`` under ``anchor``, right edges aligned, on screen."""
        self._anchor = weakref.ref(anchor)
        margin = self.MARGIN
        size = QSize(width + 2 * margin, height + 2 * margin)
        corner = anchor.mapToGlobal(QPoint(anchor.width(), anchor.height()))
        top = anchor.mapToGlobal(QPoint(0, 0)).y()
        screen = anchor.screen() or QApplication.primaryScreen()
        area = screen.availableGeometry()
        x = corner.x() - width - margin
        y = corner.y() + self._GAP - margin
        if y + size.height() > area.bottom() + 1 and top - self._GAP - height - margin >= area.top():
            y = top - self._GAP - height - margin   # no room below: open above the anchor
        x = max(area.left(), min(x, area.right() + 1 - size.width()))
        y = max(area.top(), min(y, area.bottom() + 1 - size.height()))
        self.setGeometry(QRect(QPoint(x, y), size))
        self.show()   # a Qt popup takes the keyboard by itself (activating it can close it again)

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        anchor = self.anchor()
        point = event.globalPosition().toPoint()
        on_anchor = (anchor is not None and anchor.isVisible()
                     and QRect(anchor.mapToGlobal(QPoint(0, 0)), anchor.size()).contains(point))
        self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay, on_anchor)
        super().mousePressEvent(event)

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self.closed.emit()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            geometry = self.panel.geometry()
            outline = chamfer_path(QRectF(geometry), 8)
            _draw_glow(painter, outline, QColor(0, 0, 0, 200), 9, dpr=self.devicePixelRatioF(),
                       key=("popover", geometry.getRect()))
            painter.fillPath(outline, QColor(GROUND))   # the panel's own ground is not quite opaque
        finally:
            painter.end()


# --------------------------------------------------------------------------
# Approvals
# --------------------------------------------------------------------------

CARD_PENDING = "pending"
CARD_WORKING = "working"
CARD_SIGNIN = "signin"
CARD_ADDED = "added"
CARD_EXISTS = "exists"
CARD_DENIED = "denied"
CARD_FAILED = "failed"
CARD_DONE = "done"
CARD_COUNTDOWN = "countdown"   # Undo + "SENDING IN 9 S": nothing has been sent yet
CARD_SENT = "sent"             # Jarvis carried it out ("ACCEPTED", "MOVED", ...)
CARD_UNKNOWN = "unknown"       # may have happened: the reason in amber, Deny / Retry kept
CARD_STATUSES = (CARD_PENDING, CARD_WORKING, CARD_SIGNIN, CARD_ADDED, CARD_EXISTS, CARD_DENIED, CARD_FAILED,
                 CARD_DONE, CARD_COUNTDOWN, CARD_SENT, CARD_UNKNOWN)
_CARD_RESULTS: dict[str, tuple[str, str]] = {
    CARD_WORKING: ("WORKING...", ACCENT),
    CARD_SIGNIN: ("WAITING FOR GOOGLE SIGN-IN", ACCENT),
    CARD_ADDED: ("ADDED", GREEN),
    CARD_EXISTS: ("ALREADY ON CALENDAR", GREEN),
    CARD_DENIED: ("DENIED", RED),
    CARD_FAILED: ("FAILED", RED),
    CARD_DONE: ("DONE", GREEN),
    CARD_COUNTDOWN: ("SENDING...", AMBER),
    CARD_SENT: ("DONE", GREEN),
    CARD_UNKNOWN: ("UNKNOWN", AMBER),
}
# Statuses that keep Deny and the right-hand button (to decide, or to try again).
_DECIDING_STATUSES = (CARD_PENDING, CARD_FAILED, CARD_UNKNOWN)
# Statuses whose result may have an Open link.
_LINK_STATUSES = (CARD_ADDED, CARD_EXISTS, CARD_SENT)
COPIED_TEXT = "Copied"
COPIED_MS = 1500
UNDO_TEXT = "Undo"
RETRY_TEXT = "Retry"
EDIT_TEXT = "Edit"
SIGN_IN_TEXT = "Sign in"
OPEN_EVENT_TEXT = "Open event"
# What the last place of an RSVP / Move / Cancel card's tools row shows (ActionCard.set_tool_slot).
TOOL_EDIT = "edit"          # Edit (the Edit dialog)
TOOL_SIGN_IN = "signin"     # Sign in (the account must sign in again; the right-hand button is Done)
TOOL_OPEN = "open"          # Open event (Google's own page of the event, when the line has no link)


_TIP_CHARS = 1200   # a longer tooltip (a drafted reply can have 5000) would be taller than the screen
_TIP_LINES = 24


def _capped_tip_text(text: str, more: str = "") -> str:
    """``text`` cut to about 24 lines and 1200 characters for a tooltip, ending "..." and ``more``."""
    cut = "\n".join(text.split("\n")[:_TIP_LINES])
    if len(cut) > _TIP_CHARS:
        cut = cut[:_TIP_CHARS]
        space = cut.rfind(" ", _TIP_CHARS * 3 // 4)
        cut = cut[:space] if space > 0 else cut
    if cut == text:
        return text
    return cut.rstrip() + "\u2026" + (f"\n({more})" if more else "")


def _keep_last_words(text: str) -> str:
    """``text`` with its last space unbreakable when the last word is one or two characters, so a
    wrapped result never leaves "S" alone on a line ("SENDING IN" / "10 S", not "SENDING IN 10" / "S")."""
    head, space, tail = text.rpartition(" ")
    return f"{head}\u00a0{tail}" if space and head and len(tail) <= 2 else text


class _TextMemory:
    """The texts a label has shown, so its height for a width never drops below what any of
    them needed at that width: a card can grow downward but never shrinks under the mouse.

    ``fit(text, width)`` is what the label would show of ``text`` at ``width`` (a cut label
    shows at most its lines). A hidden meter label of the same font measures them.
    """

    _REMEMBER = 4
    _CACHE_LIMIT = 256

    def __init__(self, label: QLabel, fit: Callable[[str, int], str]) -> None:
        self._fit = fit
        self._meter = QLabel(label)
        self._meter.hide()
        self._meter.setTextFormat(Qt.TextFormat.PlainText)
        self._meter.setWordWrap(True)
        self._meter.setFont(label.font())
        self._texts: list[str] = []
        self._heights: dict[tuple[str, int], int] = {}

    def remember(self, text: str) -> None:
        if text and text not in self._texts:
            self._texts.append(text)
            self._texts.sort(key=len, reverse=True)
            del self._texts[self._REMEMBER:]

    def height(self, width: int) -> int:
        if len(self._heights) > self._CACHE_LIMIT:
            self._heights.clear()
        height = 0
        for text in self._texts:
            key = (text, width)
            if key not in self._heights:
                self._meter.setText(self._fit(text, width))
                self._heights[key] = self._meter.heightForWidth(width)
            height = max(height, self._heights[key])
        return height


class _ClampedLabel(QLabel):
    """Word-wrapped plain text cut to ``max_lines`` lines at its width (ellipsis, full text as tooltip).

    ``full_text()`` is what was set, ``text()`` what is shown. A ``tooltip``
    given with the text is shown whether or not the text had to be cut. With
    ``grow_only`` the label never needs less height than a text it showed
    before (see _TextMemory).
    """

    def __init__(self, font: QFont, color: str | QColor, max_lines: int = 2, *,
                 grow_only: bool = False) -> None:
        super().__init__()
        self._full = ""
        self._tip: str | None = None
        self._max_lines = max_lines
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setFont(font)
        set_label_color(self, color)
        self._memory = _TextMemory(self, self._fit) if grow_only else None

    def set_full_text(self, text: str, tooltip: str | None = None) -> None:
        if self._memory is not None and self.isVisibleTo(self.parentWidget() or self):
            self._memory.remember(self._full)
        self._full = text
        self._tip = tooltip
        self._render()
        self.updateGeometry()

    def _fit(self, text: str, width: int) -> str:
        return _elide_to_lines(text, self.font(), width - 2, self._max_lines)

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        height = super().heightForWidth(width)
        return max(height, self._memory.height(width)) if self._memory is not None else height

    def full_text(self) -> str:
        return self._full

    def is_cut(self) -> bool:
        """The text does not fit in ``max_lines`` lines at the current width (it ends in an ellipsis)."""
        return self.text() != self._full

    def _render(self) -> None:
        shown = _elide_to_lines(self._full, self.font(), self.contentsRect().width() - 2, self._max_lines)
        cut = plain_tooltip(self._full) if shown != self._full else ""
        self.setToolTip(self._tip if self._tip is not None else cut)
        super().setText(shown)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self._render()


_SOFT_BREAK = "\u200b"      # zero-width space: a line may break here, nothing is drawn
_LONGEST_PIECE = 24         # a card's detail holds about 25 mono characters per line at the narrowest
_LONG_WORD = _LONGEST_PIECE  # a word that fits on a line even then ("jordan.smith@example.edu") stays whole


def _with_soft_breaks(text: str, min_word: int = _LONG_WORD) -> str:
    """``text`` with zero-width break chances inside long words, so an email address wraps.

    QLabel wraps only at spaces and hyphens, which cuts "firstname.lastname@cs.example.edu" at
    the card edge on a narrow window. In a word longer than ``min_word`` characters (default
    _LONG_WORD; 0 for a text that is one address, which must never break inside a domain name),
    break chances go after "@" and "/" and before "."; a piece still longer than _LONGEST_PIECE
    characters (hyphens count as breaks) gets one every _LONGEST_PIECE characters.
    """
    words = re.split(r"(\s+)", text)
    for index, word in enumerate(words):
        if len(word.rstrip(",;")) <= min_word or word.isspace():   # "ana@example.edu," in a list
            continue
        out: list[str] = []
        run = 0
        for char in word:
            if char == "." and out and run:
                out.append(_SOFT_BREAK)
                run = 0
            if run >= _LONGEST_PIECE:
                out.append(_SOFT_BREAK)
                run = 0
            out.append(char)
            run += 1
            if char in "@/":
                out.append(_SOFT_BREAK)
                run = 0
            elif char == "-":   # QLabel may already break after a hyphen
                run = 0
        words[index] = "".join(out)
    return "".join(words)


class _BreakableLabel(QLabel):
    """A word-wrapped plain-text label that can also break inside long words (see _with_soft_breaks).

    ``text()`` and the accessible name are the text as set, without the break chances. Once
    a second text is set, the label never needs less height than an earlier one did at the
    same width (a card's detail may grow after an edit, never shrink). ``min_word``: see
    _with_soft_breaks (0 for an address: it breaks after "@" or before "." when it must).
    """

    def __init__(self, text: str, font: QFont, color: str | QColor, *, min_word: int = _LONG_WORD) -> None:
        super().__init__()
        self._plain = ""
        self._min_word = min_word
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setFont(font)
        set_label_color(self, color)
        self._memory = _TextMemory(self, lambda shown, _width: _with_soft_breaks(shown, min_word))
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - mirrors QLabel
        if self._plain and (text or "") != self._plain:
            self._memory.remember(self._plain)
        self._plain = text or ""
        self.setAccessibleName(self._plain)
        super().setText(_with_soft_breaks(self._plain, self._min_word))
        self.updateGeometry()

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        return max(super().heightForWidth(width), self._memory.height(width))

    def text(self) -> str:  # noqa: D102 - mirrors QLabel
        return self._plain


class _GrowOnlyLabel(QLabel):
    """A word-wrapped plain-text line that never gets shorter once it has been shown.

    Hidden until it first gets text. From then on it stays in the layout (blank
    when cleared) and keeps the height of the tallest text it has shown, at the
    current width, so the card holding it can grow downward but never shrinks
    under the mouse. With ``max_lines`` the text is cut to that many lines at
    the current width (ellipsis; the full text is the tooltip), which bounds
    how far it can ever push the cards below. ``full_text()`` is what was set,
    ``text()`` what is shown.
    """

    _REMEMBER = 4
    _CACHE_LIMIT = 256

    def __init__(self, font: QFont, color: str | QColor, alignment: Qt.AlignmentFlag, *,
                 max_lines: int = 0) -> None:
        super().__init__()
        self._meter = QLabel(self)   # measures the earlier texts; never shown
        self._meter.hide()
        for label in (self, self._meter):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setFont(font)
            label.setAlignment(alignment | Qt.AlignmentFlag.AlignTop)
            set_label_color(label, color)
        self._max_lines = max_lines
        self._full = ""
        self._shown: list[str] = []
        self._heights: dict[tuple[str, int], int] = {}
        self.hide()

    def used(self) -> bool:
        return bool(self._shown)

    def full_text(self) -> str:
        return self._full

    def set_color(self, color: str | QColor) -> None:
        for label in (self, self._meter):
            set_label_color(label, color)

    def set_line(self, text: str) -> None:
        self._full = text
        self._render()
        if text and text not in self._shown:
            self._shown.append(text)
            self._shown.sort(key=len, reverse=True)
            del self._shown[self._REMEMBER:]
        self.setVisible(self.used())
        self.updateGeometry()

    def _fit(self, text: str, width: int) -> str:
        """``text`` as shown at ``width`` (cut to ``max_lines``, with a little slack like _ClampedLabel)."""
        return _elide_to_lines(text, self.font(), width - 2, self._max_lines) if self._max_lines else text

    def _render(self) -> None:
        shown = self._fit(self._full, self.contentsRect().width())
        self.setToolTip(plain_tooltip(self._full) if shown != self._full else "")
        self.setText(shown)

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        # A cut label is measured only through the texts it has shown (each cut
        # at ``width``), so a long text never counts for more than max_lines.
        height = 0 if self._max_lines and self._shown else super().heightForWidth(width)
        if len(self._heights) > self._CACHE_LIMIT:
            self._heights.clear()
        for text in self._shown:
            key = (text, width)
            if key not in self._heights:
                self._meter.setText(self._fit(text, width))
                self._heights[key] = self._meter.heightForWidth(width)
            height = max(height, self._heights[key])
        return height

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if self._max_lines and event.oldSize().width() != event.size().width():
            self._render()


class _ToolLink(HudButton):
    """A card's tools-row link: the LINK look, but its text starts flush with the card's texts.

    No side padding, and the text is drawn from the left edge, so "Copied" in
    a button sized for "Copy reply" stays where the words began.
    """

    def __init__(self, text: str = "") -> None:
        super().__init__(text, LINK)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        hint = super().sizeHint()
        return QSize(max(1, hint.width() - 2 * self._spec.padding), hint.height())

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            _fill, _border, text_color = self._colors()
            font = QFont(self._font)
            if (self.isEnabled() and self.underMouse()) or (self.hasFocus() and self._focus_visible):
                font.setUnderline(True)
            painter.setFont(font)
            painter.setPen(text_color)
            painter.drawText(QRectF(self.rect()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                             | Qt.TextFlag.TextShowMnemonic, self.text())
        finally:
            painter.end()


_ACTIVATING_KEYS = (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Select)


class _DecisionButton(HudButton):
    """Deny / Approve: also reports a click made while it is disabled (``disabledClicked``), and
    whether the click it is making came from the keyboard (``by_key``).

    A disabled QAbstractButton takes the mouse events itself (so they never
    reach a widget below), which is the only place such a click can be seen.

    ``set_width_hints(hint, tight)``: the width it asks for (that of its widest
    text, so a new text never moves the button beside it) and the least it may
    get on a narrow card (that text with only ``_TIGHT_PADDING`` on each side),
    so "Cancel event" still fits a 760 px window. Both depend on the texts it
    may show, never on the one it shows, so Deny never moves.
    """

    disabledClicked = Signal()
    _TIGHT_PADDING = 7   # the chamfer cut is 6 px

    def __init__(self, text: str = "", variant: str = SECONDARY, *, compact: bool = False) -> None:
        super().__init__(text, variant, compact=compact)
        self._hint_width = 0
        self._tight_width = 0
        self._keyed = False   # the press that is clicking it now came from the keyboard (Space / Enter)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def by_key(self) -> bool:
        """The click being handled came from a key (Space / Enter on the focused button), not the
        mouse: the app ignores such a click right after it moved the focus here (R1)."""
        return self._keyed

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if not event.isAutoRepeat() and event.key() in _ACTIVATING_KEYS:
            self._keyed = True
        super().keyPressEvent(event)

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._keyed = False
        super().mousePressEvent(event)

    def clear_key(self) -> None:
        self._keyed = False

    def natural_width(self, text: str | None = None) -> int:
        """The width ``text`` (default: the shown text) asks for with the full padding."""
        shown = self.text() if text is None else text
        plain = shown.replace("&&", "\0").replace("&", "").replace("\0", "&")
        return max(math.ceil(_text_advance(self._font, plain)) + 2 * self._spec.padding, self._spec.height)

    def tight_width(self, text: str | None = None) -> int:
        shown = self.text() if text is None else text
        return self.natural_width(shown) - 2 * max(0, self._spec.padding - self._TIGHT_PADDING)

    def width_hints(self) -> tuple[int, int]:
        return self._hint_width, self._tight_width

    def set_width_hints(self, hint: int, tight: int) -> None:
        if (hint, tight) != (self._hint_width, self._tight_width):
            self._hint_width, self._tight_width = hint, min(tight, hint)
            self.updateGeometry()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        hint = super().sizeHint()
        return QSize(max(hint.width(), self._hint_width), hint.height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        tight = self._tight_width or self.tight_width()
        return QSize(tight, super().sizeHint().height())

    def event(self, event: QEvent) -> bool:
        if (event.type() == QEvent.Type.MouseButtonRelease and not self.isEnabled()
                and event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.disabledClicked.emit()
        return super().event(event)


# A Reply / Email card's recipients (RecipientChips, the Edit dialog's To / Cc lists).
RECIPIENT_KNOWN = "known"          # the account's trusted domains, or sent to through Jarvis before
RECIPIENT_NEW = "new"              # red NEW RECIPIENT: Send asks to confirm it in the Edit dialog
RECIPIENT_CONFIRMED = "confirmed"  # a new recipient ticked in the Edit dialog ("Send to <address>")
RECIPIENT_OWN = "own"              # the sending account itself: never a recipient (Send refuses)
NEW_RECIPIENT_TEXT = "NEW RECIPIENT"
CONFIRMED_RECIPIENT_TEXT = "NEW " + MIDDLE_DOT + " CONFIRMED"
OWN_RECIPIENT_TEXT = "SENDING ACCOUNT"
_BADGE_TEXTS = {RECIPIENT_NEW: NEW_RECIPIENT_TEXT, RECIPIENT_CONFIRMED: CONFIRMED_RECIPIENT_TEXT,
                RECIPIENT_OWN: OWN_RECIPIENT_TEXT}
_RED_RECIPIENTS = (RECIPIENT_NEW, RECIPIENT_CONFIRMED, RECIPIENT_OWN)


def _paint_recipient_badge(painter: QPainter, rect: QRectF, text: str, state: str, font: QFont) -> None:
    """A recipient's badge, on a card and in the Edit dialog alike: NEW RECIPIENT filled red while
    the address is not confirmed, outlined red once it is ("NEW \u00b7 CONFIRMED"); SENDING ACCOUNT
    filled red on the account's own address (Send refuses until it is removed)."""
    if state in (RECIPIENT_NEW, RECIPIENT_OWN):
        painter.fillRect(rect, QColor(RED_LINE))
        painter.setPen(QColor("#1a0505"))
    else:
        painter.setPen(QPen(rgba(RED_LINE, 0.9), 1))
        painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
        painter.setPen(QColor(RED))
    painter.setFont(font)
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


@dataclass(frozen=True)
class RecipientChip:
    """One address on a Reply / Email card: ``state`` is RECIPIENT_KNOWN, _NEW, _CONFIRMED or _OWN."""

    address: str
    state: str = RECIPIENT_KNOWN


def _wrapped_lines(text: str, font: QFont, width: float) -> list[str]:
    """``text`` word-wrapped to ``width`` px; a word that does not fit breaks after "@" or "/",
    before "." or, failing that, anywhere (an address is always shown whole, and a short one
    never breaks inside a domain name while a break after "@" or before "." will do)."""
    if not text:
        return [""]
    broken = _with_soft_breaks(text, 0)
    layout = QTextLayout(broken, font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    lines: list[str] = []
    layout.beginLayout()
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(max(1.0, width))
        piece = broken[line.textStart():line.textStart() + line.textLength()]
        lines.append(piece.replace(_SOFT_BREAK, "").rstrip())
    layout.endLayout()
    return lines or [""]


class RecipientChips(QWidget):
    """A Reply / Email card's FROM line and its TO / CC recipients as chips (painted).

    ``set_rows(sender, to, cc)``: ``sender`` is the From text ("work (ana@example.edu)"),
    ``to`` / ``cc`` RecipientChip items (an empty Cc has no row). A RECIPIENT_NEW chip is red
    with a NEW RECIPIENT badge (Send asks to confirm it in the Edit dialog first); a
    RECIPIENT_CONFIRMED one is red with "NEW \u00b7 CONFIRMED"; a RECIPIENT_OWN one (the sending
    account itself: Send refuses) is red with SENDING ACCOUNT. Every address is shown whole: a
    long one wraps inside its chip. Like the card's other texts it never gets shorter once it
    has been shown (a later, shorter list keeps the room), so the card never shrinks under the
    mouse. ``text()`` is what is shown, as plain text (also the accessible name and tooltip).
    """

    _CAPTION_GAP = 10
    _ROW_GAP = 4
    _CHIP_GAP = 6
    _CHIP_VGAP = 4
    _PAD_X = 6
    _PAD_Y = 2
    _BADGE_GAP = 6
    _BADGE_PAD = 4
    _REMEMBER = 4
    _MIN_VALUE_WIDTH = 60

    def __init__(self, parent: QWidget | None = None, *, top_margin: int = 0) -> None:
        super().__init__(parent)
        self._top = top_margin
        self._caption_font = mono_font(10, 400, 0.14)
        self._value_font = mono_font(11)
        self._badge_font = mono_font(9, 600, 0.08)
        self._sender = ""
        self._to: tuple[RecipientChip, ...] = ()
        self._cc: tuple[RecipientChip, ...] = ()
        self._shown: list[tuple[str, tuple[RecipientChip, ...], tuple[RecipientChip, ...]]] = []
        self._heights: dict[tuple[Any, int], int] = {}
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.hide()

    # ---- content ---------------------------------------------------------------

    def set_rows(self, sender: str, to: Sequence[RecipientChip], cc: Sequence[RecipientChip] = ()) -> None:
        content = (sender, tuple(to), tuple(cc))
        if content == (self._sender, self._to, self._cc):
            return
        if self.isVisibleTo(self.parentWidget() or self) and (self._sender or self._to or self._cc):
            previous = (self._sender, self._to, self._cc)
            if previous not in self._shown:
                self._shown.append(previous)
                del self._shown[:-self._REMEMBER]
        self._sender, self._to, self._cc = content
        text = self.text()
        self.setAccessibleName(text)
        self.setToolTip(plain_tooltip(text))
        self.setVisible(bool(sender or to or cc))
        self.updateGeometry()
        self.update()

    def sender_text(self) -> str:
        return self._sender

    def chips(self) -> tuple[tuple[RecipientChip, ...], tuple[RecipientChip, ...]]:
        return self._to, self._cc

    def text(self) -> str:
        """"From: work (ana@example.edu)\nTo: ana@example.edu, ben@example.edu (new recipient)"."""
        def names(chips: Sequence[RecipientChip]) -> str:
            words = {RECIPIENT_NEW: " (new recipient, not confirmed)",
                     RECIPIENT_CONFIRMED: " (new recipient, confirmed)",
                     RECIPIENT_OWN: " (the sending account itself, never sent to)"}
            return ", ".join(chip.address + words.get(chip.state, "") for chip in chips)

        lines = [f"From: {self._sender}"] if self._sender else []
        if self._to:
            lines.append(f"To: {names(self._to)}")
        if self._cc:
            lines.append(f"Cc: {names(self._cc)}")
        return "\n".join(lines)

    # ---- geometry --------------------------------------------------------------

    def _caption_width(self) -> float:
        return max(_text_advance(self._caption_font, word) for word in ("FROM", "TO", "CC")) + self._CAPTION_GAP

    def _chip_height(self, lines: int) -> float:
        return lines * QFontMetricsF(self._value_font).lineSpacing() + 2 * self._PAD_Y

    def _layout(self, sender: str, to: Sequence[RecipientChip], cc: Sequence[RecipientChip],
                width: int) -> tuple[float, list[tuple]]:
        """(height, items) at ``width``; items are what paintEvent draws."""
        items: list[tuple] = []
        x0 = self._caption_width()
        avail = max(float(self._MIN_VALUE_WIDTH), width - x0)
        right = x0 + avail
        value = QFontMetricsF(self._value_font)
        line_h = value.lineSpacing()
        caption_dy = max(0.0, (line_h - QFontMetricsF(self._caption_font).lineSpacing()) / 2)
        badge_w = {text: _text_advance(self._badge_font, text) + 2 * self._BADGE_PAD
                   for text in _BADGE_TEXTS.values()}
        y = 0.0
        first = True
        if sender:
            items.append(("caption", QPointF(0, y + caption_dy), "FROM"))
            for line in _wrapped_lines(sender, self._value_font, avail):
                items.append(("value", QRectF(x0, y, avail, line_h), line))
                y += line_h
            first = False
        for caption, chips in (("TO", to), ("CC", cc)):
            if not chips:
                continue
            if not first:
                y += self._ROW_GAP
            first = False
            items.append(("caption", QPointF(0, y + self._PAD_Y + caption_dy), caption))
            x, row_h = x0, 0.0
            for chip in chips:
                badge = _BADGE_TEXTS.get(chip.state, "")
                text_w = _text_advance(self._value_font, chip.address)
                extra = self._BADGE_GAP + badge_w[badge] if badge else 0.0
                single = 2 * self._PAD_X + text_w + extra
                if single <= avail:
                    if x > x0 and x + single > right + 0.5:
                        x, y, row_h = x0, y + row_h + self._CHIP_VGAP, 0.0
                    rect = QRectF(x, y, math.ceil(single), self._chip_height(1))
                    lines = [chip.address]
                    badge_at = QPointF(x + self._PAD_X + text_w + self._BADGE_GAP, y) if badge else None
                    x += rect.width() + self._CHIP_GAP
                else:   # wider than the row: the chip takes the whole row and the address wraps
                    if x > x0:
                        x, y, row_h = x0, y + row_h + self._CHIP_VGAP, 0.0
                    inner = avail - 2 * self._PAD_X
                    lines = _wrapped_lines(chip.address, self._value_font, inner)
                    last_w = _text_advance(self._value_font, lines[-1])
                    count = len(lines)
                    badge_at = None
                    if badge:
                        if last_w + extra <= inner:
                            badge_at = QPointF(x + self._PAD_X + last_w + self._BADGE_GAP, y + (count - 1) * line_h)
                        else:
                            badge_at = QPointF(x + self._PAD_X, y + count * line_h)
                            count += 1
                    rect = QRectF(x, y, avail, self._chip_height(count))
                    x = right + 1   # the next chip starts a new row
                items.append(("chip", rect, chip.state))
                for index, line in enumerate(lines):
                    items.append(("chip_text", QRectF(rect.x() + self._PAD_X, rect.y() + self._PAD_Y + index * line_h,
                                                      rect.width() - 2 * self._PAD_X, line_h), line, chip.state))
                if badge and badge_at is not None:
                    items.append(("badge", QRectF(badge_at.x(), badge_at.y() + self._PAD_Y + 1, badge_w[badge],
                                                  line_h - 2), badge, chip.state))
                row_h = max(row_h, rect.height())
            y += row_h
        return y, items

    def _height(self, content: tuple, width: int) -> int:
        key = (content, width)
        if key not in self._heights:
            if len(self._heights) > 256:
                self._heights.clear()
            self._heights[key] = self._top + math.ceil(self._layout(*content, width)[0]) + 1
        return self._heights[key]

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        contents = [(self._sender, self._to, self._cc), *self._shown]
        return max(self._height(content, width) for content in contents)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        width = max(self.width(), 240)
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        width = math.ceil(self._caption_width()) + self._MIN_VALUE_WIDTH
        return QSize(width, self.heightForWidth(max(self.width(), width)))

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self.updateGeometry()

    # ---- painting ----------------------------------------------------------------

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.translate(0, self._top)
            _height, items = self._layout(self._sender, self._to, self._cc, self.width())
            for item in items:
                kind = item[0]
                if kind == "caption":
                    painter.setFont(self._caption_font)
                    painter.setPen(QColor(AMBER_META))
                    metrics = QFontMetricsF(self._caption_font)
                    painter.drawText(QPointF(item[1].x(), item[1].y() + metrics.ascent()), item[2])
                elif kind == "value":
                    painter.setFont(self._value_font)
                    painter.setPen(QColor(TEXT_SOFT))
                    painter.drawText(item[1], Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, item[2])
                elif kind == "chip":
                    new = item[2] in _RED_RECIPIENTS
                    rect = item[1].adjusted(0.5, 0.5, -0.5, -0.5)
                    path = chamfer_path(rect, 4)
                    painter.fillPath(path, rgba(RED, 0.10) if new else rgba(AMBER, 0.06))
                    painter.setPen(QPen(rgba(RED_LINE, 0.9) if new else rgba(AMBER, 0.35), 1))
                    painter.drawPath(path)
                elif kind == "chip_text":
                    new = item[3] in _RED_RECIPIENTS
                    painter.setFont(self._value_font)
                    painter.setPen(QColor(RED if new else AMBER_TEXT))
                    painter.drawText(item[1], Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, item[2])
                elif kind == "badge":
                    _paint_recipient_badge(painter, item[1], item[2], item[3], self._badge_font)
        finally:
            painter.end()


class _MailBody(QWidget):
    """A drafted email as a Reply / Email card shows it: its own line breaks, word-wrapped, at most
    ``max_lines`` lines (the last one ends in an ellipsis when there is more: hover, or Edit, for
    all of it), and its web links in the accent colour, underlined (never clickable).

    ``set_text(text, links, tooltip)``: ``links`` are (start, end) offsets into ``text``. Like the
    card's other texts it never needs less height than a text it showed before at the same width.
    ``shown_lines()`` returns the lines as drawn.
    """

    _REMEMBER = 4

    def __init__(self, font: QFont, color: str | QColor, link_color: str | QColor, max_lines: int, *,
                 top_margin: int = 0) -> None:
        super().__init__()
        self._font = QFont(font)
        self._color = QColor(color)
        self._link_color = QColor(link_color)
        self._max_lines = max(1, max_lines)
        self._top = top_margin
        self._text = ""
        self._links: tuple[tuple[int, int], ...] = ()
        self._shown: list[str] = []
        self._heights: dict[tuple[str, int], int] = {}
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

    def set_text(self, text: str, links: Sequence[tuple[int, int]] = (), tooltip: str = "") -> None:
        if self._text and text != self._text and self.isVisibleTo(self.parentWidget() or self):
            if self._text not in self._shown:
                self._shown.append(self._text)
                del self._shown[:-self._REMEMBER]
        self._text = text or ""
        self._links = tuple((int(start), int(end)) for start, end in links if 0 <= start < end <= len(self._text))
        self.setToolTip(tooltip)
        self.setAccessibleName(self._text)
        self.updateGeometry()
        self.update()

    def text(self) -> str:
        return self._text

    def links(self) -> tuple[tuple[int, int], ...]:
        return self._links

    def _lines(self, text: str, width: float) -> tuple[list[tuple[int, str]], bool]:
        """[(offset of the line in ``text``, the line as drawn)], and whether lines were left out."""
        spans: list[tuple[int, int]] = []
        offset = 0
        for paragraph in text.split("\n"):
            if not paragraph:
                spans.append((offset, offset))
            else:
                layout = QTextLayout(paragraph, self._font)
                option = QTextOption()
                option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
                layout.setTextOption(option)
                layout.beginLayout()
                while True:
                    line = layout.createLine()
                    if not line.isValid():
                        break
                    line.setLineWidth(max(1.0, width))
                    spans.append((offset + line.textStart(), offset + line.textStart() + line.textLength()))
                layout.endLayout()
            offset += len(paragraph) + 1
            if len(spans) > self._max_lines:
                break
        cut = len(spans) > self._max_lines
        lines = [(start, text[start:end].rstrip("\n")) for start, end in spans[:self._max_lines]]
        if cut and lines:
            start, last = lines[-1]
            metrics = QFontMetricsF(self._font)
            last = last.rstrip()
            while last and metrics.horizontalAdvance(last + "\u2026") > width:
                last = last[:-1].rstrip()
            lines[-1] = (start, last + "\u2026")
        return lines, cut

    def shown_lines(self) -> list[str]:
        return [line for _start, line in self._lines(self._text, self._text_width(self.width()))[0]]

    def is_cut(self) -> bool:
        return self._lines(self._text, self._text_width(self.width()))[1]

    @staticmethod
    def _text_width(width: int) -> float:
        return max(1.0, width - 2.0)   # a little slack, like _ClampedLabel

    def _height(self, text: str, width: int) -> int:
        key = (text, width)
        if key not in self._heights:
            if len(self._heights) > 256:
                self._heights.clear()
            count = len(self._lines(text, self._text_width(width))[0]) if text else 0
            self._heights[key] = self._top + math.ceil(count * QFontMetricsF(self._font).lineSpacing())
        return self._heights[key]

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        return max(self._height(text, width) for text in (self._text, *self._shown))

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        width = max(self.width(), 240)
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(60, self.heightForWidth(max(self.width(), 60)))

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self.updateGeometry()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            painter.setPen(self._color)
            line_h = QFontMetricsF(self._font).lineSpacing()
            link_format = QTextCharFormat()
            link_format.setForeground(self._link_color)
            link_format.setFontUnderline(True)
            lines, _cut = self._lines(self._text, self._text_width(self.width()))
            for index, (start, shown) in enumerate(lines):
                if not shown:
                    continue
                formats = []
                for link_start, link_end in self._links:
                    lo, hi = max(link_start, start), min(link_end, start + len(shown))
                    if lo < hi:
                        span = QTextLayout.FormatRange()
                        span.start, span.length, span.format = lo - start, hi - lo, link_format
                        formats.append(span)
                layout = QTextLayout(shown, self._font)
                layout.setFormats(formats)
                layout.beginLayout()
                line = layout.createLine()
                if line.isValid():
                    line.setLineWidth(max(1.0, self.width() * 4.0))
                    line.setPosition(QPointF(0, 0))
                layout.endLayout()
                layout.draw(painter, QPointF(0, self._top + index * line_h))
        finally:
            painter.end()


class ActionCard(QWidget):
    """An approval card: kind, title, detail, then one fixed-height slot with Deny / Approve or the result.

    Phase 2 additions: ``check_line=True`` reserves three lines under the detail for
    Google's own view of the event (``set_check_line``; whatever arrives by itself
    goes there, so it never moves the cards) and lets the kind label wrap once (an
    edited card's " \u00b7 EDITED"); ``edit_text`` adds an Edit link (``editClicked``,
    ``set_edit_enabled``) as the tools row's last place, which can show a Sign in link
    instead (``sign_in_text``, ``signInClicked``) or an "Open event" link to Google's
    own page of the event (``event_text``, ``sourceClicked``): ``set_tool_slot``; the
    three are equally wide, so switching never moves anything. ``set_copy_text``
    shows Copy once an edit adds a note; CARD_COUNTDOWN puts an Undo button on the
    left of the slot, away from Approve (``undoClicked``); CARD_UNKNOWN keeps the
    buttons like FAILED, with the reason in amber; ``set_approve_text`` renames the
    right-hand button ("Retry", "Sign in", "Done") without moving the left-hand one
    (the button is as wide as its widest text from the start); ``deny_text`` names
    the left-hand one ("Skip").

    The slot swaps its content (the buttons, or a result such as ADDED with an
    "Open" link) but never its height, so a click never moves this card or the
    cards below it, and a second click at the same spot cannot land on another
    card's button. FAILED keeps the buttons (to retry) and shows the reason
    under them, cut to two lines (the whole reason is the tooltip); that line
    and the amber note (``set_note``) let the card grow downward by a bounded
    amount, but a card never gets shorter again. ``actionable=False``
    makes an informational card (no buttons, dim kind). ADDED / EXISTS show
    the "Open" link when a link is known (``openClicked(link)``).

    ``approve_text`` names the right-hand button ("Approve", "Add block",
    "Done"); ``title_lines`` cuts the title to that many lines (0 = never);
    ``body`` adds a three-line preview of a drafted text (hover for all of
    it). ``open_text`` / ``copy_text`` add a tools row of links above the
    slot ("Open thread" -> ``sourceClicked(action_id)``, "Copy reply" ->
    ``copyClicked(action_id)``; ``show_copied()`` reads "Copied" for 1.5 s).
    The tools row is there from the start, is never locked or hidden, and
    its buttons keep their width, so nothing in it moves.

    ``set_locked(True, tooltip)`` disables (dims) Deny / Approve without any
    change in size, for example while another card's approval runs; a click
    on a locked button emits ``lockedClicked(action_id)`` instead of
    ``approveClicked`` / ``denyClicked``, so the caller can log it.
    """

    approveClicked = Signal(str)
    denyClicked = Signal(str)
    openClicked = Signal(str)
    lockedClicked = Signal(str)
    sourceClicked = Signal(str)
    copyClicked = Signal(str)
    undoClicked = Signal(str)
    editClicked = Signal(str)
    signInClicked = Signal(str)

    _PAD_LEFT = 14
    _PAD_TOP = 10
    _PAD_RIGHT = 12
    _PAD_BOTTOM = 10
    _SLOT_GAP = 8   # between the texts and the buttons / result
    _FAILURE_LINES = 2
    _BODY_LINES = 3
    _MAIL_BODY_LINES = 10   # a Reply / Email card: what Send sends, line breaks kept
    _BODY_GAP = 4    # above the body preview (on top of the texts' 2 px spacing)
    _RECIPIENTS_GAP = 4   # above FROM / TO / CC
    _TOOLS_GAP = 4   # above the tools row
    _TOOLS_SPACING = 18
    _CHECK_LINES = 3   # Google's title and time, then who organizes it and your answer
    _CHECK_GAP = 4   # above the check line

    def __init__(self, action_id: str, kind: str, title: str, detail: str = "", *,
                 actionable: bool = True, approve_text: str = "Approve", body: str = "",
                 title_lines: int = 0, open_text: str = "", copy_text: str = "",
                 edit_text: str = "", sign_in_text: str = "", check_line: bool = False,
                 deny_text: str = "Deny", event_text: str = "", mail: bool = False,
                 body_lines: int = 0, body_links: Sequence[tuple[int, int]] = (),
                 approve_alternatives: Sequence[str] = (), parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.action_id = action_id
        self._actionable = actionable
        self._status = CARD_PENDING
        self._link = ""
        self._result_text = ""
        self._locked = False
        self._lock_tip = ""
        self._title = title
        self._copy_text = copy_text
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        # One line, cut with an ellipsis when an account alias makes it wider than the card; an
        # RSVP / Move / Cancel card's may wrap once, so " \u00b7 EDITED" is never cut (it never
        # gets shorter again).
        self.kind_label = _ClampedLabel(mono_font(10, 400, 0.16), AMBER if actionable else TEXT_DIM,
                                        max_lines=2 if check_line else 1, grow_only=check_line)
        self.kind_label.set_full_text(kind.upper())
        if title_lines > 0:
            self.title_label: QLabel = _ClampedLabel(body_font(14, 600), AMBER_TEXT, max_lines=title_lines)
            self.title_label.set_full_text(title)
        else:
            self.title_label = make_label(title, body_font(14, 600), AMBER_TEXT, wrap=True)
        # Wraps inside long email addresses too (QLabel alone would cut them at the card edge).
        self.detail_label: QLabel = _BreakableLabel(detail, mono_font(11), AMBER_SUB)
        self.detail_label.setVisible(bool(detail))
        # Google's own view of the event (RSVP / Move / Cancel): three lines, reserved from the
        # start so filling it never moves the cards.
        self.check_label = _ClampedLabel(mono_font(11), TEXT_SOFT, max_lines=self._CHECK_LINES)
        self.check_label.setContentsMargins(0, self._CHECK_GAP, 0, 0)
        self.check_label.setFixedHeight(
            self._CHECK_GAP + math.ceil(self._CHECK_LINES * QFontMetricsF(self.check_label.font()).lineSpacing()) + 2)
        self.check_label.setVisible(check_line)
        self._check_tip = ""
        self._check_warn = False
        self._copy_more = f"{copy_text} for the whole text" if copy_text else ""
        self._mail = mail
        # A Reply / Email card: FROM and the TO / CC chips (set_recipients), and the drafted text
        # with its own line breaks and highlighted links (body_lines lines, MAIL_BODY_LINES by default).
        self.recipients = RecipientChips(top_margin=self._RECIPIENTS_GAP)
        self.mail_body = _MailBody(body_font(12), TEXT_SOFT, ACCENT, body_lines or self._MAIL_BODY_LINES,
                                   top_margin=self._BODY_GAP)
        self.body_label = _ClampedLabel(body_font(12), TEXT_SOFT, max_lines=body_lines or self._BODY_LINES,
                                        grow_only=True)
        self.body_label.setContentsMargins(0, self._BODY_GAP, 0, 0)
        self.body_label.set_full_text(" ".join(body.split()),
                                      tooltip=plain_tooltip(_capped_tip_text(body, self._copy_more)))
        self.body_label.setVisible(bool(body.strip()) and not mail)
        if mail:
            self.mail_body.set_text(body, body_links, plain_tooltip(_capped_tip_text(body, self._copy_more)))
        self.mail_body.setVisible(mail and bool(body.strip()))
        self.source_button: HudButton = _ToolLink(open_text or "Open")
        self.copy_button: HudButton = _ToolLink(copy_text or "Copy")
        self.edit_button: HudButton = _ToolLink(edit_text or EDIT_TEXT)
        self.sign_in_button: HudButton = _ToolLink(sign_in_text or SIGN_IN_TEXT)
        self.event_button: HudButton = _ToolLink(event_text or OPEN_EVENT_TEXT)
        self._make_tools(kind, title, open_text, copy_text, edit_text, sign_in_text, event_text)
        self.deny_button = _DecisionButton(deny_text or "Deny", DENY, compact=True)
        self.approve_button = _DecisionButton(approve_text or "Approve", APPROVE, compact=True)
        # Like Undo's: a screen reader says which card a decision button decides ("Add block (...)").
        self._decides = f"{kind.upper()}: {title}"
        self.deny_button.setAccessibleName(f"{self.deny_button.text()} ({self._decides})")
        self.approve_button.setAccessibleName(f"{self.approve_button.text()} ({self._decides})")
        # Every text the button may show later, so a new one never moves Deny (an RSVP, Move or
        # Cancel card may also read Sign in or Done; a Reply / Email card Send or Done).
        self._fit_approve_width((approve_text or "Approve", RETRY_TEXT)
                                + ((SIGN_IN_TEXT, "Done") if check_line else ()) + tuple(approve_alternatives))
        self.result_label = _ClampedLabel(mono_font(11, 400, 0.12), GREEN)
        self.result_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.open_button = HudButton("Open", LINK)
        self.open_button.set_link_color(ACCENT)
        self.undo_button = HudButton(UNDO_TEXT, SECONDARY, compact=True)
        self.undo_button.setAccessibleName(f"{UNDO_TEXT} ({kind.upper()}: {title})")
        # Two lines at most (hover for the whole reason): a 200-character Google
        # error would otherwise push the cards below down by about a card's
        # height and slide another card's Approve under the mouse.
        self.failure_label = _GrowOnlyLabel(mono_font(11, 400, 0.12), RED, Qt.AlignmentFlag.AlignRight,
                                            max_lines=self._FAILURE_LINES)
        self.note_label = _GrowOnlyLabel(mono_font(11), AMBER, Qt.AlignmentFlag.AlignLeft)
        self.deny_button.clicked.connect(self._on_deny)
        self.approve_button.clicked.connect(self._on_approve)
        self.deny_button.disabledClicked.connect(self._on_locked_click)
        self.approve_button.disabledClicked.connect(self._on_locked_click)
        self.open_button.clicked.connect(self._on_open)
        self.source_button.clicked.connect(self._on_source)
        self.copy_button.clicked.connect(self._on_copy)
        self.edit_button.clicked.connect(self._on_edit)
        self.sign_in_button.clicked.connect(self._on_sign_in)
        self.event_button.clicked.connect(self._on_source)
        self.undo_button.clicked.connect(self._on_undo)
        self._copied_timer = QTimer(self)
        self._copied_timer.setSingleShot(True)
        self._copied_timer.setInterval(COPIED_MS)
        self._copied_timer.timeout.connect(self._reset_copy)
        self._build_layout()
        self.set_status(CARD_PENDING)

    def _make_tools(self, kind: str, title: str, open_text: str, copy_text: str, edit_text: str = "",
                    sign_in_text: str = "", event_text: str = "") -> None:
        """The tools row: Open / Copy links, each as wide as its widest text from the start, then
        one place for Edit, Sign in or Open event (set_tool_slot); those are all as wide as the
        widest of them, so switching never moves anything. Edit shows first."""
        self._tools = QWidget()
        tools = FlowLayout(self._tools, h_spacing=self._TOOLS_SPACING, v_spacing=2)
        tools.setContentsMargins(0, self._TOOLS_GAP, 0, 0)
        self._slot_buttons = {TOOL_EDIT: self.edit_button, TOOL_SIGN_IN: self.sign_in_button,
                              TOOL_OPEN: self.event_button}
        self._slot_texts = {mode: text for mode, text in ((TOOL_EDIT, edit_text), (TOOL_SIGN_IN, sign_in_text),
                                                          (TOOL_OPEN, event_text)) if text}
        slot_width = 0
        for mode, text in self._slot_texts.items():
            self._slot_buttons[mode].setText(text)
            slot_width = max(slot_width, self._slot_buttons[mode].sizeHint().width())
        self._tool_slot = TOOL_EDIT if edit_text else ""
        for button, text, texts in ((self.source_button, open_text, (open_text,)),
                                    (self.copy_button, copy_text, (copy_text, COPIED_TEXT)),
                                    (self.edit_button, edit_text, (edit_text,)),
                                    (self.sign_in_button, sign_in_text, (sign_in_text,)),
                                    (self.event_button, event_text, (event_text,))):
            button.set_link_color(ACCENT)
            fallback = button.text()
            width = 0
            for shown in texts:
                button.setText(shown)
                width = max(width, button.sizeHint().width())
            button.setText(text or fallback)
            in_slot = text and any(button is self._slot_buttons[mode] for mode in self._slot_texts)
            button.setMinimumWidth(slot_width if in_slot else width)
            button.setAccessibleName(f"{text} ({kind.upper()}: {title})")
            button.setVisible(bool(text) and button is not self.sign_in_button and button is not self.event_button)
            tools.addWidget(button)
        self._tools.setVisible(bool(open_text or copy_text or edit_text or sign_in_text or event_text))

    def set_tool_slot(self, mode: str) -> None:
        """What the tools row's last place shows: TOOL_EDIT ("Edit"), TOOL_SIGN_IN ("Sign in": the
        card's account must sign in again) or TOOL_OPEN ("Open event": Google's own page of the
        event). A mode the card was built without shows Edit. Nothing moves."""
        if mode not in self._slot_texts:
            mode = TOOL_EDIT if TOOL_EDIT in self._slot_texts else ""
        if mode == self._tool_slot:
            return
        focus = QApplication.focusWidget()
        if focus is not None and focus is self._slot_buttons.get(self._tool_slot):
            self.setFocus(Qt.FocusReason.OtherFocusReason)   # never to the next card's link
        self._tool_slot = mode
        for name, button in self._slot_buttons.items():
            button.setVisible(name == mode)

    def tool_slot(self) -> str:
        return self._tool_slot

    # Bound methods, not lambdas: a lambda capturing self keeps the card alive.
    def _on_deny(self) -> None:
        self.denyClicked.emit(self.action_id)

    def _on_approve(self) -> None:
        try:
            self.approveClicked.emit(self.action_id)
        finally:
            if shiboken6.isValid(self.approve_button):
                self.approve_button.clear_key()

    def _on_open(self) -> None:
        self.openClicked.emit(self._link)

    def _on_source(self) -> None:
        self.sourceClicked.emit(self.action_id)

    def _on_copy(self) -> None:
        self.copyClicked.emit(self.action_id)

    def _on_edit(self) -> None:
        self.editClicked.emit(self.action_id)

    def _on_sign_in(self) -> None:
        self.signInClicked.emit(self.action_id)

    def _on_undo(self) -> None:
        self.undoClicked.emit(self.action_id)

    def _fit_approve_width(self, texts: Sequence[str]) -> None:
        """The right-hand button as wide as the widest of ``texts`` ("Accept" / "Retry"), so a new
        text never moves Deny (on a narrow card both buttons may lose some side padding)."""
        button = self.approve_button
        hint, tight = button.width_hints()
        for text in texts:
            hint = max(hint, button.natural_width(text))
            tight = max(tight, button.tight_width(text))
        button.set_width_hints(hint, tight)

    def set_approve_text(self, text: str) -> None:
        """Rename the right-hand button ("Accept" -> "Retry"); Deny never moves."""
        self._fit_approve_width((text,))
        self.approve_button.setText(text)
        self.approve_button.setAccessibleName(f"{text} ({self._decides})")

    def approve_text(self) -> str:
        return self.approve_button.text()

    def set_check_line(self, text: str, tooltip: str = "", *, warn: bool = False) -> None:
        """Google's own view of the event, under the detail (two lines at most; hover for all,
        or for ``tooltip`` when given). ``warn`` draws it in amber (Jarvis won't act on it, or
        the account needs a sign-in). Its height was reserved, so nothing ever moves."""
        if (text, tooltip, warn) == (self.check_label.full_text(), self._check_tip, self._check_warn):
            return
        self._check_tip = tooltip
        self.check_label.set_full_text(text, tooltip=plain_tooltip(tooltip or text))
        if warn != self._check_warn:
            self._check_warn = warn
            set_label_color(self.check_label, AMBER if warn else TEXT_SOFT)

    def check_warns(self) -> bool:
        """The check line is drawn in amber (set_check_line(warn=True))."""
        return self._check_warn

    def check_line(self) -> str:
        return self.check_label.full_text()

    def set_edit_enabled(self, enabled: bool) -> None:
        """Enable Edit and Sign in (both open something that must not run beside another card's
        countdown or job); Open event stays usable."""
        for button in (self.edit_button, self.sign_in_button):
            button.setEnabled(bool(enabled))

    def set_sign_in_visible(self, visible: bool) -> None:
        """Show Sign in in the tools row's last place, or Edit again (set_tool_slot)."""
        self.set_tool_slot(TOOL_SIGN_IN if visible else TOOL_EDIT)

    def set_body(self, body: str, links: Sequence[tuple[int, int]] = ()) -> None:
        """A new body preview (after an edit): the card may grow, never shrink. ``links``: the web
        links' (start, end) offsets, highlighted on a Reply / Email card."""
        tip = plain_tooltip(_capped_tip_text(body, self._copy_more))
        self.body_label.set_full_text(" ".join(body.split()), tooltip=tip)
        if self._mail:
            self.mail_body.set_text(body, links, tip)
            if body.strip():
                self.mail_body.setVisible(True)
        elif body.strip():
            self.body_label.setVisible(True)

    def mail_cut(self) -> bool:
        """A Reply / Email card that does not show all of its subject or its text at its current
        width (the title ends in an ellipsis, or the text is cut after its lines)."""
        if not self._mail:
            return False
        title_cut = isinstance(self.title_label, _ClampedLabel) and self.title_label.is_cut()
        return title_cut or (not self.mail_body.isHidden() and self.mail_body.is_cut())

    def set_recipients(self, sender: str, to: Sequence[RecipientChip], cc: Sequence[RecipientChip] = ()) -> None:
        """A Reply / Email card's FROM line and TO / CC chips (see RecipientChips): the card may
        grow, never shrink."""
        self.recipients.set_rows(sender, to, cc)

    def mail_text(self) -> str:
        """The drafted text of a Reply / Email card exactly as set (line breaks kept)."""
        return self.mail_body.text()

    def set_copy_text(self, text: str) -> None:
        """Show the tools row's Copy link as ``text`` ("Copy note"), or hide it with "" (after an
        edit added or removed the note). Its width only ever grows, so it never moves Edit."""
        if text == self._copy_text:
            return
        self._copy_text = text
        self._copy_more = f"{text} for the whole text" if text else ""
        self._copied_timer.stop()
        if text:
            width = self.copy_button.minimumWidth()
            for shown in (text, COPIED_TEXT):
                self.copy_button.setText(shown)
                width = max(width, self.copy_button.sizeHint().width())
            self.copy_button.setMinimumWidth(width)
            self.copy_button.setText(text)
            self.copy_button.setAccessibleName(f"{text} ({self.kind_label.full_text()}: {self._title})")
            self._tools.setVisible(True)
        self.copy_button.setVisible(bool(text))

    def copy_text(self) -> str:
        return self._copy_text

    def show_copied(self) -> None:
        """The Copy button reads "Copied" for 1.5 s (its width never changes)."""
        if not self._copy_text:
            return
        self.copy_button.setText(COPIED_TEXT)
        self._copied_timer.start()

    def _reset_copy(self) -> None:
        self.copy_button.setText(self._copy_text)

    def _build_layout(self) -> None:
        self._texts = QVBoxLayout()
        self._texts.setSpacing(2)
        for label in (self.kind_label, self.title_label, self.detail_label, self.check_label, self.recipients,
                      self.body_label, self.mail_body):
            self._texts.addWidget(label)
        self._texts.addWidget(self._tools)
        # Both slot pages reach the card's right and bottom edges (their margins
        # are the card padding) so the Approve glow is not clipped.
        self._buttons = QWidget()
        buttons = QHBoxLayout(self._buttons)
        buttons.setContentsMargins(0, self._SLOT_GAP, self._PAD_RIGHT, self._PAD_BOTTOM)
        buttons.setSpacing(6)
        buttons.addStretch(1)
        buttons.addWidget(self.deny_button)
        buttons.addWidget(self.approve_button)
        self._result = QWidget()
        result = QHBoxLayout(self._result)
        result.setContentsMargins(0, self._SLOT_GAP, self._PAD_RIGHT, self._PAD_BOTTOM)
        result.setSpacing(10)
        result.addWidget(self.undo_button, 0, Qt.AlignmentFlag.AlignVCenter)   # left: far from Approve
        result.addWidget(self.result_label, 1)
        result.addWidget(self.open_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.undo_button.hide()
        # One slot, one height: the result takes the buttons' place (up to two lines).
        self._slot = QWidget()
        self._stack = QStackedLayout(self._slot)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.addWidget(self._buttons)
        self._stack.addWidget(self._result)
        two_lines = math.ceil(2 * QFontMetricsF(self.result_label.font()).lineSpacing())
        self._slot.setFixedHeight(max(self._buttons.sizeHint().height(),
                                      self._SLOT_GAP + two_lines + self._PAD_BOTTOM))
        # Under the slot: the FAILED reason and the note (they only ever add height).
        self._extra = QWidget()
        extra = QVBoxLayout(self._extra)
        extra.setContentsMargins(0, 0, self._PAD_RIGHT, self._PAD_BOTTOM)
        extra.setSpacing(4)
        extra.addWidget(self.failure_label)
        extra.addWidget(self.note_label)
        self._extra.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(self._PAD_LEFT, self._PAD_TOP, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(self._texts)
        layout.addWidget(self._slot)
        layout.addWidget(self._extra)

    # ---- state -----------------------------------------------------------------

    @property
    def actionable(self) -> bool:
        return self._actionable

    def status(self) -> str:
        return self._status

    def link(self) -> str:
        return self._link

    def result_text(self) -> str:
        """The result as set (e.g. "ADDED", "FAILED: <REASON>"), never elided; "" while pending."""
        return self._result_text

    def title(self) -> str:
        """The whole title (the label may show it cut to ``title_lines``)."""
        return self._title

    def body(self) -> str:
        """The body preview as shown before any cut (line breaks folded into spaces)."""
        return self.body_label.full_text()

    def set_texts(self, kind: str, title: str, detail: str) -> None:
        self._title = title
        self.kind_label.set_full_text(kind.upper())
        if isinstance(self.title_label, _ClampedLabel):
            self.title_label.set_full_text(title)
        else:
            self.title_label.setText(title)
        self.detail_label.setText(detail)
        self.detail_label.setVisible(bool(detail))

    def set_status(self, status: str, message: str = "", link: str = "", link_text: str = "") -> None:
        """Show ``status`` (one of CARD_STATUSES); ``message`` is the FAILED reason or a custom result.

        ``link_text`` names the result's link ("Open event"; "Open" by default).
        Only the content of the button slot changes, never the card's height,
        except that a FAILED reason (under the buttons) may add a line or two
        once; the card keeps that room afterwards.
        """
        entering_countdown = status == CARD_COUNTDOWN and self._status != CARD_COUNTDOWN
        self._status = status if status in CARD_STATUSES else CARD_PENDING
        self._link = link
        self.open_button.setText(link_text or "Open")
        text, color = _CARD_RESULTS.get(self._status, ("", TEXT_DIM))
        if self._status in (CARD_FAILED, CARD_UNKNOWN) and message:
            text = f"{text}: {message}"
        elif message and self._status != CARD_PENDING:
            text = message
        text = text.upper()
        self._result_text = text
        failed = self._status in (CARD_FAILED, CARD_UNKNOWN)
        retry = self._status in _DECIDING_STATUSES
        show_buttons = self._actionable and retry
        # A Tab-focused Deny / Approve (or Undo) that goes away would hand the
        # keyboard focus to the next card's button or link (and scroll the list to
        # it), so a second Space would act on that card: keep the focus on this card.
        focus = QApplication.focusWidget()
        undo_had_focus = focus is not None and focus is self.undo_button
        focus_here = focus is not None and (focus is self or self.isAncestorOf(focus))
        if focus is not None and ((not show_buttons and self._buttons.isAncestorOf(focus))
                                  or (self._status != CARD_COUNTDOWN and focus is self.undo_button)):
            self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.result_label.set_full_text("" if failed else _keep_last_words(text))
        set_label_color(self.result_label, color)
        self.open_button.setVisible(self._status in _LINK_STATUSES and bool(link))
        self.undo_button.setVisible(self._status == CARD_COUNTDOWN)
        self.failure_label.set_color(AMBER if self._status == CARD_UNKNOWN else RED)
        self.failure_label.set_line(text if failed else "")
        self._stack.setCurrentWidget(self._buttons if show_buttons else self._result)
        self._sync_buttons()
        # The keyboard follows the decision: a Send / Approve pressed with Space hands the focus to
        # Undo while the countdown runs (only when it starts, so Tab can still move on), and an Undo
        # back to the right-hand button.
        if entering_countdown and focus_here:
            self.undo_button.setFocus(Qt.FocusReason.OtherFocusReason)
        elif undo_had_focus and show_buttons and self.approve_button.isEnabled():
            self.approve_button.setFocus(Qt.FocusReason.OtherFocusReason)
        slot = self._actionable or bool(text)
        self._slot.setVisible(slot)
        self._texts.setContentsMargins(0, 0, self._PAD_RIGHT, 0 if slot else self._PAD_BOTTOM)
        if not retry:
            self.set_note("")
        self._sync_extra()
        name = f"{self.kind_label.full_text()}: {self._title}"
        self.setAccessibleName(f"{name}, {text}" if text else name)

    def set_note(self, text: str) -> None:
        """An amber note under the buttons (e.g. "Google Calendar is not set up yet...")."""
        self.note_label.set_line(text)
        self._sync_extra()

    # ---- lock -------------------------------------------------------------------

    def set_locked(self, locked: bool, tooltip: str = "") -> None:
        """Dim and disable Deny / Approve (``tooltip`` explains why); nothing moves or resizes."""
        locked = bool(locked)
        if locked == self._locked and (tooltip if locked else "") == self._lock_tip:
            return
        self._locked = locked
        self._lock_tip = tooltip if locked else ""
        self._sync_buttons()

    def is_locked(self) -> bool:
        return self._locked

    def _buttons_shown(self) -> bool:
        return self._actionable and self._status in _DECIDING_STATUSES

    def _sync_buttons(self) -> None:
        enabled = self._buttons_shown() and not self._locked
        focus = QApplication.focusWidget()
        if not enabled and focus is not None and self._buttons.isAncestorOf(focus):
            # Qt would hand the focus to the next widget in the chain (another card's
            # button); keep it on this card instead, so a second Space decides nothing.
            self.setFocus(Qt.FocusReason.OtherFocusReason)
        for button in (self.deny_button, self.approve_button):
            button.setEnabled(enabled)
            button.setToolTip(self._lock_tip)
            button.setAccessibleDescription(self._lock_tip)

    def _on_locked_click(self) -> None:
        if self._locked and self._buttons_shown():
            self.lockedClicked.emit(self.action_id)

    def note(self) -> str:
        return self.note_label.text()

    def _sync_extra(self) -> None:
        self._extra.setVisible(self.failure_label.used() or self.note_label.used())

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            rect = QRectF(self.rect())
            strength = 1.0 if self._actionable else 0.5
            gradient = QLinearGradient(rect.topLeft(), rect.topRight())
            gradient.setColorAt(0.0, rgba(AMBER, 0.08 * strength))
            gradient.setColorAt(1.0, rgba(AMBER, 0.025 * strength))
            painter.fillRect(rect, gradient)
            # A hairline round the whole card, button row included, so the
            # buttons read as this card's and not the next one's.
            painter.setPen(QPen(rgba(AMBER, 0.22 * strength), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
            painter.fillRect(QRectF(0, 0, 2, rect.height()), rgba(AMBER, 1.0 if self._actionable else 0.35))
        finally:
            painter.end()


class GroupHeader(QWidget):
    """A group's title row in an ActionList: "ASK" and its count in mono caps, then a thin rule to
    the right edge (``title()``, ``count()``)."""

    _GAP = 8
    _TOP = 6   # more room above than below, so the title sits with the cards it heads

    def __init__(self, title: str, count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title.upper()
        self._count = int(count)
        self._font = mono_font(10, 600, 0.16)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(f"{title}: {count}")

    def title(self) -> str:
        return self._title

    def count(self) -> int:
        return self._count

    def _text(self) -> str:
        return f"{self._title} {MIDDLE_DOT} {self._count}"

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(math.ceil(_text_advance(self._font, self._text())) + 40,
                     math.ceil(_line_height(self._font)) + 4 + self._TOP)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(math.ceil(_text_advance(self._font, self._text())), self.sizeHint().height())

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            rect = QRectF(self.rect()).adjusted(0, self._TOP, 0, 0)
            painter.setFont(self._font)
            painter.setPen(QColor(AMBER))
            title_width = _text_advance(self._font, self._title)
            painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._title)
            rest = f" {MIDDLE_DOT} {self._count}"
            painter.setPen(QColor(AMBER_META))
            painter.drawText(rect.adjusted(title_width, 0, 0, 0), Qt.AlignmentFlag.AlignLeft
                             | Qt.AlignmentFlag.AlignVCenter, rest)
            start = title_width + _text_advance(self._font, rest) + self._GAP
            if start < rect.width():
                y = math.floor(rect.center().y()) + 0.5
                painter.setPen(QPen(rgba(AMBER, 0.22), 1))
                painter.drawLine(QPointF(start, y), QPointF(rect.width(), y))
        finally:
            painter.end()


class ActionList(QScrollArea):
    """Scrollable column of ActionCards with an empty-state line.

    Its size hint follows the cards' height up to ``max_height``, so a panel
    holding it shrinks to its content and scrolls beyond that. ``CARD_GAP``
    px of empty space separate the cards, so a card's buttons never sit
    right on top of the next card's title. ``add_group_header(title, count)``
    puts a GroupHeader ("ASK \u00b7 2") before the cards added after it.
    """

    CARD_GAP = 14

    def __init__(self, empty_text: str = "Nothing needs your OK", *, max_height: int = 460,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        content = _transparent_scroll(self)
        self._max_height = max_height
        self._layout = QVBoxLayout(content)
        self._layout.setContentsMargins(0, 0, 4, 0)   # keeps the cards' outline off the scroll bar
        self._layout.setSpacing(self.CARD_GAP)
        self.empty_label = make_label(empty_text, mono_font(11, 400, 0.08), TEXT_DIM)
        self._layout.addWidget(self.empty_label)
        self._layout.addStretch(1)
        self._cards: list[ActionCard] = []
        self._items: list[QWidget] = []   # the cards and group headers, in order
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    def set_empty_text(self, text: str) -> None:
        self.empty_label.setText(text)

    def add_card(self, card: ActionCard) -> ActionCard:
        self._layout.insertWidget(len(self._items), card)
        self._items.append(card)
        self._cards.append(card)
        self.empty_label.setVisible(False)
        self.updateGeometry()
        return card

    def add_group_header(self, title: str, count: int) -> GroupHeader:
        """A group title ("ASK \u00b7 2") before the cards added from now on."""
        header = GroupHeader(title, count)
        self._layout.insertWidget(len(self._items), header)
        self._items.append(header)
        self.updateGeometry()
        return header

    def group_headers(self) -> list[tuple[str, int]]:
        """(title, count) of each group header, in order."""
        return [(item.title(), item.count()) for item in self._items if isinstance(item, GroupHeader)]

    def cards(self) -> list[ActionCard]:
        return list(self._cards)

    def card(self, action_id: str) -> ActionCard | None:
        return next((card for card in self._cards if card.action_id == action_id), None)

    def clear(self) -> None:
        for item in self._items:
            item.hide()
            item.deleteLater()
        self._items = []
        self._cards = []
        self.empty_label.setVisible(True)
        self.updateGeometry()

    def _content_height(self, width: int) -> int:
        layout = self.widget().layout()
        if layout.hasHeightForWidth():
            return layout.totalHeightForWidth(width)
        return self.widget().sizeHint().height()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        # The width without a scroll bar: if everything fits there, none is needed.
        width = max(self.width() - 2 * self.frameWidth(), 200)
        return QSize(width, min(self._max_height, self._content_height(width)))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(120, min(self.sizeHint().height(), 120))

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        old_width = event.oldSize().width()
        super().resizeEvent(event)
        if old_width != event.size().width():
            self.updateGeometry()


EDIT_RSVP = "rsvp"
EDIT_MOVE = "move"
EDIT_CANCEL = "cancel"
EDIT_REPLY = "reply"
EDIT_EMAIL = "email"
EDIT_EVENT_KINDS = (EDIT_RSVP, EDIT_MOVE, EDIT_CANCEL)
EDIT_MAIL_KINDS = (EDIT_REPLY, EDIT_EMAIL)
EDIT_KINDS = EDIT_EVENT_KINDS + EDIT_MAIL_KINDS
_EDIT_ANSWERS = (("yes", "Yes"), ("no", "No"), ("maybe", "Maybe"))
_EDIT_STYLE = """
QLineEdit, QPlainTextEdit {
    color: @TEXT; background: #030a10; border: 1px solid @BORDER; padding: 4px 6px;
    selection-background-color: @SELECTION; selection-color: @BRIGHT;
}
QLineEdit:focus, QPlainTextEdit:focus { border: 1px solid @AMBER; }
QLineEdit:read-only { color: @SOFT; }
QCheckBox { color: @TEXT; spacing: 8px; }
QCheckBox::indicator { width: 13px; height: 13px; border: 1px solid @BORDER; background: #030a10; }
QCheckBox::indicator:checked { background: @AMBER; border: 1px solid @AMBER; }
QCheckBox:focus { color: @BRIGHT; }
QCheckBox::indicator:focus { width: 11px; height: 11px; border: 2px solid @BRIGHT; }
QPlainTextEdit[readOnly="true"] { color: @SOFT; }
"""
REMOVE_TEXT = "Remove"
ADD_TEXT = "Add"
# The Edit dialog of a Reply / Email: what it says about the recipients and the message.
OWN_ADDRESS_ERROR = "That is the sending account's own address; it is never a recipient"
DUPLICATE_ERROR = "{address} is already a recipient"
NOT_AN_ADDRESS_ERROR = "\"{text}\" is not an email address"
TOO_MANY_ERROR = "At most {count} recipients in To and Cc together"
REPLY_SUBJECT_NOTE = "A reply keeps the subject of its thread"
MESSAGE_CAPTION = "Message (sent exactly as written)"
NOT_ADDED_ERROR = "\"{text}\" in {name} was not added - click Add (or press Enter in that field), or clear it"
OWN_ROW_ERROR = "{address} is the sending account itself - Remove it: Jarvis never sends to it"


class _LinkHighlighter(QSyntaxHighlighter):
    """Web links in the message editor, in the accent colour and underlined (``find_links(text)``
    gives their (start, end) offsets in one line)."""

    def __init__(self, document: Any, find_links: Callable[[str], Sequence[tuple[int, int]]]) -> None:
        super().__init__(document)
        self._find = find_links
        self._format = QTextCharFormat()
        self._format.setForeground(QColor(ACCENT))
        self._format.setFontUnderline(True)

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt override
        for start, end in self._find(text):
            if 0 <= start < end <= len(text):
                self.setFormat(start, end - start, self._format)


class _GrowingTextEdit(QPlainTextEdit):
    """A plain-text field as tall as its whole text at its width, so none of it is ever hidden in
    the field (it never scrolls itself; the Edit dialog's fields scroll instead). ``min_height``
    keeps room to type. ``single_line`` (a subject): Enter is not a line break (it reaches the
    dialog, whose default button is Save) and pasted line breaks become spaces. ``text()`` /
    ``setText()`` / ``setCursorPosition()`` work as on a QLineEdit."""

    heightChanged = Signal()

    def __init__(self, *, min_height: int = 0, single_line: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._min = min_height
        self._single = single_line
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setTabChangesFocus(True)
        self.textChanged.connect(self.fit_to_text)

    def text(self) -> str:
        return self.toPlainText()

    def setText(self, text: str) -> None:  # noqa: N802 - mirrors QLineEdit
        self.setPlainText(text)

    def setCursorPosition(self, position: int) -> None:  # noqa: N802 - mirrors QLineEdit
        cursor = self.textCursor()
        cursor.setPosition(max(0, min(position, len(self.toPlainText()))))
        self.setTextCursor(cursor)

    def lines(self) -> int:
        """The lines the text takes at the field's width (QPlainTextDocumentLayout counts lines)."""
        return max(1, round(self.document().size().height()))

    def wanted_height(self) -> int:
        """Room for every line, measured as QPlainTextEdit measures what fits (block by block,
        the document margin above and below), plus the frame and padding."""
        document = self.document()
        layout = document.documentLayout()
        text = 0
        block = document.begin()
        while block.isValid():
            if block.isVisible():
                text += int(layout.blockBoundingRect(block).height())
            block = block.next()
        text = max(text, math.ceil(QFontMetricsF(self.font()).lineSpacing()))
        chrome = self.height() - self.viewport().height() if self.viewport().height() > 0 else 2 * self.frameWidth()
        return max(self._min, text + math.ceil(2 * document.documentMargin()) + 2 + chrome)

    def fit_to_text(self) -> None:
        height = self.wanted_height()
        changed = height != self.minimumHeight() or height != self.maximumHeight()
        if changed:
            self.setFixedHeight(height)
        # Shown: QPlainTextEdit says itself whether a line is still out of view; then one line more.
        step = math.ceil(QFontMetricsF(self.font()).lineSpacing())
        for _attempt in range(3):
            if not self.isVisible() or self.verticalScrollBar().maximum() <= 0:
                break
            self.setFixedHeight(self.height() + step)
            changed = True
        if changed:
            self.heightChanged.emit()

    def shows_all(self) -> bool:
        """Every line is inside the field (nothing scrolled out of it)."""
        return self.verticalScrollBar().maximum() <= 0

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if self._single and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.ignore()   # the dialog's default button (Save) gets it, as from a line edit
            return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source: Any) -> None:  # noqa: N802 - Qt override
        if self._single and source is not None and source.hasText():
            self.insertPlainText(" ".join(source.text().split()))
            return
        super().insertFromMimeData(source)


class _RecipientBadge(QWidget):
    """A recipient's badge in the Edit dialog, drawn as on the card: NEW RECIPIENT filled red while
    its "Send to" tick is not set, outlined "NEW \u00b7 CONFIRMED" once it is (as wide as the wider
    text from the start, so a tick never moves Remove); SENDING ACCOUNT on the account's own
    address (it has no tick: only Remove)."""

    _PAD = 4

    def __init__(self, state: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._font = mono_font(9, 600, 0.08)
        self._own = state == RECIPIENT_OWN
        self._state = RECIPIENT_OWN if self._own else RECIPIENT_NEW
        texts = (OWN_RECIPIENT_TEXT,) if self._own else (NEW_RECIPIENT_TEXT, CONFIRMED_RECIPIENT_TEXT)
        width = max(_text_advance(self._font, text) for text in texts)
        self.setFixedSize(math.ceil(width) + 2 * self._PAD, math.ceil(QFontMetricsF(self._font).lineSpacing()) + 4)
        self.set_state(state)

    def set_state(self, state: str) -> None:
        if self._own:
            self._state = RECIPIENT_OWN
        else:
            self._state = RECIPIENT_CONFIRMED if state == RECIPIENT_CONFIRMED else RECIPIENT_NEW
        self.setAccessibleName(self.text())
        self.update()

    def state(self) -> str:
        return self._state

    def text(self) -> str:
        return _BADGE_TEXTS[self._state]

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            _paint_recipient_badge(painter, QRectF(0, 1, self.width(), self.height() - 2), self.text(),
                                   self._state, self._font)
        finally:
            painter.end()


class _AddressEdit(QLineEdit):
    """The "Add an address" field: Enter adds the address (it never saves the dialog)."""

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.returnPressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class _TickLabel(_BreakableLabel):
    """The address after a "Send to" tick (it wraps, a checkbox's own text can't): a click on it
    toggles the tick too."""

    def __init__(self, text: str, font: QFont, color: str | QColor, box: QCheckBox, *,
                 min_word: int = _LONG_WORD) -> None:
        super().__init__(text, font, color, min_word=min_word)
        self._box = box
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton and self._box.isEnabled():
            self._box.toggle()
            event.accept()
            return
        super().mousePressEvent(event)


class _RecipientRow(QWidget):
    """One recipient in the Edit dialog: the address (red with a NEW RECIPIENT badge when Jarvis
    has not sent to it before, or SENDING ACCOUNT when it is the account itself), Remove, and for
    a new one a "Send to <address>" tick."""

    removeClicked = Signal(str)
    toggled = Signal()

    def __init__(self, address: str, state: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.address = address
        self.state = state
        new = state in (RECIPIENT_NEW, RECIPIENT_CONFIRMED)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        red = state in _RED_RECIPIENTS
        self.address_label = _BreakableLabel(address, mono_font(12), RED if red else TEXT_BODY, min_word=0)
        top.addWidget(self.address_label, 1)
        self.badge: _RecipientBadge | None = None
        if red:
            self.badge = _RecipientBadge(state)
            top.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        self.remove_button = _ToolLink(REMOVE_TEXT)
        self.remove_button.set_link_color(ACCENT)
        self.remove_button.setAccessibleName(f"{REMOVE_TEXT} {address}")
        self.remove_button.clicked.connect(self._on_remove)
        top.addWidget(self.remove_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top)
        self.confirm_box: QCheckBox | None = None
        self.confirm_label: QLabel | None = None
        if new:
            tick = QHBoxLayout()
            tick.setContentsMargins(0, 0, 0, 0)
            tick.setSpacing(6)
            self.confirm_box = QCheckBox("Send to")
            self.confirm_box.setFont(body_font(13))
            self.confirm_box.setChecked(state == RECIPIENT_CONFIRMED)
            self.confirm_box.setAccessibleName(f"Send to {address} (new recipient)")
            self.confirm_box.toggled.connect(self._on_toggled)
            self.confirm_label = _TickLabel(address, body_font(13), TEXT_BODY, self.confirm_box, min_word=0)
            tick.addWidget(self.confirm_box, 0, Qt.AlignmentFlag.AlignTop)
            tick.addWidget(self.confirm_label, 1)
            layout.addLayout(tick)

    def confirmed(self) -> bool:
        return self.confirm_box is not None and self.confirm_box.isChecked()

    def _on_remove(self) -> None:
        self.removeClicked.emit(self.address)

    def focus_chain(self) -> list[QWidget]:
        """The row's keyboard stops in reading order: Remove (top line), then the tick."""
        return [self.remove_button] + ([self.confirm_box] if self.confirm_box is not None else [])

    def _on_toggled(self, checked: bool) -> None:
        if self.badge is not None:
            self.badge.set_state(RECIPIENT_CONFIRMED if checked else RECIPIENT_NEW)
        self.toggled.emit()


class _RecipientList(QWidget):
    """The To or Cc recipients of the Edit dialog: one _RecipientRow each, then an "Add an
    address" field. ``check(text)`` (the dialog's) turns a typed address into (address, state)
    or raises ValueError with what is wrong."""

    changed = Signal()
    resized = Signal()   # a row or the error line came or went: the dialog fits itself again

    def __init__(self, name: str, chips: Sequence[RecipientChip],
                 check: Callable[[str], tuple[str, str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = name
        # The dialog's own method is held weakly: a reference cycle through the dialog would let the
        # garbage collector free the dialog together with its widgets in one pass, which corrupts
        # the heap on Windows.
        self._check: Callable[[], Callable[[str], tuple[str, str]] | None] = (
            weakref.WeakMethod(check) if hasattr(check, "__self__") else (lambda: check))
        self.rows: list[_RecipientRow] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(4)
        layout.addLayout(self._rows_layout)
        add = QHBoxLayout()
        add.setContentsMargins(0, 0, 0, 0)
        add.setSpacing(6)
        self.add_edit = _AddressEdit()
        self.add_edit.setFont(mono_font(12))
        self.add_edit.setPlaceholderText(f"Add to {name}: name@example.com")
        self.add_edit.setAccessibleName(f"Add an address to {name}")
        self.add_edit.returnPressed.connect(self.add_typed)
        self.add_button = HudButton(ADD_TEXT, SECONDARY, compact=True)
        self.add_button.setAccessibleName(f"{ADD_TEXT} to {name}")
        self.add_button.clicked.connect(self.add_typed)
        add.addWidget(self.add_edit, 1)
        add.addWidget(self.add_button)
        layout.addLayout(add)
        self.error_label = make_label("", mono_font(11), RED, wrap=True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        for chip in chips:
            self._add_row(chip.address, chip.state)

    def addresses(self) -> list[str]:
        return [row.address for row in self.rows]

    def typed(self) -> str:
        """What the Add field holds but was not added yet ("" when empty)."""
        return self.add_edit.text().strip()

    def focus_chain(self) -> list[QWidget]:
        """The list's keyboard stops in reading order: each row, then the Add field and Add."""
        chain = [widget for row in self.rows for widget in row.focus_chain()]
        return chain + [self.add_edit, self.add_button]

    def confirmed(self) -> list[str]:
        return [row.address for row in self.rows if row.confirmed()]

    def own(self) -> list[str]:
        """The rows that are the sending account itself (Save refuses while one is left)."""
        return [row.address for row in self.rows if row.state == RECIPIENT_OWN]

    def row(self, address: str) -> _RecipientRow | None:
        key = address.casefold()
        return next((row for row in self.rows if row.address.casefold() == key), None)

    def error(self) -> str:
        return self.error_label.text() if not self.error_label.isHidden() else ""

    def add_typed(self) -> None:
        """Add what the field holds (checked by the dialog); a problem shows under the field."""
        text = self.add_edit.text().strip()
        if not text:
            return
        check = self._check()
        if check is None:
            return
        try:
            address, state = check(text)
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self._show_error("")
        self.add_edit.clear()
        self._add_row(address, state)
        self.changed.emit()
        self.resized.emit()

    def _add_row(self, address: str, state: str) -> None:
        row = _RecipientRow(address, state)
        row.removeClicked.connect(self._remove)
        row.toggled.connect(self.changed)
        self.rows.append(row)
        self._rows_layout.addWidget(row)

    def _remove(self, address: str) -> None:
        row = self.row(address)
        if row is None:
            return
        self.rows.remove(row)
        self._rows_layout.removeWidget(row)
        row.hide()
        row.deleteLater()
        self._show_error("")
        self.add_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.changed.emit()
        self.resized.emit()

    def _show_error(self, text: str) -> None:
        if text == self.error() and bool(text) == self.error_label.isVisible():
            return
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))
        self.resized.emit()


class EditDialog(QDialog):
    """The Edit dialog of an RSVP / Move / Cancel or a Reply / Email card: window-modal, opened
    with ``open()``.

    It shows exactly what Jarvis will carry out and lets you change it: an
    invitation's answer, a move's new date and times, whether guests are told,
    and the note (an RSVP's note goes to the organizer with the answer; a move's
    or cancel's note is not sent by Google Calendar, which the dialog says).
    A Reply / Email (EDIT_MAIL_KINDS) shows FROM (read only), the To and Cc
    recipients (Remove; Add checks the address with ``normalize`` and
    ``classify``; a new recipient is red with a NEW RECIPIENT badge and a "Send
    to <address>" tick), the subject (an Email's can be changed; a reply keeps
    its thread's) and the message exactly as it will be sent, its links
    highlighted (``find_links``). ``banner`` is an amber line at the top (why
    the dialog opened: new recipients to confirm, a long message to read).
    The subject and the message are shown whole (their fields grow with the
    text). The dialog always fits the screen's available area
    (QScreen.availableGeometry, at any display scale): when its content is
    taller, the fields between the banner and Save / Cancel scroll (an
    invitation's too), so Save is always on screen. ``whole_seen()``
    says whether all of the fields have been on screen (the message's end
    included) while the dialog was open. An address typed into Add but not
    added keeps the dialog open on Save (it would not be sent), and so does
    the sending account's own address in To or Cc (red, SENDING ACCOUNT:
    Remove it).
    Save emits ``saved(action_id, values)`` with the fields as typed (``values()``:
    answer, notify ("all" / "external" / "none"), date, start, end, note; or for
    a Reply / Email: to, cc, subject, body, confirmed (the ticked new
    recipients)); the caller checks them and either closes the dialog
    (``accept()``) or keeps it open with ``show_error(text)``. Save never sends
    anything. Every text is plain text; nothing is read as HTML.
    """

    saved = Signal(str, object)
    WIDTH = 420
    MAIL_WIDTH = 480
    _MESSAGE_HEIGHT = 150   # the message editor: at least this tall, and as tall as the whole message
    _FIELDS_MIN = 72        # the scrolling fields on a very short screen (about three lines)
    _MIN_WIDTH = 320        # the narrowest the dialog gets on a narrow screen
    _SCREEN_MARGIN = 16

    def __init__(self, action_id: str, kind: str, kind_label: str, title: str, *, answer: str = "yes",
                 notify: str = "all", date_text: str = "", start_text: str = "", end_text: str = "",
                 note: str = "", check_text: str = "", hour24: bool = False, sender: str = "",
                 to: Sequence[RecipientChip] = (), cc: Sequence[RecipientChip] = (), body: str = "",
                 classify: Callable[[str], str] | None = None,
                 normalize: Callable[[str], str] | None = None,
                 find_links: Callable[[str], Sequence[tuple[int, int]]] | None = None,
                 banner: str = "", max_recipients: int = 5, parent: QWidget | None = None) -> None:
        flags = Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint
        super().__init__(parent, flags)
        if kind not in EDIT_KINDS:
            raise ValueError(f"no Edit dialog for {kind!r}")
        self.action_id = action_id
        self.kind = kind
        self.mail = kind in EDIT_MAIL_KINDS
        self._notify = notify
        self._answer = answer if answer in dict(_EDIT_ANSWERS) else "yes"
        self._classify = classify or (lambda _address: RECIPIENT_KNOWN)
        self._normalize = normalize or (lambda text: text.strip())
        self._find_links = find_links or (lambda _text: ())
        self._max_recipients = max_recipients
        self._fitted = False
        self._seen_all = not self.mail
        self._follow_cursor = False
        self.fields_scroll: QScrollArea | None = None
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle(f"Edit {kind_label}")
        self.setAccessibleName(f"Edit {kind_label}: {title}")
        tokens = {"TEXT": TEXT_BODY, "BRIGHT": TEXT_BRIGHT, "AMBER": AMBER, "SOFT": TEXT_SOFT,
                  "BORDER": _qss_color(rgba(AMBER, 0.35)), "SELECTION": _qss_color(rgba(ACCENT, 0.32))}
        style = _EDIT_STYLE
        for name in sorted(tokens, key=len, reverse=True):
            style = style.replace("@" + name, tokens[name])
        self.setStyleSheet(style)
        self.panel = ChamferPanel("Edit", kind_label.upper(), variant=PANEL_AMBER, border_alpha=0.5,
                                  padding=(18, 14, 18, 16), spacing=6 if self.mail else 8)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        # The dialog's size is set by _fit alone, once per change, to what fits the screen: the
        # layout never raises the window's minimum size by itself (a window grown by its layout in
        # the middle of a resize made Windows refuse the geometry, and it could outgrow the screen).
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        outer.addWidget(self.panel)
        layout = self.panel.body_layout
        self.banner_label = make_label(banner, mono_font(11), AMBER, wrap=True)
        self.banner_label.setVisible(bool(banner))
        layout.addWidget(self.banner_label)
        self.title_label = make_label(title, body_font(14, 600), AMBER_TEXT, wrap=True)
        self.title_label.setVisible(not self.mail)   # a Reply / Email shows its subject below
        layout.addWidget(self.title_label)
        self.check_label = make_label(check_text, mono_font(11), TEXT_SOFT, wrap=True)
        self.check_label.setVisible(bool(check_text))
        layout.addWidget(self.check_label)
        self.answer_buttons: dict[str, HudButton] = {}
        self.date_edit = QLineEdit(date_text, self)
        self.start_edit = QLineEdit(start_text, self)
        self.end_edit = QLineEdit(end_text, self)
        self.notify_box = QCheckBox(self)
        self.note_edit = QPlainTextEdit(self)
        self.from_label: QLabel | None = None
        self.to_list: _RecipientList | None = None
        self.cc_list: _RecipientList | None = None
        self.subject_edit = _GrowingTextEdit(single_line=True, parent=self)
        self.body_edit = _GrowingTextEdit(min_height=self._MESSAGE_HEIGHT, parent=self)
        self.links_label = make_label("", mono_font(10), TEXT_DIM, wrap=True)
        for widget in (self.date_edit, self.start_edit, self.end_edit, self.notify_box, self.note_edit,
                       self.subject_edit, self.body_edit, self.links_label):
            widget.hide()   # each kind shows its own fields below
        # The fields (a Reply / Email's FROM to the message, an invitation's answer to the note)
        # scroll between the banner and Save / Cancel when the dialog would be taller than the
        # screen (_fit), so Save is always on screen at any display scale.
        self.fields_scroll = QScrollArea(self)
        content = _transparent_scroll(self.fields_scroll)
        fields = QVBoxLayout(content)
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(6 if self.mail else 8)
        self.fields_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.fields_scroll.setFixedHeight(0)   # _fit sets the height
        layout.addWidget(self.fields_scroll)
        if self.mail:
            self._build_mail(fields, sender=sender, to=to, cc=cc, subject=title, message=body)
            self.fields_scroll.verticalScrollBar().valueChanged.connect(self._note_seen)
        else:
            self._build_event(fields, notify=notify, hour24=hour24, note=note)
        self.error_label = make_label("", mono_font(11), RED, wrap=True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 6, 0, 0)
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self.cancel_button = HudButton("Cancel", SECONDARY, compact=True)
        self.save_button = HudButton("Save", PRIMARY, compact=True)
        self.save_button.setAccessibleDescription("Keeps the changes on the card; nothing is sent")
        self.save_button.setDefault(True)   # Enter in a field saves (never sends anything)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button.clicked.connect(self._on_save)
        self.setFixedWidth(self.MAIL_WIDTH if self.mail else self.WIDTH)
        if self.mail:
            self._sync_tab_order()

    def _build_event(self, body: QVBoxLayout, *, notify: str, hour24: bool, note: str) -> None:
        """An RSVP's answer, a Move's new time, Notify guests and the note."""
        kind = self.kind
        if kind == EDIT_RSVP:
            body.addWidget(self._caption("Answer"))
            row = QHBoxLayout()
            row.setSpacing(6)
            for value, text in _EDIT_ANSWERS:
                button = HudButton(text, SECONDARY, compact=True)
                button.setCheckable(True)
                button.setAccessibleName(f"Answer {text}")
                button.clicked.connect(self._on_answer)
                self.answer_buttons[value] = button
                row.addWidget(button)
            row.addStretch(1)
            body.addLayout(row)
            self._select_answer(self._answer)
        if kind == EDIT_MOVE:
            body.addWidget(self._caption("New time"))
            row = QHBoxLayout()
            row.setSpacing(6)
            time_hint = "14:00" if hour24 else "2:00 PM"
            for edit, hint, width, name in ((self.date_edit, "YYYY-MM-DD", 112, "Date"),
                                            (self.start_edit, time_hint, 92, "Start"),
                                            (self.end_edit, time_hint, 92, "End")):
                edit.setFont(mono_font(12))
                edit.setPlaceholderText(hint)
                edit.setFixedWidth(width)
                edit.setAccessibleName(name)
                edit.show()
                row.addWidget(edit)
                if edit is self.start_edit:
                    row.addWidget(make_label("-", mono_font(12), TEXT_SOFT))
            row.addStretch(1)
            body.addLayout(row)
            body.addWidget(make_label("Times like 2:00 PM or 14:00, in your calendar's time zone",
                                      mono_font(10), TEXT_DIM, wrap=True))
        outside = notify == "external"
        self.notify_box.setText("Notify guests outside your organization" if outside else "Notify guests")
        self.notify_box.setFont(body_font(13))
        self.notify_box.setChecked(notify != "none")
        if kind == EDIT_RSVP:   # sendUpdates: an email about the answer; the event shows it either way
            self.notify_box.setText("Email the organizer only if outside your organization" if outside
                                    else "Email the organizer about my answer")
        self.notify_box.show()
        body.addWidget(self.notify_box)
        if kind == EDIT_RSVP:
            body.addWidget(make_label("Without the email, the organizer still sees your answer and note "
                                      "on the event", mono_font(10), TEXT_DIM, wrap=True))
        if kind == EDIT_RSVP:
            caption = "Note to the organizer (sent with your answer)"
        else:
            caption = "Note (not sent: Google Calendar has no message - Copy it)"
        body.addWidget(self._caption(caption))
        self.note_edit.setPlainText(note)
        self.note_edit.setFont(body_font(13))
        self.note_edit.setTabChangesFocus(True)
        self.note_edit.setFixedHeight(84)
        self.note_edit.setAccessibleName(caption)
        self.note_edit.show()
        body.addWidget(self.note_edit)

    def _build_mail(self, body: QVBoxLayout, *, sender: str, to: Sequence[RecipientChip],
                    cc: Sequence[RecipientChip], subject: str, message: str) -> None:
        """FROM, To / Cc, the subject and the message of a Reply / Email."""
        body.addWidget(self._caption("From"))
        self.from_label = _BreakableLabel(sender, mono_font(12), TEXT_SOFT, min_word=0)
        body.addWidget(self.from_label)
        body.addWidget(self._caption("To"))
        self.to_list = _RecipientList("To", to, self._check_address)
        body.addWidget(self.to_list)
        body.addWidget(self._caption("Cc"))
        self.cc_list = _RecipientList("Cc", cc, self._check_address)
        body.addWidget(self.cc_list)
        for recipients in (self.to_list, self.cc_list):
            recipients.resized.connect(self._relayout)
            recipients.resized.connect(self._sync_tab_order)
            recipients.changed.connect(self._on_recipients_changed)
        body.addWidget(self._caption("Subject"))
        self.subject_edit.setText(subject)
        self.subject_edit.setFont(body_font(13))
        self.subject_edit.setAccessibleName("Subject")
        self.subject_edit.setCursorPosition(0)
        self.subject_edit.heightChanged.connect(self._on_field_grew)
        if self.kind == EDIT_REPLY:
            self.subject_edit.setReadOnly(True)
            self.subject_edit.setToolTip(plain_tooltip(REPLY_SUBJECT_NOTE))
            self.subject_edit.setAccessibleDescription(REPLY_SUBJECT_NOTE)
        self.subject_edit.show()
        body.addWidget(self.subject_edit)
        if self.kind == EDIT_REPLY:
            body.addWidget(make_label(REPLY_SUBJECT_NOTE, mono_font(10), TEXT_DIM, wrap=True))
        body.addWidget(self._caption(MESSAGE_CAPTION))
        self.body_edit.setPlainText(message)
        self.body_edit.setFont(body_font(13))
        self.body_edit.setAccessibleName(MESSAGE_CAPTION)
        self.body_edit.heightChanged.connect(self._on_field_grew)
        self.body_edit.show()
        self._highlighter = _LinkHighlighter(self.body_edit.document(), self._find_links)
        body.addWidget(self.body_edit)
        body.addWidget(self.links_label)
        self.body_edit.textChanged.connect(self._update_links)
        self._update_links()

    def _check_address(self, text: str) -> tuple[str, str]:
        """(address, RECIPIENT_*) for an address typed into To or Cc, or ValueError with why not."""
        address = self._normalize(text)
        if not address:
            raise ValueError(NOT_AN_ADDRESS_ERROR.format(text=_short_text(text, 40)))
        for recipients in (self.to_list, self.cc_list):
            if recipients is not None and recipients.row(address) is not None:
                raise ValueError(DUPLICATE_ERROR.format(address=address))
        count = sum(len(recipients.rows) for recipients in (self.to_list, self.cc_list) if recipients is not None)
        if count >= self._max_recipients:
            raise ValueError(TOO_MANY_ERROR.format(count=self._max_recipients))
        state = self._classify(address)
        if state == RECIPIENT_OWN:
            raise ValueError(OWN_ADDRESS_ERROR)
        return address, RECIPIENT_NEW if state in (RECIPIENT_NEW, RECIPIENT_CONFIRMED) else RECIPIENT_KNOWN

    def _update_links(self) -> None:
        count = len(self._find_links(self.body_edit.toPlainText()))
        text = f"Contains {count} link{'' if count == 1 else 's'} (highlighted)" if count else ""
        self.links_label.setText(text)
        self.links_label.setVisible(bool(text))

    @staticmethod
    def _caption(text: str) -> QLabel:
        return make_label(text.upper(), mono_font(10, 400, 0.14), AMBER_META, wrap=True)

    def _select_answer(self, value: str) -> None:
        self._answer = value
        for name, button in self.answer_buttons.items():
            button.setChecked(name == value)
            button.set_selected(name == value)

    def _on_answer(self) -> None:
        sender = self.sender()
        for value, button in self.answer_buttons.items():
            if button is sender:
                self._select_answer(value)

    def values(self) -> dict[str, Any]:
        """The fields as typed: answer, notify, date, start, end, note; or for a Reply / Email:
        to, cc (lists of addresses), subject, body and confirmed (the ticked new recipients)."""
        if self.mail:
            lists = [recipients for recipients in (self.to_list, self.cc_list) if recipients is not None]
            return {"to": self.to_list.addresses() if self.to_list is not None else [],
                    "cc": self.cc_list.addresses() if self.cc_list is not None else [],
                    "subject": self.subject_edit.text(), "body": self.body_edit.toPlainText(),
                    "confirmed": [address for recipients in lists for address in recipients.confirmed()]}
        if self.notify_box.isChecked():
            notify = "external" if self._notify == "external" else "all"
        else:
            notify = "none"
        return {"answer": self._answer, "notify": notify, "date": self.date_edit.text().strip(),
                "start": self.start_edit.text().strip(), "end": self.end_edit.text().strip(),
                "note": self.note_edit.toPlainText()}

    def show_error(self, text: str) -> None:
        """Keep the dialog open and say what is wrong (plain text)."""
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))
        self._relayout()

    def error(self) -> str:
        """The error shown under the fields ("" when none)."""
        return self.error_label.text() if not self.error_label.isHidden() else ""

    def _on_save(self) -> None:
        if self.mail:
            for recipients in (self.to_list, self.cc_list):
                typed = recipients.typed() if recipients is not None else ""
                if typed:   # not a recipient: it would not be sent, so say so instead of dropping it
                    self.show_error(NOT_ADDED_ERROR.format(text=_short_text(typed, 40), name=recipients.name))
                    recipients.add_edit.setFocus(Qt.FocusReason.OtherFocusReason)
                    return
            for recipients in (self.to_list, self.cc_list):
                own = recipients.own() if recipients is not None else []
                if own:   # the sending account itself: Send would refuse it, so Save does first
                    self.show_error(OWN_ROW_ERROR.format(address=own[0]))
                    row = recipients.row(own[0])
                    if row is not None:
                        row.remove_button.setFocus(Qt.FocusReason.OtherFocusReason)
                    return
        self.saved.emit(self.action_id, self.values())

    def _on_recipients_changed(self) -> None:
        """A recipient was added, removed or ticked: the "remove the sending account" error goes once
        no such row is left."""
        error = self.error()
        if error and not any(recipients.own() for recipients in (self.to_list, self.cc_list)
                             if recipients is not None) and error.endswith(OWN_ROW_ERROR.split("}", 1)[1]):
            self.show_error("")

    def whole_seen(self) -> bool:
        """Every field of a Reply / Email (the subject and the message to their ends) has been on
        screen while the dialog was open: it fit, or its fields were scrolled to the end."""
        return self._seen_all

    def _note_seen(self, *_args: Any) -> None:
        scroll = self.fields_scroll
        if self._seen_all or scroll is None or not self.isVisible() or not shiboken6.isValid(scroll):
            return
        content = scroll.widget()
        viewport = scroll.viewport()
        layout = content.layout() if content is not None else None
        if content is None or layout is None:
            return
        needed = (layout.totalHeightForWidth(content.width()) if layout.hasHeightForWidth()
                  else layout.totalSizeHint().height())
        if content.height() + 1 < needed:
            return   # not laid out yet: decide on the next look
        # The message's end must be inside the fields' viewport, the dialog and the screen alike.
        bottom = self.body_edit.mapToGlobal(QPoint(0, self.body_edit.height())).y()
        limits = [viewport.mapToGlobal(QPoint(0, viewport.height())).y(),
                  self.mapToGlobal(QPoint(0, self.height())).y()]
        screen = self._screen()
        if screen is not None:
            limits.append(screen.availableGeometry().bottom() + 1)
        if bottom <= min(limits) + 1:
            self._seen_all = True

    def _sync_tab_order(self) -> None:
        """Tab follows the dialog from top to bottom: each recipient list (its rows, then Add), the
        subject, the message, Cancel, Save."""
        if not self.mail or self.to_list is None or self.cc_list is None:
            return
        chain = (self.to_list.focus_chain() + self.cc_list.focus_chain()
                 + [self.subject_edit, self.body_edit, self.cancel_button, self.save_button])
        for first, second in zip(chain, chain[1:]):
            QWidget.setTabOrder(first, second)

    def _screen(self) -> Any:
        parent = self.parentWidget()
        return (parent.window().screen() if parent is not None else self.screen()) or QApplication.primaryScreen()

    def _area(self) -> QRect | None:
        """The screen's available area (QScreen.availableGeometry: without the taskbar), in the same
        device-independent pixels as the dialog at any display scale; None without a screen."""
        screen = self._screen()
        return screen.availableGeometry() if screen is not None else None

    def _fit(self) -> None:
        """Fit the dialog to the screen's available area at any display scale: its kind's width
        (narrower when the area is), and as tall as its content at that width. A Reply / Email
        shows its subject and message whole; when the dialog would be taller than the area, its
        fields scroll (with a scroll bar) and the banner and Save / Cancel stay on screen.

        The fields' height is set once, to what fits: the dialog's minimum size never exceeds the
        area, so the window is never asked to be taller than the screen (Windows would refuse:
        "Unable to set geometry")."""
        scroll = self.fields_scroll
        if scroll is None:
            self._fit_height()
            return
        self.ensurePolished()   # a row added just now changes its size once styled: measure it styled
        area = self._area()
        margin = self._SCREEN_MARGIN
        base = self.MAIL_WIDTH if self.mail else self.WIDTH
        width = base if area is None else max(min(base, self._MIN_WIDTH), min(base, area.width() - 2 * margin))
        if self.minimumWidth() != width or self.maximumWidth() != width:
            self.setFixedWidth(width)
        room = area.height() - 2 * margin if area is not None else 1 << 20
        bar = scroll.verticalScrollBar()
        value = bar.value()
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedHeight(0)
        chrome = self._layout_height(width)   # the banner, title, error line, Save / Cancel and margins
        natural = self._fields_height()
        fields = natural
        if chrome + natural > room:
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
            self._fit_fields()   # the scroll bar takes some width: the fields wrap again
            fields = min(natural, max(self._FIELDS_MIN, room - chrome))
        scroll.setFixedHeight(fields)
        self._fit_height()
        bar.setValue(value)

    def _fit_fields(self) -> None:
        """Lay the fields out at the scroll area's width, the subject and message as tall as their text."""
        for _attempt in range(2):
            layout = self.layout()
            if layout is not None:
                layout.activate()
            scroll = self.fields_scroll
            content = scroll.widget() if scroll is not None else None
            if content is not None and content.layout() is not None:
                content.resize(scroll.viewport().width(), content.height())
                content.layout().activate()
            if not self.mail:
                continue
            for edit in (self.subject_edit, self.body_edit):
                edit.blockSignals(True)
                try:
                    edit.fit_to_text()
                finally:
                    edit.blockSignals(False)

    def _layout_height(self, width: int) -> int:
        """The dialog's height at ``width`` as its layout stands, measured afresh."""
        # adjustSize() measures wrapped labels at the size hint's width, not the fixed width they get.
        # A field that changed its fixed height may leave the panel's and the dialog's layouts with
        # their earlier size cached until the event loop runs: measure afresh.
        if self.panel.layout() is not None:
            self.panel.layout().invalidate()
        self.panel.updateGeometry()   # also drops the dialog layout's cached size of the panel
        layout = self.layout()
        if layout is None:
            return self.sizeHint().height()
        layout.invalidate()
        layout.activate()
        if layout.hasHeightForWidth():
            return max(layout.totalHeightForWidth(width), layout.totalMinimumSize().height())
        return max(layout.totalSizeHint().height(), layout.totalMinimumSize().height())

    def _fields_height(self) -> int:
        """The fields' whole height at the scroll area's width (FROM to the links line)."""
        self._fit_fields()
        scroll = self.fields_scroll
        content = scroll.widget() if scroll is not None else None
        layout = content.layout() if content is not None else None
        if layout is None:
            return 0
        width = scroll.viewport().width()
        return (layout.totalHeightForWidth(width) if layout.hasHeightForWidth()
                else layout.totalSizeHint().height())

    def _on_field_grew(self) -> None:
        """The subject or the message got a line more or less while typing: fit again and keep the
        text cursor in view."""
        self._follow_cursor = True
        self._relayout()

    def _keep_cursor_visible(self) -> None:
        """While typing in the subject or the message, their text cursor stays in view."""
        scroll = self.fields_scroll
        focus = QApplication.focusWidget()
        follow, self._follow_cursor = self._follow_cursor, False
        if not follow or scroll is None or focus not in (self.subject_edit, self.body_edit) or not self.isVisible():
            return
        rect = focus.cursorRect()
        content = scroll.widget()
        point = focus.viewport().mapTo(content, rect.center())
        scroll.ensureVisible(point.x(), point.y(), 10, rect.height())

    def _fit_height(self) -> None:
        width = self.width()
        self.resize(width, self._layout_height(width))

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        if not self._fitted:   # shown without open(): fit the content once all the same
            self._fitted = True
            self._fit()
        if self.mail:   # is all of it on screen from the start?
            QTimer.singleShot(0, self._note_seen)

    def _relayout(self) -> None:
        """A recipient row or an error line came or went: fit again, keeping the bottom on screen
        (and once more when the new row has been polished)."""
        self._place_after_fit()
        QTimer.singleShot(0, self._place_after_fit)

    def _place_after_fit(self) -> None:
        if not shiboken6.isValid(self):
            return
        self._fit()
        if self.isVisible():
            self._keep_on_screen()
        self._keep_cursor_visible()
        self._note_seen()

    def _keep_on_screen(self) -> None:
        """Move the dialog (not resize it) so all of it is inside the screen's available area."""
        area = self._area()
        if area is None:
            return
        margin = self._SCREEN_MARGIN
        x, y = self.x(), self.y()
        if x + self.width() > area.right() + 1 - margin:
            x = area.right() + 1 - margin - self.width()
        if y + self.height() > area.bottom() + 1 - margin:
            y = area.bottom() + 1 - margin - self.height()
        x, y = max(x, area.left()), max(y, area.top())
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def open(self) -> None:  # noqa: D102 - QDialog.open, centred over the parent window first
        self._fitted = True
        self._fit()
        parent = self.parentWidget()
        area = self._area()
        if parent is not None:
            window = parent.window()
            center = window.mapToGlobal(QPoint(window.width() // 2, window.height() // 2))
        else:
            center = area.center() if area is not None else self.geometry().center()
        self.move(center.x() - self.width() // 2, center.y() - self.height() // 2)
        self._keep_on_screen()
        super().open()
        if self.mail:   # a new recipient's tick first (why Send opened the dialog), else the message
            boxes = [row.confirm_box for recipients in (self.to_list, self.cc_list) if recipients is not None
                     for row in recipients.rows if row.confirm_box is not None and not row.confirm_box.isChecked()]
            target = boxes[0] if boxes else self.body_edit
            target.setFocus(Qt.FocusReason.OtherFocusReason)
            if self.fields_scroll is not None:
                self.fields_scroll.verticalScrollBar().setValue(0)   # the dialog opens at its top
                if boxes:
                    self.fields_scroll.ensureWidgetVisible(target)
            self._relayout()   # also looks whether everything is on screen


ACCOUNT_YES_TEXT = "Yes, that's right"
ACCOUNT_NO_TEXT = "No, use another account"
ACCOUNT_QUESTION = "Signed in as {address} for \"{alias}\" - is that right?"


class AccountDialog(QDialog):
    """The first sign-in's question for an account alias: 'Signed in as ana@example.edu for "work"
    - is that right?', with "Yes, that's right" and "No, use another account".

    Google's account chooser makes it easy to pick the wrong account, and an alias is bound to
    whichever account its first sign-in picked; until this is answered Yes, Jarvis sends and
    changes nothing for the alias (reading its calendar goes on). Window-modal, opened with
    ``open()``, it always fits the screen's available area. A button emits ``answered(alias,
    address, yes)`` and closes the dialog; Escape closes it without an answer (it is asked again
    at the next Send or Approve). Neither button is a default button, so Enter answers nothing.
    Every text is plain text.
    """

    answered = Signal(str, str, bool)
    WIDTH = 440
    _MIN_WIDTH = 300
    _SCREEN_MARGIN = 16

    def __init__(self, alias: str, address: str, *, sends_mail: bool = True, parent: QWidget | None = None) -> None:
        flags = Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint
        super().__init__(parent, flags)
        self.alias = alias
        self.address = address
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle(f"Confirm the {alias} account")
        self.panel = ChamferPanel("Confirm", "GOOGLE ACCOUNT", variant=PANEL_AMBER, border_alpha=0.5,
                                  padding=(18, 14, 18, 16), spacing=8)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)   # _fit sets the size
        outer.addWidget(self.panel)
        layout = self.panel.body_layout
        # The texts scroll above the buttons on a very small screen, so the buttons stay on screen.
        self.text_scroll = QScrollArea(self)
        content = _transparent_scroll(self.text_scroll)
        texts = QVBoxLayout(content)
        texts.setContentsMargins(0, 0, 0, 0)
        texts.setSpacing(8)
        self.text_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.text_scroll.setFixedHeight(0)   # _fit sets the height
        layout.addWidget(self.text_scroll)
        question = ACCOUNT_QUESTION.format(address=address, alias=alias)
        self.question_label = _BreakableLabel(question, body_font(15, 600), AMBER_TEXT, min_word=0)
        texts.addWidget(self.question_label)
        if sends_mail:
            what = f"Jarvis sends {alias} email and changes {alias} calendar events as this Google account."
        else:
            what = f"Jarvis changes {alias} calendar events as this Google account."
        self.detail_label = make_label(f"{what} Nothing is sent or changed until you answer.", body_font(13),
                                       TEXT_BODY, wrap=True)
        texts.addWidget(self.detail_label)
        self.hint_label = make_label("No signs this account out of Jarvis and opens Google's sign-in again, "
                                     "so you can pick the right account.", mono_font(10), TEXT_DIM, wrap=True)
        texts.addWidget(self.hint_label)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 6, 0, 0)
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self.no_button = HudButton(ACCOUNT_NO_TEXT, SECONDARY, compact=True)
        self.no_button.setAccessibleDescription("Signs this account out and opens Google's sign-in again")
        self.yes_button = HudButton(ACCOUNT_YES_TEXT, PRIMARY, compact=True)
        self.yes_button.setAccessibleDescription(f"Keeps {address} as the {alias} account")
        buttons.addWidget(self.no_button)
        buttons.addWidget(self.yes_button)
        layout.addLayout(buttons)
        self.no_button.clicked.connect(self._on_no)
        self.yes_button.clicked.connect(self._on_yes)
        self.setAccessibleName(question)
        self.setFixedWidth(self.WIDTH)

    def _on_yes(self) -> None:
        self.answered.emit(self.alias, self.address, True)
        self.accept()

    def _on_no(self) -> None:
        self.answered.emit(self.alias, self.address, False)
        self.accept()

    def _area(self) -> QRect | None:
        parent = self.parentWidget()
        screen = (parent.window().screen() if parent is not None else self.screen()) or QApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else None

    def _height(self, width: int) -> int:
        if self.panel.layout() is not None:
            self.panel.layout().invalidate()
        self.panel.updateGeometry()
        layout = self.layout()
        layout.invalidate()
        layout.activate()
        return max(layout.totalHeightForWidth(width) if layout.hasHeightForWidth()
                   else layout.totalSizeHint().height(), layout.totalMinimumSize().height())

    def _texts_height(self) -> int:
        scroll = self.text_scroll
        content = scroll.widget()
        content.resize(scroll.viewport().width(), content.height())
        texts = content.layout()
        texts.activate()
        width = scroll.viewport().width()
        return texts.totalHeightForWidth(width) if texts.hasHeightForWidth() else texts.totalSizeHint().height()

    def _fit(self) -> None:
        """Its width (narrower on a narrow screen) and the height of its content at that width; on a
        screen too short for the texts they scroll and the buttons stay on screen."""
        self.ensurePolished()
        area = self._area()
        margin = self._SCREEN_MARGIN
        width = self.WIDTH
        if area is not None:
            width = max(self._MIN_WIDTH, min(self.WIDTH, area.width() - 2 * margin))
        if self.minimumWidth() != width or self.maximumWidth() != width:
            self.setFixedWidth(width)
        room = area.height() - 2 * margin if area is not None else 1 << 20
        scroll = self.text_scroll
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedHeight(0)
        chrome = self._height(width)
        natural = self._texts_height()
        texts = natural
        if chrome + natural > room:
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
            self._height(width)
            natural = self._texts_height()   # the scroll bar takes some width: the texts wrap again
            texts = min(natural, max(48, room - chrome))
        scroll.setFixedHeight(texts)
        self.resize(width, self._height(width))

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._fit()

    def open(self) -> None:  # noqa: D102 - QDialog.open, centred over the parent window
        self._fit()
        area = self._area()
        parent = self.parentWidget()
        if parent is not None:
            window = parent.window()
            center = window.mapToGlobal(QPoint(window.width() // 2, window.height() // 2))
            x, y = center.x() - self.width() // 2, center.y() - self.height() // 2
        else:
            x, y = self.x(), self.y()
        if area is not None:
            margin = self._SCREEN_MARGIN
            x = max(area.left(), min(x, area.right() + 1 - margin - self.width()))
            y = max(area.top(), min(y, area.bottom() + 1 - margin - self.height()))
        self.move(x, y)
        super().open()
        self.setFocus(Qt.FocusReason.OtherFocusReason)   # no button has the focus: Enter answers nothing


def _short_text(text: str, limit: int) -> str:
    """``text`` cut in the middle to ``limit`` characters ("abc...xyz")."""
    if len(text) <= limit:
        return text
    half = (limit - 3) // 2
    return text[:half] + "..." + text[-half:]


# --------------------------------------------------------------------------
# Command bar (Ask Jarvis)
# --------------------------------------------------------------------------

TONE_IDLE = "idle"          # a hint, or nothing going on (dim)
TONE_WORKING = "working"    # reading the calendar, planning (cyan)
TONE_DONE = "done"          # the planner's answer (bright)
TONE_WARN = "warn"          # a question back, a limit, something to do first (amber)
TONE_ERROR = "error"        # nothing was proposed (red)
TONE_GOOD = "good"          # an approved card that was carried out (green; the conversation)
TONES = (TONE_IDLE, TONE_WORKING, TONE_DONE, TONE_WARN, TONE_ERROR, TONE_GOOD)
_TONE_COLORS = {TONE_IDLE: TEXT_MUTED, TONE_WORKING: ACCENT, TONE_DONE: TEXT_BODY, TONE_WARN: AMBER,
                TONE_ERROR: RED, TONE_GOOD: GREEN}
COMMAND_MAX_LENGTH = 500
COMMAND_PLACEHOLDER = "Ask Jarvis - nothing happens without your OK"
# Shorter placeholders for a narrower field: the longest that fits is shown (never cut).
COMMAND_PLACEHOLDERS = (COMMAND_PLACEHOLDER, "Ask Jarvis - needs your OK", "Ask - needs your OK", "Ask Jarvis")
ASK_BUTTON_TEXT = "Ask"
CANCEL_BUTTON_TEXT = "Cancel"
_PROMPT_GLYPH = "\u203a"    # single right-pointing angle quotation mark


class _CommandInput(QLineEdit):
    """The bar's text field: frameless (the field around it is painted), plain text. Esc is the
    bar's (``escapePressed``); Enter submits (``returnPressed``)."""

    escapePressed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrame(False)
        self.setMaxLength(COMMAND_MAX_LENGTH)
        self.setPlaceholderText(COMMAND_PLACEHOLDER)
        self.setFont(body_font(13))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        palette = self.palette()
        for role, color in ((QPalette.ColorRole.Base, rgba(GROUND, 0.0)), (QPalette.ColorRole.Text, QColor(TEXT_BODY)),
                            (QPalette.ColorRole.PlaceholderText, QColor(TEXT_DIM)),
                            (QPalette.ColorRole.Highlight, rgba(ACCENT, 0.35)),
                            (QPalette.ColorRole.HighlightedText, QColor(TEXT_BRIGHT))):
            palette.setColor(role, color)
        self.setPalette(palette)
        self.setStyleSheet("QLineEdit { background: transparent; border: none; padding: 0px; }")
        self.setAccessibleName("Ask Jarvis")

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            event.accept()
            self.escapePressed.emit()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._fit_placeholder()

    def _fit_placeholder(self) -> None:
        """The longest COMMAND_PLACEHOLDERS text the field shows whole."""
        room = self.width() - 8   # QLineEdit's own text margins
        metrics = QFontMetricsF(self.font())
        text = next((item for item in COMMAND_PLACEHOLDERS if metrics.horizontalAdvance(item) <= room),
                    COMMAND_PLACEHOLDERS[-1])
        if text != self.placeholderText():
            self.setPlaceholderText(text)


class _CommandField(QWidget):
    """The chamfered field around the input: a cyan outline (brighter while the input has the
    focus) and a leading prompt glyph."""

    HEIGHT = 36
    _CUT = 8

    def __init__(self, line_edit: _CommandInput, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.line_edit = line_edit
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        glyph = make_label(_PROMPT_GLYPH, mono_font(15, 600), ACCENT)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 10, 0)
        layout.setSpacing(8)
        layout.addWidget(glyph, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(line_edit, 1, Qt.AlignmentFlag.AlignVCenter)
        line_edit.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if watched is self.line_edit and event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut,
                                                           QEvent.Type.ReadOnlyChange):
            self.update()
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self.line_edit.setFocus(Qt.FocusReason.MouseFocusReason)   # a click beside the text
        super().mousePressEvent(event)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            focused = self.line_edit.hasFocus()
            painter.fillPath(chamfer_path(rect, self._CUT), rgba("#030a10", 0.92))
            if focused:
                painter.fillPath(chamfer_path(rect, self._CUT), rgba(ACCENT, 0.05))
            alpha = 0.85 if focused else 0.4 if not self.line_edit.isReadOnly() else 0.25
            painter.setPen(QPen(rgba(ACCENT, alpha), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(chamfer_path(rect, self._CUT))
        finally:
            painter.end()


class CommandBar(QWidget):
    """Ask Jarvis's typed command bar: the field and its Ask button, then a status line (the
    planner's answer, what Jarvis is doing, or why nothing was proposed) and a meta line with an
    optional link at its right end.

    Enter (or Ask) emits ``submitted(text)`` with the whitespace collapsed; an empty field or a
    running request emits nothing. While a request runs (``set_running(True)``) the field is read
    only and the button reads Cancel: it, and Esc in the field, emit ``cancelRequested``. Esc
    otherwise clears the field, and in an empty field emits ``statusDismissed`` (the owner puts
    the last answer away). ``set_status(text, tone)`` (TONE_*) shows at most two lines (the
    whole text is the tooltip); ``set_meta(text)`` one line; ``set_link(text, tooltip)`` the link
    (``linkClicked``; "" hides it; never shown while a request runs). ``set_compact(True)`` (a
    narrow or short reading screen) drops the meta line (its text goes into the status tooltip)
    and puts the link beside the status while the bar is idle (TONE_IDLE) or the status asks for
    it (``set_status(..., keep_link=True)``), so the controls below keep their room and an answer
    keeps the whole width. ``set_hint(text)`` is the status with nothing to report: the status and
    meta lines fold away (the field's placeholder and tooltip say it) unless a link is to be
    shown, so the transcript below keeps its room; anything else the bar says brings them back.
    Otherwise the bar's height changes only with ``set_compact``, never with its texts. The field
    takes at most COMMAND_MAX_LENGTH characters and never reads its text as HTML. Nothing here
    plans or runs anything.
    """

    submitted = Signal(str)
    cancelRequested = Signal()
    linkClicked = Signal()
    statusDismissed = Signal()   # Esc in an empty field while nothing runs: put the last answer away

    _STATUS_LINES = 2

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._running = False
        self._compact = False
        self._status_text = ""
        self._meta_text = ""
        self._tone = TONE_IDLE
        self._keep_link = False      # the status asks for the link beside it (compact)
        self._hint = False           # the status is only the idle hint: it may fold away
        self.input = _CommandInput()
        self.field = _CommandField(self.input)
        self.button = HudButton(ASK_BUTTON_TEXT, SECONDARY, compact=True)
        width = 0
        for text in (ASK_BUTTON_TEXT, CANCEL_BUTTON_TEXT):
            self.button.setText(text)
            width = max(width, self.button.sizeHint().width())
        self.button.setText(ASK_BUTTON_TEXT)
        self.button.setMinimumWidth(width)
        self.button.setToolTip("Plan this request (Enter)")
        self.status_label = _ClampedLabel(body_font(12), _TONE_COLORS[TONE_IDLE], max_lines=self._STATUS_LINES)
        self.status_label.setObjectName("askStatus")
        self._line = math.ceil(QFontMetricsF(self.status_label.font()).lineSpacing())
        self.status_label.setFixedHeight(self._line * self._STATUS_LINES + 2)   # never moves the controls below
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.meta_label = _ClampedLabel(mono_font(10, 400, 0.12), TEXT_DIM, max_lines=1)
        self.meta_label.setObjectName("askMeta")
        self.link_button = HudButton("", LINK, compact=True)
        self.link_button.set_link_color(ACCENT)
        self.link_button.hide()
        self.input.returnPressed.connect(self._on_return)
        self.input.escapePressed.connect(self._on_escape)
        self.button.clicked.connect(self._on_button)
        self.link_button.clicked.connect(self.linkClicked)
        self._build_layout()

    def _build_layout(self) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self.field, 1)
        row.addWidget(self.button, 0, Qt.AlignmentFlag.AlignVCenter)
        self._status_row = QHBoxLayout()
        self._status_row.setContentsMargins(0, 0, 0, 0)
        self._status_row.setSpacing(10)
        self._status_row.addWidget(self.status_label, 1)
        self._meta_row = QHBoxLayout()
        self._meta_row.setContentsMargins(0, 0, 0, 0)
        self._meta_row.setSpacing(10)
        self._meta_row.addWidget(self.meta_label, 1, Qt.AlignmentFlag.AlignVCenter)
        self._meta_row.addWidget(self.link_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(row)
        layout.addLayout(self._status_row)
        layout.addLayout(self._meta_row)

    def set_compact(self, compact: bool) -> None:
        """No meta line (its text is in the status tooltip) and the link beside the status while
        idle: for a narrow or short reading screen."""
        compact = bool(compact)
        if compact == self._compact:
            return
        self._compact = compact
        source, target = (self._meta_row, self._status_row) if compact else (self._status_row, self._meta_row)
        source.removeWidget(self.link_button)
        target.addWidget(self.link_button, 0, Qt.AlignmentFlag.AlignTop if compact else Qt.AlignmentFlag.AlignVCenter)
        self._render_status()
        self._show_link()
        self.updateGeometry()

    def is_compact(self) -> bool:
        return self._compact

    def _show_link(self) -> None:
        # Never while a request runs; compact: beside the status, only while nothing else is said
        # there (an answer keeps the width) or the status asks for it.
        shown = (bool(self.link_button.text()) and not self._running
                 and (not self._compact or self._tone == TONE_IDLE or self._keep_link))
        self.link_button.setVisible(shown)
        self._fold()

    def _fold(self) -> None:
        """The status and meta lines fold away while the status is only the hint and no link shows.
        Unfolded and not compact, a hidden link keeps its room in the meta line, so the bar's
        height does not change when the link comes and goes (while a request runs, say)."""
        folded = self._hint and not self._running and self.link_button.isHidden()
        self.status_label.setVisible(not folded)
        self.meta_label.setVisible(not folded and not self._compact)
        policy = self.link_button.sizePolicy()
        retain = not folded and not self._compact and bool(self.link_button.text())
        if policy.retainSizeWhenHidden() != retain:
            policy.setRetainSizeWhenHidden(retain)
            self.link_button.setSizePolicy(policy)
        self.updateGeometry()

    def is_folded(self) -> bool:
        """Only the field shows (the idle hint folded away)."""
        return self.status_label.isHidden()

    def _render_status(self) -> None:
        tooltip = None   # cut: the whole text is the tooltip
        if self._compact and self._meta_text:
            tooltip = plain_tooltip("\n".join(part for part in (self._status_text, self._meta_text.upper()) if part))
        self.status_label.set_full_text(self._status_text, tooltip)

    # ---- input --------------------------------------------------------------------------

    def text(self) -> str:
        """The field's text with its whitespace collapsed (what Enter submits)."""
        return " ".join(self.input.text().split())

    def set_text(self, text: str) -> None:
        self.input.setText(text[:COMMAND_MAX_LENGTH])

    def clear_input(self) -> None:
        self.input.clear()

    def focus_input(self) -> None:
        self.input.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _on_return(self) -> None:
        text = self.text()
        if self._running or not text:
            return
        self.submitted.emit(text)

    def _on_escape(self) -> None:
        if self._running:
            if self.button.isEnabled():
                self.cancelRequested.emit()
        elif self.input.text():
            self.input.clear()
        else:
            self.statusDismissed.emit()

    def _on_button(self) -> None:
        if self._running:
            self.cancelRequested.emit()
        else:
            self._on_return()
            self.focus_input()

    # ---- state --------------------------------------------------------------------------

    def set_running(self, running: bool) -> None:
        """A request runs: the field is read only and the button reads Cancel (enabled)."""
        self._running = bool(running)
        self.input.setReadOnly(self._running)
        self.button.setText(CANCEL_BUTTON_TEXT if self._running else ASK_BUTTON_TEXT)
        self.button.set_variant(DENY if self._running else SECONDARY)
        self.button.setToolTip("Stop planning; nothing is proposed (Esc)" if self._running
                               else "Plan this request (Enter)")
        self.button.setEnabled(True)
        self.field.update()
        self._show_link()

    def set_cancel_enabled(self, enabled: bool) -> None:
        """While running: False after a Cancel click (it is on its way)."""
        if self._running:
            self.button.setEnabled(enabled)

    def is_running(self) -> bool:
        return self._running

    def set_status(self, text: str, tone: str = TONE_IDLE, *, keep_link: bool = False) -> None:
        """The status line; ``keep_link`` keeps the link beside it in compact mode (it is about it)."""
        self._hint = False
        self._show_status(text, tone, keep_link)

    def set_hint(self, text: str, *, tooltip: str = "") -> None:
        """Nothing to report: ``text`` is the idle hint (the field's tooltip too, or ``tooltip``
        when given); the status and meta lines fold away unless the link shows."""
        self._hint = True
        tip = tooltip or text
        self.input.setToolTip(plain_tooltip(tip) if tip else "")
        self._show_status(text, TONE_IDLE, False)

    def _show_status(self, text: str, tone: str, keep_link: bool) -> None:
        self._tone = tone if tone in _TONE_COLORS else TONE_IDLE
        self._keep_link = bool(keep_link)
        set_label_color(self.status_label, _TONE_COLORS[self._tone])
        self._status_text = " ".join((text or "").split())
        self._render_status()
        self._show_link()

    def status(self) -> str:
        return self.status_label.full_text()

    def tone(self) -> str:
        return self._tone

    def set_meta(self, text: str) -> None:
        self._meta_text = text or ""
        self.meta_label.set_full_text(text.upper() if text else "")
        if self._compact:
            self._render_status()

    def meta(self) -> str:
        return self.meta_label.full_text()

    def set_link(self, text: str, tooltip: str = "") -> None:
        self.link_button.setText(text)
        self.link_button.setToolTip(plain_tooltip(tooltip) if tooltip else "")
        self.link_button.setAccessibleName(tooltip or text)
        self._show_link()

    def link(self) -> str:
        """The link's text ("" when there is none; it may be out of sight in compact mode)."""
        return self.link_button.text()

    def link_shown(self) -> bool:
        return not self.link_button.isHidden()


# --------------------------------------------------------------------------
# Activity log
# --------------------------------------------------------------------------

TAG_RUN = "run"
TAG_DONE = "done"
TAG_WAIT = "wait"
TAG_STOP = "stop"
TAG_ASK = "ask"
TAG_COLORS = {TAG_RUN: ACCENT, TAG_DONE: GREEN, TAG_WAIT: AMBER, TAG_STOP: RED, TAG_ASK: PINK}


class _ActivityRow(QWidget):
    """One log row. On the 24-hour clock the time and tag columns are 38 and 44 px wide.

    The 12-hour clock sizes them from the font instead: the time column fits
    the widest time ("12:59 PM", right-aligned so the colons and AM / PM line
    up) and the tag column the widest tag, which gives the message back most
    of the width the longer times take.
    """

    _TIME_WIDTH_24H = 38
    _TAG_WIDTH_24H = 44
    _PAD = 3          # after the widest time and the widest tag on the 12-hour clock

    def __init__(self, when: str, tag: str, message: str, sub: str, hour24: bool = False) -> None:
        super().__init__()
        font = mono_font(11)
        self.entry = (when, tag, message, sub)
        time_label = make_label(when, font, TEXT_TIME)
        tag_label = make_label(tag, font, TAG_COLORS.get(tag, TEXT_MUTED))
        if hour24:
            time_label.setFixedWidth(self._TIME_WIDTH_24H)
            tag_label.setFixedWidth(self._TAG_WIDTH_24H)
        else:
            time_label.setFixedWidth(math.ceil(_clock_width(font, False)) + self._PAD)
            time_label.setContentsMargins(0, 0, self._PAD, 0)
            time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            widest_tag = max(_text_advance(font, text) for text in (*TAG_COLORS, tag))
            tag_label.setFixedWidth(math.ceil(widest_tag) + self._PAD)
        texts = QVBoxLayout()
        texts.setContentsMargins(0, 0, 0, 0)
        texts.setSpacing(0)
        texts.addWidget(make_label(message, font, TEXT_BODY, wrap=True))
        if sub:
            texts.addWidget(make_label(sub, font, TEXT_SUB, wrap=True))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(time_label, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(tag_label, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(texts, 1)
        self.setAccessibleName(f"{when} {tag} {message}" + (f", {sub}" if sub else ""))


class ActivityLog(QScrollArea):
    """Newest-first log rows "time  tag  message / sub"; keeps the last MAX_ENTRIES.

    Times read "1:04 PM", or "13:04" after ``set_hour24(True)``; the 12-hour
    time column is as wide as its widest time.
    """

    MAX_ENTRIES = 50

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        content = _transparent_scroll(self)
        self._layout = QVBoxLayout(content)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._layout.addStretch(1)
        self._rows: list[_ActivityRow] = []
        self._hour24 = False

    def set_hour24(self, hour24: bool) -> None:
        """The clock of the entries added from now on (call it before the first one)."""
        self._hour24 = hour24

    def add(self, tag: str, message: str, sub: str = "", when: datetime | str | None = None) -> None:
        stamp = when if isinstance(when, str) else format_time(when or datetime.now(), hour24=self._hour24)
        row = _ActivityRow(stamp, tag, message, sub, self._hour24)
        self._layout.insertWidget(0, row)
        self._rows.insert(0, row)
        while len(self._rows) > self.MAX_ENTRIES:
            old = self._rows.pop()
            old.hide()
            old.deleteLater()

    def entries(self) -> list[tuple[str, str, str, str]]:
        """(time, tag, message, sub) newest first."""
        return [row.entry for row in self._rows]

    def clear(self) -> None:
        for row in self._rows:
            row.hide()
            row.deleteLater()
        self._rows = []


# --------------------------------------------------------------------------
# Tabs (JARVIS / BRIEFING) and the conversation
# --------------------------------------------------------------------------

TAB_JARVIS = 0
TAB_BRIEFING = 1
TAB_LIVE = 2
TAB_MEMORY = 3                  # the productivity bundle's MEMORY tab (needs the LIVE tab's slot before it)
TABS = (TAB_JARVIS, TAB_BRIEFING, TAB_LIVE, TAB_MEMORY)
MEMORY_TAB_TIP = "Memory - notes kept on this PC"
NEW_BADGE_TEXT = "NEW"
_TAB_TEXT = "#8592a0"           # a tab that is not current (the step chips' "todo" text)
ACTIVITY_UNREAD = "unread"      # the LIVE tab's dot: steps arrived while LIVE was not current
ACTIVITY_WORKING = "working"    # ... the same dot blinking: Jarvis is working on an Ask or a card
ACTIVITIES = ("", ACTIVITY_UNREAD, ACTIVITY_WORKING)
BADGE_UNREAD = "unread"         # what a tab's badge can be (TabButton.set_badge_kinds)
BADGE_NEW = "new"
BADGE_ACTIVITY = "activity"
_ACTIVITY_NAMES = {ACTIVITY_UNREAD: ", new steps", ACTIVITY_WORKING: ", Jarvis is working"}
_ACTIVITY_TIPS = {ACTIVITY_UNREAD: "New steps", ACTIVITY_WORKING: "Jarvis is working - see every step"}


def _sync_blink_clock(widget: QWidget, wanted: bool) -> None:
    """Register ``widget`` with the shared blink clock while ``wanted`` and shown (it repaints at
    each on/off flip), else unregister it."""
    clock = _blink_clock()
    if wanted and widget.isVisible():
        clock.register(widget)
    else:
        clock.unregister(widget)


class TabButton(QAbstractButton):
    """One folder tab: mono capitals, a 2 px bottom border (accent when current) and an optional
    badge after the label - the amber NEW pill (an 8 px amber dot when compact), a 6 px accent
    "unread" dot, or the LIVE tab's activity dot (``set_activity``: ``ACTIVITY_UNREAD``, or
    ``ACTIVITY_WORKING``, which blinks on the shared blink clock). Checkable and auto-exclusive
    with its siblings; Tab focus only (a ring for keyboard focus); Left / Right move to the
    neighbouring tab."""

    HEIGHT = 28
    _PAD = 12
    _PAD_COMPACT = 8
    _PAD_TIGHT = 5          # four tabs that do not fit even compact (the 900 px window with MEMORY)
    _GAP = 7
    _GAP_TIGHT = 5
    _DOT = 6
    _PILL_PAD = 5
    _PILL_HEIGHT = 15

    def __init__(self, text: str, name: str, parent: QWidget | None = None, *, tip: str = "") -> None:
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._font = mono_font(11, 400, 0.08)
        self._badge_font = mono_font(9, 700, 0.06)
        self._name = name
        self._tip = tip                 # the tooltip while no badge has one of its own
        self._new = False
        self._unread = False
        self._activity = ""
        self._badge_kinds: frozenset[str] = frozenset()   # the badges this tab can show (natural_width)
        self._compact = False
        self._tight = False
        self._focus_visible = False
        self._update_accessible()

    # ---- state ----------------------------------------------------------------

    def set_badge_kinds(self, kinds: Sequence[str]) -> None:
        """The badges this tab can carry (BADGE_UNREAD, BADGE_NEW, BADGE_ACTIVITY): its
        ``natural_width`` has room for the widest of them, so the strip never flips to compact
        when one appears."""
        self._badge_kinds = frozenset(kinds)

    def set_activity(self, state: str) -> None:
        """The LIVE tab's dot: "" (none), ACTIVITY_UNREAD or ACTIVITY_WORKING (blinking)."""
        state = state if state in ACTIVITIES else ""
        if state != self._activity:
            self._activity = state
            self._update_accessible()
            self.updateGeometry()
            self.update()
        _sync_blink_clock(self, self._activity == ACTIVITY_WORKING)

    def activity(self) -> str:
        return self._activity

    def set_new(self, new: bool) -> None:
        if bool(new) != self._new:
            self._new = bool(new)
            self._update_accessible()
            self.updateGeometry()
            self.update()

    def is_new(self) -> bool:
        return self._new

    def set_unread(self, unread: bool) -> None:
        if bool(unread) != self._unread:
            self._unread = bool(unread)
            self._update_accessible()
            self.updateGeometry()
            self.update()

    def is_unread(self) -> bool:
        return self._unread

    def set_compact(self, compact: bool, *, tight: bool = False) -> None:
        """Narrower padding (and the NEW pill as a dot); ``tight`` narrower still (compact only)."""
        tight = bool(tight) and bool(compact)
        if bool(compact) != self._compact or tight != self._tight:
            self._compact = bool(compact)
            self._tight = tight
            self.updateGeometry()
            self.update()

    def is_tight(self) -> bool:
        return self._tight

    def _update_accessible(self) -> None:
        name = self._name
        if self._new:
            name += ", new briefing"
        if self._unread:
            name += ", new entries"
        name += _ACTIVITY_NAMES.get(self._activity, "")
        self.setAccessibleName(name)
        if self._new:
            self.setToolTip("A new briefing you have not viewed yet")
        elif self._unread:
            self.setToolTip("Jarvis added something here")
        else:
            self.setToolTip(_ACTIVITY_TIPS.get(self._activity, "") or getattr(self, "_tip", ""))

    # ---- geometry -------------------------------------------------------------

    def _pad(self) -> int:
        if self._tight:
            return self._PAD_TIGHT
        return self._PAD_COMPACT if self._compact else self._PAD

    def _gap(self) -> int:
        return self._GAP_TIGHT if self._tight else self._GAP

    def _badge_width(self) -> float:
        if self._new and not self._compact:
            return self._GAP + 2 * self._PILL_PAD + _text_advance(self._badge_font, NEW_BADGE_TEXT)
        if self._new:
            return self._gap() + 8
        if self._unread or self._activity:
            return self._gap() + self._DOT
        return 0.0

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        width = 2 * self._pad() + _text_advance(self._font, self.text().upper()) + self._badge_width()
        return QSize(math.ceil(width), self.HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    def natural_width(self) -> int:
        """The tab's width when not compact, with the widest badge it can carry (set_badge_kinds)."""
        badges = [0.0]
        if BADGE_NEW in self._badge_kinds:
            badges.append(self._GAP + 2 * self._PILL_PAD + _text_advance(self._badge_font, NEW_BADGE_TEXT))
        if self._badge_kinds & {BADGE_UNREAD, BADGE_ACTIVITY}:
            badges.append(self._GAP + self._DOT)
        return math.ceil(2 * self._PAD + _text_advance(self._font, self.text().upper()) + max(badges))

    def compact_width(self) -> int:
        """The tab's width when compact (not tight), with the widest badge it can carry."""
        badges = [0.0]
        if BADGE_NEW in self._badge_kinds:
            badges.append(self._GAP + 8)
        if self._badge_kinds & {BADGE_UNREAD, BADGE_ACTIVITY}:
            badges.append(self._GAP + self._DOT)
        return math.ceil(2 * self._PAD_COMPACT + _text_advance(self._font, self.text().upper()) + max(badges))

    def badge_rect(self) -> QRectF:
        """Where the NEW pill / dot, the unread dot or the activity dot is painted (empty when
        there is none)."""
        if not (self._new or self._unread or self._activity):
            return QRectF()
        x = self._pad() + _text_advance(self._font, self.text().upper()) + self._gap()
        cy = (self.height() - 2) / 2
        if self._new and not self._compact:
            width = 2 * self._PILL_PAD + _text_advance(self._badge_font, NEW_BADGE_TEXT)
            return QRectF(x, cy - self._PILL_HEIGHT / 2, width, self._PILL_HEIGHT)
        size = 8.0 if self._new else float(self._DOT)
        return QRectF(x, cy - size / 2, size, size)

    # ---- events ---------------------------------------------------------------

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        strip = self.parentWidget()
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right) and isinstance(strip, TabStrip):
            strip.step(-1 if event.key() == Qt.Key.Key_Left else 1, focus=True)
            event.accept()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._focus_visible = _focus_ring_after(event.reason(), self._focus_visible)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().focusOutEvent(event)
        self.update()

    def enterEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().leaveEvent(event)
        self.update()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        _sync_blink_clock(self, self._activity == ACTIVITY_WORKING)

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        _blink_clock().unregister(self)

    def _dot_opacity(self) -> float:
        if self._activity != ACTIVITY_WORKING or self._unread or self._new:
            return 1.0
        return 1.0 if _BlinkClock.is_on() else 0.15

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        current, hover = self.isChecked(), self.underMouse() and self.isEnabled()
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect())
            if current or hover:
                painter.fillRect(rect, rgba(ACCENT, 0.08 if current else 0.04))
            painter.fillRect(QRectF(0, rect.height() - 2, rect.width(), 2),
                             QColor(ACCENT) if current else rgba("#ffffff", 0.08))
            painter.setFont(self._font)
            painter.setPen(QColor(BUTTON_TEXT if current else TEXT_SOFT if hover else _TAB_TEXT))
            label = self.text().upper()
            painter.drawText(QRectF(self._pad(), 0, rect.width(), rect.height() - 2),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
            badge = self.badge_rect()
            if not badge.isEmpty():
                painter.setPen(Qt.PenStyle.NoPen)
                if self._new and not self._compact:
                    painter.setBrush(QColor(AMBER))
                    painter.drawPath(chamfer_path(badge, 3))
                    painter.setFont(self._badge_font)
                    painter.setPen(QColor(AMBER_INK))
                    painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, NEW_BADGE_TEXT)
                else:
                    painter.setBrush(QColor(AMBER) if self._new else rgba(ACCENT, self._dot_opacity()))
                    painter.drawEllipse(badge)
            if self.hasFocus() and self._focus_visible:
                painter.setPen(QPen(QColor(ACCENT), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(rect.adjusted(1, 1, -1, -3))
        finally:
            painter.end()


class TabStrip(QWidget):
    """The JARVIS / BRIEFING (/ LIVE) tabs, left-aligned, 28 px tall; one is always current.

    ``currentChanged(index)`` when the current tab changes (click, Left / Right, ``set_current``).
    BRIEFING carries the NEW badge (``set_badge``), JARVIS the unread dot (``set_unread``; cleared
    when JARVIS becomes current). ``live=True`` adds the LIVE tab (``TAB_LIVE``, "Live steps"):
    ``set_live_visible`` shows or hides it (hidden, it is never current and Left / Right skip
    it), ``set_activity`` its dot (cleared when LIVE becomes current). ``memory=True`` (with
    ``live``) adds the MEMORY tab (``TAB_MEMORY``, "Memory notes"), hidden until
    ``set_memory_visible(True)`` (the productivity bundle's memory), with its own unread dot
    (``set_memory_unread``; cleared when MEMORY becomes current). Below
    ``max(COMPACT_BELOW, natural_width())`` px of width the tabs get narrower padding and the NEW
    pill becomes a dot (``set_compact`` sets it by hand); the natural width counts every badge,
    so a dot that appears never flips it.
    """

    currentChanged = Signal(int)
    HEIGHT = TabButton.HEIGHT
    COMPACT_BELOW = 220

    def __init__(self, parent: QWidget | None = None, *, live: bool = False, memory: bool = False) -> None:
        super().__init__(parent)
        self._tabs = [TabButton("Jarvis", "Jarvis conversation", self),
                      TabButton("Briefing", "Briefing transcript", self)]
        self._tabs[TAB_JARVIS].set_badge_kinds((BADGE_UNREAD,))
        self._tabs[TAB_BRIEFING].set_badge_kinds((BADGE_NEW,))
        if live:
            self._tabs.append(TabButton("Live", "Live steps", self))
            self._tabs[TAB_LIVE].set_badge_kinds((BADGE_ACTIVITY,))
            if memory:
                self._tabs.append(TabButton("Memory", "Memory notes", self, tip=MEMORY_TAB_TIP))
                self._tabs[TAB_MEMORY].set_badge_kinds((BADGE_UNREAD,))
                self._tabs[TAB_MEMORY].setVisible(False)   # shown by set_memory_visible(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        for index, tab in enumerate(self._tabs):
            tab.clicked.connect(lambda _checked=False, i=index: self.set_current(i))
            layout.addWidget(tab, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addStretch(1)
        self._current = TAB_JARVIS
        self._tabs[TAB_JARVIS].setChecked(True)
        self._compact = False
        self._tight = False
        self._auto_compact = True
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(self.HEIGHT)

    def tab(self, index: int) -> TabButton:
        return self._tabs[index]

    def has_live(self) -> bool:
        return len(self._tabs) > TAB_LIVE

    def has_memory(self) -> bool:
        return len(self._tabs) > TAB_MEMORY

    def current(self) -> int:
        return self._current

    def _available(self, index: int) -> bool:
        return 0 <= index < len(self._tabs) and not self._tabs[index].isHidden()

    def set_current(self, index: int) -> None:
        if index not in TABS or not self._available(index):
            return
        self._tabs[index].setChecked(True)   # auto-exclusive: the other one unchecks
        if index == self._current:
            return
        self._current = index
        if index == TAB_JARVIS:
            self.set_unread(False)
        elif index == TAB_LIVE:
            self.set_activity("")
        elif index == TAB_MEMORY:
            self.set_memory_unread(False)
        self.currentChanged.emit(index)

    def step(self, delta: int, *, focus: bool = False) -> None:
        """Left / Right: the neighbouring shown tab (it stays at the ends)."""
        shown = [index for index in range(len(self._tabs)) if self._available(index)]
        position = shown.index(self._current) if self._current in shown else 0
        index = shown[max(0, min(len(shown) - 1, position + delta))]
        self.set_current(index)
        if focus:
            self._tabs[index].setFocus(Qt.FocusReason.TabFocusReason)

    def set_live_visible(self, visible: bool) -> None:
        """Show or hide the LIVE tab ([live] enabled); hidden while current, JARVIS becomes current."""
        if not self.has_live():
            return
        tab = self._tabs[TAB_LIVE]
        tab.setVisible(bool(visible))
        if not visible:
            tab.set_activity("")
            if self._current == TAB_LIVE:
                self.set_current(TAB_JARVIS)
        self._fit_compact()

    def live_visible(self) -> bool:
        return self.has_live() and not self._tabs[TAB_LIVE].isHidden()

    def set_memory_visible(self, visible: bool) -> None:
        """Show or hide the MEMORY tab (the productivity bundle's memory); hidden while current,
        JARVIS becomes current."""
        if not self.has_memory():
            return
        tab = self._tabs[TAB_MEMORY]
        tab.setVisible(bool(visible))
        if not visible:
            tab.set_unread(False)
            if self._current == TAB_MEMORY:
                self.set_current(TAB_JARVIS)
        self._fit_compact()

    def memory_visible(self) -> bool:
        return self.has_memory() and not self._tabs[TAB_MEMORY].isHidden()

    def set_memory_unread(self, unread: bool) -> None:
        """The MEMORY tab's dot (a note saved or a suggestion arrived elsewhere); none while current."""
        if self.has_memory():
            self._tabs[TAB_MEMORY].set_unread(bool(unread) and self._current != TAB_MEMORY
                                              and self.memory_visible())

    def memory_unread(self) -> bool:
        return self.has_memory() and self._tabs[TAB_MEMORY].is_unread()

    def set_activity(self, state: str) -> None:
        """The LIVE tab's dot (ACTIVITY_UNREAD / ACTIVITY_WORKING / ""); none while LIVE is current."""
        if self.has_live():
            self._tabs[TAB_LIVE].set_activity(state if self._current != TAB_LIVE else "")

    def activity(self) -> str:
        return self._tabs[TAB_LIVE].activity() if self.has_live() else ""

    def natural_width(self) -> int:
        """The shown tabs' widths when not compact, each with its widest badge."""
        return sum(tab.natural_width() for tab in self._tabs if not tab.isHidden())

    def compact_width(self) -> int:
        """The shown tabs' widths when compact, each with its widest badge (narrower: tight)."""
        return sum(tab.compact_width() for tab in self._tabs if not tab.isHidden())

    def set_badge(self, new: bool) -> None:
        self._tabs[TAB_BRIEFING].set_new(new)

    def badge(self) -> bool:
        return self._tabs[TAB_BRIEFING].is_new()

    def set_unread(self, unread: bool) -> None:
        self._tabs[TAB_JARVIS].set_unread(bool(unread) and self._current != TAB_JARVIS)

    def unread(self) -> bool:
        return self._tabs[TAB_JARVIS].is_unread()

    def set_compact(self, compact: bool) -> None:
        """Narrow tabs by hand (no longer follows the width)."""
        self._auto_compact = False
        self._apply_compact(compact)

    def is_compact(self) -> bool:
        return self._compact

    def is_tight(self) -> bool:
        """Compact and narrower still: the shown tabs do not fit even compact (four tabs, MEMORY)."""
        return self._compact and self._tight

    def _apply_compact(self, compact: bool, tight: bool = False) -> None:
        self._compact = bool(compact)
        self._tight = bool(tight) and self._compact
        for tab in self._tabs:
            tab.set_compact(self._compact, tight=self._tight)

    def _fit_compact(self, width: int | None = None) -> None:
        if self._auto_compact:
            width = self.width() if width is None else width
            compact = width < max(self.COMPACT_BELOW, self.natural_width())
            self._apply_compact(compact, compact and width < self.compact_width())

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._fit_compact(event.size().width())


ROLE_JARVIS = "jarvis"
ROLE_YOU = "you"
_ROLE_TEXT = {ROLE_JARVIS: "JARVIS", ROLE_YOU: "YOU"}
_ROLE_COLORS = {ROLE_JARVIS: ACCENT, ROLE_YOU: TEXT_MUTED}
CONVERSATION_EMPTY_TEXT = "Jarvis's answers and what he has done appear here."
_LINK_NAME_CHARS = 120      # a link's accessible name: the entry's first line, at most this long


class _ConversationEntry(QWidget):
    """One entry: "JARVIS  2:41 PM", the text (plain, wrapped, never cut, selectable), an optional
    sub line and an optional link."""

    linkClicked = Signal(str)

    def __init__(self, entry_id: int, role: str, stamp: str, text: str, tone: str, sub: str,
                 link_text: str, link_id: str) -> None:
        super().__init__()
        self.entry_id = entry_id
        self.role = role if role in _ROLE_TEXT else ROLE_JARVIS
        self.stamp = stamp
        self._link_id = link_id
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self.role_label = make_label(_ROLE_TEXT[self.role], mono_font(10, 500, 0.12), _ROLE_COLORS[self.role])
        self.time_label = make_label(stamp, mono_font(10, 400, 0.04), TEXT_TIME)
        header.addWidget(self.role_label)
        header.addWidget(self.time_label)
        header.addStretch(1)
        self.text_label = _BreakableLabel(text, body_font(14), _TONE_COLORS.get(tone, TEXT_BODY))
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text_label.setCursor(Qt.CursorShape.IBeamCursor)
        self.text_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.sub_label = make_label(sub, mono_font(10, 400, 0.02), TEXT_SUB, wrap=True)
        self.sub_label.setVisible(bool(sub))
        self.link_button = HudButton(link_text, LINK)
        self.link_button.setVisible(bool(link_text))
        self.link_button.clicked.connect(lambda: self.linkClicked.emit(self._link_id))
        link_row = QHBoxLayout()
        link_row.setContentsMargins(0, 0, 0, 0)
        link_row.addWidget(self.link_button)
        link_row.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(header)
        layout.addWidget(self.text_label)
        layout.addWidget(self.sub_label)
        layout.addLayout(link_row)
        self._tone = tone
        self._update_accessible()

    @property
    def entry(self) -> tuple[str, str, str, str]:
        return (self.role, self.stamp, self.text_label.text(), self.sub_label.text())

    def tone(self) -> str:
        return self._tone

    def link(self) -> tuple[str, str]:
        return (self.link_button.text() if not self.link_button.isHidden() else "", self._link_id)

    def set_content(self, *, text: str | None = None, sub: str | None = None, tone: str | None = None,
                    link_text: str | None = None, link_id: str | None = None) -> None:
        if text is not None:
            self.text_label.setText(text)
        if tone is not None:
            self._tone = tone
            set_label_color(self.text_label, _TONE_COLORS.get(tone, TEXT_BODY))
        if sub is not None:
            self.sub_label.setText(sub)
            self.sub_label.setVisible(bool(sub))
        if link_id is not None:
            self._link_id = link_id
        if link_text is not None:
            self.link_button.setText(link_text)
            self.link_button.setVisible(bool(link_text))
        self._update_accessible()

    def _update_accessible(self) -> None:
        role, stamp, text, sub = self.entry
        self.setAccessibleName(f"{_ROLE_TEXT[role]} {stamp}: {text}" + (f" ({sub})" if sub else ""))
        # The link says what it acts on ("Prep this one: Project sync at 2:00 PM (work)"): several
        # entries may carry the same link words. The entry's first line only, capped: a brief's "See
        # every step" must not read the whole brief again (the entry itself already carries it).
        link = self.link_button.text()
        first = text.split("\n", 1)[0][:_LINK_NAME_CHARS]
        self.link_button.setAccessibleName(f"{link}: {first}" if link else "")


class ConversationLog(QScrollArea):
    """The JARVIS tab: the owner's asks and Jarvis's replies, announcements and results, oldest
    first. Every text is plain text, wrapped and shown whole (never elided); the area scrolls.

    ``add`` returns the entry's id for ``update`` (the greeting is updated once composed). At most
    ``MAX_ENTRIES`` are kept (the oldest go). Adding or updating scrolls to the newest entry unless
    the owner scrolled within the last ``SCROLL_HOLD_S`` seconds: to the bottom, or, when that entry
    is taller than the view, to its top (its "JARVIS  2:41 PM" header first), so a long reply is
    read from its first line and never shows up cut off at the top. ``follow_from(entry_id)`` does
    the same for a group of entries posted together (a question and its choices): while they do not
    all fit, the view starts at that entry, until the next ``add`` or the owner's scroll. Memory only.
    """

    linkClicked = Signal(str)
    MAX_ENTRIES = 100
    SCROLL_HOLD_S = 4.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        content = _transparent_scroll(self)
        self._layout = QVBoxLayout(content)
        self._layout.setContentsMargins(_CONVERSATION_MARGIN, 10, _CONVERSATION_MARGIN, 10)
        self._layout.setSpacing(14)
        self.empty_label = make_label(CONVERSATION_EMPTY_TEXT, body_font(13), TEXT_DIM, wrap=True)
        self._layout.addWidget(self.empty_label)
        self._layout.addStretch(1)
        self._entries: list[_ConversationEntry] = []
        self._next_id = 1
        self._hour24 = False
        self._user_scrolled_at = -math.inf
        self._follow = True
        self._anchor = 0     # follow_from: the entry a group is followed from (0: the newest entry alone)
        bar = self.verticalScrollBar()
        bar.actionTriggered.connect(self._on_user_scroll)
        bar.rangeChanged.connect(self._on_range_changed)

    def set_hour24(self, hour24: bool) -> None:
        """The clock of the entries added from now on."""
        self._hour24 = hour24

    def add(self, role: str, text: str, *, when: datetime | str | None = None, tone: str = TONE_DONE,
            sub: str = "", link_text: str = "", link_id: str = "") -> int:
        stamp = when if isinstance(when, str) else format_time(when or datetime.now(), hour24=self._hour24)
        entry = _ConversationEntry(self._next_id, role, stamp, text, tone, sub, link_text, link_id)
        self._next_id += 1
        entry.linkClicked.connect(self.linkClicked)
        self._layout.insertWidget(self._layout.count() - 1, entry)   # before the stretch
        self._entries.append(entry)
        while len(self._entries) > self.MAX_ENTRIES:
            old = self._entries.pop(0)
            self._layout.removeWidget(old)
            old.hide()
            old.deleteLater()
        self.empty_label.hide()
        self._anchor = 0
        self._follow_newest()
        return entry.entry_id

    def follow_from(self, entry_id: int) -> None:
        """Follow the entries from ``entry_id`` (a group posted together) as one: while they do not
        all fit the view, it starts at that entry's header instead of ending at the newest entry.
        Until the next ``add`` or the owner's scroll; a gone entry means the newest entry alone."""
        self._anchor = entry_id if self.entry(entry_id) is not None else 0
        self._follow_newest()

    def update(self, *args: Any, text: str | None = None, sub: str | None = None,  # type: ignore[override]
               tone: str | None = None, link_text: str | None = None, link_id: str | None = None) -> Any:
        """``update(entry_id, text=, sub=, tone=, link_text=, link_id=)`` changes an entry (False when
        it is gone: beyond MAX_ENTRIES, or cleared). Without an entry id it is QWidget.update()."""
        if len(args) != 1 or not isinstance(args[0], int) or isinstance(args[0], bool):
            return super().update(*args)
        entry = self.entry(args[0])
        if entry is None:
            return False
        entry.set_content(text=text, sub=sub, tone=tone, link_text=link_text, link_id=link_id)
        self._follow_newest()
        return True

    def entry(self, entry_id: int) -> _ConversationEntry | None:
        return next((entry for entry in self._entries if entry.entry_id == entry_id), None)

    def entry_widgets(self) -> list[_ConversationEntry]:
        return list(self._entries)

    def entries(self) -> list[tuple[str, str, str, str]]:
        """(role, time, text, sub), oldest first."""
        return [entry.entry for entry in self._entries]

    def clear(self) -> None:
        for entry in self._entries:
            self._layout.removeWidget(entry)
            entry.hide()
            entry.deleteLater()
        self._entries = []
        self._anchor = 0
        self.empty_label.show()

    def following(self) -> bool:
        """The view keeps the newest entry in sight."""
        return self._follow

    def scroll_to_newest(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(self._newest_target(bar.maximum()))

    def _newest_target(self, maximum: int) -> int:
        """Where following the newest entry scrolls to: the bottom, or the top of that entry (of the
        follow_from entry, for a group) when it does not fit in the view."""
        if not self._entries:
            return maximum
        newest = self._entries[-1]
        entry = (self.entry(self._anchor) if self._anchor else None) or newest
        height = newest.y() + newest.height() - entry.y()   # the entry, or the group from its anchor down
        # Not shown and laid out yet (just added): the bottom, as before; the range change that
        # follows its layout comes back here with its real place and height.
        if not newest.isVisibleTo(self) or newest.height() <= 0 \
                or height + 2 * _ENTRY_TOP_GAP <= self.viewport().height():
            return maximum
        return max(0, min(maximum, entry.y() - _ENTRY_TOP_GAP))

    def _follow_newest(self) -> None:
        if time.monotonic() - self._user_scrolled_at >= self.SCROLL_HOLD_S:
            self._follow = True
        if self._follow:
            self.scroll_to_newest()
            QTimer.singleShot(0, self._scroll_if_following)   # once the new entry is laid out

    def _scroll_if_following(self) -> None:
        if shiboken6.isValid(self) and self._follow:
            self.scroll_to_newest()

    def _on_range_changed(self, _minimum: int, maximum: int) -> None:
        if self._follow:
            self.verticalScrollBar().setValue(self._newest_target(maximum))

    def _on_user_scroll(self, _action: int) -> None:
        # actionTriggered fires for wheel, drag, clicks and keys, never for setValue().
        self._user_scrolled_at = time.monotonic()
        self._anchor = 0
        bar = self.verticalScrollBar()
        QTimer.singleShot(0, lambda: self._after_user_scroll(bar))

    def _after_user_scroll(self, bar: Any) -> None:
        if shiboken6.isValid(self) and shiboken6.isValid(bar):
            self._follow = bar.value() >= bar.maximum() - 2   # scrolled back to the bottom: follow again

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if self._follow:   # a taller or shorter view: the newest entry's start or the bottom again
            QTimer.singleShot(0, self._scroll_if_following)


_CONVERSATION_MARGIN = 14
_ENTRY_TOP_GAP = 6     # a long entry followed from its top keeps this much room above its header


# --------------------------------------------------------------------------
# MEMORY: the owner's notes kept on this PC (the productivity bundle)
# --------------------------------------------------------------------------

MEMORY_ADD_PLACEHOLDER = "Add a note..."
MEMORY_ADD_TEXT = "Add"
MEMORY_EDIT_TEXT = "Edit"
MEMORY_DELETE_TEXT = "Delete"
MEMORY_UNDO_TEXT = "Undo"
MEMORY_SAVE_TEXT = "Save"
MEMORY_DISMISS_TEXT = "Dismiss"
MEMORY_SHOW_ALL_TEXT = "Show all"
MEMORY_DELETED_TEXT = "Deleted."
MEMORY_NOT_SENT_TEXT = "NOT SENT"
MEMORY_NOT_SENT_TIP = "Past the 4 KB your Claude gets with each request: not sent until newer notes go"
MEMORY_SUGGESTS_TITLE = "Jarvis suggests"
MEMORY_NOTES_TITLE = "Your notes"
MEMORY_EMPTY_TEXT = 'No notes yet. Add one above, or type "remember that ..." in the bar.'
MEMORY_FILTER_TEXT = 'Showing notes with "{words}"'
MEMORY_READ_ONLY_TEXT = ("memory.json can't be read, so Jarvis won't change it. Fix or delete the file, then "
                         "restart Jarvis.")
MEMORY_NOT_SAVED_TEXT = "Not saved to disk - kept for this session only."
_MEMORY_EDIT_STYLE = ("QLineEdit {{ color: {text}; background: #030a10; border: 1px solid {border}; padding: 4px 6px;"
                      " selection-background-color: {selection}; selection-color: {bright}; }}"
                      " QLineEdit:focus {{ border: 1px solid {focus}; }}"
                      " QLineEdit:disabled {{ color: {dim}; }}")


@dataclass(frozen=True)
class MemoryRowInfo:
    """One note as MEMORY lists it (the controller's words; the page never reads memory.json)."""

    id: str
    text: str
    meta: str                    # "TOLD . OCT 10" (+ " . EDITED")
    sent: bool = True            # False: past the 4 KB prompt cap (NOT SENT)
    deleted: bool = False        # the "Deleted. Undo" placeholder for 10 s after Delete

    def __repr__(self) -> str:   # never the note
        return f"MemoryRowInfo({self.id}, {len(self.text)} chars, deleted={self.deleted})"


@dataclass(frozen=True)
class SuggestionRowInfo:
    """A note Jarvis suggested (pending: kept only on Save)."""

    id: int
    text: str

    def __repr__(self) -> str:
        return f"SuggestionRowInfo({self.id}, {len(self.text)} chars)"


def _memory_edit_style() -> str:
    return _MEMORY_EDIT_STYLE.format(text=TEXT_BODY, border=_qss_color(rgba(ACCENT, 0.35)),
                                     selection=_qss_color(rgba(ACCENT, 0.32)), bright=TEXT_BRIGHT, focus=ACCENT,
                                     dim=TEXT_DIM)


def _holds_focus(widget: QWidget) -> bool:
    """The keyboard focus is on ``widget`` or inside it."""
    focused = QApplication.focusWidget()
    return focused is not None and (focused is widget or widget.isAncestorOf(focused))


def _memory_line_edit(placeholder: str = "") -> QLineEdit:
    edit = QLineEdit()
    edit.setFont(body_font(13))
    edit.setPlaceholderText(placeholder)
    edit.setMaxLength(400)   # longer than any note: a long one is refused with its own words
    edit.setStyleSheet(_memory_edit_style())
    return edit


class _PlainWrapLabel(QLabel):
    """A word-wrapped plain-text label that may also break inside a long word (an address), with
    no memory of earlier texts (a reused row takes the height of its new text)."""

    def __init__(self, text: str, font: QFont, color: str | QColor) -> None:
        super().__init__()
        self._plain = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setFont(font)
        set_label_color(self, color)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - mirrors QLabel
        self._plain = text or ""
        self.setAccessibleName(self._plain)
        super().setText(_with_soft_breaks(self._plain))
        self.updateGeometry()

    def text(self) -> str:  # noqa: D102 - mirrors QLabel
        return self._plain


class _MemoryEdit(QLineEdit):
    """A row's inline edit field: Enter saves (``submitted``), Esc cancels (``cancelled``)."""

    submitted = Signal()
    cancelled = Signal()

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


class _MemoryRow(QWidget):
    """One note: its text (wrapped, selectable), then a meta line with Edit and Delete; while
    editing, a field (Enter saves, Esc cancels); after Delete, "Deleted." with Undo. Why an Edit or
    an Undo was not kept shows under the row (``set_refusal``). When a control the keyboard is on
    goes away (Enter or Esc in the field, Delete, Undo), the row's control that takes its place gets
    the focus, never a control of another note. Each button's accessible name says which note."""

    editRequested = Signal(str)
    editSubmitted = Signal(str, str)
    editCancelled = Signal(str)
    deleteRequested = Signal(str)
    undoRequested = Signal(str)

    def __init__(self, info: MemoryRowInfo) -> None:
        super().__init__()
        self._info = info
        self._editing = False
        self._read_only = False
        self._refusal = ""
        self.text_label = _PlainWrapLabel(info.text, body_font(13), TEXT_BODY)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.edit_field = _MemoryEdit()
        self.edit_field.setFont(body_font(13))
        self.edit_field.setMaxLength(400)
        self.edit_field.setStyleSheet(_memory_edit_style())
        self.edit_field.setAccessibleName("Change the note")
        self.edit_field.hide()
        self.edit_field.submitted.connect(lambda: self.editSubmitted.emit(self._info.id, self.edit_field.text()))
        self.edit_field.cancelled.connect(lambda: self.editCancelled.emit(self._info.id))
        self.meta_label = make_label(info.meta, mono_font(10, 400, 0.08), TEXT_DIM)
        self.not_sent_label = make_label(MEMORY_NOT_SENT_TEXT, mono_font(10, 500, 0.08), AMBER)
        self.not_sent_label.setToolTip(MEMORY_NOT_SENT_TIP)
        self.edit_button = HudButton(MEMORY_EDIT_TEXT, LINK, compact=True)
        self.delete_button = HudButton(MEMORY_DELETE_TEXT, LINK, compact=True)
        self.undo_button = HudButton(MEMORY_UNDO_TEXT, LINK, compact=True)
        self.edit_button.clicked.connect(lambda: self.editRequested.emit(self._info.id))
        self.delete_button.clicked.connect(lambda: self.deleteRequested.emit(self._info.id))
        self.undo_button.clicked.connect(lambda: self.undoRequested.emit(self._info.id))
        self.refusal_label = make_label("", body_font(12), AMBER, wrap=True, name="memoryRowRefusal")
        self.refusal_label.hide()
        meta = QWidget()
        flow = FlowLayout(meta, 10, 2)
        for widget in (self.meta_label, self.not_sent_label, self.edit_button, self.delete_button, self.undo_button):
            flow.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.text_label)
        layout.addWidget(self.edit_field)
        layout.addWidget(meta)
        layout.addWidget(self.refusal_label)
        self._apply()

    def info(self) -> MemoryRowInfo:
        return self._info

    def set_info(self, info: MemoryRowInfo) -> None:
        if info.id != self._info.id:
            self._editing = False
        if info.id != self._info.id or info.deleted != self._info.deleted:
            self._refusal = ""
        self._info = info
        self._apply()

    def editing(self) -> bool:
        return self._editing

    def set_editing(self, editing: bool, *, focus: bool = True) -> None:
        """Open the field with the note's text, all selected (``focus``: and the keyboard on it), or
        close it. Opening a field that is open keeps what the owner typed."""
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

    def _apply(self) -> None:
        info = self._info
        deleted = info.deleted
        self.text_label.setText(MEMORY_DELETED_TEXT if deleted else info.text)
        set_label_color(self.text_label, TEXT_DIM if deleted else TEXT_BODY)
        self.meta_label.setText(info.meta)
        self.meta_label.setVisible(not deleted)
        self.not_sent_label.setVisible(not deleted and not info.sent)
        self.refusal_label.setText(self._refusal)
        self.refusal_label.setVisible(bool(self._refusal))
        controls = {self.edit_field: self._editing,
                    self.edit_button: not deleted and not self._editing and not self._read_only,
                    self.delete_button: not deleted and not self._editing and not self._read_only,
                    self.undo_button: deleted and not self._read_only}
        held = next((widget for widget in controls if widget.hasFocus()), None)
        # The new controls first: hiding the one the keyboard is on hands the focus to the next
        # control in the chain, which must not be another note's.
        for widget, visible in controls.items():
            if visible:
                widget.setVisible(True)
        self.text_label.setVisible(not self._editing)
        for widget, visible in controls.items():
            if not visible:
                widget.setVisible(False)
        if held is not None and not controls[held]:
            target = next((widget for widget in (self.edit_button, self.undo_button, self.delete_button)
                           if controls[widget]), None)
            if target is not None:
                target.setFocus(Qt.FocusReason.OtherFocusReason)
        self.edit_button.setAccessibleName(f"{MEMORY_EDIT_TEXT} note: {info.text}")
        self.delete_button.setAccessibleName(f"{MEMORY_DELETE_TEXT} note: {info.text}")
        self.undo_button.setAccessibleName(f"{MEMORY_UNDO_TEXT} delete")
        self.setAccessibleName(MEMORY_DELETED_TEXT if deleted else f"Note: {info.text}")


class _SuggestionRow(QWidget):
    """A suggested note with Save and Dismiss."""

    saveRequested = Signal(int)
    dismissRequested = Signal(int)

    def __init__(self, info: SuggestionRowInfo) -> None:
        super().__init__()
        self._info = info
        self.text_label = _PlainWrapLabel(info.text, body_font(13), TEXT_SOFT)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.save_button = HudButton(MEMORY_SAVE_TEXT, LINK, compact=True)
        self.dismiss_button = HudButton(MEMORY_DISMISS_TEXT, LINK, compact=True)
        self.save_button.clicked.connect(lambda: self.saveRequested.emit(self._info.id))
        self.dismiss_button.clicked.connect(lambda: self.dismissRequested.emit(self._info.id))
        links = QWidget()
        flow = FlowLayout(links, 10, 2)
        flow.addWidget(self.save_button)
        flow.addWidget(self.dismiss_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.text_label)
        layout.addWidget(links)
        self._name()

    def info(self) -> SuggestionRowInfo:
        return self._info

    def set_info(self, info: SuggestionRowInfo) -> None:
        self._info = info
        self.text_label.setText(info.text)
        self._name()

    def _name(self) -> None:
        """Accessible names that say which suggestion a button acts on."""
        text = self._info.text
        self.setAccessibleName(f"Suggested note: {text}")
        self.save_button.setAccessibleName(f"{MEMORY_SAVE_TEXT} suggested note: {text}")
        self.dismiss_button.setAccessibleName(f"{MEMORY_DISMISS_TEXT} suggested note: {text}")


class MemoryPage(QScrollArea):
    """The MEMORY tab: the owner's notes, kept only on this PC (memory.json).

    A status line (where the notes are kept, who gets them, how many and how much of the 4 KB
    the planner gets), a banner while memory.json can't be read (nothing can change then) or a
    write failed, the "Add a note..." field (Enter or Add; ``added(text)``; a refusal shows under
    it, ``set_refusal``), JARVIS SUGGESTS (``set_suggestions``: Save / Dismiss), then the notes
    newest first (``set_rows``: text, TOLD / SUGGESTED and the date, NOT SENT past the 4 KB, Edit
    inline - Enter saves, Esc cancels - and Delete, after which the row reads "Deleted." with Undo
    while the controller keeps it). ``set_filter(words)`` shows 'Showing notes with "Ana"' and
    Show all (``filterCleared``). A plain view: the controller holds the notes and decides; the
    page never reads or writes a file. Each note keeps its row widget across refreshes (an open
    edit keeps its draft and focus); Tab goes through the page in the order it shows; a refused
    Edit or Undo says why under its note. The Add field stays usable while an Ask runs (the
    command bar is read-only then)."""

    added = Signal(str)
    edited = Signal(str, str)
    deleted = Signal(str)
    undoDelete = Signal(str)
    suggestionSaved = Signal(int)
    suggestionDismissed = Signal(int)
    filterCleared = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        content = _transparent_scroll(self)
        self._layout = QVBoxLayout(content)
        self._layout.setContentsMargins(_CONVERSATION_MARGIN, 10, _CONVERSATION_MARGIN, 10)
        self._layout.setSpacing(8)
        self.status_label = make_label("", mono_font(10, 400, 0.02), TEXT_SUB, wrap=True, name="memoryStatus")
        self.banner_label = make_label("", body_font(12), AMBER, wrap=True, name="memoryBanner")
        self.banner_label.hide()
        self.input = _memory_line_edit(MEMORY_ADD_PLACEHOLDER)
        self.input.setAccessibleName("Add a note to Jarvis's memory")
        self.input.returnPressed.connect(self._submit)
        self.add_button = HudButton(MEMORY_ADD_TEXT, SECONDARY, compact=True)
        self.add_button.clicked.connect(self._submit)
        add_row = QHBoxLayout()
        add_row.setContentsMargins(0, 0, 0, 0)
        add_row.setSpacing(8)
        add_row.addWidget(self.input, 1)
        add_row.addWidget(self.add_button, 0)
        self.refusal_label = make_label("", body_font(12), AMBER, wrap=True, name="memoryRefusal")
        self.refusal_label.hide()
        self.filter_label = make_label("", body_font(12), TEXT_SOFT, wrap=True, name="memoryFilter")
        self.show_all_button = HudButton(MEMORY_SHOW_ALL_TEXT, LINK, compact=True)
        self.show_all_button.clicked.connect(self._clear_filter)
        self._filter_box = QWidget()
        filter_layout = QVBoxLayout(self._filter_box)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.setSpacing(0)
        filter_layout.addWidget(self.filter_label)
        show_all_row = QHBoxLayout()
        show_all_row.setContentsMargins(0, 0, 0, 0)
        show_all_row.addWidget(self.show_all_button)
        show_all_row.addStretch(1)
        filter_layout.addLayout(show_all_row)
        self._filter_box.hide()
        self.suggests_title = make_label(MEMORY_SUGGESTS_TITLE.upper(), mono_font(10, 500, 0.14), ACCENT)
        self._suggestion_box = QWidget()
        self._suggestion_layout = QVBoxLayout(self._suggestion_box)
        self._suggestion_layout.setContentsMargins(0, 0, 0, 4)
        self._suggestion_layout.setSpacing(10)
        self.notes_title = make_label(MEMORY_NOTES_TITLE.upper(), mono_font(10, 500, 0.14), TEXT_MUTED)
        self._notes_box = QWidget()
        self._notes_layout = QVBoxLayout(self._notes_box)
        self._notes_layout.setContentsMargins(0, 0, 0, 0)
        self._notes_layout.setSpacing(12)
        self.empty_label = make_label(MEMORY_EMPTY_TEXT, body_font(13), TEXT_DIM, wrap=True, name="memoryEmpty")
        # The notes get the room of the short panel: the banner (when there is one) and the Add field
        # first, the status line (where the notes are kept, who gets them) last.
        self._layout.addWidget(self.banner_label)
        self._layout.addLayout(add_row)
        for widget in (self.refusal_label, self._filter_box, self.suggests_title, self._suggestion_box,
                       self.notes_title, self._notes_box, self.empty_label, self.status_label):
            self._layout.addWidget(widget)
        self._layout.addStretch(1)
        self._rows: list[_MemoryRow] = []
        self._suggestions: list[_SuggestionRow] = []
        self._read_only = False
        self._filter = ""
        self._editing = ""
        self.set_suggestions(())
        self.set_rows(())

    # ---- what the controller shows ----------------------------------------------------------------

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
        """memory.json can't be read: Add, Edit and Delete are off."""
        self._read_only = bool(read_only)
        self.input.setEnabled(not self._read_only)
        self.add_button.setEnabled(not self._read_only)
        for row in self._rows:
            row.set_read_only(self._read_only)
        for suggestion in self._suggestions:
            suggestion.save_button.setEnabled(not self._read_only)
        self.empty_label.setVisible(not self._rows and not self._filter and not self._read_only)
        if self._read_only:
            self._editing = ""

    def read_only(self) -> bool:
        return self._read_only

    def set_refusal(self, text: str, entry_id: str = "") -> None:
        """Why the last Add or Save was not kept, under the Add field ("" hides it); with
        ``entry_id``, why that note's Edit or Undo was not kept, under the note (scrolled into view)."""
        row = next((row for row in self._rows if row.info().id == entry_id), None) if entry_id else None
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
            row = next((row for row in self._rows if row.info().id == entry_id), None)
            return row.refusal() if row is not None else ""
        return self.refusal_label.text() if not self.refusal_label.isHidden() else ""

    def clear_input(self) -> None:
        self.input.clear()

    def set_filter(self, words: str) -> None:
        """'Showing notes with "<words>"' and Show all ("" hides it); the controller passes the
        matching rows."""
        self._filter = " ".join(str(words or "").split())
        self.filter_label.setText(MEMORY_FILTER_TEXT.format(words=self._filter) if self._filter else "")
        self._filter_box.setVisible(bool(self._filter))

    def filter_words(self) -> str:
        return self._filter

    def set_rows(self, rows: Sequence[MemoryRowInfo]) -> None:
        """The notes, newest first. Each note keeps its own row widget whatever moves around it (a
        note added above, a Deleted placeholder that goes), so an edit in progress keeps what the
        owner typed, its cursor and its focus. When the row the keyboard is on goes, the row now
        in its place takes the focus (never a field outside MEMORY's rows)."""
        rows = tuple(rows)
        focused = next((index for index, row in enumerate(self._rows) if _holds_focus(row)), None)
        old: dict[str, _MemoryRow] = {}
        for row in self._rows:
            old.setdefault(row.info().id, row)
        kept: list[_MemoryRow] = []
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
        self.notes_title.setVisible(bool(rows))
        self.empty_label.setVisible(not rows and not self._filter and not self._read_only)
        self._chain_tab_order()

    def rows(self) -> list[_MemoryRow]:
        return list(self._rows)

    def row_infos(self) -> list[MemoryRowInfo]:
        return [row.info() for row in self._rows]

    def set_suggestions(self, rows: Sequence[SuggestionRowInfo]) -> None:
        rows = tuple(rows)
        _reuse_rows(self._suggestions, self._suggestion_layout, rows, self._make_suggestion)
        self.suggests_title.setVisible(bool(rows))
        self._suggestion_box.setVisible(bool(rows))
        self._chain_tab_order()

    def _chain_tab_order(self) -> None:
        """Tab goes through MEMORY in the order it shows: Add, Show all, each suggestion's Save and
        Dismiss, then each note's field and links (rows made later would otherwise come last in
        the window's focus chain, after everything built after the page)."""
        chain: list[QWidget] = [self.input, self.add_button, self.show_all_button]
        for suggestion in self._suggestions:
            chain += [suggestion.save_button, suggestion.dismiss_button]
        for row in self._rows:
            chain += [row.edit_field, row.edit_button, row.delete_button, row.undo_button]
        for first, second in zip(chain, chain[1:]):
            QWidget.setTabOrder(first, second)

    def suggestion_rows(self) -> list[_SuggestionRow]:
        return list(self._suggestions)

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
        """Scroll ``widget`` (a row or its field) into view once the layout has placed it."""
        def scroll() -> None:
            if not (shiboken6.isValid(self) and shiboken6.isValid(widget) and widget.isVisibleTo(self)):
                return
            # Place the rows (and the field inside its row) now: a field just shown has no place yet.
            for holder in (self.widget(), widget.parentWidget()):
                layout = holder.layout() if holder is not None else None
                if layout is not None:
                    layout.activate()
            # A field's own row (a QLineEdit would only bring its text cursor into view).
            parent = widget.parentWidget()
            target = parent if isinstance(parent, (_MemoryRow, _SuggestionRow)) else widget
            self.ensureWidgetVisible(target, 0, 8)

        scroll()
        QTimer.singleShot(0, scroll)

    def editing(self) -> str:
        return self._editing

    def stop_editing(self) -> None:
        self._editing = ""
        for row in self._rows:
            row.set_editing(False)

    # ---- the owner's clicks ---------------------------------------------------------------------

    def deleted_row(self) -> _MemoryRow | None:
        return next((row for row in self._rows if row.info().deleted), None)

    def _make_row(self, info: MemoryRowInfo) -> _MemoryRow:
        row = _MemoryRow(info)
        row.set_read_only(self._read_only)
        row.editRequested.connect(self.edit_row)
        row.editSubmitted.connect(self.edited)
        row.editCancelled.connect(lambda _entry_id: self.stop_editing())
        row.deleteRequested.connect(self.deleted)
        row.undoRequested.connect(self.undoDelete)
        return row

    def _make_suggestion(self, info: SuggestionRowInfo) -> _SuggestionRow:
        row = _SuggestionRow(info)
        row.save_button.setEnabled(not self._read_only)
        row.saveRequested.connect(self.suggestionSaved)
        row.dismissRequested.connect(self.suggestionDismissed)
        return row

    def _submit(self) -> None:
        text = " ".join(self.input.text().split())
        if text and not self._read_only:
            self.added.emit(text)

    def _clear_filter(self) -> None:
        self.set_filter("")
        self.filterCleared.emit()


# --------------------------------------------------------------------------
# LIVE view: every step Jarvis takes, as it happens (live.py's stream)
# --------------------------------------------------------------------------

LIVE_EMPTY_TEXT = ("Nothing yet. When Jarvis works on something - an Ask, a card you approved - every step shows "
                   "here as it happens: what he read, what he gave the planner, what he will send. On screen only; "
                   "never saved.")
LIVE_UNTRUSTED_TEXT = "Written by other people - shown as data, never followed"
LIVE_NOT_KEPT_TEXT = "{chars} characters - not kept ([live] text = false)"
LIVE_CUT_TEXT = "[{cut} more characters not shown]"
LIVE_TAG_COLORS = {
    live_events.TASK_ASK: PINK, live_events.TASK_ACTION: AMBER, live_events.TASK_BRIEFING: TEXT_MUTED,
    live_events.TASK_BACKGROUND: TEXT_DIM, live_events.TASK_WEB: ACCENT, live_events.TASK_MEMORY: GREEN,
    live_events.TASK_PLAN: LAVENDER, live_events.TASK_PREP: PEACH,
}
_LIVE_BORDERS = {
    live_events.STATUS_RUNNING: rgba(ACCENT, 0.45), live_events.STATUS_OK: rgba(ACCENT, 0.18),
    live_events.STATUS_WARN: rgba(AMBER, 0.45), live_events.STATUS_BLOCKED: rgba(AMBER, 0.45),
    live_events.STATUS_FAILED: rgba(RED_LINE, 0.5), live_events.STATUS_CANCELLED: rgba(OFF, 0.5),
}
LIVE_TEXT_MAX_PX = 320          # an open text block grows to this height, then scrolls
LIVE_FIELD_LABEL_PX = 104       # the field labels' column ...
LIVE_FIELDS_WIDE_PX = 360       # ... when the details are at least this wide
LIVE_FIELDS_MEDIUM_PX = 200     # narrower down to this: a column as wide as the longest label word (else above)
LIVE_FIELD_LABEL_MIN_PX = 76    # ... but at least this wide ("MAY ASK TO" on one line)
LIVE_PILL_WRAP_PX = 320         # a task header narrower than this puts its status pill under the title
LIVE_CONTENT_MAX_PX = 960       # the log's column is at most this wide (centred in a wider view)
_LIVE_INDENT = 22               # step titles and details start here (after the glyph column)


def _count(count: int, word: str, plural: str = "") -> str:
    return f"{count:,} {word}" if count == 1 else f"{count:,} {plural or word + 's'}"


def _when_text(moment: datetime, hour24: bool) -> str:
    """"2:41:07 PM" / "14:41:07" (format_time with seconds)."""
    try:
        text = format_time(moment, hour24=hour24)
    except Exception:  # noqa: BLE001 - a broken time shows as nothing
        return ""
    digits, _space, meridiem = text.partition(" ")
    digits = f"{digits}:{moment.second:02d}"
    return f"{digits} {meridiem}" if meridiem else digits


def _set_text_color(widget: QWidget, color: str | QColor) -> None:
    """The widget's text colour through its palette (cheaper than a widget stylesheet)."""
    palette = widget.palette()
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        palette.setColor(group, QPalette.ColorRole.WindowText, QColor(color))
        palette.setColor(group, QPalette.ColorRole.Text, QColor(color))
    widget.setPalette(palette)


def _live_label(text: str, font: QFont, color: str | QColor, *, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)
    label.setFont(font)
    _set_text_color(label, color)
    return label


class _LiveFonts:
    """The LIVE view's fonts, made once (after the bundled fonts are loaded)."""

    _instance: _LiveFonts | None = None

    def __init__(self) -> None:
        self.task_title = body_font(13, 500)
        self.step_title = body_font(13)
        self.summary = mono_font(10)
        self.word = mono_font(10, 500, 0.06)
        self.time = mono_font(10, 400, 0.02)
        self.tag = mono_font(9, 700, 0.06)
        self.label = mono_font(10, 400, 0.08)
        self.value = body_font(12)
        self.value_mono = mono_font(11)
        self.note = mono_font(10)
        self.link = mono_font(11, 400, 0.04)
        self.link_underline = mono_font(11, 400, 0.04)
        self.link_underline.setUnderline(True)
        self.text = mono_font(11)
        # The pictures (email, calendar change, week strip): drawn at the size they are shown.
        self.pic_tag = mono_font(9, 700, 0.06)
        self.pic_label = mono_font(10, 400, 0.08)
        self.pic_value = body_font(12)
        self.pic_subject = body_font(13, 600)
        self.pic_body = mono_font(11)
        self.pic_small = mono_font(10)
        self.pic_tiny = mono_font(9)
        self.pic_head = display_font(13, 600)
        self.pic_headline = body_font(13, 500)
        self.pic_block = body_font(11, 500)
        self.pic_block_struck = body_font(11, 500)
        self.pic_block_struck.setStrikeOut(True)
        self.pic_sender = body_font(11, 500)

    @classmethod
    def get(cls) -> _LiveFonts:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance


class _TextLines:
    """``text`` laid out in lines ``width`` px wide (breaks at spaces, else anywhere: a long address
    or id wraps instead of running out of the view), at most ``max_lines`` lines (the last one
    elided), in the _LiveFonts font called ``font_name``. ``height``, ``line_count``, ``shown``,
    ``natural_width`` and ``draw(painter, point)`` (in the painter's pen colour).

    Only plain Python data is kept (each line's text and place): no QTextLayout or QFont outlives
    the measuring, so nothing of Qt's font cache is ever freed by a garbage collection that runs
    on another thread."""

    __slots__ = ("font_name", "height", "line_count", "lines", "natural_width", "shown")

    def __init__(self, text: str, font_name: str, width: float, max_lines: int = 0) -> None:
        font = getattr(_LiveFonts.get(), font_name)
        width = max(1.0, float(width))
        text = (text or "").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        lines, height = self._lay_out(text, font, width)
        if max_lines and len(lines) > max_lines:
            begin = lines[max_lines - 1][0]
            tail = QFontMetricsF(font).elidedText(text[begin:], Qt.TextElideMode.ElideRight, width)
            text = text[:begin] + tail
            lines, height = self._lay_out(text, font, width)
        self.font_name = font_name
        self.shown = text
        self.lines = [(text[begin:begin + length], y, ascent) for begin, length, y, ascent, _width in lines]
        self.natural_width = max((line[4] for line in lines), default=0.0)
        self.height = height
        self.line_count = len(lines)

    @staticmethod
    def _lay_out(text: str, font: QFont, width: float) -> tuple[list[tuple[int, int, float, float, float]], float]:
        layout = QTextLayout(text, font)
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        spacing = QFontMetricsF(font).lineSpacing()
        lines: list[tuple[int, int, float, float, float]] = []
        y = 0.0
        layout.beginLayout()
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            line.setPosition(QPointF(0, y))
            lines.append((line.textStart(), line.textLength(), y, line.ascent(), line.naturalTextWidth()))
            y += max(line.height(), spacing)
        layout.endLayout()
        return lines, y

    def draw(self, painter: QPainter, point: QPointF) -> None:
        painter.setFont(getattr(_LiveFonts.get(), self.font_name))
        for segment, y, ascent in self.lines:
            painter.drawText(QPointF(point.x(), point.y() + y + ascent), segment)


def _paint_pill(painter: QPainter, rect: QRectF, text: str, font: QFont, color: str | QColor, *,
                filled: bool, glyph: str = "", glyph_opacity: float = 1.0) -> None:
    """A small chamfered pill: ``filled`` (the tag: ink on ``color``) or outlined (the status:
    ``color`` text on a faint fill, with the status glyph first)."""
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if filled:
            painter.fillPath(chamfer_path(rect, 3), QColor(color))
            painter.setPen(QColor(GROUND))
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
            return
        painter.fillPath(chamfer_path(rect, 4), rgba(color, 0.08))
        painter.setPen(QPen(rgba(color, 0.45), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(chamfer_path(rect.adjusted(0.5, 0.5, -0.5, -0.5), 4))
        x = rect.left() + 6
        if glyph:
            paint_status_glyph(painter, QRectF(x, rect.center().y() - 4, 8, 8), glyph, glyph_opacity)
            x += 8 + 5
        painter.setFont(font)
        painter.setPen(QColor(color))
        painter.drawText(QRectF(x, rect.top(), rect.right() - x - 4, rect.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
    finally:
        painter.restore()


def _paint_caret(painter: QPainter, center: QPointF, open_: bool, color: str | QColor) -> None:
    """A small chevron: pointing down when open, right when closed."""
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(color), 1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        x, y = center.x(), center.y()
        if open_:
            points = [QPointF(x - 3.5, y - 1.8), QPointF(x, y + 1.8), QPointF(x + 3.5, y - 1.8)]
        else:
            points = [QPointF(x - 1.8, y - 3.5), QPointF(x + 1.8, y), QPointF(x - 1.8, y + 3.5)]
        painter.drawPolyline(QPolygonF(points))
    finally:
        painter.restore()


class _LiveButton(QAbstractButton):
    """Base of the LIVE view's painted buttons (task headers, step rows, links): Tab focus with a
    ring for keyboard focus only, hover, Space and Enter both click, and a height for any width
    (``_height_for``; the layouts give them the whole width)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self._focus_visible = False

    def _height_for(self, width: int) -> int:
        raise NotImplementedError

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        return self._height_for(max(1, width))

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        width = self.width() if self.width() > 0 else 300
        return QSize(240, self._height_for(width))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(60, 16)

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.isAutoRepeat():
            event.accept()
            self.click()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._focus_visible = _focus_ring_after(event.reason(), self._focus_visible)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().focusOutEvent(event)
        self.update()

    def enterEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().leaveEvent(event)
        self.update()

    def focus_ring(self) -> bool:
        return self.hasFocus() and self._focus_visible


class _WrapLink(_LiveButton):
    """A LIVE link (a text block's Show / Hide, a result link such as "Open the thread in Gmail"):
    mono 11 in accent, wrapped over as many lines as it needs, underlined on hover and keyboard
    focus. Only its text is clickable."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cache: dict[tuple[str, int, bool], _TextLines] = {}
        self.setText(text)
        self.setAccessibleName(text)

    def set_text(self, text: str) -> None:
        if text != self.text():
            self.setText(text)
            self.setAccessibleName(text)
            self._cache.clear()
            self.updateGeometry()
            self.update()

    def _lines(self, width: int, underline: bool = False) -> _TextLines:
        key = (self.text(), width, underline)
        lines = self._cache.get(key)
        if lines is None:
            if len(self._cache) > 16:
                self._cache.clear()
            lines = self._cache[key] = _TextLines(self.text(), "link_underline" if underline else "link", width)
        return lines

    def _height_for(self, width: int) -> int:
        return math.ceil(self._lines(width).height) + 4

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        width = min(math.ceil(_text_advance(_LiveFonts.get().link, self.text())) + 2, 420)
        return QSize(width, self._height_for(width))

    def hitButton(self, pos: QPoint) -> bool:  # noqa: N802 - Qt override
        lines = self._lines(max(1, self.width()))
        return 0 <= pos.y() <= self.height() and 0 <= pos.x() <= lines.natural_width + 6

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            active = self.isEnabled() and (self.underMouse() or self.isDown())
            lines = self._lines(max(1, self.width()), underline=active or self.focus_ring())
            painter.setPen(QColor(TEXT_BRIGHT if active else ACCENT if self.isEnabled() else TEXT_DIM))
            lines.draw(painter, QPointF(0, 2))
        finally:
            painter.end()


class _LiveValue(QLabel):
    """A field value or the notes: plain text, wrapped (long words may break: zero-width break
    chances are added for display), selectable by mouse and keyboard. Copying gives the exact
    text, never the break chances (Ctrl+C and the context menu's Copy)."""

    def __init__(self, text: str, font: QFont, color: str | QColor) -> None:
        super().__init__()
        self._plain = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setFont(font)
        _set_text_color(self, color)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                     | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.set_text(text)

    def set_text(self, text: str) -> None:
        text = text or ""
        if text != self._plain:
            self._plain = text
            self.setText(_with_soft_breaks(text, 0) if len(text) > _LONGEST_PIECE else text)
            self.setAccessibleName(text)

    def plain_text(self) -> str:
        return self._plain

    def copy_selection(self) -> None:
        selected = self.selectedText().replace(_SOFT_BREAK, "")
        if selected:
            QApplication.clipboard().setText(selected)

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        menu = QMenu(self)
        copy = menu.addAction("Copy")
        copy.setEnabled(self.hasSelectedText())
        copy.triggered.connect(self.copy_selection)
        menu.addAction("Select All").triggered.connect(lambda: self.setSelection(0, len(self.text())))
        menu.exec(event.globalPos())
        menu.deleteLater()


LIVE_PICTURE_LINK_WORDS = "shows that picture larger, or its step while it has none"


def _link_name(item: live_events.Field) -> str:
    """A link field's accessible name: its label and value (the label is a separate, plain label),
    and for a "picture:<step id>" link what pressing it does."""
    name = f"{item.label}: {item.value}" if item.label else item.value
    if item.link.startswith(live_events.PICTURE_LINK):
        name += f" - {LIVE_PICTURE_LINK_WORDS}"
    return name


class _LiveFields(QWidget):
    """A step's fields: "LABEL  value" rows; a value with a link is a _WrapLink (its accessible name
    says its label too: _link_name). Two columns (the
    label column LIVE_FIELD_LABEL_PX wide) when at least LIVE_FIELDS_WIDE_PX wide; narrower, down to
    LIVE_FIELDS_MEDIUM_PX, still two columns with the label column as wide as its longest word (a
    short LIVE tab shows more rows); narrower still each label above its value. It places its
    children itself (no layout object), and is as tall as they need at its width (heightForWidth)."""

    _GAP = 10
    _ROW_GAP = 6
    _NARROW_GAP = 8
    _NARROW_ROW_GAP = 4

    def __init__(self, on_link: Callable[[str, QWidget | None], None]) -> None:
        super().__init__()
        self._on_link = on_link
        self._fields: tuple[live_events.Field, ...] = ()
        self._widgets: list[QWidget] = []
        self._heights: dict[int, int] = {}
        self._narrow_label = LIVE_FIELD_LABEL_MIN_PX
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def fields(self) -> tuple[live_events.Field, ...]:
        return self._fields

    def _fit_narrow_label(self) -> None:
        """The narrow two-column layout's label column: the longest word of any label fits whole."""
        font = _LiveFonts.get().label
        words = [word for item in self._fields for word in item.label.upper().split()]
        widest = max((_text_advance(font, word) for word in words), default=0.0)
        self._narrow_label = int(min(LIVE_FIELD_LABEL_PX, max(LIVE_FIELD_LABEL_MIN_PX, math.ceil(widest) + 4)))

    def set_fields(self, fields: Sequence[live_events.Field]) -> bool:
        """Show ``fields``; True when focusable widgets came or went."""
        fields = tuple(fields)
        if fields == self._fields:
            return False
        fonts = _LiveFonts.get()
        self._heights.clear()
        if len(fields) == len(self._fields) and all(
                (old.label, bool(old.link), old.mono) == (new.label, bool(new.link), new.mono)
                for old, new in zip(self._fields, fields)):
            for index, (old, new) in enumerate(zip(self._fields, fields)):   # same rows: new values only
                if old != new:
                    value = self._widgets[2 * index + 1]
                    value.set_text(new.value)   # type: ignore[attr-defined]
                    if isinstance(value, _WrapLink):
                        value.setProperty("live_link", new.link)
                        value.setAccessibleName(_link_name(new))
            self._fields = fields
            self._fit_narrow_label()
            self._arrange()
            self.updateGeometry()
            return False
        for widget in self._widgets:
            widget.hide()
            widget.deleteLater()
        self._widgets = []
        for item in fields:
            label = _live_label(item.label.upper(), fonts.label, TEXT_DIM)
            if item.link:
                value: QWidget = _WrapLink(item.value)
                value.setAccessibleName(_link_name(item))
                value.setProperty("live_link", item.link)
                value.clicked.connect(lambda _checked=False, button=value: self._clicked(button))
            else:
                value = _LiveValue(item.value, fonts.value_mono if item.mono else fonts.value, TEXT_BODY)
            for widget in (label, value):
                widget.setParent(self)
                widget.show()
                self._widgets.append(widget)
        self._fields = fields
        self._fit_narrow_label()
        self._arrange()
        self.updateGeometry()
        return True

    def _clicked(self, button: QWidget) -> None:
        link = button.property("live_link")
        if isinstance(link, str) and link:
            self._on_link(link, button)

    def value_widget(self, label: str) -> QWidget | None:
        for index, item in enumerate(self._fields):
            if item.label == label:
                return self._widgets[2 * index + 1]
        return None

    def label_widgets(self) -> list[QLabel]:
        return [widget for widget in self._widgets[0::2] if isinstance(widget, QLabel)]

    def links(self) -> list[_WrapLink]:
        return [widget for widget in self._widgets if isinstance(widget, _WrapLink)]

    @staticmethod
    def wide(width: int) -> bool:
        """Two columns at ``width`` (either tier)."""
        return width >= LIVE_FIELDS_MEDIUM_PX

    def is_wide(self) -> bool:
        return self.wide(self.width())

    def label_column(self, width: int) -> int:
        """The label column's width at ``width`` (0: each label above its value)."""
        if width >= LIVE_FIELDS_WIDE_PX:
            return LIVE_FIELD_LABEL_PX
        return self._narrow_label if width >= LIVE_FIELDS_MEDIUM_PX else 0

    @staticmethod
    def _height(widget: QWidget, width: int) -> int:
        height = widget.heightForWidth(width) if widget.hasHeightForWidth() else -1
        return height if height >= 0 else widget.sizeHint().height()

    def _layout(self, width: int, *, apply: bool) -> int:
        y, width = 0, max(1, width)
        column = self.label_column(width)
        narrow = width < LIVE_FIELDS_WIDE_PX
        gap = self._NARROW_GAP if narrow else self._GAP
        row_gap = self._NARROW_ROW_GAP if narrow and column else self._ROW_GAP
        pairs = list(zip(self._widgets[0::2], self._widgets[1::2]))
        for index, (label, value) in enumerate(pairs):
            if index:
                y += row_gap
            if column:
                value_width = max(1, width - column - gap)
                label_height = self._height(label, column)
                value_height = self._height(value, value_width)
                if apply:
                    label.setGeometry(QRect(0, y + 1, column, label_height))
                    value.setGeometry(QRect(column + gap, y, value_width, value_height))
                y += max(label_height + 1, value_height)
            else:
                label_height = self._height(label, width)
                value_height = self._height(value, width)
                if apply:
                    label.setGeometry(QRect(0, y, width, label_height))
                    value.setGeometry(QRect(0, y + label_height + 1, width, value_height))
                y += label_height + 1 + value_height
        return y

    def _arrange(self) -> None:
        if self.width() > 0:
            self._layout(self.width(), apply=True)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._layout(event.size().width(), apply=True)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        height = self._heights.get(width)
        if height is None:
            if len(self._heights) > 8:
                self._heights.clear()
            height = self._heights[width] = self._layout(width, apply=False)
        return height

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(240, self.heightForWidth(self.width() if self.width() > 0 else 240))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(40, 0)


class _LiveItems(QWidget):
    """A step's items, painted: each with its small status glyph, its text (wrapped) and a note line
    (the status word and the note: amber for WARN / BLOCKED, red for FAILED); "N more not listed"
    after the last."""

    _TEXT_X = 16
    _GAP = 5

    def __init__(self) -> None:
        super().__init__()
        self._items: tuple[live_events.Item, ...] = ()
        self._more = 0
        self._cache: dict[int, tuple[list[tuple[float, _TextLines, _TextLines | None]], float]] = {}
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def items(self) -> tuple[live_events.Item, ...]:
        return self._items

    def set_items(self, items: Sequence[live_events.Item], more: int) -> None:
        items = tuple(items)
        if (items, more) == (self._items, self._more):
            return
        self._items, self._more = items, int(more)
        self._cache.clear()
        texts = [item.text + (f" ({item.status_text})" if item.status_text else "") + (f": {item.note}" if item.note else "")
                 for item in items]
        name = "; ".join(texts[:20]) + (f"; {_count(len(items) - 20 + more, 'more')}" if len(items) > 20 or more else "")
        self.setAccessibleName(name)
        self.updateGeometry()
        self.update()

    def _more_text(self) -> str:
        return f"{self._more:,} more not listed" if self._more else ""

    def _laid_out(self, width: int) -> tuple[list[tuple[float, _TextLines, _TextLines | None]], float]:
        cached = self._cache.get(width)
        if cached is not None:
            return cached
        if len(self._cache) > 8:
            self._cache.clear()
        fonts = _LiveFonts.get()
        text_width = max(1, width - self._TEXT_X)
        rows: list[tuple[float, _TextLines, _TextLines | None]] = []
        y = 0.0
        for index, item in enumerate(self._items):
            if index:
                y += self._GAP
            text = _TextLines(item.text, "value_mono" if item.mono else "value", text_width)
            note_text = "  ".join(part for part in (item.status_text, item.note) if part)
            note = _TextLines(note_text, "note", text_width) if note_text else None
            rows.append((y, text, note))
            y += text.height + (1 + note.height if note is not None else 0)
        if self._more:
            y += self._GAP + QFontMetricsF(fonts.note).lineSpacing()
        result = (rows, y)
        self._cache[width] = result
        return result

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        return math.ceil(self._laid_out(max(1, width))[1])

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(240, self.heightForWidth(self.width() if self.width() > 0 else 240))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(40, 0)

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        rows, height = self._laid_out(max(1, self.width()))
        exposed = QRectF(event.rect())
        painter = QPainter(self)
        try:
            for (y, text, note), item in zip(rows, self._items):
                bottom = y + text.height + (1 + note.height if note is not None else 0)
                if bottom < exposed.top() or y > exposed.bottom():
                    continue
                if item.status:
                    first = QFontMetricsF(_LiveFonts.get().value).lineSpacing()
                    paint_status_glyph(painter, QRectF(0, y + (first - 9) / 2, 9, 9), item.status)
                painter.setPen(QColor(TEXT_BODY))
                text.draw(painter, QPointF(self._TEXT_X, y))
                if note is not None:
                    color = (AMBER if item.status in (live_events.STATUS_WARN, live_events.STATUS_BLOCKED)
                             else RED if item.status == live_events.STATUS_FAILED else TEXT_SUB)
                    painter.setPen(QColor(color))
                    note.draw(painter, QPointF(self._TEXT_X, y + text.height + 1))
            if self._more:
                fonts = _LiveFonts.get()
                painter.setFont(fonts.note)
                painter.setPen(QColor(TEXT_DIM))
                line = QFontMetricsF(fonts.note).lineSpacing()
                painter.drawText(QRectF(self._TEXT_X, height - line, self.width() - self._TEXT_X, line),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._more_text())
        finally:
            painter.end()


class _LiveText(QPlainTextEdit):
    """An open text block: the exact text (thread text, the planner's stdin and reply, an outgoing
    body), read only and plain text only (nothing is a link, nothing loads), mono 11, wrapped at
    the widget's width (a long token wraps anywhere), as tall as its text up to LIVE_TEXT_MAX_PX,
    then it scrolls. Tab leaves it; its context menu has Copy and Select All only."""

    _MEASURE_CHARS = 6000    # a longer text is always at least LIVE_TEXT_MAX_PX tall
    _PAD = 2

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = ""
        self._measured_width = -1
        self._fit_pending = False
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setTabChangesFocus(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                     | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFont(_LiveFonts.get().text)
        self.document().setDocumentMargin(5)
        self.setStyleSheet(
            "QPlainTextEdit { color: %s; background: %s; border: 1px solid %s; padding: %dpx; "
            "selection-background-color: %s; selection-color: %s; }"
            % (_qss_color(TEXT_SOFT), _qss_color(rgba(ACCENT, 0.03)), _qss_color(rgba(ACCENT, 0.12)), self._PAD,
               _qss_color(rgba(ACCENT, 0.32)), _qss_color(TEXT_BRIGHT)))
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(60)

    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        """Show ``text`` (only when it changed: a block's text is set once per revision)."""
        if text == self._text:
            return
        self._text = text
        self.setPlainText(text)
        self._measured_width = -1
        self._fit_height()

    def _fit_height(self) -> None:
        width = self.viewport().width()
        if width == self._measured_width:
            return
        self._measured_width = width
        chrome = self.height() - self.viewport().height()
        if len(self._text) > self._MEASURE_CHARS:
            height = LIVE_TEXT_MAX_PX
        else:
            total = 2 * self.document().documentMargin()
            block = self.document().firstBlock()
            while block.isValid() and total <= LIVE_TEXT_MAX_PX:
                total += self.blockBoundingRect(block).height()
                block = block.next()
            height = min(LIVE_TEXT_MAX_PX, math.ceil(total + chrome) + 1)
        height = max(height, 28)
        if height != self.height():
            self.setFixedHeight(height)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width() and not self._fit_pending:
            # Not from inside the resize: a new fixed height there would leave the viewport at
            # the old size.
            self._fit_pending = True
            QTimer.singleShot(0, self._fit_later)

    def _fit_later(self) -> None:
        if shiboken6.isValid(self):
            self._fit_pending = False
            self._fit_height()

    def contextMenuEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        menu = QMenu(self)
        copy = menu.addAction("Copy")
        copy.setEnabled(self.textCursor().hasSelection())
        copy.triggered.connect(self.copy)
        menu.addAction("Select All").triggered.connect(self.selectAll)
        menu.exec(event.globalPos())
        menu.deleteLater()


def _block_label(label: str) -> str:
    """"Text given to the planner" -> "text given to the planner" (an acronym keeps its case)."""
    if len(label) > 1 and label[0].isupper() and label[1].islower():
        return label[0].lower() + label[1:]
    return label


class _LiveBlock(QWidget):
    """A collapsible text block: "Show <label> - 5,812 characters" / "Hide <label>"; open, the
    exact text (_LiveText, made on the first open) with "Written by other people - shown as
    data, never followed" above an untrusted one and "[N more characters not shown]" under a
    cut one; a block kept as its size only says so."""

    def __init__(self, block: live_events.Block) -> None:
        super().__init__()
        fonts = _LiveFonts.get()
        self._block = block
        self._open = bool(block.start_open)
        self.toggle = _WrapLink("")
        self.toggle.clicked.connect(self.flip)
        self.not_kept = _live_label("", fonts.note, TEXT_DIM)
        self.caption = _live_label(LIVE_UNTRUSTED_TEXT, fonts.note, AMBER_META)
        self.cut_label = _live_label("", fonts.note, TEXT_DIM)
        self.text_edit: _LiveText | None = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(3)
        for widget in (self.toggle, self.not_kept, self.caption, self.cut_label):
            self._layout.addWidget(widget)
        self.set_block(block)

    def block(self) -> live_events.Block:
        return self._block

    def is_open(self) -> bool:
        return self._open and self._block.kept

    def flip(self) -> None:
        self._open = not self._open
        self.set_block(self._block)
        log = self._log()
        if log is not None:
            log._structure_changed()

    def _log(self) -> LiveLog | None:
        widget = self.parentWidget()
        while widget is not None and not isinstance(widget, LiveLog):
            widget = widget.parentWidget()
        return widget

    def set_block(self, block: live_events.Block) -> None:
        self._block = block
        name = _block_label(block.label)
        if not block.kept:
            self.toggle.hide()
            self.not_kept.setText(f"{block.label}: " + LIVE_NOT_KEPT_TEXT.format(chars=f"{block.chars:,}"))
            self.not_kept.show()
            for widget in (self.caption, self.cut_label, self.text_edit):
                if widget is not None:
                    widget.hide()
            return
        self.not_kept.hide()
        self.toggle.show()
        if self._open:
            self.toggle.set_text(f"Hide {name}")
            if self.text_edit is None:
                self.text_edit = _LiveText()
                self._layout.insertWidget(self._layout.indexOf(self.cut_label), self.text_edit)
            self.text_edit.set_text(block.text)
            self.text_edit.show()
            self.caption.setVisible(block.untrusted)
            self.cut_label.setText(LIVE_CUT_TEXT.format(cut=f"{block.cut:,}"))
            self.cut_label.setVisible(block.cut > 0)
        else:
            self.toggle.set_text(f"Show {name} - {_count(block.chars, 'character')}")
            for widget in (self.caption, self.cut_label, self.text_edit):
                if widget is not None:
                    widget.hide()

    def focusables(self) -> list[QWidget]:
        widgets: list[QWidget] = [self.toggle]
        if self.text_edit is not None:
            widgets.append(self.text_edit)
        return widgets


# --------------------------------------------------------------------------
# LIVE pictures (pictures.py's data), drawn by Jarvis in the HUD theme at the size they are shown:
# plain text only, no email HTML, nothing loads, nothing inside reacts
# --------------------------------------------------------------------------

LIVE_PICTURE_MAX_W = 640          # a thumbnail is never wider
LIVE_PICTURE_MAIL_MAX_PX = 300    # an email picture taller than this is cut with a fade ("Enter: see all")
LIVE_PICTURE_DAY_MAX_PX = 440     # a calendar change is drawn to fit (its hours closer together)
LIVE_PICTURE_WEEK_MAX_PX = 320
LIVE_PICTURE_PAGE_MAX_PX = 400    # a page picture is scaled down to fit
LIVE_PICTURE_SEARCH_MAX_PX = 320  # a web search's results taller than this are cut with a fade ("Enter: see all")
LIVE_PICTURE_PENDING_PX = 120
PICTURE_VIEWER_SCREEN = 0.9       # the viewer is at most this much of its screen
PICTURE_VIEWER_MODEL_W = 720      # an email / calendar picture is drawn at most this wide in the viewer
PICTURE_VIEWER_PAGE_W = 960       # a page picture at its own width, at most this
PICTURE_SEE_ALL = "Enter: see all"
PICTURE_TAG = "PICTURE"
PICTURE_HINT = "Enter: larger"
PICTURE_TONE_COLORS = {
    live_pictures.TONE_WILL: AMBER, live_pictures.TONE_DOING: ACCENT, live_pictures.TONE_DONE: GREEN,
    live_pictures.TONE_FAILED: RED, live_pictures.TONE_UNDONE: TEXT_DIM, live_pictures.TONE_CHECK: AMBER,
}
_PICTURE_MAX = {
    live_pictures.KIND_EMAIL: LIVE_PICTURE_MAIL_MAX_PX, live_pictures.KIND_OUTGOING: LIVE_PICTURE_MAIL_MAX_PX,
    live_pictures.KIND_DAY: LIVE_PICTURE_DAY_MAX_PX, live_pictures.KIND_WEEK: LIVE_PICTURE_WEEK_MAX_PX,
    live_pictures.KIND_PAGE: LIVE_PICTURE_PAGE_MAX_PX, live_pictures.KIND_SEARCH: LIVE_PICTURE_SEARCH_MAX_PX,
}
_RSVP_CHIP_COLORS = {"YES": GREEN, "NO": RED, "MAYBE": AMBER}
_PIC_PAD = 12
_PIC_FADE = 24
_PIC_LABEL_W = 64
_PIC_WIDE = 300            # header labels beside their values from this width (else above them)
_PIC_HOUR_PX = 28.0        # a day picture's hour at its natural size ...
_PIC_HOUR_MIN_PX = 8.0     # ... and at its most squeezed (a thumbnail; hour labels then every 4 h)
_PIC_BLOCK_MIN_PX = 14.0   # a timed block's least height (30 minutes at the natural hour size)
_PIC_STRIP_ROWS = 2        # a day thumbnail's all-day / "no time" rows per day; more fold into "+N more"
_PIC_STRIP_TAGS = ("ALL DAY", "NO TIME")
_WEEK_HOUR_PX = 12.0
_NO_STAMP = ("", "")


def _pic_draw(painter: QPainter | None, lines: _TextLines, x: float, y: float, color: str | QColor) -> None:
    if painter is not None:
        painter.setPen(QColor(color))
        lines.draw(painter, QPointF(x, y))


def _pic_rule(painter: QPainter | None, x: float, y: float, width: float) -> None:
    if painter is not None:
        painter.setPen(QPen(rgba(ACCENT, 0.18), 1))
        painter.drawLine(QPointF(x, y + 0.5), QPointF(x + width, y + 0.5))


def _pic_paragraphs(painter: QPainter | None, text: str, font_name: str, x: float, y: float, width: float,
                    color: str | QColor, stop: float | None) -> float:
    """``text`` with its line breaks kept, wrapped at ``width``; drawing stops past ``stop``. The y
    after it."""
    spacing = QFontMetricsF(getattr(_LiveFonts.get(), font_name)).lineSpacing()
    for paragraph in text.split("\n"):
        if stop is not None and y > stop:
            break
        if not paragraph.strip():
            y += spacing
            continue
        lines = _TextLines(paragraph, font_name, width)
        _pic_draw(painter, lines, x, y, color)
        y += lines.height
    return y


def _pic_stamp(painter: QPainter | None, stamp: tuple[str, str], x: float, y: float, width: float,
               left_room: float) -> float:
    """The stamp pill at the right of a top bar (under it when the bar has no room): the bar's height."""
    words, tone = stamp
    if not words:
        return 18.0
    pill_w = _text_advance(_LiveFonts.get().word, words) + 14
    if left_room + 8 + pill_w <= width:
        rect, bar = QRectF(x + width - pill_w, y, pill_w, 18), 18.0
    else:
        rect, bar = QRectF(x, y + 22, min(pill_w, width), 18), 40.0
    if painter is not None:
        _paint_pill(painter, rect, words, _LiveFonts.get().word, PICTURE_TONE_COLORS.get(tone, AMBER), filled=False)
    return bar


def _pic_chip(painter: QPainter | None, text: str, x: float, y: float, width: float, color: str) -> float:
    fonts = _LiveFonts.get()
    chip_w = min(width, _text_advance(fonts.pic_tag, text) + 10)
    if painter is not None:
        _paint_pill(painter, QRectF(x, y + 1, chip_w, 16), text, fonts.pic_tag, color, filled=True)
    return chip_w


def _paint_mail(painter: QPainter | None, rect: QRectF, picture: Any, stamp: tuple[str, str],
                limit: float | None = None) -> float:
    """An email as a mail client shows it: the account (and the stamp of an outgoing one), From /
    To / Cc / Date / Subject, the reply line, the text with its line breaks, the earlier messages as
    short lines. Measures only without a painter; stops past ``limit`` (its height then exceeds it)."""
    mail = picture.mail
    fonts = _LiveFonts.get()
    left, width, top = rect.left() + _PIC_PAD, max(40.0, rect.width() - 2 * _PIC_PAD), rect.top()
    stop = None if limit is None else top + limit
    y = top + 10
    chip = (f"FROM {mail.account}" if mail.outgoing else (mail.account or "EMAIL")).upper()
    chip_w = _pic_chip(painter, chip, left, y, width, ACCENT if mail.outgoing else TEXT_MUTED)
    y += _pic_stamp(painter, stamp, left, y, width, chip_w) + 6
    if not mail.outgoing:
        caption = _TextLines(LIVE_UNTRUSTED_TEXT, "pic_small", width, max_lines=2)
        _pic_draw(painter, caption, left, y, AMBER_META)
        y += caption.height + 6
    head = mail.head
    # (label, value, font, most lines): To and Cc are never cut - every recipient shows (an outgoing
    # email names at most 5, an incoming line is at most HEAD_CAP with its "+N more" count kept).
    rows = [("FROM", head.sender, "pic_value", 4), ("TO", head.to or "(nobody)", "pic_value", 0)]
    if head.cc:
        rows.append(("CC", head.cc, "pic_value", 0))
    if head.when:
        rows.append(("DATE", head.when, "pic_value", 4))
    rows.append(("SUBJECT", head.subject, "pic_subject", 4))
    beside = width >= _PIC_WIDE
    label_ascent = QFontMetricsF(fonts.pic_label).ascent()
    for label, value, font_name, most in rows:
        if beside:
            label_lines = _TextLines(label, "pic_label", _PIC_LABEL_W, 1)
            value_lines = _TextLines(value, font_name, width - _PIC_LABEL_W - 8, max_lines=most)
            shift = max(0.0, QFontMetricsF(getattr(fonts, font_name)).ascent() - label_ascent)
            _pic_draw(painter, label_lines, left, y + shift, TEXT_DIM)
            _pic_draw(painter, value_lines, left + _PIC_LABEL_W + 8, y, TEXT_BRIGHT)
            y += max(label_lines.height + shift, value_lines.height) + 3
        else:
            label_lines = _TextLines(label, "pic_label", width, 1)
            _pic_draw(painter, label_lines, left, y, TEXT_DIM)
            y += label_lines.height
            value_lines = _TextLines(value, font_name, width, max_lines=most)
            _pic_draw(painter, value_lines, left, y, TEXT_BRIGHT)
            y += value_lines.height + 4
    if mail.reply_line:
        reply = _TextLines(mail.reply_line, "pic_small", width, max_lines=2)
        _pic_draw(painter, reply, left, y + 2, TEXT_SUB)
        y += reply.height + 2
    y += 7
    _pic_rule(painter, left, y, width)
    y += 9
    if not mail.body_kept:
        lines = _TextLines(LIVE_NOT_KEPT_TEXT.format(chars=f"{mail.body_chars:,}"), "pic_small", width)
        _pic_draw(painter, lines, left, y, TEXT_DIM)
        y += lines.height
    elif not mail.body.strip():
        lines = _TextLines("(no text)", "pic_small", width)
        _pic_draw(painter, lines, left, y, TEXT_DIM)
        y += lines.height
    else:
        y = _pic_paragraphs(painter, mail.body, "pic_body", left, y, width, TEXT_SOFT, stop)
        if stop is not None and y > stop:
            return y - top
        cut = (LIVE_CUT_TEXT.format(cut=f"{mail.body_cut:,}") if mail.body_cut
               else "[Jarvis read a shortened text of this email]" if mail.shortened else "")
        if cut:
            lines = _TextLines(cut, "pic_small", width)
            _pic_draw(painter, lines, left, y + 4, TEXT_DIM)
            y += lines.height + 4
    if mail.older or mail.older_more:
        y += 10
        _pic_rule(painter, left, y, width)
        y += 8
        heading = _TextLines("EARLIER IN THIS THREAD", "pic_tag", width, 1)
        _pic_draw(painter, heading, left, y, TEXT_DIM)
        y += heading.height + 4
        for line in mail.older:
            if stop is not None and y > stop:
                return y - top
            who = _TextLines(" - ".join(part for part in (line.sender, line.when) if part), "pic_sender", width, 1)
            _pic_draw(painter, who, left, y, TEXT_BODY)
            y += who.height
            if line.snippet:
                snippet = _TextLines(line.snippet, "pic_small", width, 1)
                _pic_draw(painter, snippet, left, y, TEXT_SUB)
                y += snippet.height
            y += 5
        if mail.older_more:
            more = _TextLines(f"+{mail.older_more:,} earlier not shown", "pic_small", width, 1)
            _pic_draw(painter, more, left, y, TEXT_DIM)
            y += more.height
    return y + 12 - top


def _pic_block_colors(role: str) -> tuple[QColor, QColor, str, str]:
    """(fill, border, title colour, tag) of a day picture's block."""
    if role == live_pictures.ROLE_BEFORE:
        return rgba(AMBER, 0.06), rgba(AMBER, 0.7), TEXT_MUTED, "was"
    if role == live_pictures.ROLE_AFTER:
        return rgba(ACCENT, 0.28), QColor(ACCENT), TEXT_BRIGHT, "new"
    if role == live_pictures.ROLE_NEW:
        return rgba(ACCENT, 0.34), QColor(ACCENT), TEXT_BRIGHT, "new"
    if role == live_pictures.ROLE_CANCEL:
        return rgba(RED_LINE, 0.25), QColor(RED_LINE), TEXT_SOFT, "cancel"
    if role == live_pictures.ROLE_RSVP:
        return rgba(ACCENT, 0.2), rgba(ACCENT, 0.6), TEXT_BRIGHT, ""
    return rgba(SLATE, 0.35), rgba(SLATE, 0.6), TEXT_SOFT, ""


def _paint_day_block(painter: QPainter, rect: QRectF, block: Any) -> None:
    fonts = _LiveFonts.get()
    fill, border, color, tag = _pic_block_colors(block.role)
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = chamfer_path(rect, min(4.0, rect.height() / 3), CUT_TL | CUT_BR)
        if block.role == live_pictures.ROLE_NEW:   # a soft glow around the new block
            painter.fillPath(chamfer_path(rect.adjusted(-3, -3, 3, 3), 5, CUT_TL | CUT_BR), rgba(ACCENT, 0.14))
        if block.role != live_pictures.ROLE_OTHER:   # the change on solid ground: no other event shows through
            painter.fillPath(path, PANEL_GROUND)
        painter.fillPath(path, fill)
        pen = QPen(border, 1.2 if block.role != live_pictures.ROLE_OTHER else 1)
        if block.role == live_pictures.ROLE_BEFORE:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(chamfer_path(rect.adjusted(0.5, 0.5, -0.5, -0.5), min(4.0, rect.height() / 3),
                                      CUT_TL | CUT_BR))
        if block.role == live_pictures.ROLE_CANCEL:
            painter.setPen(QPen(rgba(RED_LINE, 0.8), 1.2))
            painter.drawLine(rect.topLeft() + QPointF(2, 2), rect.bottomRight() - QPointF(2, 2))
        painter.setClipRect(rect.adjusted(1, 1, -1, -1))
        inner = rect.adjusted(5, 2, -4, -2)
        right = inner.right()
        if block.chip:
            chip_w = _text_advance(fonts.pic_tag, block.chip) + 10
            if chip_w < inner.width() - 20:
                _paint_pill(painter, QRectF(right - chip_w, inner.top() + 1, chip_w, 14), block.chip, fonts.pic_tag,
                            _RSVP_CHIP_COLORS.get(block.chip, AMBER), filled=True)
                right -= chip_w + 4
        elif tag:
            tag_w = _text_advance(fonts.pic_tiny, tag) + 2
            if tag_w < inner.width() - 30:
                painter.setFont(fonts.pic_tiny)
                painter.setPen(QColor(border))
                painter.drawText(QRectF(right - tag_w, inner.top(), tag_w, 14),
                                 Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, tag)
                right -= tag_w + 4
        title_font = "pic_block_struck" if block.role == live_pictures.ROLE_CANCEL else "pic_block"
        line = QFontMetricsF(fonts.pic_block).lineSpacing()
        small = QFontMetricsF(fonts.pic_tiny).lineSpacing()
        room = inner.height()
        if room < line + small:   # one line: the title, then the time after it
            title = _TextLines(f"{block.title}  {block.time_text}", title_font, max(8.0, right - inner.left()), 1)
            _pic_draw(painter, title, inner.left(), inner.top() + max(0.0, (room - line) / 2) - 1, color)
            return
        max_lines = max(1, int((room - small) // line))
        title = _TextLines(block.title, title_font, max(8.0, right - inner.left()), max_lines)
        _pic_draw(painter, title, inner.left(), inner.top(), color)
        time_lines = _TextLines(block.time_text, "pic_tiny", inner.width(), 1)
        _pic_draw(painter, time_lines, inner.left(), inner.top() + title.height,
                  TEXT_SUB if block.role == live_pictures.ROLE_OTHER else color)
    finally:
        painter.restore()


_ARROW_GUTTER = 14.0   # a Move's day picture keeps this much room left of its columns for the arrow


def _arrow_path(start: QRectF, end: QRectF) -> tuple[QPainterPath, QPointF, QPointF]:
    """(path, the point before the tip, the tip) of a Move's arrow from ``start`` to ``end``: from
    the side of the old block that faces the new one into the new one's facing side; blocks one
    above the other (sharing their left edge) get a hook through the gutter on their left."""
    if end.left() >= start.right() - 2:        # the new time is to the right (another day, another lane)
        a, b = QPointF(start.right(), start.center().y()), QPointF(end.left(), end.center().y())
        reach = max(8.0, min(28.0, abs(b.x() - a.x()) / 2 + 8))
        c1, c2 = QPointF(a.x() + reach, a.y()), QPointF(b.x() - reach, b.y())
    elif end.right() <= start.left() + 2:      # to the left
        a, b = QPointF(start.left(), start.center().y()), QPointF(end.right(), end.center().y())
        reach = max(8.0, min(28.0, abs(a.x() - b.x()) / 2 + 8))
        c1, c2 = QPointF(a.x() - reach, a.y()), QPointF(b.x() + reach, b.y())
    else:                                      # above / below each other: a hook on the left
        a = QPointF(start.left(), start.center().y())
        b = QPointF(end.left(), end.center().y())
        hook = min(start.left(), end.left()) - _ARROW_GUTTER * 1.3
        c1, c2 = QPointF(hook, a.y()), QPointF(hook, b.y())
    path = QPainterPath(a)
    path.cubicTo(c1, c2, b)
    return path, c2, b


def _paint_arrow(painter: QPainter, start: QRectF, end: QRectF) -> None:
    """The amber arrow of a Move: from where the event is to where it goes (a dark halo under it, so
    it reads over the grid and the blocks)."""
    path, before, tip = _arrow_path(start, end)
    dx, dy = tip.x() - before.x(), tip.y() - before.y()
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return
    ux, uy = dx / length, dy / length
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        halo = QPen(rgba(GROUND, 0.85), 4.0)
        halo.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(halo)
        painter.drawPath(path)
        pen = QPen(QColor(AMBER), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawPath(path)
        size = 7.0
        left = QPointF(tip.x() - ux * size - uy * size * 0.55, tip.y() - uy * size + ux * size * 0.55)
        right = QPointF(tip.x() - ux * size + uy * size * 0.55, tip.y() - uy * size - ux * size * 0.55)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(AMBER))
        painter.drawPolygon(QPolygonF([tip, left, right]))
    finally:
        painter.restore()


def _hour_text(minute: int, hour24: bool) -> str:
    hour = (minute // 60) % 24
    if hour24:
        return f"{hour:02d}:00"
    return f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}"


def _fold_strip(group: list[Any], rows: int) -> list[Any]:
    """A day thumbnail's all-day (or "no time") blocks with at most ``rows`` rows a day: the change
    always shows; other events beyond the room fold into one "+N more" row."""
    folded: list[Any] = []
    for column in sorted({block.column for block in group}):
        blocks = [block for block in group if block.column == column]
        changed = [block for block in blocks if block.role != live_pictures.ROLE_OTHER]
        others = [block for block in blocks if block.role == live_pictures.ROLE_OTHER]
        room = max(0, rows - len(changed))
        if len(others) <= room:
            folded.extend(others + changed)
            continue
        kept = others[:max(0, room - 1)]
        rest = len(others) - len(kept)
        folded.extend(kept + changed)
        folded.append(dataclasses.replace(others[0], title=f"+{rest:,} more", time_text="", chip=""))
    return folded


def _paint_day(painter: QPainter | None, rect: QRectF, picture: Any, stamp: tuple[str, str],
               max_height: float | None = None, marks: list[QRectF] | None = None) -> float:
    """A calendar change on its day: the headline, the day(s) as columns with their hours, the
    all-day and "time not known" rows, the blocks (other events, the ghost of where it is, the
    new time, the struck-through cancel, the answer chip), the Move's arrow, the time now and how
    many other events were not drawn (whose events these are is the picture's note, shown under a
    thumbnail and above the viewer's picture). Squeezes its hours (and folds its all-day rows) to
    fit ``max_height``; measures without a painter. ``marks`` gets the rectangles of the changed
    blocks."""
    day = picture.day
    fonts = _LiveFonts.get()
    left, width, top = rect.left() + _PIC_PAD, max(60.0, rect.width() - 2 * _PIC_PAD), rect.top()
    y = top + 10
    chip_w = _pic_chip(painter, f"{day.account} calendar".upper() if day.account else "CALENDAR", left, y, width,
                       TEXT_MUTED)
    y += _pic_stamp(painter, stamp, left, y, width, chip_w) + 6
    headline = _TextLines(day.headline, "pic_headline", width, max_lines=2)
    _pic_draw(painter, headline, left, y, TEXT_BRIGHT)
    y += headline.height + 8
    axis = max(_text_advance(fonts.pic_small, _hour_text(12 * 60, day.hour24)),
               _text_advance(fonts.pic_small, _hour_text(10 * 60, day.hour24))) + 8
    if any(block.all_day or block.timeless for block in day.blocks):   # room for "ALL DAY" / "NO TIME"
        axis = max(axis, max(_text_advance(fonts.pic_tiny, tag) for tag in _PIC_STRIP_TAGS) + 8)
    gutter = _ARROW_GUTTER if day.change == live_pictures.CHANGE_MOVE else 0.0
    grid_left = left + axis + gutter
    columns = max(1, len(day.days))
    col_w = (left + width - grid_left) / columns
    head_h = QFontMetricsF(fonts.pic_head).lineSpacing()
    if painter is not None:
        painter.setFont(fonts.pic_head)
        painter.setPen(QColor(TEXT_BRIGHT))
        for index, label in enumerate(day.days):
            cell = QRectF(grid_left + index * col_w, y, col_w, head_h)
            text = QFontMetricsF(fonts.pic_head).elidedText(label, Qt.TextElideMode.ElideRight, cell.width() - 4)
            painter.drawText(cell, Qt.AlignmentFlag.AlignCenter, text)
    y += head_h + 4
    rects: dict[str, QRectF] = {}
    strip = 20.0
    for flag, label in zip(("all_day", "timeless"), _PIC_STRIP_TAGS):
        group = [block for block in day.blocks if getattr(block, flag)]
        if not group:
            continue
        if max_height is not None:
            group = _fold_strip(group, _PIC_STRIP_ROWS)
        rows: dict[int, int] = {}
        if painter is not None:
            tag = _TextLines(label, "pic_tiny", axis - 4, 1)
            _pic_draw(painter, tag, left, y + 4, TEXT_DIM)
        for block in group:
            row = rows.get(block.column, 0)
            rows[block.column] = row + 1
            cell = QRectF(grid_left + block.column * col_w + 2, y + row * (strip + 2), col_w - 4, strip)
            if painter is not None:
                _paint_day_block(painter, cell, block)
            rects.setdefault(block.role, cell)
            if marks is not None and block.role != live_pictures.ROLE_OTHER:
                marks.append(cell)
        y += max(rows.values()) * (strip + 2) + 4
    # Whose events these are (day.others_note) is the picture's note: the line under a thumbnail and
    # the note above it in the viewer - not drawn in the picture a second time.
    more = (_TextLines(f"+{day.others_more:,} more events that day not drawn", "pic_small", width, 1)
            if day.others_more else None)
    footer = 8 + (more.height if more is not None else 0) + 10
    hours = max(1.0, (day.last_min - day.first_min) / 60)
    pph = _PIC_HOUR_PX
    label_h = QFontMetricsF(fonts.pic_small).lineSpacing()
    if max_height is not None:
        room = max_height - (y - top) - footer - label_h
        pph = max(_PIC_HOUR_MIN_PX, min(_PIC_HOUR_PX, room / hours))
    grid_top = y + label_h / 2
    grid_h = hours * pph

    def y_of(minute: int) -> float:
        return grid_top + (minute - day.first_min) / 60 * pph

    # The picture's lanes count a block as at least 30 minutes, its _PIC_BLOCK_MIN_PX at the natural
    # hour size; squeezed (a thumbnail), that height stands for more: lanes for the scale drawn, so
    # short neighbours never print over each other (the changed block included).
    blocks = day.blocks
    if pph < _PIC_HOUR_PX:
        blocks = live_pictures.with_lanes(blocks, math.ceil(_PIC_BLOCK_MIN_PX * 60 / pph))
    timed: list[tuple[Any, QRectF]] = []
    for block in blocks:
        if block.all_day or block.timeless:
            continue
        lane_w = col_w / max(1, block.lanes)
        x = grid_left + block.column * col_w + block.lane * lane_w + 2
        top_y = y_of(block.start_min)
        cell = QRectF(x, top_y, max(8.0, lane_w - 4), max(_PIC_BLOCK_MIN_PX, y_of(block.end_min) - top_y))
        timed.append((block, cell))
        rects.setdefault(block.role, cell)
        if marks is not None and block.role != live_pictures.ROLE_OTHER:
            marks.append(cell)
    if painter is not None:
        every = 1 if pph >= 18 else 2 if pph >= 10 else 4
        painter.setFont(fonts.pic_small)
        for number, minute in enumerate(range(day.first_min, day.last_min + 1, 60)):
            line_y = y_of(minute)
            painter.setPen(QPen(rgba(ACCENT, 0.08), 1))
            painter.drawLine(QPointF(left + axis, line_y + 0.5), QPointF(left + width, line_y + 0.5))
            if number % every == 0:
                painter.setPen(QColor(TEXT_TIME))
                painter.drawText(QRectF(left, line_y - label_h / 2, axis - 6, label_h),
                                 Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                                 _hour_text(minute, day.hour24))
        for index in range(1, columns):
            x = grid_left + index * col_w
            painter.setPen(QPen(rgba(ACCENT, 0.16), 1))
            painter.drawLine(QPointF(x + 0.5, grid_top), QPointF(x + 0.5, grid_top + grid_h))
        for block, cell in timed:
            _paint_day_block(painter, cell, block)
        before, after = rects.get(live_pictures.ROLE_BEFORE), rects.get(live_pictures.ROLE_AFTER)
        if before is not None and after is not None:
            _paint_arrow(painter, before, after)
        if day.now_column >= 0 and day.first_min <= day.now_min <= day.last_min:
            now_y = y_of(day.now_min)
            x = grid_left + day.now_column * col_w
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(RED_LINE), 1))
            painter.drawLine(QPointF(x, now_y), QPointF(x + col_w, now_y))
            painter.setBrush(QColor(RED_LINE))
            painter.drawEllipse(QPointF(x, now_y), 2.5, 2.5)
            painter.restore()
    y = grid_top + grid_h + 8
    if more is not None:
        _pic_draw(painter, more, left, y, TEXT_DIM)
        y += more.height
    return y + 10 - top


def _week_forms(label: str) -> list[str]:
    """A week column's label in its three forms: "Thu 8", "T 8", "8"."""
    parts = label.split()
    return [label, f"{parts[0][:1]} {parts[1]}", parts[1]] if len(parts) == 2 else [label]


def week_label(label: str, room: float, metrics: QFontMetricsF) -> str:
    """A week column's day label that fits ``room`` px: "Thu 8", else "T 8", else "8" (the day of
    the month always shows), elided only when even that does not fit."""
    candidates = _week_forms(label)
    for text in candidates:
        if metrics.horizontalAdvance(text) <= room:
            return text
    return metrics.elidedText(candidates[-1], Qt.TextElideMode.ElideRight, max(1.0, room))


def week_labels(labels: Sequence[str], room: float, metrics: QFontMetricsF) -> list[str]:
    """The week strip's day labels in ONE form for all of them (the first of "Thu 8", "T 8", "8"
    in which every label fits ``room`` px), so no day loses its letter while its neighbours keep
    theirs; the last form elided when even that does not fit."""
    forms = [_week_forms(label) for label in labels]
    for index in range(3):
        texts = [found[min(index, len(found) - 1)] for found in forms]
        if all(metrics.horizontalAdvance(text) <= room for text in texts):
            return texts
    return [text if metrics.horizontalAdvance(text) <= room
            else metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(1.0, room))
            for text in (found[-1] for found in forms)]


def _paint_week(painter: QPainter | None, rect: QRectF, picture: Any, stamp: tuple[str, str],
                max_height: float | None = None) -> float:
    """The 7 days of an Ask's calendar read: day labels (today outlined), a faint hour grid, the
    timed events as blocks, all-day counts, each day's count and (wide enough) its first titles."""
    week = picture.week
    fonts = _LiveFonts.get()
    left, width, top = rect.left() + _PIC_PAD, max(60.0, rect.width() - 2 * _PIC_PAD), rect.top()
    y = top + 10
    chip_w = _pic_chip(painter, "WHAT ASK READ", left, y, width, TEXT_MUTED)
    total = _TextLines(f"{week.total:,} event{'s' if week.total != 1 else ''} in "
                       f"{', '.join(week.accounts) or 'the calendar'}", "pic_small", max(10.0, width - chip_w - 8), 1)
    _pic_draw(painter, total, left + chip_w + 8, y + 1, TEXT_DIM)
    y += 26
    axis = _text_advance(fonts.pic_tiny, _hour_text(12 * 60, week.hour24)) + 6
    columns = max(1, len(week.columns))
    col_w = (width - axis) / columns
    label_h = QFontMetricsF(fonts.pic_small).lineSpacing()
    if painter is not None:
        painter.setFont(fonts.pic_small)
        metrics = QFontMetricsF(fonts.pic_small)
        labels = week_labels([column.label for column in week.columns], col_w - 2, metrics)
        for index, column in enumerate(week.columns):
            painter.setPen(QColor(ACCENT if column.today else TEXT_SOFT))
            cell = QRectF(left + axis + index * col_w, y, col_w, label_h)
            painter.drawText(cell, Qt.AlignmentFlag.AlignCenter, labels[index])
    y += label_h + 2
    tiny = QFontMetricsF(fonts.pic_tiny).lineSpacing()
    if any(column.all_day for column in week.columns):
        if painter is not None:
            painter.setFont(fonts.pic_tiny)
            painter.setPen(QColor(TEXT_SUB))
            for index, column in enumerate(week.columns):
                if column.all_day:
                    painter.drawText(QRectF(left + axis + index * col_w, y, col_w, tiny), Qt.AlignmentFlag.AlignCenter,
                                     f"{column.all_day} all day")
        y += tiny + 2
    titled = col_w >= 84
    title_rows = max((len(column.titles) for column in week.columns), default=0) if titled else 0
    footer = 4 + label_h + title_rows * tiny + (tiny + 4 if week.later else 0) + 10
    hours = max(1.0, (week.last_min - week.first_min) / 60)
    pph = _WEEK_HOUR_PX if not titled else 16.0
    if max_height is not None:
        pph = max(6.0, min(pph, (max_height - (y - top) - footer - 4) / hours))
    grid_top = y + 4
    grid_h = hours * pph
    if painter is not None:
        painter.setFont(fonts.pic_tiny)
        every = 2 if pph >= 10 else 4
        for number, minute in enumerate(range(week.first_min, week.last_min + 1, 60)):
            line_y = grid_top + (minute - week.first_min) / 60 * pph
            painter.setPen(QPen(rgba(ACCENT, 0.06), 1))
            painter.drawLine(QPointF(left + axis, line_y + 0.5), QPointF(left + width, line_y + 0.5))
            if number % every == 0:
                painter.setPen(QColor(TEXT_TIME))
                painter.drawText(QRectF(left, line_y - tiny / 2, axis - 4, tiny),
                                 Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                                 _hour_text(minute, week.hour24))
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for index, column in enumerate(week.columns):
            x = left + axis + index * col_w
            if column.today:
                painter.setPen(QPen(rgba(ACCENT, 0.5), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(QRectF(x + 1.5, grid_top - 1.5, col_w - 3, grid_h + 3))
            for start, end in column.blocks:
                first = max(start, week.first_min)
                last = min(end, week.last_min)
                if last <= first:
                    continue
                cell = QRectF(x + 4, grid_top + (first - week.first_min) / 60 * pph, col_w - 8,
                              max(3.0, (last - first) / 60 * pph))
                painter.fillRect(cell, rgba(ACCENT, 0.5))
        painter.restore()
    y = grid_top + grid_h + 4
    if painter is not None:
        painter.setFont(fonts.pic_small)
        for index, column in enumerate(week.columns):
            painter.setPen(QColor(TEXT_SOFT if column.count else TEXT_DIM))
            painter.drawText(QRectF(left + axis + index * col_w, y, col_w, label_h), Qt.AlignmentFlag.AlignCenter,
                             str(column.count))
    y += label_h
    if title_rows:
        for index, column in enumerate(week.columns):
            for row, title in enumerate(column.titles):
                lines = _TextLines(title, "pic_tiny", col_w - 6, 1)
                _pic_draw(painter, lines, left + axis + index * col_w + 3, y + row * tiny, TEXT_SUB)
        y += title_rows * tiny
    if week.later:
        later = _TextLines(f"+{week.later:,} later in what Ask read", "pic_tiny", width, 1)
        _pic_draw(painter, later, left, y + 4, TEXT_DIM)
        y += tiny + 4
    return y + 10 - top


_SEARCH_GLASS_W = 26.0     # the search box's magnifier column
_SEARCH_PATH_GAP = 6.0     # between a result's site and the rest of its address
_SEARCH_PATH_MIN = 24.0    # narrower than this left for the rest of the address: the site alone


def _paint_glass(painter: QPainter, center: QPointF, radius: float, color: str | QColor) -> None:
    """A small magnifying glass (the search box's sign)."""
    pen = QPen(QColor(color), 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(center, radius, radius)
    reach = radius * 0.72
    painter.drawLine(QPointF(center.x() + reach, center.y() + reach),
                     QPointF(center.x() + reach + radius * 0.8, center.y() + reach + radius * 0.8))


def search_address_lines(hit: Any, width: float) -> tuple[_TextLines, _TextLines | None]:
    """A search result's address line ``width`` px wide as a results page shows it: its site (the
    host, whose site it is; "https://" left out) and the rest of the address after it, cut at its
    end (None when there is no rest or no room for it). A site too wide for the line is cut in its
    middle, so the end of the host (".example.edu") always shows. An address without a host is
    the address as listed, cut at its end, alone."""
    if not hit.site:
        return _TextLines(hit.url, "pic_small", width, max_lines=1), None
    font = _LiveFonts.get().pic_small
    site = hit.site
    if _text_advance(font, site) > width - 1:
        site = QFontMetricsF(font).elidedText(site, Qt.TextElideMode.ElideMiddle, max(1.0, width - 1))
    shown = _TextLines(site, "pic_small", width, max_lines=1)
    room = width - shown.natural_width - _SEARCH_PATH_GAP
    if not hit.path or room < _SEARCH_PATH_MIN:
        return shown, None
    return shown, _TextLines(hit.path, "pic_small", room, max_lines=1)


def _paint_search_address(painter: QPainter | None, hit: Any, left: float, y: float, width: float) -> float:
    """A result's address line (search_address_lines): the site, then the rest dimmer. Returns its
    height."""
    site, rest = search_address_lines(hit, width)
    _pic_draw(painter, site, left, y, TEXT_MUTED if hit.site else TEXT_SUB)
    if rest is not None:
        _pic_draw(painter, rest, left + site.natural_width + _SEARCH_PATH_GAP, y, TEXT_DIM)
    return site.height


def _paint_search(painter: QPainter | None, rect: QRectF, picture: Any, stamp: tuple[str, str] = _NO_STAMP,
                  limit: float | None = None) -> float:
    """A web search as a compact results page: the count of results, the exact query in a search
    box (the sites it was kept to or kept from under it), then each result as its title, its
    address (its site first: _paint_search_address) and (only when the search gave one) its
    snippet, "+N more" when not all are drawn.
    Plain text only: no link, nothing loads. Measures only without a painter; stops past
    ``limit`` (its height then exceeds it)."""
    search = picture.search
    left, width, top = rect.left() + _PIC_PAD, max(40.0, rect.width() - 2 * _PIC_PAD), rect.top()
    stop = None if limit is None else top + limit
    y = top + 10
    chip_w = _pic_chip(painter, "WEB SEARCH", left, y, width, TEXT_MUTED)
    total = len(search.hits) + search.more
    count = ("results Jarvis could not list" if search.unread
             else f"{total:,} result{'s' if total != 1 else ''} listed")
    counted = _TextLines(count, "pic_small", max(10.0, width - chip_w - 8), 1)
    _pic_draw(painter, counted, left + chip_w + 8, y + 1, TEXT_DIM)
    y += 26
    query = _TextLines(search.query, "pic_value", max(10.0, width - _SEARCH_GLASS_W - 10), max_lines=3)
    box_h = max(32.0, query.height + 14)
    if painter is not None:
        box = QRectF(left, y, width, box_h)
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.fillPath(chamfer_path(box, 5, CUT_TL | CUT_BR), rgba(ACCENT, 0.07))
            painter.setPen(QPen(rgba(ACCENT, 0.55), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(chamfer_path(box.adjusted(0.5, 0.5, -0.5, -0.5), 5, CUT_TL | CUT_BR))
            _paint_glass(painter, QPointF(left + _SEARCH_GLASS_W / 2 - 1, y + box_h / 2 - 1.5), 4.6, ACCENT)
        finally:
            painter.restore()
    _pic_draw(painter, query, left + _SEARCH_GLASS_W, y + (box_h - query.height) / 2, TEXT_BRIGHT)
    y += box_h + 6
    for label, sites in (("ONLY THESE SITES", search.only), ("NOT THESE SITES", search.never)):
        if sites:
            lines = _TextLines(f"{label}: {sites}", "pic_small", width, max_lines=2)
            _pic_draw(painter, lines, left, y, TEXT_SUB)
            y += lines.height + 2
    if search.hits:
        caption = _TextLines(LIVE_UNTRUSTED_TEXT, "pic_small", width, max_lines=2)
        _pic_draw(painter, caption, left, y + 2, AMBER_META)
        y += caption.height + 2
    y += 6
    _pic_rule(painter, left, y, width)
    y += 9
    if not search.hits:
        words = live_pictures.SEARCH_UNREAD if search.unread else live_pictures.SEARCH_EMPTY
        lines = _TextLines(words, "pic_small", width, max_lines=2)
        _pic_draw(painter, lines, left, y, AMBER if search.unread else TEXT_DIM)
        y += lines.height
    for index, hit in enumerate(search.hits):
        if stop is not None and y > stop:
            return y - top
        if index:
            y += 9
        title = _TextLines(hit.title, "pic_headline", width, max_lines=2)
        _pic_draw(painter, title, left, y, TEXT_BRIGHT)
        y += title.height + 1
        y += _paint_search_address(painter, hit, left, y, width)
        if hit.snippet:
            snippet = _TextLines(hit.snippet, "pic_small", width, max_lines=3)
            _pic_draw(painter, snippet, left, y + 2, TEXT_SOFT)
            y += snippet.height + 2
    if search.more:
        more = _TextLines(f"+{search.more:,} more result{'s' if search.more != 1 else ''}", "pic_small", width,
                          max_lines=1)
        _pic_draw(painter, more, left, y + 8, TEXT_DIM)
        y += more.height + 8
    return y + 12 - top


def _page_size(page: Any, width: float, max_height: float | None = None) -> tuple[float, float]:
    """A page picture's size at most ``width`` wide (and ``max_height`` tall), its shape kept."""
    if page is None or page.width <= 0 or page.height <= 0:
        return 0.0, 0.0
    w = float(min(width, page.width))
    h = w * page.height / page.width
    if max_height is not None and h > max_height:
        h = float(max_height)
        w = h * page.width / page.height
    return w, h


def picture_height(picture: Any, width: int, stamp: tuple[str, str] = _NO_STAMP,
                   max_height: float | None = None) -> int:
    """The natural height of a READY picture drawn ``width`` logical px wide (texts wrap at that
    width in readable fonts, never a scaled-down bitmap). A day / week picture squeezes its hours to
    fit ``max_height``; an email or search picture stops measuring past it (then it is taller); a
    page keeps its shape. 0 for a picture that is not READY."""
    if picture is None or picture.state != live_events.PICTURE_READY:
        return 0
    rect = QRectF(0, 0, max(1, width), 0)
    kind = picture.kind
    if picture.mail is not None and kind in (live_pictures.KIND_EMAIL, live_pictures.KIND_OUTGOING):
        return math.ceil(_paint_mail(None, rect, picture, stamp, max_height))
    if picture.day is not None and kind == live_pictures.KIND_DAY:
        return math.ceil(_paint_day(None, rect, picture, stamp, max_height))
    if picture.week is not None and kind == live_pictures.KIND_WEEK:
        return math.ceil(_paint_week(None, rect, picture, stamp, max_height))
    if picture.page is not None and kind == live_pictures.KIND_PAGE:
        return math.ceil(_page_size(picture.page, width, max_height)[1])
    if picture.search is not None and kind == live_pictures.KIND_SEARCH:
        return math.ceil(_paint_search(None, rect, picture, stamp, max_height))
    return 0


def picture_focus(picture: Any, width: int, stamp: tuple[str, str] = _NO_STAMP,
                  max_height: float | None = None) -> tuple[float, float] | None:
    """(top, bottom) of what a READY calendar change picture changes (the ghost and the new time,
    the new block, the struck-through or answered event), drawn ``width`` px wide: what a short
    view shows first during a countdown. None for other pictures."""
    if picture is None or picture.state != live_events.PICTURE_READY or picture.day is None:
        return None
    marks: list[QRectF] = []
    _paint_day(None, QRectF(0, 0, max(1, width), 0), picture, stamp, max_height, marks)
    if not marks:
        return None
    return min(mark.top() for mark in marks), max(mark.bottom() for mark in marks)


def paint_picture(painter: QPainter, rect: QRectF, picture: Any, stamp: tuple[str, str] = _NO_STAMP,
                  max_height: float | None = None) -> None:
    """Draw a READY picture into ``rect`` (an email / calendar / search picture on its card; a
    page's image in a thin border)."""
    if picture is None or picture.state != live_events.PICTURE_READY:
        return
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setClipRect(rect)
        if picture.kind == live_pictures.KIND_PAGE:
            image = QImage.fromData(picture.page.image) if picture.page is not None and picture.page.image else QImage()
            if not image.isNull():
                painter.drawImage(rect, image)
            painter.setPen(QPen(rgba(ACCENT, 0.3), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
            return
        corners = CUT_TL | CUT_BR
        painter.fillPath(chamfer_path(rect, 6, corners), rgba(ACCENT, 0.2))
        painter.fillPath(chamfer_path(rect.adjusted(1, 1, -1, -1), 6, corners), PANEL_GROUND)
        painter.fillPath(chamfer_path(rect.adjusted(1, 1, -1, -1), 6, corners), rgba(ACCENT, 0.025))
        if picture.mail is not None:
            _paint_mail(painter, rect, picture, stamp, rect.height())
        elif picture.day is not None:
            _paint_day(painter, rect, picture, stamp, max_height)
        elif picture.week is not None:
            _paint_week(painter, rect, picture, stamp, max_height)
        elif picture.search is not None:
            _paint_search(painter, rect, picture, stamp, rect.height())
    finally:
        painter.restore()


def picture_pixmap(picture: Any, width: float, dpr: float = 1.0, stamp: tuple[str, str] = _NO_STAMP,
                   max_height: float | None = None) -> QPixmap:
    """``picture`` drawn ``width`` logical px wide into a pixmap of that size times ``dpr`` (crisp
    at 150 %). With ``max_height`` a picture taller than that (an email, a search; a calendar picture
    even squeezed) is cut with a fade and "Enter: see all", never silently; a page is scaled down to fit."""
    dpr = max(1.0, float(dpr or 1.0))
    if picture is not None and picture.kind == live_pictures.KIND_PAGE:
        w, h = _page_size(picture.page, width, max_height)
        natural = h
    else:
        w = float(width)
        natural = float(picture_height(picture, int(width), stamp, max_height))
        h = natural if max_height is None else min(natural, float(max_height))
    image = QImage(max(1, math.ceil(w * dpr)), max(1, math.ceil(h * dpr)), QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        rect = QRectF(0, 0, w, h)
        paint_picture(painter, rect, picture, stamp, max_height)
        if natural > h + 0.5 and picture is not None and picture.kind != live_pictures.KIND_PAGE:
            fade = QRectF(1, h - _PIC_FADE - 1, w - 2, _PIC_FADE)
            gradient = QLinearGradient(fade.topLeft(), fade.bottomLeft())
            gradient.setColorAt(0.0, rgba(GROUND, 0.0))
            gradient.setColorAt(1.0, QColor(GROUND))
            painter.fillRect(fade, QBrush(gradient))
            fonts = _LiveFonts.get()
            painter.setFont(fonts.pic_small)
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(QRectF(_PIC_PAD, h - 18, w - 2 * _PIC_PAD, 14),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, PICTURE_SEE_ALL)
    finally:
        painter.end()
    return QPixmap.fromImage(image)


def picture_state_text(picture: Any) -> str:
    """What a picture box says when it shows no picture (its note; "Picture unavailable" / "Picture
    not kept" when it has none)."""
    if picture is None:
        return ""
    if picture.note:
        return picture.note
    if picture.state == live_events.PICTURE_DROPPED:
        return "Picture not kept"
    if picture.state == live_events.PICTURE_PENDING:
        return "Taking the picture..."
    return "Picture unavailable"


class _LivePicture(_LiveButton):
    """A step's picture in LIVE (first in its details): a caption row (the PICTURE tag and the
    caption), the thumbnail drawn to fit the column (at most LIVE_PICTURE_MAX_W wide; an email cut
    with a fade at LIVE_PICTURE_MAIL_MAX_PX, a calendar picture squeezed to fit, a page scaled down)
    and its note. PENDING: a dashed box with the blinking glyph; UNAVAILABLE: an amber box saying
    why; DROPPED: a dim box saying why. Click, Space or Enter on a READY picture opens it larger
    (``on_open``); nothing inside it reacts on its own."""

    _CAPTION_H = 18
    _GAP = 4

    def __init__(self, on_open: Callable[[], None] | None = None) -> None:
        super().__init__()
        self._on_open = on_open
        self._picture: Any = None
        self._stamp: tuple[str, str] = _NO_STAMP
        self._pixmaps: dict[tuple, QPixmap] = {}
        self._heights: dict[int, int] = {}
        self._thumbs: dict[int, tuple[float, float]] = {}
        self.clicked.connect(self._clicked)

    def picture(self) -> Any:
        return self._picture

    def stamp(self) -> tuple[str, str]:
        return self._stamp

    def ready(self) -> bool:
        return self._picture is not None and self._picture.state == live_events.PICTURE_READY

    def pending(self) -> bool:
        return self._picture is not None and self._picture.state == live_events.PICTURE_PENDING

    def set_picture(self, picture: Any, stamp: tuple[str, str] = _NO_STAMP) -> None:
        stamp = (str(stamp[0]), str(stamp[1])) if stamp else _NO_STAMP
        if picture is self._picture and stamp == self._stamp:
            return
        self._picture, self._stamp = picture, stamp
        self._pixmaps.clear()
        self._heights.clear()
        self._thumbs.clear()
        self.setCursor(Qt.CursorShape.PointingHandCursor if self.ready() else Qt.CursorShape.ArrowCursor)
        self.setAccessibleName(self.accessible_text())
        # Not READY: the note already ends the name (a screen reader would say it twice).
        self.setAccessibleDescription(picture.note if picture is not None and self.ready() else "")
        _sync_blink_clock(self, self.pending())
        self.updateGeometry()
        self.update()

    def accessible_text(self) -> str:
        picture = self._picture
        if picture is None:
            return ""
        text = f"{picture.caption} picture: {picture.alt}" if picture.alt else f"{picture.caption} picture"
        if self._stamp[0]:
            text += f", {self._stamp[0]}"
        if self.ready():
            return text + ". Press Enter to see it larger"
        return f"{text}. {picture_state_text(picture)}"

    def _clicked(self) -> None:
        if self.ready() and self._on_open is not None:
            self._on_open()

    # ---- geometry ----------------------------------------------------------------------------------

    def _thumb(self, width: int) -> tuple[float, float]:
        """(width, height) of the thumbnail at the widget's ``width`` (kept per width: every repaint
        asks, and measuring lays the whole picture out)."""
        cached = self._thumbs.get(width)
        if cached is not None:
            return cached
        if len(self._thumbs) > 8:
            self._thumbs.clear()
        picture = self._picture
        w = float(min(max(1, width), LIVE_PICTURE_MAX_W))
        cap = _PICTURE_MAX.get(picture.kind, LIVE_PICTURE_PAGE_MAX_PX)
        if picture.kind == live_pictures.KIND_PAGE:
            size = _page_size(picture.page, w, cap)
        else:
            size = (w, float(min(cap, picture_height(picture, int(w), self._stamp, cap))))
        self._thumbs[width] = size
        return size

    def focus_span(self) -> tuple[float, float] | None:
        """(top, bottom) in this widget of what a calendar change picture changes (None for other
        pictures): the part a short LIVE view shows during a countdown."""
        if not self.ready() or self._picture.day is None:
            return None
        width = max(1, self.width())
        w, h = self._thumb(width)
        cap = _PICTURE_MAX.get(self._picture.kind, LIVE_PICTURE_PAGE_MAX_PX)
        span = picture_focus(self._picture, int(w), self._stamp, cap)
        if span is None:
            return None
        top = float(self._CAPTION_H + self._GAP)
        return top + min(span[0], h), top + min(span[1], h)

    def _note_lines(self, width: int) -> _TextLines | None:
        picture = self._picture
        if picture is None or not picture.note or not self.ready():
            return None
        return _TextLines(picture.note, "note", max(1, width), max_lines=3)

    def _box_lines(self, width: int) -> _TextLines:
        return _TextLines(picture_state_text(self._picture), "pic_small", max(1, min(width, LIVE_PICTURE_MAX_W) - 34),
                          max_lines=4)

    def _height_for(self, width: int) -> int:
        cached = self._heights.get(width)
        if cached is not None:
            return cached
        if len(self._heights) > 8:
            self._heights.clear()
        picture = self._picture
        if picture is None:
            return 0
        height = float(self._CAPTION_H + self._GAP)
        if self.ready():
            height += self._thumb(width)[1]
            note = self._note_lines(width)
            if note is not None:
                height += self._GAP + note.height
        elif self.pending():
            height += max(LIVE_PICTURE_PENDING_PX, self._box_lines(width).height + 24)
        else:
            height += self._box_lines(width).height + 16
        result = self._heights[width] = math.ceil(height) + 2
        return result

    # ---- painting ------------------------------------------------------------------------------

    def _pixmap(self, width: float, height: float) -> QPixmap:
        picture = self._picture
        dpr = self.devicePixelRatioF() or 1.0
        cap = _PICTURE_MAX.get(picture.kind, LIVE_PICTURE_PAGE_MAX_PX)
        key = (round(width, 2), round(dpr, 3), self._stamp, cap)
        pixmap = self._pixmaps.get(key)
        if pixmap is None:
            if len(self._pixmaps) > 3:
                self._pixmaps.clear()
            pixmap = self._pixmaps[key] = picture_pixmap(picture, width, dpr, self._stamp, cap)
        return pixmap

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        picture = self._picture
        if picture is None:
            return
        fonts = _LiveFonts.get()
        width = max(1, self.width())
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            tag_w = _text_advance(fonts.tag, PICTURE_TAG) + 10
            _paint_pill(painter, QRectF(0, 1, tag_w, 15), PICTURE_TAG, fonts.tag, TEXT_MUTED, filled=True)
            hint = PICTURE_HINT if self.ready() else ""
            hint_w = _text_advance(fonts.note, hint) + 4 if hint else 0.0
            room = min(width, LIVE_PICTURE_MAX_W) - tag_w - 8 - (hint_w + 8 if hint else 0)
            if room < 60:
                hint, room = "", min(width, LIVE_PICTURE_MAX_W) - tag_w - 8
            painter.setFont(fonts.note)
            painter.setPen(QColor(TEXT_SOFT))
            caption = QFontMetricsF(fonts.note).elidedText(picture.caption, Qt.TextElideMode.ElideRight, max(1.0, room))
            painter.drawText(QRectF(tag_w + 8, 0, max(1.0, room), self._CAPTION_H),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, caption)
            if hint:
                painter.setPen(QColor(TEXT_DIM))
                painter.drawText(QRectF(min(width, LIVE_PICTURE_MAX_W) - hint_w, 0, hint_w, self._CAPTION_H),
                                 Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, hint)
            top = float(self._CAPTION_H + self._GAP)
            if self.ready():
                w, h = self._thumb(width)
                painter.drawPixmap(QPointF(0, top), self._pixmap(w, h))
                if self.isEnabled() and (self.underMouse() or self.isDown()):
                    painter.setPen(QPen(rgba(ACCENT, 0.55), 1))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRect(QRectF(0.5, top + 0.5, w - 1, h - 1))
                note = self._note_lines(width)
                if note is not None:
                    painter.setPen(QColor(TEXT_SUB))
                    note.draw(painter, QPointF(0, top + h + self._GAP))
            else:
                box_w = float(min(width, LIVE_PICTURE_MAX_W))
                lines = self._box_lines(width)
                if self.pending():
                    box = QRectF(0.5, top + 0.5, box_w - 1, max(LIVE_PICTURE_PENDING_PX, lines.height + 24) - 1)
                    pen = QPen(rgba(ACCENT, 0.4), 1)
                    pen.setStyle(Qt.PenStyle.DashLine)
                    color = TEXT_SOFT
                    opacity = 1.0 if _BlinkClock.is_on() else 0.15
                    paint_status_glyph(painter, QRectF(10, top + 12, 9, 9), live_events.STATUS_RUNNING, opacity)
                elif picture.state == live_events.PICTURE_UNAVAILABLE:
                    box = QRectF(0.5, top + 0.5, box_w - 1, lines.height + 15)
                    pen, color = QPen(rgba(AMBER, 0.55), 1), AMBER
                    painter.fillRect(box, rgba(AMBER, 0.05))
                    paint_status_glyph(painter, QRectF(10, top + 9, 9, 9), live_events.STATUS_WARN)
                else:
                    box = QRectF(0.5, top + 0.5, box_w - 1, lines.height + 15)
                    pen, color = QPen(rgba(OFF, 0.6), 1), TEXT_DIM
                    paint_status_glyph(painter, QRectF(10, top + 9, 9, 9), live_events.STATUS_CANCELLED)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(box)
                painter.setPen(QColor(color))
                lines.draw(painter, QPointF(26, top + 8))
            if self.focus_ring():
                painter.setPen(QPen(QColor(ACCENT), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))
        finally:
            painter.end()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        _sync_blink_clock(self, self.pending())

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        _blink_clock().unregister(self)
        self._pixmaps.clear()   # drawn again on the next paint; nothing kept while it is not seen


class _PictureCanvas(QWidget):
    """The viewer's picture: drawn once at its size (nothing in it reacts)."""

    def __init__(self) -> None:
        super().__init__()
        self._pixmap = QPixmap()
        self._source: tuple[Any, tuple[str, str], float] | None = None   # what is drawn, to draw it again
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    def set_picture(self, picture: Any, stamp: tuple[str, str], width: float) -> None:
        self._source = (picture, stamp, width)
        self._render()
        size = self._pixmap.deviceIndependentSize()
        self.setFixedSize(max(1, math.ceil(size.width())), max(1, math.ceil(size.height())))
        self.setAccessibleName(picture.alt or picture.caption)
        self.update()

    def _render(self) -> None:
        picture, stamp, width = self._source
        self._pixmap = picture_pixmap(picture, width, self.devicePixelRatioF() or 1.0, stamp, None)

    def pixmap(self) -> QPixmap:
        return self._pixmap

    def clear(self) -> None:
        """Let the picture go (the viewer closed): nothing of it stays drawn in memory."""
        self._pixmap = QPixmap()
        self._source = None
        self.setAccessibleName("")
        self.update()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        wanted = max(1.0, self.devicePixelRatioF() or 1.0)   # as picture_pixmap draws it
        if self._source is not None and abs(self._pixmap.devicePixelRatio() - wanted) > 0.01:
            self._render()   # on a screen with another scale now: drawn again, crisp there
        painter = QPainter(self)
        try:
            painter.drawPixmap(QPointF(0, 0), self._pixmap)
        finally:
            painter.end()


class PictureViewer(QDialog):
    """A LIVE picture, larger, inside Jarvis (non-modal): the caption and its note on top, the
    picture in a scroll area (an email or calendar picture drawn again at up to
    PICTURE_VIEWER_MODEL_W px, a page at its own width), and Close. Nothing in it reacts to the
    page or the email. Esc, Enter, Space and Ctrl+W close it; the arrows, Page Up / Down, Home and
    End scroll. ``closed(step_id)`` when it closes. At most PICTURE_VIEWER_SCREEN of the screen,
    centred on its parent window."""

    closed = Signal(int)
    _MARGIN = 16

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Dialog)
        self.setObjectName("PictureViewer")
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(GROUND))
        self.setPalette(palette)
        self._step_id = 0
        self._picture: Any = None
        self._stamp: tuple[str, str] = _NO_STAMP
        layout = QVBoxLayout(self)
        layout.setContentsMargins(self._MARGIN, 14, self._MARGIN, 14)
        layout.setSpacing(6)
        self.caption_label = make_label("", mono_font(11, 600, 0.04), TEXT_BRIGHT, wrap=True)
        self.note_label = make_label("", mono_font(10), TEXT_SUB, wrap=True)
        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("hudScroll")
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.scroll.viewport().setAutoFillBackground(False)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.canvas = _PictureCanvas()
        self.scroll.setWidget(self.canvas)
        self.close_button = HudButton("Close", SECONDARY, compact=True)
        self.close_button.setAccessibleName("Close")
        self.close_button.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 4, 0, 0)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout.addWidget(self.caption_label)
        layout.addWidget(self.note_label)
        layout.addWidget(self.scroll, 1)
        layout.addLayout(buttons)
        self.finished.connect(lambda _result: self.closed.emit(self._step_id))
        self.finished.connect(lambda _result: self._release())

    def step_id(self) -> int:
        return self._step_id

    def picture(self) -> Any:
        return self._picture

    def stamp(self) -> tuple[str, str]:
        return self._stamp

    def _area(self) -> QRect | None:
        parent = self.parentWidget()
        screen = (parent.window().screen() if parent is not None else self.screen()) or QApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else None

    def _draw_width(self, picture: Any) -> float:
        area = self._area()
        room = (area.width() * PICTURE_VIEWER_SCREEN if area is not None else 1200) - 2 * self._MARGIN - 14
        if picture.kind == live_pictures.KIND_PAGE and picture.page is not None:
            return float(max(120, min(room, PICTURE_VIEWER_PAGE_W, picture.page.width or PICTURE_VIEWER_PAGE_W)))
        return float(max(240, min(room, PICTURE_VIEWER_MODEL_W)))

    def _set(self, picture: Any, stamp: tuple[str, str]) -> None:
        self._picture, self._stamp = picture, stamp
        caption = picture.caption + (f" - {stamp[0]}" if stamp and stamp[0] else "")
        self.caption_label.setText(caption)
        self.note_label.setText(picture.note)
        self.note_label.setVisible(bool(picture.note))
        self.setWindowTitle(f"{picture.caption} - Jarvis")
        self.setAccessibleName(f"{picture.caption} picture, larger")
        # The alt is said once: by the scroll area, which takes the focus when the viewer opens (a
        # screen reader names it and reads the picture's description there). The dialog's own
        # description stays empty, or the alt would be read twice as the dialog comes up.
        self.setAccessibleDescription("")
        self.scroll.setAccessibleName(f"{picture.caption} picture")
        self.scroll.setAccessibleDescription(picture.alt)
        self.canvas.set_picture(picture, stamp, self._draw_width(picture))

    def show_picture(self, step_id: int, picture: Any, stamp: tuple[str, str] = _NO_STAMP, title: str = "") -> None:
        """Show ``picture`` (step ``step_id``'s), sized to it and centred on the parent window."""
        self._step_id = int(step_id)
        self._set(picture, tuple(stamp) if stamp else _NO_STAMP)
        if title:
            self.caption_label.setToolTip(plain_tooltip(title))
        self._fit()
        self.show()
        self.raise_()
        self.activateWindow()
        self.scroll.verticalScrollBar().setValue(0)
        self.scroll.setFocus(Qt.FocusReason.OtherFocusReason)

    def _release(self) -> None:
        """Closed: the picture and its drawing go (the viewer itself is kept for the next one)."""
        self._picture, self._stamp = None, _NO_STAMP
        self.canvas.clear()
        self.caption_label.setText("")
        self.note_label.setText("")
        self.setAccessibleDescription("")
        self.scroll.setAccessibleName("")
        self.scroll.setAccessibleDescription("")

    def refresh(self, picture: Any, stamp: tuple[str, str] = _NO_STAMP) -> None:
        """The step's picture (or its stamp) changed while it shows."""
        stamp = tuple(stamp) if stamp else _NO_STAMP
        if picture is self._picture and stamp == self._stamp:
            return
        self._set(picture, stamp)

    def _fit(self) -> None:
        area = self._area()
        self.ensurePolished()
        canvas = self.canvas.size()
        width = canvas.width() + 2 * self._MARGIN + 14
        layout = self.layout()
        layout.activate()
        inner = width - 2 * self._MARGIN
        chrome = (14 + 14 + self.caption_label.heightForWidth(inner) + 6
                  + (self.note_label.heightForWidth(inner) + 6 if self.note_label.isVisibleTo(self) else 0)
                  + self.close_button.sizeHint().height() + 10)
        height = chrome + canvas.height() + 4
        if area is not None:
            width = min(width, int(area.width() * PICTURE_VIEWER_SCREEN))
            height = min(height, int(area.height() * PICTURE_VIEWER_SCREEN))
        self.resize(max(280, width), max(200, height))
        parent = self.parentWidget()
        if parent is not None:
            window = parent.window()
            center = window.mapToGlobal(QPoint(window.width() // 2, window.height() // 2))
            x, y = center.x() - self.width() // 2, center.y() - self.height() // 2
            if area is not None:
                x = max(area.left(), min(x, area.right() + 1 - self.width()))
                y = max(area.top(), min(y, area.bottom() + 1 - self.height()))
            self.move(x, y)

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        key = event.key()
        if event.isAutoRepeat() and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            event.accept()   # the Enter held down that opened it: not a close
            return
        if key in (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) or (
                key == Qt.Key.Key_W and event.modifiers() & Qt.KeyboardModifier.ControlModifier) \
                or event.matches(QKeySequence.StandardKey.Close):
            event.accept()
            self.close()
            return
        bar = self.scroll.verticalScrollBar()
        if key == Qt.Key.Key_Home:
            bar.setValue(bar.minimum())
        elif key == Qt.Key.Key_End:
            bar.setValue(bar.maximum())
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            bar.triggerAction({Qt.Key.Key_Up: bar.SliderAction.SliderSingleStepSub,
                               Qt.Key.Key_Down: bar.SliderAction.SliderSingleStepAdd,
                               Qt.Key.Key_PageUp: bar.SliderAction.SliderPageStepSub,
                               Qt.Key.Key_PageDown: bar.SliderAction.SliderPageStepAdd}[key])
        else:
            super().keyPressEvent(event)
            return
        event.accept()


class _LiveDetails(QWidget):
    """A step's details, made on its first open: its picture first, then the fields, the items, the
    notes ("+1.2 s  ..."), the text blocks. Only what changed is redone (a block's text is set once
    per revision)."""

    def __init__(self, on_link: Callable[[str, QWidget | None], None],
                 on_picture: Callable[[], None] | None = None) -> None:
        super().__init__()
        fonts = _LiveFonts.get()
        self.picture = _LivePicture(on_picture)
        self.fields = _LiveFields(on_link)
        self.items = _LiveItems()
        self.notes = _LiveValue("", fonts.note, TEXT_SOFT)
        self.blocks: dict[str, _LiveBlock] = {}
        self._blocks_box = QWidget()
        self._blocks_layout = QVBoxLayout(self._blocks_box)
        self._blocks_layout.setContentsMargins(0, 0, 0, 0)
        self._blocks_layout.setSpacing(6)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(_LIVE_INDENT, 0, 4, 8)
        layout.setSpacing(6)
        for widget in (self.picture, self.fields, self.items, self.notes, self._blocks_box):
            layout.addWidget(widget)
            widget.hide()
        self._notes_key: tuple = ()

    def apply(self, view: live_events.StepView) -> bool:
        """Show ``view``; True when focusable widgets came or went."""
        changed = False
        picture = view.picture
        # None too: a picture that went (a keyed step replaced) is let go, not kept hidden.
        self.picture.set_picture(picture, live_pictures.stamp(picture, view.status, view.status_text))
        if self.picture.isHidden() != (picture is None):
            self.picture.setVisible(picture is not None)
            changed = True
        changed |= self.fields.set_fields(view.fields)
        self.fields.setVisible(bool(view.fields))
        self.items.set_items(view.items, view.items_more)
        self.items.setVisible(bool(view.items or view.items_more))
        notes_key = (view.notes, view.notes_more, view.started)
        if notes_key != self._notes_key:
            self._notes_key = notes_key
            # The stream keeps the first note and the newest ones: the gap is between them.
            more = _count(view.notes_more, "earlier note") + " not shown" if view.notes_more else ""
            lines = []
            for index, note in enumerate(view.notes):
                if index == 1 and more:
                    lines.append(more)
                lines.append(f"+{live_events.elapsed_text(note.at - view.started)}  {note.text}")
            if more and len(view.notes) == 1:
                lines.append(more)
            self.notes.set_text("\n".join(lines))
        self.notes.setVisible(bool(view.notes))
        labels = [block.label for block in view.blocks]
        for label in [label for label in self.blocks if label not in labels]:
            widget = self.blocks.pop(label)
            self._blocks_layout.removeWidget(widget)
            widget.hide()
            widget.deleteLater()
            changed = True
        for index, block in enumerate(view.blocks):
            widget = self.blocks.get(block.label)
            if widget is None:
                widget = self.blocks[block.label] = _LiveBlock(block)
                self._blocks_layout.insertWidget(index, widget)
                changed = True
            elif widget.block() != block:
                had_text = widget.text_edit is not None
                widget.set_block(block)
                changed |= had_text != (widget.text_edit is not None)
        self._blocks_box.setVisible(bool(view.blocks))
        return changed

    def sync_picture(self, view: live_events.StepView) -> None:
        """The details are closed: the picture still follows the step (a picture the stream dropped
        or replaced is let go here too, not kept hidden with its bytes and drawings)."""
        picture = view.picture
        self.picture.set_picture(picture, live_pictures.stamp(picture, view.status, view.status_text))

    def focusables(self) -> list[QWidget]:
        widgets: list[QWidget] = [self.picture] if not self.picture.isHidden() else []
        widgets.extend(self.fields.links())
        for block in self.blocks.values():
            widgets.extend(block.focusables())
        return widgets


class _LiveStepRow(_LiveButton):
    """A step's row: its status glyph (a running one blinks), title, summary, the status word over
    the elapsed time on the right, and a caret when there are details (click, Space or Enter
    shows / hides them). Painted, so a burst of steps stays cheap."""

    _GLYPH = 10
    _PAD = 5
    _GAP = 8
    _CARET = 10

    def __init__(self) -> None:
        super().__init__()
        self._view: live_events.StepView | None = None
        self._word = ""
        self._elapsed = ""
        self._open = False
        self._has_details = False
        self._cache: dict[int, tuple[_TextLines, _TextLines | None]] = {}
        self._right_chars = 9

    def view(self) -> live_events.StepView | None:
        return self._view

    def word(self) -> str:
        return self._word

    def elapsed(self) -> str:
        return self._elapsed

    def title(self) -> str:
        return self._view.title if self._view is not None else ""

    def is_open(self) -> bool:
        return self._open

    def set_view(self, view: live_events.StepView, now: float, open_: bool, has_details: bool) -> None:
        old = self._view
        if old is None or (old.title, old.summary) != (view.title, view.summary):
            self._cache.clear()
        self._view = view
        self._open, self._has_details = open_, has_details
        self._refresh(now, force=True)
        _sync_blink_clock(self, view.status == live_events.STATUS_RUNNING)
        self.updateGeometry()
        self.update()

    def tick(self, now: float) -> None:
        if self._view is not None and self._view.status == live_events.STATUS_RUNNING and self._refresh(now):
            self.update()

    def _refresh(self, now: float, force: bool = False) -> bool:
        view = self._view
        assert view is not None
        word = live_events.status_word(view, now)
        elapsed = live_events.elapsed_text(view.elapsed(now))
        if not force and (word, elapsed) == (self._word, self._elapsed):
            return False
        self._word, self._elapsed = word, elapsed
        countdown = view.deadline is not None and view.status == live_events.STATUS_RUNNING
        chars = max(len(word) + (1 if countdown else 0), len(elapsed), 9)
        if chars != self._right_chars:
            self._right_chars = chars
            self._cache.clear()
            self.updateGeometry()
        parts = [view.title, word, view.summary, elapsed]
        self.setAccessibleName(", ".join(part for part in parts if part))
        self.setAccessibleDescription(("details shown" if self._open else "details hidden") if self._has_details
                                      else "")
        return True

    def _right_width(self) -> float:
        return _text_advance(_LiveFonts.get().word, "M" * self._right_chars)

    def _texts(self, width: int) -> tuple[_TextLines, _TextLines | None]:
        cached = self._cache.get(width)
        if cached is None:
            if len(self._cache) > 8:
                self._cache.clear()
            fonts = _LiveFonts.get()
            text_width = width - _LIVE_INDENT - self._GAP - self._right_width() - 6 - self._CARET
            view = self._view
            title = _TextLines(view.title if view is not None else "", "step_title", text_width)
            summary = (_TextLines(view.summary, "summary", text_width)
                       if view is not None and view.summary else None)
            cached = self._cache[width] = (title, summary)
        return cached

    def _height_for(self, width: int) -> int:
        title, summary = self._texts(width)
        left = title.height + (2 + summary.height if summary is not None else 0)
        right = 2 * QFontMetricsF(_LiveFonts.get().word).lineSpacing()
        return math.ceil(self._PAD + max(left, right) + self._PAD)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        view = self._view
        if view is None:
            return
        fonts = _LiveFonts.get()
        width = max(1, self.width())
        title, summary = self._texts(width)
        painter = QPainter(self)
        try:
            rect = QRectF(self.rect())
            if self.isEnabled() and (self.underMouse() or self.isDown()):
                painter.fillRect(rect, rgba(ACCENT, 0.05))
            first_line = QFontMetricsF(fonts.step_title).lineSpacing()
            opacity = (1.0 if _BlinkClock.is_on() else 0.15) if view.status == live_events.STATUS_RUNNING else 1.0
            paint_status_glyph(painter, QRectF(2, self._PAD + (first_line - self._GLYPH) / 2, self._GLYPH,
                                               self._GLYPH), view.status, opacity)
            painter.setPen(QColor(TEXT_BODY))
            title.draw(painter, QPointF(_LIVE_INDENT, self._PAD))
            if summary is not None:
                painter.setPen(QColor(TEXT_SUB))
                summary.draw(painter, QPointF(_LIVE_INDENT, self._PAD + title.height + 2))
            line = QFontMetricsF(fonts.word).lineSpacing()
            right_width = self._right_width()
            right = width - self._CARET - 6
            countdown = view.deadline is not None and view.status == live_events.STATUS_RUNNING
            color = AMBER if countdown else live_status_color(view.status)
            painter.setFont(fonts.word)
            painter.setPen(QColor(color))
            align = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            painter.drawText(QRectF(right - right_width, self._PAD, right_width, line), align, self._word)
            painter.setFont(fonts.time)
            painter.setPen(QColor(TEXT_TIME))
            painter.drawText(QRectF(right - right_width, self._PAD + line, right_width, line), align, self._elapsed)
            if self._has_details:
                _paint_caret(painter, QPointF(width - self._CARET / 2, self._PAD + line / 2), self._open, TEXT_MUTED)
            if self.focus_ring():
                painter.setPen(QPen(QColor(ACCENT), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
        finally:
            painter.end()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        _sync_blink_clock(self, self._view is not None and self._view.status == live_events.STATUS_RUNNING)

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        _blink_clock().unregister(self)


class _LiveStep(QWidget):
    """One step: its row and, below it, its details (made on the first open). The details follow
    the step's ``open_hint`` until the owner opens or closes them; then his choice sticks."""

    def __init__(self, log: LiveLog) -> None:
        super().__init__()
        self._log = log
        self.row = _LiveStepRow()
        self.row.clicked.connect(self.toggle)
        self.details: _LiveDetails | None = None
        self.view: live_events.StepView | None = None
        self._user_open: bool | None = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._layout.addWidget(self.row)

    @staticmethod
    def has_details(view: live_events.StepView) -> bool:
        return bool(view.fields or view.items or view.items_more or view.notes or view.blocks
                    or view.picture is not None)

    def is_open(self) -> bool:
        view = self.view
        if view is None or not self.has_details(view):
            return False
        return self._user_open if self._user_open is not None else view.open_hint

    def set_view(self, view: live_events.StepView, now: float) -> bool:
        """Show ``view``; True when focusable widgets came or went."""
        self.view = view
        open_ = self.is_open()
        changed = False
        if open_:
            if self.details is None:
                self.details = _LiveDetails(self._log.follow_link, self._open_picture)
                self._layout.addWidget(self.details)
                changed = True
            changed |= self.details.apply(view)
            if self.details.isHidden():
                self.details.show()
                changed = True
        elif self.details is not None:
            self.details.sync_picture(view)
            if not self.details.isHidden():
                self.details.hide()
                changed = True
        self.row.set_view(view, now, open_, self.has_details(view))
        return changed

    def toggle(self) -> None:
        if self.view is None or not self.has_details(self.view):
            return
        self._user_open = not self.is_open()
        self.set_view(self.view, self._log.now())
        self._log._structure_changed()

    def _open_picture(self) -> None:
        if self.view is not None:
            self._log.open_picture(self.view.id)

    def picture_widget(self) -> _LivePicture | None:
        """The picture in the details (None while they are closed or were never opened, or there is
        none)."""
        details = self.details
        if (details is None or details.isHidden() or details.picture.isHidden()
                or details.picture.picture() is None):
            return None
        return details.picture

    def set_open(self, open_: bool) -> None:
        if self.is_open() != bool(open_):
            self.toggle()

    def focusables(self) -> list[QWidget]:
        widgets: list[QWidget] = [self.row]
        if self.details is not None and not self.details.isHidden():
            widgets.extend(self.details.focusables())
        return widgets


class _LiveTaskHeader(_LiveButton):
    """A task's header: the tag pill (ASK, ACTION, ...), the title (Sora 13, at most two lines,
    one for a compact task; the whole title in a plain tooltip), the status pill on the right
    (under the title when the header is narrower than LIVE_PILL_WRAP_PX; RUNNING counts its time,
    a countdown its seconds) and the meta line ("2:41:07 PM - 6 steps - 2 cards"). Click, Space
    or Enter shows / hides the steps."""

    _PAD = 3
    _TAG_H = 16
    _PILL_H = 18
    _CARET = 10

    def __init__(self) -> None:
        super().__init__()
        self._view: live_events.TaskView | None = None
        self._expanded = True
        self._hour24 = False
        self._word = ""
        self._word_color = TEXT_DIM
        self._pill_chars = 9
        self._cache: dict[int, tuple] = {}

    def view(self) -> live_events.TaskView | None:
        return self._view

    def word(self) -> str:
        return self._word

    def meta(self) -> str:
        view = self._view
        if view is None:
            return ""
        parts = [_when_text(view.at, self._hour24), _count(len(view.steps) + view.steps_more, "step")]
        if view.summary:
            parts.append(view.summary)
        return " - ".join(part for part in parts if part)

    def shown_title(self) -> str:
        """The title as painted at the current width (cut to its lines with an ellipsis)."""
        return self._layout_at(max(1, self.width()))[0].shown

    def set_view(self, view: live_events.TaskView, now: float, expanded: bool, hour24: bool) -> None:
        self._view = view
        self._expanded = expanded
        self._hour24 = hour24
        self._cache.clear()
        self.setToolTip(plain_tooltip(view.title))
        self._refresh(now, force=True)
        _sync_blink_clock(self, view.status == live_events.STATUS_RUNNING)
        self.updateGeometry()
        self.update()

    def tick(self, now: float) -> None:
        if self._view is not None and self._view.status == live_events.STATUS_RUNNING and self._refresh(now):
            self.update()

    def _refresh(self, now: float, force: bool = False) -> bool:
        view = self._view
        assert view is not None
        word, color = _task_word(view, now)
        if not force and (word, color) == (self._word, self._word_color):
            return False
        self._word, self._word_color = word, color
        chars = max(len(word), 13 if view.status == live_events.STATUS_RUNNING else 0, 4)
        if chars != self._pill_chars:
            self._pill_chars = chars
            self._cache.clear()
            self.updateGeometry()
        state = "steps shown" if self._expanded else "steps hidden"
        self.setAccessibleName(f"{view.tag} task: {view.title}, {word}, {self.meta()}, {state}")
        return True

    def _tag_width(self) -> float:
        return _text_advance(_LiveFonts.get().tag, self._view.tag if self._view is not None else "") + 10

    def _pill_width(self) -> float:
        return 6 + 8 + 5 + _text_advance(_LiveFonts.get().word, "M" * self._pill_chars) + 7

    def _layout_at(self, width: int) -> tuple:
        """(title lines, meta lines or None, title x, pill rect, meta y, height) at ``width``."""
        cached = self._cache.get(width)
        if cached is not None:
            return cached
        if len(self._cache) > 8:
            self._cache.clear()
        fonts = _LiveFonts.get()
        view = self._view
        title_x = self._tag_width() + 8
        pill_w = self._pill_width()
        wide = width >= LIVE_PILL_WRAP_PX
        title_w = width - title_x - (pill_w + 8 if wide else 0)
        compact = view is not None and view.compact and not self._expanded
        title = _TextLines(view.title if view is not None else "", "task_title", title_w,
                           max_lines=1 if (view is not None and view.compact) else 2)
        top = self._PAD
        bottom = top + 1 + title.height
        if wide:
            pill = QRectF(width - pill_w, top, pill_w, self._PILL_H)
            bottom = max(bottom, top + self._PILL_H)
        else:
            pill = QRectF(title_x, bottom + 3, pill_w, self._PILL_H)
            bottom = pill.bottom()
        meta = None if compact else _TextLines(self.meta(), "time", width - title_x - self._CARET - 6)
        meta_y = bottom + 3
        height = (meta_y + meta.height if meta is not None else bottom) + self._PAD + 1
        result = (title, meta, title_x, pill, meta_y, height)
        self._cache[width] = result
        return result

    def _height_for(self, width: int) -> int:
        return math.ceil(self._layout_at(width)[5])

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        view = self._view
        if view is None:
            return
        fonts = _LiveFonts.get()
        width = max(1, self.width())
        title, meta, title_x, pill, meta_y, height = self._layout_at(width)
        painter = QPainter(self)
        try:
            rect = QRectF(self.rect())
            if self.isEnabled() and (self.underMouse() or self.isDown()):
                painter.fillRect(rect, rgba(ACCENT, 0.04))
            first_line = QFontMetricsF(fonts.task_title).lineSpacing()
            tag_rect = QRectF(0, self._PAD + 1 + (first_line - self._TAG_H) / 2, self._tag_width(), self._TAG_H)
            _paint_pill(painter, tag_rect, view.tag, fonts.tag, LIVE_TAG_COLORS.get(view.kind, TEXT_DIM),
                        filled=True)
            painter.setPen(QColor(TEXT_BRIGHT))
            title.draw(painter, QPointF(title_x, self._PAD + 1))
            glyph = view.status if not (view.pinned and view.status == live_events.STATUS_OK) else ""
            opacity = (1.0 if _BlinkClock.is_on() else 0.15) if view.status == live_events.STATUS_RUNNING else 1.0
            _paint_pill(painter, pill, self._word, fonts.word, self._word_color, filled=False, glyph=glyph,
                        glyph_opacity=opacity)
            caret_y = (meta_y + QFontMetricsF(fonts.time).lineSpacing() / 2) if meta is not None else pill.center().y()
            if meta is not None:
                painter.setPen(QColor(TEXT_TIME))
                meta.draw(painter, QPointF(title_x, meta_y))
                _paint_caret(painter, QPointF(width - self._CARET / 2, caret_y), self._expanded, TEXT_MUTED)
            elif pill.left() > title_x + 1 and not (width >= LIVE_PILL_WRAP_PX):
                _paint_caret(painter, QPointF(width - self._CARET / 2, caret_y), self._expanded, TEXT_MUTED)
            if self.focus_ring():
                painter.setPen(QPen(QColor(ACCENT), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
        finally:
            painter.end()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        _sync_blink_clock(self, self._view is not None and self._view.status == live_events.STATUS_RUNNING)

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        _blink_clock().unregister(self)


def _running_countdown(view: live_events.TaskView) -> live_events.StepView | None:
    return next((step for step in reversed(view.steps)
                 if step.status == live_events.STATUS_RUNNING and step.deadline is not None), None)


def _task_word(view: live_events.TaskView, now: float) -> tuple[str, str]:
    """The task's status pill: "RUNNING 0:12", "SENDING IN 7 S" (a countdown), "DONE", ...; the
    rolling background task reads "RUNNING" / "IDLE"."""
    if view.pinned:
        if view.status == live_events.STATUS_RUNNING:
            return "RUNNING", ACCENT
        return ("IDLE", TEXT_DIM) if view.status == live_events.STATUS_OK else (live_events.status_word(view),
                                                                               live_status_color(view.status))
    if view.status == live_events.STATUS_RUNNING:
        countdown = _running_countdown(view)
        if countdown is not None:
            return live_events.status_word(countdown, now), AMBER
        return f"RUNNING {live_events.elapsed_text(view.elapsed(now))}", ACCENT
    return live_events.status_word(view), live_status_color(view.status)


class _LiveTaskCard(QWidget):
    """One task: a chamfered card (its border in the task's status colour), the header and the
    steps ("N more steps not shown" past the stream's limit). A compact task (briefing fetch,
    background reads) is its header only until opened; the owner's choice sticks."""

    def __init__(self, log: LiveLog) -> None:
        super().__init__()
        self._log = log
        self.view: live_events.TaskView | None = None
        self.header = _LiveTaskHeader()
        self.header.clicked.connect(self.toggle)
        self.steps_box = QWidget()
        self._steps_layout = QVBoxLayout(self.steps_box)
        self._steps_layout.setContentsMargins(0, 2, 0, 0)
        self._steps_layout.setSpacing(0)
        self.more_label = _live_label("", _LiveFonts.get().note, TEXT_DIM)
        self.more_label.hide()
        self._steps: dict[int, _LiveStep] = {}
        self._order: list[int] = []
        self._user_expanded: bool | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(2)
        layout.addWidget(self.header)
        layout.addWidget(self.steps_box)
        layout.addWidget(self.more_label)

    @property
    def revision(self) -> int:
        return self.view.revision if self.view is not None else -1

    def expanded(self) -> bool:
        if self._user_expanded is not None:
            return self._user_expanded
        return self.view is not None and not self.view.compact

    def set_view(self, view: live_events.TaskView, now: float) -> bool:
        """Show ``view``; True when focusable widgets came or went."""
        self.view = view
        changed = False
        expanded = self.expanded()
        if expanded or self._steps:
            ids = [step.id for step in view.steps]
            for step_view in view.steps:
                widget = self._steps.get(step_view.id)
                if widget is None:
                    widget = self._steps[step_view.id] = _LiveStep(self._log)
                    self._steps_layout.addWidget(widget)
                    changed |= widget.set_view(step_view, now) or True
                elif widget.view is None or widget.view.revision != step_view.revision:
                    changed |= widget.set_view(step_view, now)
            for step_id in [step_id for step_id in self._steps if step_id not in ids]:
                widget = self._steps.pop(step_id)
                self._steps_layout.removeWidget(widget)
                widget.hide()
                widget.deleteLater()
                changed = True
            if ids != self._order:
                kept = [step_id for step_id in self._order if step_id in ids]
                if kept + [step_id for step_id in ids if step_id not in self._order] != ids:
                    for index, step_id in enumerate(ids):   # out of order: put every row in its place
                        self._steps_layout.insertWidget(index, self._steps[step_id])
                    changed = True
                self._order = ids
        if self.steps_box.isHidden() == expanded:
            self.steps_box.setVisible(expanded)
            changed = True
        more = view.steps_more
        self.more_label.setText(f"{more:,} more steps not shown" if more != 1 else "1 more step not shown")
        self.more_label.setVisible(bool(more) and expanded)
        self.header.set_view(view, now, expanded, self._log.hour24())
        self.update()
        return changed

    def toggle(self) -> None:
        self._user_expanded = not self.expanded()
        if self.view is not None:
            self.set_view(self.view, self._log.now())
        self._log._structure_changed()

    def set_expanded(self, expanded: bool) -> None:
        if self.expanded() != bool(expanded):
            self.toggle()

    def tick(self, now: float) -> None:
        self.header.tick(now)
        if self.expanded():
            for widget in self._steps.values():
                widget.row.tick(now)

    def step_widgets(self) -> list[_LiveStep]:
        return [self._steps[step_id] for step_id in self._order if step_id in self._steps]

    def step_widget(self, step_id: int) -> _LiveStep | None:
        return self._steps.get(step_id)

    def newest_step(self) -> _LiveStep | None:
        steps = self.step_widgets()
        return steps[-1] if steps and self.expanded() else None

    def focusables(self) -> list[QWidget]:
        widgets: list[QWidget] = [self.header]
        if self.expanded():
            for step in self.step_widgets():
                widgets.extend(step.focusables())
        return widgets

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        view = self.view
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect())
            status = view.status if view is not None else live_events.STATUS_OK
            border = _LIVE_BORDERS.get(status, rgba(ACCENT, 0.18))
            if view is not None and view.pinned:
                border = rgba(ACCENT, 0.3 if status == live_events.STATUS_RUNNING else 0.12)
            corners = CUT_TL | CUT_BR
            painter.fillPath(chamfer_path(rect, 8, corners), border)
            painter.fillPath(chamfer_path(rect.adjusted(1, 1, -1, -1), 8, corners), PANEL_GROUND)
        finally:
            painter.end()


class LiveLog(QScrollArea):
    """The LIVE view: every task Jarvis works on (an Ask, an approved card, a briefing fetch, the
    rolling "Background reads" pinned first) as a card of its steps, newest at the bottom.

    ``apply(changes)`` takes a live.LiveChanges (only tasks whose revision changed are redone;
    ``removed`` ids go; ``reset`` replaces everything). Every text is plain text (painted, or a
    PlainText QLabel / read-only QPlainTextEdit); nothing is a link but a field's result link
    (``linkClicked(link)``: a Jarvis-made Google result link or an internal id such as
    "tab:jarvis"; a "picture:<step id>" link is followed here: follow_link), and nothing loads
    from anywhere. Details and text blocks are made on their
    first open. The elapsed times tick with the view's own timer: every 250 ms while a countdown
    shows, every second while a step runs, never while hidden, minimized or idle; running glyphs
    blink on the shared blink clock. It follows the newest step unless the owner scrolled in the
    last ``SCROLL_HOLD_S`` seconds (a new task is shown from its header; what came during the hold
    is followed when it ends). While an approved card counts down, what it will send or change
    (its payload step) stays in view, and a card the owner just approved comes into view whatever
    he scrolled. ``reveal(task_id)`` scrolls to a task and opens it. The empty-state text shows
    while no task but the pinned "Background reads" is there. The column is at most
    LIVE_CONTENT_MAX_PX wide. ``clock``: monotonic seconds (the stream's clock).
    """

    linkClicked = Signal(str)
    SCROLL_HOLD_S = ConversationLog.SCROLL_HOLD_S
    TICK_RUNNING_MS = 1000
    TICK_COUNTDOWN_MS = 250
    _GAP = 6
    _CHANGE_MARGIN = 24   # px kept under a calendar picture's change when the countdown scrolls to it

    def __init__(self, parent: QWidget | None = None, *, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(parent)
        _LiveFonts.get()
        content = _transparent_scroll(self)
        self._layout = QVBoxLayout(content)
        self._layout.setContentsMargins(_CONVERSATION_MARGIN, 10, _CONVERSATION_MARGIN, 10)
        self._layout.setSpacing(10)
        self.empty_label = make_label(LIVE_EMPTY_TEXT, body_font(13), TEXT_DIM, wrap=True)
        self._layout.addWidget(self.empty_label)
        self._layout.addStretch(1)
        self._clock = clock
        self._hour24 = False
        self._cards: dict[int, _LiveTaskCard] = {}
        self._order: list[int] = []
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.tick)
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._hold_over)
        self._missed = False
        self._user_scrolled_at = -math.inf
        self._follow = True
        self._header_first = False
        self._tab_order_pending = False
        self._viewer: PictureViewer | None = None   # made on the first picture opened larger (kept)
        self._viewer_ring = False                   # its opener had a keyboard focus ring when opened
        self._viewer_opener: QWidget | None = None  # the "Picture [n]" link it was opened from (None: a thumbnail)
        self._viewer_restore = True                 # its close gives the focus back (False: Jarvis closed it unseen)
        self.apply_count = 0
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        bar = self.verticalScrollBar()
        bar.actionTriggered.connect(self._on_user_scroll)
        bar.rangeChanged.connect(self._on_range_changed)

    # ---- API ----------------------------------------------------------------------------------

    def now(self) -> float:
        return self._clock()

    def hour24(self) -> bool:
        return self._hour24

    def set_hour24(self, hour24: bool) -> None:
        """The clock of the task headers' times ("2:41:07 PM" or "14:41:07")."""
        if bool(hour24) != self._hour24:
            self._hour24 = bool(hour24)
            now = self.now()
            for card in self._cards.values():
                if card.view is not None:
                    card.header.set_view(card.view, now, card.expanded(), self._hour24)

    def apply(self, changes: live_events.LiveChanges) -> None:
        """Show what changed in the stream (only tasks whose revision changed are redone)."""
        self.apply_count += 1
        now = self.now()
        created: set[int] = set()
        self.setUpdatesEnabled(False)
        try:
            if changes.reset:
                keep = {view.id for view in changes.tasks}
                for task_id in [task_id for task_id in self._cards if task_id not in keep]:
                    self._remove(task_id)
            for task_id in changes.removed:
                if task_id in self._cards:
                    self._remove(task_id)
            for view in changes.tasks:
                card = self._cards.get(view.id)
                if card is None:
                    card = self._cards[view.id] = _LiveTaskCard(self)
                    self._insert(view, card)
                    card.set_view(view, now)
                    created.add(view.id)
                elif card.revision != view.revision:
                    card.set_view(view, now)
            self._sync_empty()
        finally:
            self.setUpdatesEnabled(True)
        self._sync_timer()
        self._sync_viewer()
        if any(view.id in created and view.kind == live_events.TASK_ACTION and view.status == live_events.STATUS_RUNNING
               for view in changes.tasks):
            # A card the owner just approved: its payload comes into view whatever the owner scrolled.
            self._user_scrolled_at = -math.inf
            self._follow = True
        if changes.tasks or changes.removed or changes.reset:
            # A task that just came is shown from its header (not a pinned row that came later).
            self._header_first = bool(self._order) and self._order[-1] in created
            self._follow_newest()

    def clear(self) -> None:
        """Empty the view (the stream is unchanged); a picture shown larger closes."""
        for task_id in list(self._cards):
            self._remove(task_id)
        self._sync_empty()
        self._sync_timer()
        self._close_viewer()

    # ---- pictures ------------------------------------------------------------------------------

    def picture_viewer(self) -> PictureViewer | None:
        """The viewer of pictures shown larger (None until one was opened)."""
        viewer = self._viewer
        return viewer if viewer is not None and shiboken6.isValid(viewer) else None

    def open_picture(self, step_id: int, opener: QWidget | None = None) -> bool:
        """Show step ``step_id``'s READY picture larger in the viewer (inside Jarvis, its parent this
        log's window: the main window or the pop-out). False when the step or a READY picture is
        not there. Its close gives the focus back to ``opener`` (a link in this log it was opened
        from), else to the step's thumbnail."""
        step = self.step_widget(step_id)
        view = step.view if step is not None else None
        picture = view.picture if view is not None else None
        if picture is None or picture.state != live_events.PICTURE_READY:
            return False
        if opener is not None and shiboken6.isValid(opener) and self.isAncestorOf(opener):
            self._viewer_opener = opener
            self._viewer_ring = isinstance(opener, _LiveButton) and opener.focus_ring()
        else:
            thumbnail = step.picture_widget()
            self._viewer_opener = None
            self._viewer_ring = thumbnail is not None and thumbnail.focus_ring()
        viewer = self.picture_viewer()
        if viewer is None or viewer.parentWidget() is not self.window():
            if viewer is not None:
                viewer.hide()
                viewer.deleteLater()
            viewer = self._viewer = PictureViewer(self.window())
            viewer.closed.connect(self._viewer_closed)
        viewer.show_picture(step_id, picture, live_pictures.stamp(picture, view.status, view.status_text), view.title)
        return True

    def follow_link(self, link: str, opener: QWidget | None = None) -> None:
        """A field's link: "picture:<step id>" (a research's check pointing at a source's picture)
        shows that picture larger here when it is READY (its close comes back to ``opener``, the
        link pressed), else that step (its picture on its way or unavailable; when ``opener`` had
        the focus, that step's picture takes it, with a ring if the link had one: the focus never
        stays on a link scrolled out of view); every other link goes out as ``linkClicked``."""
        if not isinstance(link, str) or not link:
            return
        if link.startswith(live_events.PICTURE_LINK):
            try:
                step_id = int(link[len(live_events.PICTURE_LINK):])
            except ValueError:
                return
            if opener is None:
                focused = QApplication.focusWidget()
                opener = focused if focused is not None and self.isAncestorOf(focused) else None
            if not self.open_picture(step_id, opener):
                focus = None
                if opener is not None and shiboken6.isValid(opener) and opener.hasFocus():
                    ring = isinstance(opener, _LiveButton) and opener.focus_ring()
                    focus = Qt.FocusReason.ShortcutFocusReason if ring else Qt.FocusReason.OtherFocusReason
                self.reveal_step(step_id, focus)
            return
        self.linkClicked.emit(link)

    def reveal_step(self, step_id: int, focus: Qt.FocusReason | None = None) -> bool:
        """Scroll to step ``step_id`` with its details open (its task opened too); False when it is
        not shown. The view then holds there like after the owner's own scroll. With ``focus`` (the
        link followed here had the keyboard focus) the step's picture (its row when it shows none)
        takes the focus for that reason once in view, while the focus is still in this log."""
        card = next((card for card in self._cards.values() if card.step_widget(step_id) is not None), None)
        if card is None:
            return False
        card.set_expanded(True)
        step = card.step_widget(step_id)
        if step is None:
            return False
        step.set_open(True)
        self._user_scrolled_at = time.monotonic()
        self._follow = False
        self.widget().layout().activate()
        QTimer.singleShot(0, lambda: self._scroll_to_step(step_id, focus))
        return True

    def _scroll_to_step(self, step_id: int, focus: Qt.FocusReason | None = None) -> None:
        if not shiboken6.isValid(self):
            return
        step = self.step_widget(step_id)
        if step is None or not step.isVisible():
            return
        bar = self.verticalScrollBar()
        top = step.mapTo(self.widget(), QPoint(0, 0)).y()
        bar.setValue(max(0, min(bar.maximum(), top - self._GAP)))
        current = QApplication.focusWidget()
        if focus is None or current is None or not self.isAncestorOf(current):
            return   # a click, or the focus went elsewhere meanwhile: it is not taken
        picture = step.details.picture if step.details is not None else None
        target = picture if picture is not None and picture.isVisible() else step.row
        if target.isVisible():
            target.setFocus(focus)
            self.ensureWidgetVisible(target, 0, self._GAP)

    def _sync_viewer(self) -> None:
        """The viewer follows its step: a new picture or stamp is shown again; it closes when the
        step is gone or its picture is no longer READY."""
        viewer = self.picture_viewer()
        if viewer is None or not viewer.isVisible():
            return
        step = self.step_widget(viewer.step_id())
        view = step.view if step is not None else None
        picture = view.picture if view is not None else None
        if picture is None or picture.state != live_events.PICTURE_READY:
            self._close_viewer()
            return
        viewer.refresh(picture, live_pictures.stamp(picture, view.status, view.status_text))

    def _close_viewer(self) -> None:
        """Jarvis closes the viewer (its picture went, Clear): the focus goes back to the thumbnail
        only when the owner was in the viewer - never taken from another window or app."""
        viewer = self.picture_viewer()
        if viewer is not None and viewer.isVisible():
            self._viewer_restore = viewer.isActiveWindow()
            try:
                viewer.close()
            finally:
                self._viewer_restore = True

    def _viewer_closed(self, step_id: int) -> None:
        """Back to where it was opened from (with its ring when it had one), when the owner closed
        it or was in it: the "Picture [n]" link, scrolled into view, else the step's thumbnail."""
        if not shiboken6.isValid(self):
            return
        opener, self._viewer_opener = self._viewer_opener, None
        if not self._viewer_restore:
            return
        reason = Qt.FocusReason.ShortcutFocusReason if self._viewer_ring else Qt.FocusReason.OtherFocusReason
        if opener is not None and shiboken6.isValid(opener) and opener.isVisible() and self.isAncestorOf(opener):
            opener.window().activateWindow()
            opener.setFocus(reason)
            self.ensureWidgetVisible(opener, 0, self._GAP)
            return
        step = self.step_widget(step_id)
        thumbnail = step.picture_widget() if step is not None else None
        if thumbnail is not None and thumbnail.isVisible():
            thumbnail.window().activateWindow()
            thumbnail.setFocus(reason)

    def _sync_empty(self) -> None:
        """The empty-state text while no task but the pinned "Background reads" is shown (a pinned
        task with no steps left, after Clear, is not shown at all)."""
        tasks = False
        for card in self._cards.values():
            view = card.view
            if view is None:
                continue
            empty_pinned = view.pinned and not view.steps and not view.steps_more
            if card.isHidden() != empty_pinned:
                card.setVisible(not empty_pinned)
            tasks |= not view.pinned
        self.empty_label.setVisible(not tasks)

    def task_ids(self) -> list[int]:
        """The tasks shown, in display order (the pinned one first, then oldest -> newest)."""
        return list(self._order)

    def task_widget(self, task_id: int) -> _LiveTaskCard | None:
        return self._cards.get(task_id)

    def step_widget(self, step_id: int) -> _LiveStep | None:
        for card in self._cards.values():
            widget = card.step_widget(step_id)
            if widget is not None:
                return widget
        return None

    def reveal(self, task_id: int) -> bool:
        """Scroll to that task's header and open its steps (False when it is not shown); the view
        then holds there like after the owner's own scroll."""
        card = self._cards.get(task_id)
        if card is None:
            return False
        card.set_expanded(True)
        self._user_scrolled_at = time.monotonic()
        self._follow = False
        self.widget().layout().activate()
        bar = self.verticalScrollBar()
        bar.setValue(max(0, min(bar.maximum(), card.y() - self._GAP)))
        QTimer.singleShot(0, lambda: self._scroll_to_card(task_id))
        return True

    def following(self) -> bool:
        return self._follow

    def scroll_to_newest(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(self._newest_target(bar.maximum()))

    def ticking(self) -> int:
        """The tick interval in ms while the view's timer runs, else 0."""
        return self._timer.interval() if self._timer.isActive() else 0

    def focusables(self) -> list[QWidget]:
        widgets: list[QWidget] = []
        for task_id in self._order:
            widgets.extend(self._cards[task_id].focusables())
        return widgets

    # ---- cards ---------------------------------------------------------------------------------

    @staticmethod
    def _order_key(view: live_events.TaskView) -> tuple[int, int]:
        return (0 if view.pinned else 1, view.id)

    def _insert(self, view: live_events.TaskView, card: _LiveTaskCard) -> None:
        key = self._order_key(view)
        index = 0
        while index < len(self._order):
            other = self._cards[self._order[index]].view
            if other is not None and self._order_key(other) > key:
                break
            index += 1
        self._order.insert(index, view.id)
        self._layout.insertWidget(1 + index, card)   # after the empty label

    def _remove(self, task_id: int) -> None:
        card = self._cards.pop(task_id, None)
        if task_id in self._order:
            self._order.remove(task_id)
        if card is not None:
            self._layout.removeWidget(card)
            card.hide()
            card.deleteLater()

    def _structure_changed(self) -> None:
        """A step or a block was opened or closed by hand: the timer again."""
        self._sync_timer()

    def focusNextPrevChild(self, next_: bool) -> bool:  # noqa: N802 - Qt override
        """Tab / Shift+Tab inside the log go in the order the rows are shown (task header, its step
        rows, an open step's links and text blocks, the next task), whatever order the widgets were
        made in; past either end, on to the window's next focus widget outside the log."""
        focus = QApplication.focusWidget()
        items = [widget for widget in self.focusables() if widget.isVisibleTo(self) and widget.isEnabled()]
        if focus is None or focus not in items:
            return super().focusNextPrevChild(next_)
        reason = Qt.FocusReason.TabFocusReason if next_ else Qt.FocusReason.BacktabFocusReason
        index = items.index(focus) + (1 if next_ else -1)
        if 0 <= index < len(items):
            items[index].setFocus(reason)
            self.ensureWidgetVisible(items[index], 0, self._GAP)
            return True
        widget = focus
        for _ in range(2000):   # leave the log: the window's next (previous) Tab stop outside it
            widget = widget.nextInFocusChain() if next_ else widget.previousInFocusChain()
            if widget is None or widget is focus:
                break
            if (not self.isAncestorOf(widget) and widget is not self and widget.isVisible() and widget.isEnabled()
                    and widget.focusPolicy() & Qt.FocusPolicy.TabFocus and widget.window() is self.window()):
                widget.setFocus(reason)
                return True
        return super().focusNextPrevChild(next_)

    # ---- ticking -------------------------------------------------------------------------------

    def _wanted_interval(self) -> int:
        window = self.window()
        if not self.isVisible() or (window is not None and window.isMinimized()):
            return 0
        interval = 0
        for card in self._cards.values():
            view = card.view
            if view is None or view.status != live_events.STATUS_RUNNING:
                continue
            if _running_countdown(view) is not None:
                return self.TICK_COUNTDOWN_MS
            if not view.pinned or any(step.status == live_events.STATUS_RUNNING for step in view.steps):
                interval = self.TICK_RUNNING_MS
        return interval

    def _sync_timer(self) -> None:
        interval = self._wanted_interval()
        if not interval:
            self._timer.stop()
        elif not self._timer.isActive() or self._timer.interval() != interval:
            self._timer.start(interval)

    def tick(self) -> None:
        """Count the running steps' times and the countdowns on (the timer calls it)."""
        now = self.now()
        for card in self._cards.values():
            if card.view is not None and card.view.status == live_events.STATUS_RUNNING:
                card.tick(now)
        self._sync_timer()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        window = self.window()
        if window is not self:
            window.installEventFilter(self)
        self.tick()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self._timer.stop()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if event.type() == QEvent.Type.WindowStateChange and watched is self.window():
            QTimer.singleShot(0, self._resync)
        return False

    def _resync(self) -> None:
        if shiboken6.isValid(self):
            self.tick()

    # ---- following the newest step -----------------------------------------------------------

    def _newest_card(self) -> _LiveTaskCard | None:
        return self._cards[self._order[-1]] if self._order else None

    def _countdown_card(self) -> _LiveTaskCard | None:
        """The newest approved card's task still in its undo countdown (shown, with its steps)."""
        for task_id in reversed(self._order):
            card = self._cards[task_id]
            view = card.view
            if (view is not None and view.kind == live_events.TASK_ACTION and view.status == live_events.STATUS_RUNNING
                    and _running_countdown(view) is not None and card.expanded() and card.isVisibleTo(self)):
                return card
        return None

    def _countdown_target(self, maximum: int) -> int | None:
        """While an approved card counts down: the whole task when it fits the view, else from what
        it will send or change (its payload step), so that is in view before it goes out."""
        card = self._countdown_card()
        if card is None or card.height() <= 0 or card.view is None:
            return None
        content = self.widget()
        view_height = self.viewport().height()
        top = card.y()
        if card.height() + 2 * self._GAP <= view_height:
            return max(0, min(maximum, top + card.height() + self._GAP - view_height))
        payload = next((step for step in card.step_widgets()
                        if step.view is not None and step.view.kind == live_events.ACTION_PAYLOAD), None)
        if payload is not None and payload.isVisibleTo(self) and payload.height() > 0:
            top = payload.mapTo(content, QPoint(0, 0)).y()
            details = payload.details
            picture = details.picture if details is not None else None
            fields = details.fields if details is not None else None
            if picture is not None and picture.isVisibleTo(self) and picture.height() > 0:
                # What will be sent or changed, drawn: its top in view (the row above it when both fit);
                # a calendar change in a view too short for it: the changed blocks themselves (they sit
                # well below the picture's top, under the headline and the morning hours).
                picture_top = picture.mapTo(content, QPoint(0, 0)).y()
                if picture_top + picture.height() + self._GAP > top + view_height - self._GAP:
                    top = picture_top
                    span = picture.focus_span()
                    if span is not None:
                        # Only as far as the change needs: the picture's top (its stamp, headline and
                        # day) stays in view when the changed blocks fit below it; else they are
                        # centred, or shown from their top when taller than the view.
                        first, last = span
                        room = view_height - 2 * self._GAP
                        if last - first > room:
                            offset = float(first)
                        else:
                            lead = (room - (last - first)) / 2
                            offset = min(first - lead, last + self._CHANGE_MARGIN - room)
                        top = picture_top + max(0, int(offset))
            elif fields is not None and fields.isVisibleTo(self) and fields.height() > 0:
                fields_top = fields.mapTo(content, QPoint(0, 0)).y()
                if fields_top + fields.height() + self._GAP > top + view_height - self._GAP:
                    top = fields_top   # a short view: from the first field (the strip says SENDING IN)
        return max(0, min(maximum, top - self._GAP))

    def _newest_target(self, maximum: int) -> int:
        """The bottom, unless the newest task (when it just came) or its newest step is taller than
        the view: then its top. While an approved card counts down, that card's payload instead
        (_countdown_target)."""
        target = self._countdown_target(maximum)
        if target is not None:
            return target
        card = self._newest_card()
        content = self.widget()
        if card is None or not card.isVisibleTo(self) or card.height() <= 0:
            return maximum
        view_height = self.viewport().height()
        if card.height() + 2 * self._GAP <= view_height:
            return maximum
        if self._header_first:
            return max(0, min(maximum, card.y() - self._GAP))
        step = card.newest_step()
        if step is None or not step.isVisibleTo(self) or step.height() <= 0:
            return maximum
        if step.height() + 2 * self._GAP <= view_height:
            return maximum
        top = step.mapTo(content, QPoint(0, 0)).y()
        return max(0, min(maximum, top - self._GAP))

    def _follow_newest(self) -> None:
        held = time.monotonic() - self._user_scrolled_at
        if held >= self.SCROLL_HOLD_S:
            self._follow = True
        if self._follow:
            self._missed = False
            self.scroll_to_newest()
            QTimer.singleShot(0, self._scroll_if_following)
        else:
            # Held by the owner's own scroll: what came meanwhile is followed once the hold is over,
            # even if nothing else changes by then (a countdown changes nothing until it ends).
            self._missed = True
            self._hold_timer.start(max(0, math.ceil((self.SCROLL_HOLD_S - held) * 1000)) + 50)

    def _hold_over(self) -> None:
        if shiboken6.isValid(self) and self._missed:
            self._follow_newest()

    def _scroll_if_following(self) -> None:
        if shiboken6.isValid(self) and self._follow:
            self.scroll_to_newest()

    def _scroll_to_card(self, task_id: int) -> None:
        if not shiboken6.isValid(self):
            return
        card = self._cards.get(task_id)
        if card is not None:
            bar = self.verticalScrollBar()
            bar.setValue(max(0, min(bar.maximum(), card.y() - self._GAP)))

    def _on_range_changed(self, _minimum: int, maximum: int) -> None:
        if self._follow:
            self.verticalScrollBar().setValue(self._newest_target(maximum))

    def _on_user_scroll(self, _action: int) -> None:
        self._user_scrolled_at = time.monotonic()
        bar = self.verticalScrollBar()
        QTimer.singleShot(0, lambda: self._after_user_scroll(bar))

    def _after_user_scroll(self, bar: Any) -> None:
        if shiboken6.isValid(self) and shiboken6.isValid(bar):
            self._follow = bar.value() >= bar.maximum() - 2

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._fit_column()
        if self._follow:
            QTimer.singleShot(0, self._scroll_if_following)

    def _fit_column(self) -> None:
        """At most LIVE_CONTENT_MAX_PX of content, centred (a maximized pop-out on a wide screen keeps
        each status word next to its step)."""
        side = max(_CONVERSATION_MARGIN, (self.viewport().width() - LIVE_CONTENT_MAX_PX) // 2)
        margins = self._layout.contentsMargins()
        if (margins.left(), margins.right()) != (side, side):
            self._layout.setContentsMargins(side, margins.top(), side, margins.bottom())


# --------------------------------------------------------------------------
# Window frame
# --------------------------------------------------------------------------

_RESIZE_ZONE = 6


class HudWindowFrame(QWidget):
    """Base for frameless HUD windows: chamfered translucent outline, painted ground, header.

    ``header`` is a HeaderBar whose close button calls ``close()`` (so a
    subclass's closeEvent keeps deciding what closing means) and whose minimize
    button calls ``showMinimized()`` (a subclass sees the WindowStateChange).
    Put the content in ``body_layout``. ``set_glow_anchor(widget)`` centres the ground glow on
    that widget (e.g. the orb). ``set_resizable(True)`` lets the edges resize
    the window (the frame's margins are the grab zone).
    """

    def __init__(self, parent: QWidget | None = None, *, cut: int = 14, corners: int = CUT_DIAGONAL,
                 margins: tuple[int, int, int, int] = (10, 4, 10, 10), always_on_top: bool = True,
                 with_header: bool = True) -> None:
        # A plain top-level window (never Qt.Tool, never owned) keeps its taskbar button, which
        # restores it after a minimize. The minimize hint adds no visible frame to a frameless
        # window; on Windows it lets that taskbar button (and Win+Down) minimize it as well.
        flags = (Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowMinimizeButtonHint)
        if always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        super().__init__(parent, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self._cut = cut
        self._corners = corners
        self._glow_anchor: weakref.ref[QWidget] | None = None   # weak: the anchor is our own child
        self._resizable = False
        self._resize_edges = Qt.Edge(0)
        self._resize_start: tuple[QPoint, QRect] | None = None
        self._cache: QPixmap | None = None
        self._cache_key: tuple = ()
        self.header: HeaderBar | None = HeaderBar() if with_header else None
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*margins)
        layout.setSpacing(0)
        if self.header is not None:
            layout.addWidget(self.header)
            self.header.closeRequested.connect(self.close)
            self.header.minimizeRequested.connect(self.showMinimized)
        layout.addWidget(self.body, 1)

    # ---- configuration ------------------------------------------------------------

    def set_glow_anchor(self, widget: QWidget | None) -> None:
        self._glow_anchor = weakref.ref(widget) if widget is not None else None
        self.update()

    def set_resizable(self, resizable: bool) -> None:
        self._resizable = resizable
        if not resizable:
            self.unsetCursor()

    def outline_path(self) -> QPainterPath:
        """The window's chamfered outline in widget coordinates."""
        return chamfer_path(QRectF(self.rect()), self._cut, self._corners)

    # ---- painting -------------------------------------------------------------------

    def _glow_center(self) -> QPointF | None:
        anchor = self._glow_anchor() if self._glow_anchor is not None else None
        if anchor is None or not shiboken6.isValid(anchor) or not anchor.isVisible() or anchor.window() is not self:
            return None
        return QPointF(anchor.mapTo(self, anchor.rect().center()))

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        dpr = self.devicePixelRatioF()
        center = self._glow_center()
        anchor_key = None if center is None else (round(center.x()), round(center.y()))
        key = (self.width(), self.height(), dpr, anchor_key)
        if self._cache is None or key != self._cache_key:
            self._cache = self._render_ground(dpr, center)
            self._cache_key = key
        # Copy only the exposed part: the orb repaints its own small rect ~30 times a second.
        exposed = QRectF(event.rect())
        source = QRectF(exposed.x() * dpr, exposed.y() * dpr, exposed.width() * dpr, exposed.height() * dpr)
        painter = QPainter(self)
        try:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            painter.drawPixmap(exposed, self._cache, source)
        finally:
            painter.end()

    def _render_ground(self, dpr: float, center: QPointF | None) -> QPixmap:
        size = self.size()
        image = QImage(max(1, round(size.width() * dpr)), max(1, round(size.height() * dpr)),
                       QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(Qt.GlobalColor.transparent)
        rect = QRectF(0, 0, size.width(), size.height())
        radii = (rect.width() * 0.528, rect.height() * 0.533) if center is not None else None
        painter = QPainter(image)
        try:
            paint_ground(painter, rect, center, radii)
            # Clear the cut-off corners (antialiased edges). Masking with DestinationIn would
            # not work: a composition mode only acts on the pixels the fill covers.
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            outside = QPainterPath()
            outside.addRect(rect)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillPath(outside.subtracted(chamfer_path(rect, self._cut, self._corners)), QColor(0, 0, 0, 255))
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setPen(QPen(rgba(ACCENT, 0.28), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(chamfer_path(rect.adjusted(0.5, 0.5, -0.5, -0.5), self._cut, self._corners))
        finally:
            painter.end()
        return QPixmap.fromImage(image)

    # ---- edge resizing ----------------------------------------------------------------

    def _edges_at(self, pos: QPoint) -> Qt.Edge:
        edges = Qt.Edge(0)
        if not self._resizable:
            return edges
        if pos.x() <= _RESIZE_ZONE:
            edges |= Qt.Edge.LeftEdge
        elif pos.x() >= self.width() - _RESIZE_ZONE:
            edges |= Qt.Edge.RightEdge
        if pos.y() <= _RESIZE_ZONE:
            edges |= Qt.Edge.TopEdge
        elif pos.y() >= self.height() - _RESIZE_ZONE:
            edges |= Qt.Edge.BottomEdge
        return edges

    @staticmethod
    def _cursor_for(edges: Qt.Edge) -> Qt.CursorShape | None:
        horizontal = bool(edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge))
        vertical = bool(edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge))
        if horizontal and vertical:
            falling = edges in (Qt.Edge.LeftEdge | Qt.Edge.TopEdge, Qt.Edge.RightEdge | Qt.Edge.BottomEdge)
            return Qt.CursorShape.SizeFDiagCursor if falling else Qt.CursorShape.SizeBDiagCursor
        if horizontal:
            return Qt.CursorShape.SizeHorCursor
        if vertical:
            return Qt.CursorShape.SizeVerCursor
        return None

    def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if self._resize_start is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self._manual_resize(event.globalPosition().toPoint())
            return
        cursor = self._cursor_for(self._edges_at(event.position().toPoint()))
        if cursor is None:
            self.unsetCursor()
        else:
            self.setCursor(cursor)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        edges = self._edges_at(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and edges:
            event.accept()
            handle = self.windowHandle()
            if handle is not None and handle.startSystemResize(edges):
                return
            self._resize_edges = edges
            self._resize_start = (event.globalPosition().toPoint(), self.geometry())
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._resize_start = None
        super().mouseReleaseEvent(event)

    def _manual_resize(self, global_pos: QPoint) -> None:
        start, geometry = self._resize_start
        delta = global_pos - start
        rect = QRect(geometry)
        minimum = self.minimumSize()
        if self._resize_edges & Qt.Edge.LeftEdge:
            rect.setLeft(min(rect.left() + delta.x(), rect.right() - minimum.width()))
        if self._resize_edges & Qt.Edge.RightEdge:
            rect.setRight(max(rect.right() + delta.x(), rect.left() + minimum.width()))
        if self._resize_edges & Qt.Edge.TopEdge:
            rect.setTop(min(rect.top() + delta.y(), rect.bottom() - minimum.height()))
        if self._resize_edges & Qt.Edge.BottomEdge:
            rect.setBottom(max(rect.bottom() + delta.y(), rect.top() + minimum.height()))
        self.setGeometry(rect)
