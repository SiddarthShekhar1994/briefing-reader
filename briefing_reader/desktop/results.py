"""What a desktop request's results show on the desktop (SPEC sections 2.1, 3 step 6, 5.3, 5.4, 8.1 and 10.3; owner
decision D5; hard rules 4, 9 and 14; Phase 1, part 9; no Qt).

``present(run, ...)`` runs on the desktop thread right after a run (desktop.service: a local route, a planner's plan
or a card's click) and turns its results into ``Card`` s - the result cards the HUD floats beside what they are about
(desktop_results, hud_overlay.ResultCard):

* ``OPENED``: what opened (owner decision D5: a file's best match is ALREADY OPEN): the file, folder, app or web page,
  where it is and when it changed, the app it opened in (backend.app_for: the type's association - its name for the
  card and its exe to find its window by) and, for a file, the other matches as "Open this instead" rows;
* ``WAITING``: a step held for the owner's click (hard rule 4): ONE_CLICK - an app he did not name exactly, a web
  page he did not type or typed without its https:// (its host shown), a file whose name has hidden characters
  (spelled out as <U+202E>) - or CARD, amber - a document that can run macros or scripts, a shortcut (its real target
  shown first: backend.link_target, read and never resolved; it opens only when that is a document in his folders);
* ``FOUND``: what a find found, as rows to open (or show in their folder: a type Jarvis never opens).

Every card keeps its OWN items - the resolved target and the identity it passed its check with (verified review
note: never an id resolved again against a newer index) - and a row is offered only after its own re-check
(executor.recheck: still where it was, unchanged, inside the folders allowed, never secret-shaped), never when its
name reads like instructions (guard.suspect_text: treated as data, not offered) and only when it is a type Jarvis
may open or show. A card's click goes back through executor.run_click with that item.

``card_text(card, hour24=)`` is each card's copy (British spelling; no "sir" - that is the caption's): the kicker
with its stamp ("OPENED IN WORD <middle dot> 10:21 AM"), the title, the meta line ("Documents > Uni > LING <middle dot> edited
yesterday, 9:14 PM"), the note ("Opened LING essay draft.docx in Word. Nothing else changed."), the buttons and the
rows. Names, places and addresses go on the cards only: nothing here is logged but kinds and counts, and every repr
names kinds and counts only.

Public API
==========
Card kinds ``OPENED``, ``WAITING``, ``FOUND``; row actions ``OPEN``, ``SHOW``; button roles ``PRIMARY``,
``SECONDARY``, ``COMMIT``, ``QUIET``; tones ``CYAN``, ``AMBER``; note tones ``NOTE_DONE``, ``NOTE_INFO``,
``NOTE_AMBER``. ``Row(item, name, where, when, action)``; ``Card(...)`` (``other_items()``); ``Click(skill, target,
granted, flags, via, words, others_from, run_id)`` (a card's click for desktop.service.click), ``click_for(card,
key)``, ``row_index(key)``, the keys ``OPEN_KEY``, ``FOLDER_KEY``, ``DISMISS_KEY``, ``ROW_PREFIX``; ``present(run,
executor=, scope=, backend=, now=, words=, hour24=)``; ``CardText`` / ``card_text(card, hour24=)``; ``shown_name(name)``,
``edited_words(moment, now, hour24=)``, ``where_words(path, scope, folder=)``, ``type_words(name)``,
``app_words(name)``; ``MAX_CARDS``, ``MAX_ROWS``; the words (``NOTHING_ELSE`` ...).
"""

from __future__ import annotations

import logging
import ntpath
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from ..persona import clock_text
from . import guard, paths
from . import validate as dv
from .backend import AppInfo, DesktopBackend, FileIdentity
from .context import AppItem, FileItem, Place, SiteItem
from .executor import DONE, FOUND as FOUND_STATUS, WAITING as WAITING_STATUS, DesktopRun, Executor
from .index import file_item

logger = logging.getLogger(__name__)

OPENED, WAITING, FOUND = "opened", "waiting", "found"
OPEN, SHOW = "open", "show"                      # a row's button: open it, or show it in its folder
PRIMARY, SECONDARY, COMMIT, QUIET = "primary", "secondary", "commit", "quiet"   # a button's look (desktop_results)
CYAN, AMBER = "cyan", "amber"
NOTE_DONE, NOTE_INFO, NOTE_AMBER = "done", "info", "amber"
MAX_CARDS = 4                                    # result cards a run shows at most (the caption says the rest)
MAX_ROWS = 5                                     # rows a card offers at most
SEP = " \u00b7 "                            # a middle dot between spaces
CRUMB = " \u203a "                          # a single right-pointing angle quotation mark between spaces

NOTHING_ELSE = "Nothing else changed."
OPEN_INSTEAD_TEXT = "Open this instead"
SHOW_IN_FOLDER_TEXT = "Show in folder"
OPEN_TEXT = "Open"
NOT_NOW_TEXT = "Not now"
SCRIPT_NOTE = "It can run macros or scripts, so it opens only when you click Open. Open it only if you trust it."
HIDDEN_NOTE = "Its name had hidden characters (spelled out above). Check it is the file you want before you open it."
NOT_NAMED_NOTE = "You didn't name this app exactly, so it opens only on your click."
SUGGESTED_NOTE = "You didn't type this address, so it opens only on your click. Check the site first."
BARE_HOST_NOTE = "You typed it without https://, so it opens only on your click. Check the site first."
LINK_NOTE = "It leads to {name}{where}. Open opens that file."
LINK_SCRIPT_NOTE = "It leads to {name}{where}, which can run macros or scripts. Open opens it - only if you trust it."
LINK_NEVER_NOTE = "It leads to something Jarvis doesn't open - an app, a program or a place outside your folders."
LINK_NONE_NOTE = "Jarvis couldn't read where it leads, so it won't open it."

# A file's type in the kicker (by its real last extension).
_TYPE_WORDS = {
    "WORD DOCUMENT": ("docx", "doc", "docm", "dotm"),
    "EXCEL WORKBOOK": ("xlsx", "xls", "xlsm", "xlsb", "xltm", "xlam"),
    "POWERPOINT PRESENTATION": ("pptx", "ppt", "pptm", "potm", "ppsm", "ppam"),
    "PDF": ("pdf",), "SPREADSHEET": ("csv", "ods"), "PRESENTATION": ("odp",), "DOCUMENT": ("odt", "rtf", "txt", "md"),
    "WEB PAGE FILE": ("html", "htm", "xhtml", "mht", "mhtml"), "SHORTCUT": ("lnk",),
    "PICTURE": ("jpg", "jpeg", "png", "gif", "bmp", "webp", "heic", "tif", "tiff", "svg"),
    "AUDIO": ("mp3", "m4a", "wav", "flac", "ogg"), "VIDEO": ("mp4", "mov", "mkv", "avi", "webm")}
_TYPES = {kind: words for words, kinds in _TYPE_WORDS.items() for kind in kinds}
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# --------------------------------------------------------------------------
# The cards
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Row:
    """One item a card offers (an "Open this instead" match, a found file or folder): Jarvis's own item with the
    identity it passed its re-check with, how it shows and what its button does (``OPEN`` or ``SHOW``)."""

    item: Any = field(repr=False)
    name: str = field(default="", repr=False)
    where: str = field(default="", repr=False)
    when: str = field(default="", repr=False)
    action: str = OPEN

    def __repr__(self) -> str:
        return f"Row({type(self.item).__name__}, {self.action})"


@dataclass(frozen=True)
class Card:
    """One result card (see the module docstring); memory only. ``target``: Jarvis's own item with its identity (a
    FileItem, a Place, an AppItem, an https address or a SiteItem); ``name`` / ``where`` / ``when``: how it shows;
    ``app`` / ``exe``: the app it opened in and that app's exe (OPENED; "" unknown: no window to bracket); ``host`` /
    ``address``: a web page's; ``leads``: a shortcut's target as a row (None: it may not open; ``leads_note`` says
    why); ``rows``: what the card offers; ``left_out``: found but not offered; ``at``: when it opened; ``words``: the
    owner's words (D5's other matches on a later click)."""

    kind: str
    skill: str
    tier: str = dv.INSTANT
    flags: frozenset[str] = frozenset()
    target: Any = field(default=None, repr=False)
    name: str = field(default="", repr=False)
    where: str = field(default="", repr=False)
    when: str = field(default="", repr=False)
    type_words: str = ""
    app: str = field(default="", repr=False)
    exe: str = field(default="", repr=False)
    host: str = field(default="", repr=False)
    address: str = field(default="", repr=False)
    leads: Row | None = field(default=None, repr=False)
    leads_note: str = field(default="", repr=False)
    rows: tuple[Row, ...] = field(default=(), repr=False)
    left_out: int = 0
    at: datetime | None = field(default=None, repr=False)
    run_id: str = ""
    words: str = field(default="", repr=False)

    def other_items(self) -> tuple[Any, ...]:
        """The items of its rows, in order."""
        return tuple(row.item for row in self.rows)

    def __repr__(self) -> str:
        return (f"Card({self.kind}, {self.skill}, {self.tier}, flags={sorted(self.flags)}, rows={len(self.rows)}, "
                f"left_out={self.left_out})")


@dataclass(frozen=True)
class Click:
    """A card's click as the desktop thread carries it out (executor.run_click): the step, the card's OWN item and
    what the click stands for at most (``granted``); a shortcut's card also gives the shortcut (``via``); a file that
    opens gets D5's other matches from ``others_from`` by the owner's ``words``."""

    skill: str
    target: Any = field(repr=False)
    granted: str = dv.ONE_CLICK
    flags: frozenset[str] = frozenset()
    via: FileItem | None = field(default=None, repr=False)
    words: str = field(default="", repr=False)
    others_from: tuple[FileItem, ...] = field(default=(), repr=False)
    run_id: str = ""

    def __repr__(self) -> str:
        return f"Click({self.skill}, {self.granted})"


OPEN_KEY, FOLDER_KEY, DISMISS_KEY, ROW_PREFIX = "open", "folder", "dismiss", "row:"


def row_index(key: str) -> int:
    """The row a "row:<n>" key names; -1 for any other key."""
    if not isinstance(key, str) or not key.startswith(ROW_PREFIX) or not key[len(ROW_PREFIX):].isdigit():
        return -1
    return int(key[len(ROW_PREFIX):])


def click_for(card: Card, key: str) -> Click | None:
    """What a card's button asks the desktop thread to carry out, with the card's OWN items: its Open (a held step's
    item at the tier the card stood for - an amber card's click stands for CARD, a cyan one's for ONE_CLICK; a
    shortcut's Open opens the document it leads to, through the shortcut), Show in folder (its file), or a row's
    button ("Open this instead" or a found item's Open: ONE_CLICK, D5's other matches from the card's other items;
    a found item Jarvis never opens: Show in folder). None for anything else - Not now carries nothing out, and a
    shortcut with no document to open has no Open."""
    granted = dv.CARD if card.tier == dv.CARD else dv.ONE_CLICK
    if key == OPEN_KEY and card.kind == WAITING:
        if guard.open_kind(getattr(card.target, "name", "")) == guard.OPEN_SHORTCUT:
            if card.leads is None or not isinstance(card.target, FileItem):
                return None
            return Click(dv.OPEN_FILE, card.leads.item, dv.CARD, card.flags, card.target, card.words, (), card.run_id)
        return Click(card.skill, card.target, granted, card.flags, None, card.words, (), card.run_id)
    if key == FOLDER_KEY and isinstance(card.target, FileItem):
        return Click(dv.SHOW_IN_FOLDER, card.target, dv.ONE_CLICK, frozenset(), None, "", (), card.run_id)
    index = row_index(key)
    if 0 <= index < len(card.rows) and card.kind in (OPENED, FOUND):
        row = card.rows[index]
        if row.action == SHOW:
            return Click(dv.SHOW_IN_FOLDER, row.item, dv.ONE_CLICK, frozenset(), None, "", (), card.run_id)
        if isinstance(row.item, Place):
            return Click(dv.OPEN_FOLDER, row.item, dv.ONE_CLICK, frozenset(), None, "", (), card.run_id)
        others = [card.target] if card.kind == OPENED and isinstance(card.target, FileItem) else []
        others += [other.item for other in card.rows if other is not row and isinstance(other.item, FileItem)]
        return Click(dv.OPEN_FILE, row.item, dv.ONE_CLICK, frozenset(), None, card.words, tuple(others), card.run_id)
    return None


def present(run: DesktopRun | None, *, executor: Executor, scope: paths.Scope, backend: DesktopBackend,
            now: datetime, words: str = "", hour24: bool = False) -> tuple[Card, ...]:
    """The cards of ``run``'s results (on the desktop thread, right after the run; see the module docstring), in
    order, at most MAX_CARDS. A result that could not be shown is left out (logged by its type only)."""
    if run is None:
        return ()
    cards: list[Card] = []
    shown_at = _local(now)
    for result in run.results:
        try:
            card = _card(result, executor, scope, backend, shown_at, words, run.run_id, hour24)
        except Exception as exc:  # noqa: BLE001 - one card that can't be made never stops the others
            logger.warning("Desktop %s: a result card could not be made (%s)", run.run_id or "-", type(exc).__name__)
            card = None
        if card is not None:
            cards.append(card)
    if len(cards) > MAX_CARDS:
        logger.info("Desktop %s: %d result cards, %d shown", run.run_id or "-", len(cards), MAX_CARDS)
    return tuple(cards[:MAX_CARDS])


def _card(result: Any, executor: Executor, scope: paths.Scope, backend: DesktopBackend, now: datetime, words: str,
          run_id: str, hour24: bool) -> Card | None:
    skill, target = result.skill, result.target
    base = {"skill": skill, "tier": result.tier, "flags": frozenset(result.flags), "target": target,
            "run_id": run_id, "words": words}
    if result.status == DONE:
        return _opened(result, base, executor, scope, backend, now, hour24)
    if result.status == WAITING_STATUS:
        return _waiting(base, executor, scope, backend, now, hour24)
    if result.status == FOUND_STATUS:
        rows = _rows(result.found, executor, scope, now, hour24, files_only=False)
        if not rows:
            return None
        return Card(FOUND, **base, rows=rows[:MAX_ROWS], left_out=len(result.found) - min(len(rows), MAX_ROWS))
    return None


def _opened(result: Any, base: dict[str, Any], executor: Executor, scope: paths.Scope, backend: DesktopBackend,
            now: datetime, hour24: bool) -> Card | None:
    skill, target = result.skill, result.target
    if skill in (dv.OPEN_FILE, dv.SHOW_IN_FOLDER) and isinstance(target, FileItem):
        app = _app(backend, "." + guard.suffix(target.name)) if skill == dv.OPEN_FILE else AppInfo()
        rows = _rows(result.others, executor, scope, now, hour24, files_only=True, besides=target) \
            if skill == dv.OPEN_FILE else ()
        return Card(OPENED, **base, name=shown_name(target.name), where=where_words(target.path, scope),
                    when=edited_words(_changed(target), now, hour24=hour24), type_words=type_words(target.name),
                    app=app_words(app.name), exe=app.exe, rows=rows[:MAX_ROWS], at=now)
    if skill == dv.OPEN_FOLDER and isinstance(target, Place):
        label = target.parts[-1] if target.parts else ntpath.basename(target.path.rstrip("\\"))
        return Card(OPENED, **base, name=shown_name(label), where=where_words(target.path, scope, folder=True),
                    type_words="FOLDER", at=now)
    if skill == dv.OPEN_APP and isinstance(target, AppItem):
        return Card(OPENED, **base, name=shown_name(target.name), type_words="APP", app=app_words(target.name),
                    exe=target.exe, at=now)
    if skill == dv.OPEN_URL:
        address = target.url if isinstance(target, SiteItem) else target if isinstance(target, str) else ""
        app = _app(backend, "https")
        return Card(OPENED, **base, name=_host(address), host=_host(address), address=address,
                    type_words="WEB PAGE", app=app_words(app.name), exe=app.exe, at=now)
    return None


def _waiting(base: dict[str, Any], executor: Executor, scope: paths.Scope, backend: DesktopBackend,
             now: datetime, hour24: bool) -> Card | None:
    skill, target = base["skill"], base["target"]
    if skill == dv.OPEN_FILE and isinstance(target, FileItem):
        if not executor.recheck(target):
            return None                              # changed since: the caption says so; nothing to click
        card = Card(WAITING, **base, name=shown_name(target.name), where=where_words(target.path, scope),
                    when=edited_words(_changed(target), now, hour24=hour24), type_words=type_words(target.name))
        if guard.open_kind(target.name) == guard.OPEN_SHORTCUT:
            leads, note = _leads(target, scope, backend, now, hour24)
            card = replace(card, leads=leads, leads_note=note)
        return card
    if skill == dv.OPEN_APP and isinstance(target, AppItem):
        return Card(WAITING, **base, name=shown_name(target.name), type_words="APP")
    if skill == dv.OPEN_URL:
        address = target.url if isinstance(target, SiteItem) else target if isinstance(target, str) else ""
        if guard.url_problem(address):
            return None
        return Card(WAITING, **base, name=_host(address), host=_host(address), address=address, type_words="WEB PAGE")
    return None


def _leads(link: FileItem, scope: paths.Scope, backend: DesktopBackend, now: datetime,
           hour24: bool) -> tuple[Row | None, str]:
    """A shortcut's real target (SPEC 2.4): the document it names, checked like any listed file, as a row - or None
    and why it may not open."""
    try:
        named = backend.link_target(link.path)
    except (OSError, ValueError):
        return None, LINK_NONE_NOTE
    if not named:
        return None, LINK_NONE_NOTE
    kind = guard.open_kind(ntpath.basename(named))
    if kind not in (guard.OPEN_DOCUMENT, guard.OPEN_SCRIPT) or guard.secret_file(named):
        return None, LINK_NEVER_NOTE
    item = file_item(paths.check_path(named, scope, backend), backend)
    if item is None or guard.suspect_text(item.name):
        return None, LINK_NEVER_NOTE
    row = Row(item, shown_name(item.name), where_words(item.path, scope),
              edited_words(_changed(item), now, hour24=hour24), OPEN)
    where = f" in {row.where}" if row.where else ""
    template = LINK_SCRIPT_NOTE if kind == guard.OPEN_SCRIPT else LINK_NOTE
    return row, template.format(name=row.name, where=where)


def _rows(items: Sequence[Any], executor: Executor, scope: paths.Scope, now: datetime, hour24: bool, *,
          files_only: bool, besides: FileItem | None = None) -> tuple[Row, ...]:
    """The items a card may offer, re-checked first (see the module docstring), at most MAX_ROWS."""
    rows: list[Row] = []
    seen = {_key(besides.path)} if besides is not None else set()
    for item in items:
        if len(rows) >= MAX_ROWS:
            break
        if isinstance(item, Place) and not files_only:
            name = item.parts[-1] if item.parts else ntpath.basename(item.path.rstrip("\\"))
            action = OPEN
        elif isinstance(item, FileItem):
            name = item.name
            kind = guard.open_kind(name)
            if files_only and kind not in (guard.OPEN_DOCUMENT, guard.OPEN_SCRIPT, guard.OPEN_SHORTCUT):
                continue                             # "Open this instead" offers what may open
            action = OPEN if kind in (guard.OPEN_DOCUMENT, guard.OPEN_SCRIPT, guard.OPEN_SHORTCUT) else SHOW
        else:
            continue
        key = _key(item.path)
        if key in seen or guard.suspect_text(name) or not executor.recheck(item):
            continue                                 # twice, reads like instructions, or changed since: not offered
        seen.add(key)
        folder = isinstance(item, Place)
        rows.append(Row(item, shown_name(name), where_words(item.path, scope, folder=folder),
                        "" if folder else edited_words(_changed(item), now, hour24=hour24), action))
    return tuple(rows)


def _app(backend: DesktopBackend, kind: str) -> AppInfo:
    try:
        info = backend.app_for(kind)
    except Exception as exc:  # noqa: BLE001 - no app known: the card says "Opened ...", no bracket
        logger.debug("Desktop: the app for a type could not be read (%s)", type(exc).__name__)
        return AppInfo()
    return info if isinstance(info, AppInfo) else AppInfo()


def _changed(item: Any) -> datetime | date | None:
    identity = getattr(item, "identity", None)
    if isinstance(identity, FileIdentity) and identity.mtime_ns:
        try:
            return datetime.fromtimestamp(identity.mtime_ns / 1e9)
        except (OverflowError, OSError, ValueError):
            pass
    modified = getattr(item, "modified", None)
    return modified if isinstance(modified, (date, datetime)) else None


def _host(address: str) -> str:
    try:
        return (urlsplit(address).hostname or "").lower()
    except ValueError:
        return ""


def _key(path: str) -> str:
    return ntpath.normcase(path or "").rstrip("\\")


def _local(moment: datetime) -> datetime:
    """``moment`` as a local wall-clock time without a zone."""
    return moment.astimezone().replace(tzinfo=None) if moment.tzinfo is not None else moment


# --------------------------------------------------------------------------
# Words (pure)
# --------------------------------------------------------------------------

def shown_name(name: Any) -> str:
    """A name as a card shows it: its hidden characters (guard.hidden_char: format, tag and separator characters)
    spelled out ("<U+202E>"), so nothing is hidden or reordered on screen (SPEC 5.4)."""
    text = str(name or "")
    return "".join(f"<U+{ord(char):04X}>" if guard.hidden_char(char) else char for char in text)


def type_words(name: Any) -> str:
    """"WORD DOCUMENT", "PDF", "PICTURE" ... by the real last extension; "FILE" for anything else."""
    return _TYPES.get(guard.suffix(name), "FILE")


def app_words(name: Any) -> str:
    """An app's name as a card says it: "Microsoft Word" -> "Word", "Microsoft Edge" -> "Edge"; spaces tidied."""
    text = " ".join(str(name or "").split())
    if text.casefold().startswith("microsoft ") and len(text) > len("microsoft "):
        text = text[len("microsoft "):]
    return shown_name(text)


def where_words(path: str, scope: paths.Scope, *, folder: bool = False) -> str:
    """Where an item is, from the folder the owner allowed down: "Documents > Uni > LING" (CRUMB between; a file's or a folder's
    holders; "" for an allowed folder itself or anything outside them)."""
    root = scope.root_of(path) if path else None
    if root is None:
        return ""
    base = root.path.rstrip("\\")
    rest = path.rstrip("\\")[len(base):].strip("\\")
    parts = [part for part in rest.split("\\") if part]
    holders = parts[:-1] if parts else []
    if not parts and folder:
        return ""
    return CRUMB.join([root.label, *(shown_name(part) for part in holders)])


def edited_words(moment: datetime | date | None, now: datetime, *, hour24: bool = False) -> str:
    """"edited today, 9:14 AM", "edited yesterday, 9:14 PM", "edited Monday, 9:14 PM" (this week), "edited 12 Oct",
    "edited 12 Oct 2024"; "" when unknown."""
    if moment is None:
        return ""
    timed = isinstance(moment, datetime)
    day = moment.date() if timed else moment
    today = now.date()
    clock = f", {clock_text(moment, hour24)}" if timed else ""
    if day == today:
        return f"edited today{clock}"
    if day == today - timedelta(days=1):
        return f"edited yesterday{clock}"
    if today - timedelta(days=6) <= day < today:
        return f"edited {_WEEKDAYS[day.weekday()]}{clock}"
    if day.year == today.year:
        return f"edited {day.day} {_MONTHS[day.month - 1]}"
    return f"edited {day.day} {_MONTHS[day.month - 1]} {day.year}"


@dataclass(frozen=True)
class CardText:
    """A card's copy and its buttons: ``buttons`` (key, words, role, at the right), ``rows`` (key, title, meta, action
    words); ``tone`` CYAN or AMBER; ``note_first``: the note right under the meta (what happened, before the
    actions)."""

    kicker: str
    title: str
    meta: str = ""
    note: str = ""
    note_tone: str = NOTE_INFO
    tone: str = CYAN
    buttons: tuple[tuple[str, str, str, bool], ...] = ()
    tag: str = ""
    rows: tuple[tuple[str, str, str, str], ...] = ()
    note_first: bool = False


def _meta(*parts: str) -> str:
    return SEP.join(part for part in parts if part)


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def card_text(card: Card, hour24: bool = False) -> CardText:
    """``card``'s copy (see the module docstring)."""
    stamp = clock_text(card.at, hour24) if card.at is not None else ""
    rows = tuple((f"row:{index}", row.name, _meta(row.where, row.when),
                  OPEN_INSTEAD_TEXT if card.kind == OPENED else OPEN_TEXT if row.action == OPEN
                  else SHOW_IN_FOLDER_TEXT) for index, row in enumerate(card.rows))
    if card.kind == OPENED:
        return _opened_text(card, stamp, rows)
    if card.kind == FOUND:
        files = sum(1 for row in card.rows if isinstance(row.item, FileItem))
        folders = len(card.rows) - files
        what = [_count(files, "file", "files")] if files else []
        if folders:
            what.append(_count(folders, "folder", "folders"))
        title = " and ".join(what)
        title = f"{title} {'matches' if len(card.rows) == 1 else 'match'}"
        note = (f"{card.left_out} more {'was' if card.left_out == 1 else 'were'} found - not shown here."
                if card.left_out else "")
        return CardText(f"FOUND{SEP}" + ", ".join(part.upper() for part in what), title[:1].upper() + title[1:],
                        note=note, rows=rows)
    return _waiting_text(card)


def _opened_text(card: Card, stamp: str, rows: tuple[tuple[str, str, str, str], ...]) -> CardText:
    app = card.app
    when = f"{SEP}{stamp}" if stamp else ""
    if card.skill == dv.OPEN_FILE:
        kicker = f"OPENED IN {app.upper()}{when}" if app else f"OPENED{when}"
        note = f"Opened {card.name} in {app}. {NOTHING_ELSE}" if app else f"Opened {card.name}. {NOTHING_ELSE}"
        tag = _count(len(rows), "other match", "other matches").upper() if rows else ""
        return CardText(kicker, card.name, _meta(card.where, card.when), note, NOTE_DONE,
                        buttons=(("folder", SHOW_IN_FOLDER_TEXT, SECONDARY, False),), tag=tag, rows=rows,
                        note_first=True)
    if card.skill == dv.SHOW_IN_FOLDER:
        return CardText(f"SHOWN IN ITS FOLDER{when}", card.name, _meta(card.where, card.when),
                        f"Showed {card.name} in its folder. {NOTHING_ELSE}", NOTE_DONE, note_first=True)
    if card.skill == dv.OPEN_FOLDER:
        return CardText(f"FOLDER{SEP}OPENED{when}", card.name, card.where,
                        f"Opened {card.name}. {NOTHING_ELSE}", NOTE_DONE, note_first=True)
    if card.skill == dv.OPEN_APP:
        return CardText(f"APP{SEP}OPENED{when}", card.name, "", f"Opened {card.name}. {NOTHING_ELSE}", NOTE_DONE,
                        note_first=True)
    kicker = f"WEB PAGE{SEP}OPENED IN {app.upper()}{when}" if app else f"WEB PAGE{SEP}OPENED{when}"
    note = f"Opened {card.host} in {app}. {NOTHING_ELSE}" if app else f"Opened {card.host}. {NOTHING_ELSE}"
    return CardText(kicker, card.host, card.address, note, NOTE_DONE, note_first=True)


def _waiting_text(card: Card) -> CardText:
    flags = card.flags
    folder = ((("folder", SHOW_IN_FOLDER_TEXT, SECONDARY, False),) if card.skill == dv.OPEN_FILE else ())
    if card.tier == dv.CARD:
        if dv.FLAG_SHORTCUT in flags or guard.open_kind(getattr(card.target, "name", "")) == guard.OPEN_SHORTCUT:
            buttons = ((("dismiss", NOT_NOW_TEXT, QUIET, False),) + folder
                       + ((("open", OPEN_TEXT, COMMIT, True),) if card.leads is not None else ()))
            return CardText(f"SHORTCUT{SEP}NEEDS YOUR OK", card.name, _meta(card.where, card.when),
                            card.leads_note, NOTE_AMBER, AMBER, buttons, note_first=True)
        return CardText(f"{card.type_words}{SEP}NEEDS YOUR OK", card.name, _meta(card.where, card.when), SCRIPT_NOTE,
                        NOTE_AMBER, AMBER, (("dismiss", NOT_NOW_TEXT, QUIET, False),) + folder
                        + (("open", OPEN_TEXT, COMMIT, True),), note_first=True)
    buttons = (("open", OPEN_TEXT, PRIMARY, False),) + folder + (("dismiss", NOT_NOW_TEXT, SECONDARY, True),)
    if card.skill == dv.OPEN_APP:
        return CardText(f"APP{SEP}YOUR CLICK OPENS IT", card.name, "", NOT_NAMED_NOTE, NOTE_INFO, CYAN, buttons,
                        note_first=True)
    if card.skill == dv.OPEN_URL:
        note = BARE_HOST_NOTE if dv.FLAG_BARE_HOST in flags else SUGGESTED_NOTE
        return CardText(f"WEB PAGE{SEP}YOUR CLICK OPENS IT", card.host, card.address, note, NOTE_INFO, CYAN, buttons,
                        note_first=True)
    kicker = f"{card.type_words}{SEP}NAME HAD HIDDEN CHARACTERS" if dv.FLAG_HIDDEN_CHARS in flags \
        else f"{card.type_words}{SEP}YOUR CLICK OPENS IT"
    return CardText(kicker, card.name, _meta(card.where, card.when), HIDDEN_NOTE if dv.FLAG_HIDDEN_CHARS in flags
                    else "", NOTE_INFO, CYAN, buttons, note_first=True)
