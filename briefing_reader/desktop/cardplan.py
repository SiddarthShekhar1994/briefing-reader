"""The frozen card plan of a request's file steps (Jarvis on your desktop, P2.2; SPEC sections 3 step 7, 4.4, 5.2-5.4,
6.2 and 10.3; hard rules 3, 4, 5, 7, 8 and 12; no Qt).

The file skills (desktop.validate.ORGANISE_SKILLS: move_files, copy_files, rename_file, make_folder, zip_files) are
always CARD: nothing of them happens until the owner's click on a NEEDS YOUR OK card that shows every row, with ticks
(P2.3). ``build_card`` makes that card's plan on the desktop thread, right before the card shows (hard rule 8), from
the checked plan's accepted file steps - their RESOLVED targets: the run's own RefIndex entries with the 5.3 identity
they were listed with, never a bare id resolved again against a newer index. Each step becomes its rows (a move, a
copy or a rename: one row per file; a new folder: one row, keyed by its n id so later rows go into it; a zip: one
row, into ``to`` or else next to its first file), and each row is checked as things are now by
desktop.fileops.FileOps.check - the executor's own re-check: every source and the destination through the path check
(the roots, the NEVER list, secret names, code folders, network and removable drives, links), the same item
unchanged, a folder's bounded subtree walk, the name rules, the drive (a move stays on its volume in v1), the space -
with the card's earlier rows taken into account (a new folder not made yet; a name an earlier row takes).

* A name that is taken - on the disk or by an earlier row of the card - gets its free name ON THE PLAN ("Budget 2026
  (2).xlsx": fileops.free_name; hard rule 5: never chosen silently at run time), and the row says so
  (``FLAG_RENAMED``).
* A row that can't go keeps its reason (``code``: a fileops reason - "changed", "cross_volume", "online_only",
  "name_clash" ...): the card shows it as an information row, never ticked, never run.
* Each row's flags (SPEC 5.4; shown as text, never as colour alone): it uploads to OneDrive or leaves it (owner
  decision D3: allowed on the click, flagged), online-only, pinned, a folder whose listing is in the cloud, hidden
  characters in a name, renamed "(2)", a program or an installer (validate.FLAG_INSTALLER: unticked - installers stay
  where they are unless he ticks them), big (validate.BIG_BYTES or more), many (more than validate.MANY_FILES files in
  one row: a zip, a folder), a type changed on his word.

The plan is then FROZEN (``CardPlan``): the rows - what each does, to what (the items' paths and 5.3 identities),
where, under which name, with which flags and tick - with the run's id and when it was made and expires (``CARD_TTL``:
15 minutes), as canonical JSON (``canonical_json``: sorted keys, no spaces, ASCII escapes) and the sha256 of that
JSON. The card runs exactly that plan and nothing else (P2.3): before a click is carried out ``verify`` refuses a
plan that changed since it was frozen (``CARD_CHANGED``: its JSON no longer hashes to its sha256, its lifetime is
longer than CARD_TTL, or the card shown carried another sha256) or has expired (``CARD_EXPIRED``: by the wall clock
or the monotonic one; a wall clock set back never extends it); ``ops(ticked)`` then gives the rows that may go and
are ticked, in order, as fileops.FileOp rows, and fileops.FileOps.run checks each one again right before its OS call
(an item changed since: "Changed since - left as it is.").

The plan holds the owner's paths (memory only, like the journal): it is never logged and never sent to Claude; every
repr names kinds, codes and counts only.

Public API
==========
``CARD_TTL``, ``CLOCK_SLACK``; codes ``CARD_EXPIRED``, ``CARD_CHANGED`` (``CODE_WORDS``, ``row_words(code)``);
``FLAG_RENAMED`` (the other row flags are fileops' and validate's: ``ROW_FLAGS``); ``CardRow`` (``ok``, ``op()``);
``CardPlan`` (``body()``, ``canonical()``, ``digest()``, ``verify(now, mono=, sha=)``, ``chosen(ticked=)``,
``ops(ticked=)``, ``rows_of(step)``, ``counts()``); ``build_card(plan, backend, scope, now=, mono=, clock=, caps=)``;
``freeze(run_id, rows, now=, mono=, refs=)``; ``carry_out(card, ops, now=, mono=, sha=, ticked=)`` (the click: verify,
then exactly the chosen rows through fileops); ``canonical_json(value)``; ``describe_rows(card)`` (Live's lines).
"""

from __future__ import annotations

import hashlib
import json
import logging
import ntpath
import time
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from . import guard, paths
from . import fileops as fo
from . import validate as dv
from .backend import DesktopBackend, FileIdentity
from .context import FileItem, Place
from .refs import FILE, NEW, RefIndex, parse_ref

logger = logging.getLogger(__name__)

CARD_TTL = timedelta(minutes=15)        # a card's plan runs only within this long of being made (SPEC 16 P2.2)
CLOCK_SLACK = timedelta(seconds=60)     # a wall clock this far behind the plan's making is still believed
CARD_EXPIRED = "card_expired"           # the plan is older than CARD_TTL: nothing of it runs
CARD_CHANGED = "card_changed"           # the plan is not the one frozen (or not the one the card showed)
FLAG_RENAMED = "renamed"                # its name was taken: the card shows the free one ("name (2).ext")
ROW_FLAGS = (fo.FLAG_TO_ONEDRIVE, fo.FLAG_LEAVES_ONEDRIVE, fo.FLAG_ONLINE_ONLY, fo.FLAG_RECALL_ON_OPEN, fo.FLAG_PINNED,
             fo.FLAG_HIDDEN_CHARS, FLAG_RENAMED, dv.FLAG_INSTALLER, dv.FLAG_BIG, dv.FLAG_MANY, dv.FLAG_TYPE_CHANGED)
FORMAT = 1                              # the canonical JSON's own version
_KINDS = {dv.MOVE_FILES: fo.MOVE, dv.COPY_FILES: fo.COPY, dv.RENAME_FILE: fo.RENAME, dv.MAKE_FOLDER: fo.MKDIR,
          dv.ZIP_FILES: fo.ZIP}

# A row's reason in Jarvis's own words (Live; never a name or a path).
CODE_WORDS = {
    CARD_EXPIRED: "the card is more than 15 minutes old - ask again",
    CARD_CHANGED: "the card's plan isn't the one you were shown",
    fo.R_CHANGED: "it changed since I looked - left as it is",
    fo.R_NAME_CLASH: "something already has that name there",
    fo.R_CROSS_VOLUME: "it would go to another drive - Jarvis moves files within one drive only",
    fo.R_ONLINE_ONLY: "it's online-only - copying or zipping it would download it first",
    fo.R_SAME_PLACE: "it's there already",
    fo.R_INTO_ITSELF: "a folder can't go into itself",
    fo.R_ROOT: "it's one of your main folders",
    fo.R_NO_SPACE: "there isn't room for it there",
    fo.R_CAP: "it's more than one zip may hold",
    fo.R_NO_FOLDER: "its new folder can't be made",
    fo.R_WALK_CAP: "it holds too much to check",
    fo.R_UNREADABLE: "something in it can't be read",
}
CODE_WORDS.update({reason: "it holds something Jarvis never moves" for reason in
                   (fo.R_HOLDS_PLACE, fo.R_HOLDS_CODE, fo.R_HOLDS_SECRET_FOLDER, fo.R_HOLDS_EXCLUDED,
                    fo.R_HOLDS_SECRET_FILE, fo.R_HOLDS_LINK, fo.R_HOLDS_ONLINE_ONLY)})


def row_words(code: str) -> str:
    """A card row's (or a card's) reason in Jarvis's words; "" for none it knows (validate's words then)."""
    return CODE_WORDS.get(code, "")


def canonical_json(value: Any) -> str:
    """``value`` as canonical JSON: sorted keys, no spaces, every character past ASCII escaped, no NaN."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _utc(moment: datetime) -> datetime:
    """``moment`` in UTC (a naive one is taken as UTC already)."""
    if not isinstance(moment, datetime):
        raise TypeError("a card's times are datetimes")
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# The rows and the frozen plan
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CardRow:
    """One row of a card: what it does (``kind``: a fileops kind; from step ``step``'s ``skill``), to what
    (``sources``: the plan's own items with the identity they were listed with), where (``folder``: a Place with its
    identity, the n id of the row that makes it, or None for a rename), under which name (``name``: a new name, the
    free "(2)" name, or "" for the item's own), a new folder's own id (``key``), its flags, whether it is ticked when
    the card shows and why it can't go (``code``; "" when it may)."""

    number: int
    step: int
    skill: str
    kind: str
    sources: tuple[Any, ...] = field(default=(), repr=False)
    folder: Any = field(default=None, repr=False)
    name: str = field(default="", repr=False)
    key: str = ""
    flags: frozenset[str] = frozenset()
    ticked: bool = True
    code: str = ""

    @property
    def ok(self) -> bool:
        return not self.code

    def op(self) -> fo.FileOp:
        """The row as fileops carries it out (re-checked right before its OS call)."""
        return fo.FileOp(self.kind, self.sources, self.folder, self.name, self.key)

    def __repr__(self) -> str:
        state = self.code or ("ticked" if self.ticked else "unticked")
        return f"CardRow({self.number}, {self.kind}, {state})"


def _identity_json(identity: Any) -> dict[str, Any] | None:
    if not isinstance(identity, FileIdentity):
        return None
    return {"volume": int(identity.volume), "file_id": int(identity.file_id), "size": int(identity.size),
            "mtime_ns": int(identity.mtime_ns), "is_dir": bool(identity.is_dir)}


def _item_json(item: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"path": str(getattr(item, "path", "")),
                            "identity": _identity_json(getattr(item, "identity", None))}
    if isinstance(item, FileItem):
        body["name"] = item.name
    elif isinstance(item, Place):
        body["parts"] = list(item.parts)
    return body


def _row_json(row: CardRow) -> dict[str, Any]:
    folder = row.folder
    where: dict[str, Any] | None
    if isinstance(folder, str):
        where = {"new": folder}
    elif folder is None:
        where = None
    else:
        where = _item_json(folder)
    return {"number": row.number, "step": row.step, "skill": row.skill, "kind": row.kind,
            "sources": [_item_json(item) for item in row.sources], "folder": where, "name": row.name,
            "key": row.key, "flags": sorted(row.flags), "ticked": bool(row.ticked), "code": row.code}


@dataclass(frozen=True)
class CardPlan:
    """A card's plan, frozen (see the module docstring): the run's id, the rows, when it was made and expires (UTC),
    the sha256 of its canonical JSON, the monotonic clock when it was made (``mono``: not in the JSON - a second
    clock for ``verify``) and the run's own RefIndex (``refs``: what each ref meant in that run; never in the JSON)."""

    run_id: str
    rows: tuple[CardRow, ...] = field(default=(), repr=False)
    made_at: datetime = field(default=datetime(1970, 1, 1, tzinfo=timezone.utc), repr=False)
    expires_at: datetime = field(default=datetime(1970, 1, 1, tzinfo=timezone.utc), repr=False)
    sha256: str = field(default="", repr=False)
    mono: float = field(default=0.0, repr=False, compare=False)
    refs: RefIndex | None = field(default=None, repr=False, compare=False)

    def body(self) -> dict[str, Any]:
        """What the hash covers: the format, the run's id, when it was made and expires, every row."""
        return {"format": FORMAT, "run_id": self.run_id, "made_at": _utc(self.made_at).isoformat(),
                "expires_at": _utc(self.expires_at).isoformat(), "rows": [_row_json(row) for row in self.rows]}

    def canonical(self) -> str:
        return canonical_json(self.body())

    def digest(self) -> str:
        """The sha256 of the canonical JSON as the plan is NOW (equal to ``sha256`` while it is unchanged)."""
        return hashlib.sha256(self.canonical().encode("ascii")).hexdigest()

    def verify(self, now: datetime, *, mono: float | None = None, sha: str = "") -> str:
        """"" when the click may carry this plan out at ``now`` (and ``mono``, the monotonic clock, when given);
        CARD_CHANGED when it is not the plan frozen - or not the one the card showed (``sha``) -, CARD_EXPIRED when
        it is CARD_TTL old by either clock or the wall clock is set back before it was made."""
        try:
            if not self.sha256 or self.digest() != self.sha256 or (sha and sha != self.sha256) \
                    or _utc(self.expires_at) - _utc(self.made_at) > CARD_TTL:
                return CARD_CHANGED
            wall = _utc(now)
        except (TypeError, ValueError):
            return CARD_CHANGED                            # it can't even be read back: never carried out
        if wall >= _utc(self.expires_at) or wall < _utc(self.made_at) - CLOCK_SLACK:
            return CARD_EXPIRED
        if mono is not None and not 0 <= mono - self.mono < CARD_TTL.total_seconds():
            return CARD_EXPIRED
        return ""

    def chosen(self, ticked: Collection[int] | None = None) -> tuple[CardRow, ...]:
        """The rows a click carries out, in order: the ones that may go (no code) and are ticked - ``ticked``: the
        row numbers the owner left ticked (None: the card's own ticks). A row number the plan doesn't have is
        nothing."""
        return tuple(row for row in self.rows
                     if row.ok and (row.ticked if ticked is None else row.number in ticked))

    def ops(self, ticked: Collection[int] | None = None) -> tuple[fo.FileOp, ...]:
        """``chosen(ticked)`` as fileops rows (fileops.FileOps.run re-checks each right before its OS call)."""
        return tuple(row.op() for row in self.chosen(ticked))

    def rows_of(self, step: int) -> tuple[CardRow, ...]:
        """The rows plan step ``step`` became."""
        return tuple(row for row in self.rows if row.step == step)

    def counts(self) -> dict[str, int]:
        """Sizes only (safe to log): rows, the ones that may go, ticked, refused."""
        return {"rows": len(self.rows), "ok": sum(row.ok for row in self.rows),
                "ticked": sum(row.ok and row.ticked for row in self.rows),
                "refused": sum(not row.ok for row in self.rows)}

    def __repr__(self) -> str:
        return f"CardPlan({self.run_id}, {self.counts()})"


def freeze(run_id: str, rows: Iterable[CardRow], *, now: datetime, mono: float = 0.0,
           refs: RefIndex | None = None) -> CardPlan:
    """``rows`` frozen as a card's plan made at ``now`` (expiring CARD_TTL later), its sha256 taken."""
    made = _utc(now)
    plan = CardPlan(run_id, tuple(rows), made, made + CARD_TTL, "", float(mono), refs)
    return replace(plan, sha256=plan.digest())


# --------------------------------------------------------------------------
# Making a card's plan (the desktop thread, right before the card shows)
# --------------------------------------------------------------------------

def _key(path: str) -> str:
    return ntpath.normcase(path or "").rstrip("\\")


def _own_folder(item: Any, backend: DesktopBackend, scope: paths.Scope) -> tuple[str, Place | None]:
    """The folder ``item`` is in, checked as a place now (a zip without ``to`` goes next to its first file)."""
    path = getattr(item, "path", "")
    if not isinstance(path, str) or not path:
        return fo.R_CHANGED, None
    verdict = paths.check_path(ntpath.dirname(path), scope, backend)
    if not verdict.ok:
        return (fo.R_CHANGED if verdict.reason == paths.R_MISSING else verdict.code or fo.R_OUTSIDE), None
    try:
        identity = backend.file_id(verdict.path)
    except OSError:
        return fo.R_CHANGED, None
    if not isinstance(identity, FileIdentity) or not identity.is_dir:
        return fo.R_CHANGED, None
    return "", Place(verdict.path, (), identity)


def _step_rows(step: dv.CheckedStep, backend: DesktopBackend,
               scope: paths.Scope) -> list[tuple[str, fo.FileOp | None]]:
    """The fileops rows a checked file step becomes, before they are checked: (a code, None) for one that can't even
    be put together."""
    planned = step.planned
    kind = _KINDS[step.skill]
    items = tuple(ref.target for ref in step.targets if ref.kind == FILE)
    to = planned.to if planned is not None else ""
    folder: Any = None
    if to:
        parsed = parse_ref(to)
        folder = to if parsed is not None and parsed[0] == NEW else next(
            (ref.target for ref in step.targets if ref.id == to and isinstance(ref.target, Place)), None)
    name = planned.name if planned is not None else ""
    if kind in (fo.MOVE, fo.COPY):
        return [("", fo.FileOp(kind, (item,), folder)) for item in items]
    if kind == fo.RENAME:
        return [("", fo.FileOp(kind, (item,), None, name)) for item in items]
    if kind == fo.MKDIR:
        place = next((ref.target for ref in step.targets if isinstance(ref.target, Place)), None)
        return [("", fo.FileOp(kind, (), place, name, planned.id if planned is not None else ""))]
    if folder is None and items:                         # a zip without "to": next to its first file
        code, folder = _own_folder(items[0], backend, scope)
        if code:
            return [(code, None)]
    return [("", fo.FileOp(kind, items, folder, name))]


def _recorded_folder(op: fo.FileOp, pending: dict[str, tuple[Place, str]]) -> str:
    """Where a row puts its result, by the plan's own paths (only asked once the row's folder passed its check)."""
    if op.kind == fo.RENAME:
        return ntpath.dirname(getattr(op.sources[0], "path", ""))
    if isinstance(op.folder, str):
        parent, made = pending.get(op.folder, (None, ""))
        return (parent.path.rstrip("\\") + "\\" + made) if parent is not None else ""
    return str(getattr(op.folder, "path", ""))


def _row_flags(op: fo.FileOp, check: fo.Check) -> set[str]:
    """The row's flags beyond fileops' own: an installer, big, many, a type changed."""
    flags = set(check.flags)
    names = [getattr(item, "name", "") or ntpath.basename(getattr(item, "path", "")) for item in op.sources]
    if op.kind in (fo.MOVE, fo.COPY) and any(guard.blocked_type(name) for name in names):
        flags.add(dv.FLAG_INSTALLER)
    if any(any(guard.hidden_char(char) for char in name) for name in names):
        flags.add(fo.FLAG_HIDDEN_CHARS)
    size = check.bytes or sum(max(0, source.identity.size) for source in check.sources
                              if not source.identity.is_dir)
    size += sum(source.subtree.bytes for source in check.sources if source.subtree is not None and not check.bytes)
    if size >= dv.BIG_BYTES:
        flags.add(dv.FLAG_BIG)
    files = (sum(not member.is_dir for member in check.members) if check.members
             else sum(len(source.subtree.files) for source in check.sources if source.subtree is not None))
    if files > dv.MANY_FILES:
        flags.add(dv.FLAG_MANY)
    if op.kind == fo.RENAME and any(guard.suffix(name) != guard.suffix(op.name) for name in names):
        flags.add(dv.FLAG_TYPE_CHANGED)
    return flags


def _checked_row(number: int, step: dv.CheckedStep, op: fo.FileOp, checker: fo.FileOps,
                 pending: dict[str, tuple[Place, str]], taken: dict[str, set[str]],
                 backend: DesktopBackend) -> CardRow:
    """One row checked as things are now; a name taken (on the disk, or by an earlier row) swapped for its free
    name ON THE PLAN."""
    check = checker.check(op, pending=pending)
    renamed = False
    clash = check.reason == fo.R_NAME_CLASH
    if check.ok and fo.fold_name(ntpath.basename(check.dst)) in taken.get(_key(check.folder), set()):
        clash = True                                     # an earlier row of this card takes that name there
    if clash and not (op.kind == fo.RENAME and fo.fold_name(op.name) == fo.fold_name(
            ntpath.basename(getattr(op.sources[0], "path", "")))):
        folder = check.folder if check.ok else _recorded_folder(op, pending)
        wanted = op.name or ntpath.basename(getattr(op.sources[0], "path", "") if op.sources else "")
        free = fo.free_name(folder, wanted, backend, taken=taken.get(_key(folder), set())) if folder and wanted else ""
        if not free or fo.fold_name(free) == fo.fold_name(wanted):
            return CardRow(number, step.number, step.skill, op.kind, op.sources, op.folder, op.name, op.key,
                           frozenset(), False, fo.R_NAME_CLASH)
        op = replace(op, name=free)
        renamed = True
        check = checker.check(op, pending=pending)
    if not check.ok:
        return CardRow(number, step.number, step.skill, op.kind, op.sources, op.folder, op.name, op.key,
                       frozenset(check.flags), False, check.reason)
    flags = _row_flags(op, check)
    if renamed:
        flags.add(FLAG_RENAMED)
    taken.setdefault(_key(check.folder), set()).add(fo.fold_name(ntpath.basename(check.dst)))
    if op.kind == fo.MKDIR and op.key and isinstance(op.folder, Place):
        pending[op.key] = (op.folder, ntpath.basename(check.dst))
    return CardRow(number, step.number, step.skill, op.kind, op.sources, op.folder, op.name, op.key,
                   frozenset(flags), dv.FLAG_INSTALLER not in flags, "")


def build_card(plan: dv.DesktopPlan, backend: DesktopBackend, scope: paths.Scope, *, now: datetime,
               mono: float = 0.0, clock: Callable[[], float] = time.monotonic,
               caps: fo.SubtreeCaps = fo.SUBTREE_CAPS) -> CardPlan | None:
    """The frozen plan of ``plan``'s accepted file steps (see the module docstring), checked against ``scope`` (the
    scope as it is now) through ``backend``; made at ``now`` (and ``mono`` by the monotonic clock). None when the plan
    has no file step. Nothing is changed: only fileops' checks are made."""
    steps = [step for step in plan.accepted() if step.skill in _KINDS]
    if not steps:
        return None
    checker = fo.FileOps(backend, None, scope=lambda: scope, clock=clock, caps=caps)   # type: ignore[arg-type]
    pending: dict[str, tuple[Place, str]] = {}
    taken: dict[str, set[str]] = {}
    rows: list[CardRow] = []
    for step in steps:
        for code, op in _step_rows(step, backend, scope):
            number = len(rows) + 1
            if op is None:
                rows.append(CardRow(number, step.number, step.skill, _KINDS[step.skill], ticked=False, code=code))
                continue
            rows.append(_checked_row(number, step, op, checker, pending, taken, backend))
    card = freeze(plan.run_id, rows, now=now, mono=mono, refs=plan.refs)
    reasons: dict[str, int] = {}
    for row in card.rows:
        if row.code:
            reasons[row.code] = reasons.get(row.code, 0) + 1
    logger.info("Desktop %s: card plan of %d row(s) (%d ticked, %d refused%s)", plan.run_id or "-", len(card.rows),
                card.counts()["ticked"], card.counts()["refused"],
                (": " + ", ".join(f"{code}={count}" for code, count in sorted(reasons.items()))) if reasons else "")
    return card


def carry_out(card: CardPlan, ops: fo.FileOps, *, now: datetime, mono: float | None = None, sha: str = "",
              ticked: Collection[int] | None = None) -> tuple[str, fo.BatchReport | None]:
    """The owner's click on a card (P2.3's card calls this on the desktop thread): ``verify`` first - an expired plan,
    or one that is not the plan frozen and shown (``sha``), runs nothing at all: (its code, None) -, then exactly the
    chosen rows (``ticked``) through ``ops`` (fileops.FileOps: each row re-checked right before its OS call and
    journalled): ("", the batch's report)."""
    code = card.verify(now, mono=mono, sha=sha)
    if code:
        logger.info("Desktop %s: a card's click was refused (%s)", card.run_id or "-", code)
        return code, None
    return "", ops.run(list(card.ops(ticked)))


def describe_rows(card: CardPlan | None) -> list[str]:
    """Live's lines for a card's rows: "Row 2 - move - ticked (to_onedrive)" / "Row 3 - zip - refused: changed"
    (kinds, flags and codes; never a name or a path)."""
    if card is None:
        return []
    lines = []
    for row in card.rows:
        if row.code:
            lines.append(f"Row {row.number} - {row.kind} - refused: {row.code}")
            continue
        flags = f" ({', '.join(sorted(row.flags))})" if row.flags else ""
        lines.append(f"Row {row.number} - {row.kind} - {'ticked' if row.ticked else 'unticked'}{flags}")
    return lines
