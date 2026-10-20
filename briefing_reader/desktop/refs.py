"""The per-run ref index: the ids the planner sees instead of paths, programs and addresses (SPEC hard rule 8; no Qt).

Claude never writes a path, an exe, a command line or a host. Every file, folder, app, site, window, field and cell
the planner may name is an id that Jarvis issued in THIS run's context: f12 (a file), d3 (a place), a1 (an app), s2
(a site), w1 (a window), e7 (a field), c4 (a cell); n1 is a new folder the plan itself makes (make_folder's id).
Each id resolves to the entry Jarvis's own index gave it (``Ref.target``: a path, an app entry ...), and only in the
index that issued it: a new run starts a new index, so an id from another run, a typed path or an id with a
leading zero is unknown (validate.py: unknown_ref). Numbers follow the schema's patterns (f up to 4 digits; d, a,
e, c up to 3; s, w, n up to 2): once a kind's numbers run out it issues no more (``issue`` returns "").

``issue(kind, target, label)`` gives the same id to the same target twice. The label is what the planner sees of
it ("name.ext", "Microsoft Word"); for the kinds other people name (files, places, windows, fields, cells) the
suspect scanner (guard.suspect_text) reads it, and a label that reads like an order marks the ref ``suspect``.
Nothing here is logged but ``counts()``; ``repr`` never shows a target or a label.

Public API
==========
Kinds: ``FILE`` "f", ``PLACE`` "d", ``APP`` "a", ``SITE`` "s", ``WINDOW`` "w", ``FIELD`` "e", ``CELL`` "c", ``NEW``
"n" (``KINDS``, ``CAPS``); ``REF_RE``.
``Ref(id, kind, target, label, suspect, flags)``.
``RefIndex(run_id)``: ``issue(kind, target, label, suspect=None, flags=())``, ``resolve(ref_id, kind=None)``,
``knows(ref_id)``, ``ids(kind)``, ``refs(kind)``, ``suspects()``, ``counts()``.
``parse_ref(text)`` -> (kind, number) or None.
"""

from __future__ import annotations

import re
from collections.abc import Hashable, Iterable
from dataclasses import dataclass, field
from typing import Any

from . import guard

FILE = "f"
PLACE = "d"
APP = "a"
SITE = "s"
WINDOW = "w"
FIELD = "e"
CELL = "c"
NEW = "n"
KINDS = (FILE, PLACE, APP, SITE, WINDOW, FIELD, CELL, NEW)
# The highest number of each kind (the schema's digit counts: f{1,4}, d{1,3}, a{1,3}, s{1,2}, w{1,2}, e{1,3},
# c{1,3}, n{1,2}).
CAPS = {FILE: 9999, PLACE: 999, APP: 999, SITE: 99, WINDOW: 99, FIELD: 999, CELL: 999, NEW: 99}
# Kinds whose labels other people wrote: read by the suspect scanner.
SCANNED_KINDS = frozenset({FILE, PLACE, WINDOW, FIELD, CELL})
REF_RE = re.compile(r"([fdaswecn])([1-9][0-9]{0,3})")


def parse_ref(text: Any) -> tuple[str, int] | None:
    """("f", 12) for "f12"; None for anything else (leading zeros, spaces, capitals, a number past its kind's cap)."""
    if not isinstance(text, str):
        return None
    match = REF_RE.fullmatch(text)
    if match is None:
        return None
    kind, number = match.group(1), int(match.group(2))
    return (kind, number) if number <= CAPS[kind] else None


@dataclass(frozen=True)
class Ref:
    id: str
    kind: str
    target: Any = field(repr=False)          # Jarvis's own index entry (a path, an app entry ...)
    label: str = field(repr=False, default="")
    suspect: bool = False
    flags: frozenset[str] = frozenset()


class RefIndex:
    """The refs of one run; see the module docstring."""

    def __init__(self, run_id: str = "") -> None:
        self.run_id = run_id
        self._refs: dict[str, Ref] = {}
        self._by_target: dict[tuple[str, Hashable], str] = {}
        self._next = {kind: 1 for kind in KINDS}

    def issue(self, kind: str, target: Any, label: str = "", *, suspect: bool | None = None,
              flags: Iterable[str] = ()) -> str:
        """The id of ``target`` (a new one, or the one it already has); "" once ``kind``'s numbers run out."""
        if kind not in KINDS:
            raise ValueError(f"unknown ref kind {kind!r}")
        key = (kind, _hashable(target))
        known = self._by_target.get(key)
        if known is not None:
            return known
        number = self._next[kind]
        if number > CAPS[kind]:
            return ""
        self._next[kind] = number + 1
        ref_id = f"{kind}{number}"
        text = label if isinstance(label, str) else ""
        if suspect is None:
            suspect = kind in SCANNED_KINDS and guard.suspect_text(text)
        self._refs[ref_id] = Ref(ref_id, kind, target, text, bool(suspect), frozenset(flags))
        self._by_target[key] = ref_id
        return ref_id

    def resolve(self, ref_id: Any, kind: str | None = None) -> Ref | None:
        """The ref this index issued as ``ref_id`` (of ``kind`` when given), else None."""
        if not isinstance(ref_id, str) or parse_ref(ref_id) is None:
            return None
        ref = self._refs.get(ref_id)
        if ref is None or (kind is not None and ref.kind != kind):
            return None
        return ref

    def knows(self, ref_id: Any) -> bool:
        return self.resolve(ref_id) is not None

    def refs(self, kind: str) -> list[Ref]:
        return [ref for ref in self._refs.values() if ref.kind == kind]

    def ids(self, kind: str) -> list[str]:
        return [ref.id for ref in self.refs(kind)]

    def suspects(self) -> list[str]:
        return [ref.id for ref in self._refs.values() if ref.suspect]

    def counts(self) -> dict[str, int]:
        """How many refs of each kind (and how many suspect): safe to log."""
        counts = {kind: len(self.refs(kind)) for kind in KINDS if self.refs(kind)}
        suspects = len(self.suspects())
        if suspects:
            counts["suspect"] = suspects
        return counts

    def __len__(self) -> int:
        return len(self._refs)

    def __repr__(self) -> str:
        return f"RefIndex({self.counts()})"


def _hashable(target: Any) -> Hashable:
    try:
        hash(target)
    except TypeError:
        return ("id", id(target))
    return target
