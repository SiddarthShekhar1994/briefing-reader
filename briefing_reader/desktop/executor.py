"""Carrying out a desktop request's steps (SPEC sections 2.1, 3 step 6, 5 and 6.4; hard rules 3, 6, 8 and 14; no Qt).

The executor runs on the desktop worker's thread only (desktop.worker), one step at a time, in order. A step is a
planner's checked step (desktop.validate.CheckedStep: its tier and its targets as this run's RefIndex resolved them)
or a local route (desktop.routes.LocalRoute). Right before each OS call its target is re-resolved and re-checked
(hard rule 6; SPEC 5.2 and 5.3) - Jarvis's own entry, never a bare id:

* a file or a folder: the scope loaded afresh (paths.load_scope through ``scope()``), paths.check_path (inside a
  root, a fixed local drive, no link anywhere, no excluded place, code folder or secret file), the same final path as
  when it was listed and the same identity (backend.FileIdentity: volume, file id and - for a file - size and mtime);
  a known folder of a local route ("open Downloads") is found in the fresh scope by its key;
* the NEVER list again: a file opens at once only when its real type is a document (guard.open_kind), never a secret
  file; an app only when the Start menu, read afresh (``apps()``), still lists the same id with the same name and it
  is on no never-open list (desktop.apps); a web address only when it is https on a public host (guard.url_problem).

Then the tier decides (checks only ever promote it, hard rule 3): INSTANT steps make their one OS call (backend:
open_document, show_in_folder, open_folder, launch_app, open_url); ONE_CLICK and CARD steps make none - they are
held (WAITING) for the owner's click on their card (P1.9); find_files lists what the local search finds (FOUND; never
fed back to Claude). An open_file that opens keeps the request's other matches (``others``: owner decision D5's
"Open this instead" rows, P1.9). The rate caps hold again per request (MAX_OPENS opens, MAX_URLS web pages: SPEC
2.1), and a cancel stops before the next step: one OS call in flight completes, no new one starts.

A click on a result card's button (P1.9, ``run_click``) goes the same way with the card's OWN item - the resolved
target and the identity it was shown with, never an id resolved again against a newer index (verified review note)
- re-checked as before any OS call; the click stands for at most ``granted`` (ONE_CLICK: a cyan card's Open, an "Open
this instead" row, Show in folder; CARD: an amber NEEDS YOUR OK card's Open). A step whose tier, from the item as it
is now, is above that is held again (WAITING) and nothing opens. A document that can run macros or scripts opens
only on a CARD click (backend.open_script_document); a shortcut never opens itself: its card's Open opens the
document it names (``via``: the shortcut is re-checked and its target read again first - it must still name that
document, unchanged).

A target that changed since it was listed is left as it is (``CHANGED``: SPEC 5.3's "Changed since - left as it
is"); an OS call that fails is FAILED with its winerror. ``run_words`` is Jarvis's own sentence for a run (the HUD's
caption): kinds and counts - a known folder's own label at most - never a name, a path, a title or an address.
``describe`` is a step's line in Live (its skill, tier, status and code). Logs carry run ids, skills, statuses, codes,
counts, durations, exception types and winerrors only.

Public API
==========
Statuses ``DONE``, ``WAITING``, ``FOUND``, ``REFUSED``, ``FAILED``, ``CANCELLED``; codes ``CHANGED``, ``OS_FAILED``,
``GUARD``, ``STOPPED`` (and desktop.validate's NEVER codes); ``StepResult``; ``DesktopRun`` (``done()``,
``waiting()``, ``found()``, ``not_done()``, ``counts()``, ``statuses_text()``; ``shown``: what the result cards show
of it - desktop.results, P1.9); ``FindQuery``; ``Executor(backend, scope=, apps=, find=, clock=)`` (``run_plan(plan,
words=, others_from=, cancel=)``, ``run_route(route, words=, run_id=)``, ``run_click(skill, target, granted=, ...)``,
``recheck(item)``); ``run_words(run, plan=None, address=)``; ``run_soft(run, plan=None)``; ``describe(result)``;
``status_words(result)``; ``WAITING_NOUNS``; ``MAX_OTHERS``.
"""

from __future__ import annotations

import logging
import ntpath
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..persona import with_address
from . import apps as desktop_apps
from . import guard, paths
from . import validate as dv
from .backend import DesktopBackend, FileIdentity
from .context import AppItem, FileItem, Place, SiteItem
from .index import name_score, query_words
from .refs import FILE, PLACE, Ref

logger = logging.getLogger(__name__)

DONE, WAITING, FOUND, REFUSED, FAILED, CANCELLED = "done", "waiting", "found", "refused", "failed", "cancelled"
STATUSES = (DONE, WAITING, FOUND, REFUSED, FAILED, CANCELLED)
CHANGED = "changed"            # the target changed since it was listed: left as it is (SPEC 5.3)
OS_FAILED = "os_error"         # Windows did not do it
GUARD = "guard"                # the backend's own last guard refused it
STOPPED = "cancelled"          # the owner cancelled before this step
MAX_OTHERS = 5                 # the other matches kept for "Open this instead" (D5; P1.9)
CODE_WORDS = {CHANGED: "it changed since I looked - left as it is",
              OS_FAILED: "Windows couldn't open it",
              GUARD: "Jarvis's last check stopped it",
              STOPPED: "you cancelled first"}


def code_words(code: str) -> str:
    """A refusal or failure code in Jarvis's own words (Live, the caption): validate's REASON_WORDS or the run's."""
    return CODE_WORDS.get(code) or dv.reason_words(code) or "it couldn't be done"


@dataclass(frozen=True)
class StepResult:
    """One step carried out: its number in the plan (0: a local route), skill, tier and status; the code when it was
    refused, failed or cancelled; why its tier went up (``flags``); what it opened or holds (``target``: Jarvis's own
    entry - a FileItem, a Place, an AppItem, an https address or a SiteItem; memory only); a known folder's key when
    it is one ("downloads"); the other matches (``others``, D5) and what a find found (``found``); a winerror."""

    number: int
    skill: str
    tier: str
    status: str
    code: str = ""
    flags: frozenset[str] = frozenset()
    target: Any = field(default=None, repr=False)
    folder: str = ""
    others: tuple[Any, ...] = field(default=(), repr=False)
    found: tuple[Any, ...] = field(default=(), repr=False)
    winerror: int = 0

    def __repr__(self) -> str:
        return f"StepResult({self.number}, {self.skill}, {self.status}{', ' + self.code if self.code else ''})"


@dataclass(frozen=True)
class DesktopRun:
    """What a request's steps came to (memory only): every result in order and how long it took; ``shown``: what
    its result cards show (desktop.results.present, made on the desktop thread right after the run)."""

    run_id: str = ""
    results: tuple[StepResult, ...] = ()
    seconds: float = 0.0
    shown: tuple[Any, ...] = field(default=(), repr=False)

    def of(self, *statuses: str) -> tuple[StepResult, ...]:
        return tuple(result for result in self.results if result.status in statuses)

    def done(self) -> tuple[StepResult, ...]:
        return self.of(DONE)

    def waiting(self) -> tuple[StepResult, ...]:
        return self.of(WAITING)

    def found(self) -> tuple[StepResult, ...]:
        return self.of(FOUND)

    def not_done(self) -> tuple[StepResult, ...]:
        return self.of(REFUSED, FAILED, CANCELLED)

    def counts(self) -> dict[str, int]:
        """Statuses and their counts (safe to log)."""
        counts: dict[str, int] = {}
        for result in self.results:
            counts[result.status] = counts.get(result.status, 0) + 1
        return counts

    def statuses_text(self) -> str:
        """"done=1, waiting=1" (safe to log)."""
        return ", ".join(f"{status}={count}" for status, count in self.counts().items()) or "nothing"

    def __repr__(self) -> str:
        return f"DesktopRun({self.run_id}, {self.counts()})"


@dataclass(frozen=True)
class FindQuery:
    """A find_files step as the local search takes it: its words, the folders it looks in (None: the roots), the kind
    and how many days back."""

    words: tuple[str, ...] = ()
    folders: tuple[str, ...] | None = None
    kind: str = ""
    within_days: int | None = None


def _key(path: str) -> str:
    return ntpath.normcase(path or "").rstrip("\\")


class Executor:
    """See the module docstring. ``scope()``: the scope loaded afresh; ``apps()``: the Start menu read afresh;
    ``find(query)``: the local search for a find_files step (the files or folders found, checked)."""

    def __init__(self, backend: DesktopBackend, *, scope: Callable[[], paths.Scope],
                 apps: Callable[[], Sequence[AppItem]],
                 find: Callable[[FindQuery], Sequence[Any]] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.backend = backend
        self._scope = scope
        self._apps = apps
        self._find = find
        self._clock = clock
        self._cancel: Any = None                          # the run's cancel (anything with is_set())

    # ---- a planner's plan -------------------------------------------------------------------------

    def run_plan(self, plan: dv.DesktopPlan, *, words: str = "", others_from: Sequence[FileItem] = (),
                 cancel: threading.Event | None = None) -> DesktopRun:
        """Every accepted step of ``plan`` (in order; the refused ones were never accepted), as the module docstring
        says. ``words``: the owner's typed words (the D5 other matches are ranked by them); ``others_from``: the
        files the planner was shown."""
        started = self._clock()
        self._cancel = cancel
        caps = _Caps()
        results: list[StepResult] = []
        for step in plan.accepted():
            if cancel is not None and cancel.is_set():
                results.append(StepResult(step.number, step.skill, step.tier, CANCELLED, STOPPED, step.flags))
                continue
            result = self._plan_step(step, words, others_from, caps)
            results.append(result)
        run = DesktopRun(plan.run_id, tuple(results), round(self._clock() - started, 3))
        self._log(run)
        return run

    def _plan_step(self, step: dv.CheckedStep, words: str, others_from: Sequence[FileItem],
                   caps: _Caps) -> StepResult:
        skill, tier, flags = step.skill, step.tier, step.flags
        planned = step.planned
        if skill == dv.FIND_FILES:
            folders = tuple(ref.target.path for ref in step.targets
                            if ref.kind == PLACE and isinstance(ref.target, Place)) or None
            query = FindQuery(query_words(planned.query if planned is not None else ""), folders,
                              planned.kind if planned is not None else "",
                              planned.within_days if planned is not None else None)
            return self._found(step.number, tier, query)
        target_ref: Ref | None = step.targets[0] if step.targets else None
        target = target_ref.target if target_ref is not None else None
        if skill == dv.OPEN_URL:
            target = planned.url if planned is not None and planned.url else target
        result = self._open(step.number, skill, tier, flags, target, "", caps)
        if skill == dv.OPEN_FILE and result.status in (DONE, WAITING) and target_ref is not None \
                and target_ref.kind == FILE:
            result = _with_others(result, _others(target, words, others_from))
        return result

    # ---- a local route ------------------------------------------------------------------------------

    def run_route(self, route: Any, *, words: str = "", run_id: str = "",
                  cancel: threading.Event | None = None) -> DesktopRun:
        """A local route (desktop.routes.LocalRoute) carried out: an open (INSTANT: the owner named it exactly),
        or a find whose files the route's search found (checked as they were listed); nothing once ``cancel`` is
        set."""
        started = self._clock()
        self._cancel = cancel
        kind, tier = getattr(route, "kind", ""), getattr(route, "tier", "") or dv.INSTANT
        if cancel is not None and cancel.is_set():
            result = StepResult(0, kind, tier, CANCELLED, STOPPED)
        elif getattr(route, "code", ""):
            result = StepResult(0, kind, tier, REFUSED, route.code)
        elif kind == dv.FIND_FILES:
            hits = tuple(getattr(route, "hits", ()))
            result = StepResult(0, kind, tier, FOUND, found=hits)
        else:
            result = self._open(0, kind, tier, frozenset(), getattr(route, "target", None),
                                getattr(route, "folder", "") or "", _Caps())
        run = DesktopRun(run_id, (result,), round(self._clock() - started, 3))
        self._log(run)
        return run

    # ---- a click on a result card (P1.9) ---------------------------------------------------------------

    def run_click(self, skill: str, target: Any, *, granted: str, run_id: str = "", flags: frozenset[str] = frozenset(),
                  via: FileItem | None = None, words: str = "", others_from: Sequence[FileItem] = (),
                  cancel: Any = None) -> DesktopRun:
        """The owner's click on a card's button, as the module docstring says: ``target`` the card's own item (with
        its identity), ``granted`` what the click stands for at most (ONE_CLICK or CARD; never more), ``flags`` why
        the card asked (kept on the result), ``via`` the shortcut whose target ``target`` is, ``words`` and
        ``others_from`` the candidates of D5's other matches when a file opens."""
        started = self._clock()
        self._cancel = cancel
        granted = granted if granted in (dv.ONE_CLICK, dv.CARD) else dv.ONE_CLICK
        flags = frozenset(flags)
        code = self._check_link(via, target) if via is not None else ""
        if cancel is not None and cancel.is_set():
            result = StepResult(0, skill, dv.INSTANT, CANCELLED, STOPPED, flags, target)
        elif code:
            logger.info("Desktop guard: refused %s %s", skill, code)
            result = StepResult(0, skill, dv.CARD, REFUSED, code, flags, via)
        else:
            result = self._open(0, skill, dv.INSTANT, flags, target, "", _Caps(), granted=granted)
            if skill == dv.OPEN_FILE and result.status == DONE and isinstance(target, FileItem):
                result = _with_others(result, _others(target, words, others_from))
        run = DesktopRun(run_id, (result,), round(self._clock() - started, 3))
        self._log(run)
        return run

    def recheck(self, item: Any) -> bool:
        """A listed file or folder (with the identity it was listed with) is still where it was, unchanged, inside the
        scope as it is now and never secret-shaped - the check before any OS call on it; True when it passes."""
        if isinstance(item, Place):
            return not self._check_item(item, folder=True)[0]
        if isinstance(item, FileItem):
            return not self._check_item(item, folder=False)[0]
        return False

    def _check_link(self, via: Any, target: Any) -> str:
        """A shortcut's card: the shortcut is still the one shown (re-checked) and still names ``target``'s path."""
        code, _verdict = self._check_item(via, folder=False)
        if code:
            return code
        if guard.open_kind(via.name) != guard.OPEN_SHORTCUT or not isinstance(target, FileItem):
            return dv.BLOCKED_TYPE
        try:
            named = self.backend.link_target(via.path)
        except (OSError, ValueError):
            return CHANGED
        return "" if _key(named) == _key(target.path) else CHANGED

    # ---- one step -----------------------------------------------------------------------------------

    def _found(self, number: int, tier: str, query: FindQuery) -> StepResult:
        if self._find is None:
            return StepResult(number, dv.FIND_FILES, tier, FOUND)
        try:
            found = tuple(self._find(query))
        except Exception as exc:  # noqa: BLE001 - a search that fails finds nothing
            logger.warning("Desktop: a find could not search (%s)", type(exc).__name__)
            found = ()
        return StepResult(number, dv.FIND_FILES, tier, FOUND, found=found)

    def _open(self, number: int, skill: str, tier: str, flags: frozenset[str], target: Any, folder: str,
              caps: _Caps, *, granted: str = dv.INSTANT) -> StepResult:
        """Re-check ``target`` for ``skill`` (the module docstring), then - its tier no higher than ``granted`` (a
        step: INSTANT; a card's click: what the click stands for) - its one OS call, or else hold it (WAITING)."""
        def refused(code: str, shown: Any = target) -> StepResult:
            logger.info("Desktop guard: refused %s %s", skill, code)
            return StepResult(number, skill, tier, REFUSED, code, frozenset(flags), shown, folder)

        if skill == dv.OPEN_FILE:
            code, verdict = self._check_item(target, folder=False)
            if code:
                return refused(code)
            tier, flags = _file_tier(verdict.path, tier, flags)
            kind = guard.open_kind(ntpath.basename(verdict.path))
            if tier == dv.INSTANT and kind != guard.OPEN_DOCUMENT:
                return refused(dv.BLOCKED_TYPE)
            if kind in (guard.OPEN_BLOCKED, guard.OPEN_OTHER):
                return refused(dv.BLOCKED_TYPE)
            if kind == guard.OPEN_SHORTCUT:              # never opened itself: its card opens what it names
                return StepResult(number, skill, tier, WAITING, "", frozenset(flags), target, folder)
            method = "open_script_document" if kind == guard.OPEN_SCRIPT else "open_document"
            return self._act(number, skill, tier, flags, target, folder, caps, method, verdict.path, granted)
        if skill == dv.SHOW_IN_FOLDER:
            code, verdict = self._check_item(target, folder=False)
            if code:
                return refused(code)
            return self._act(number, skill, tier, flags, target, folder, caps, "show_in_folder", verdict.path,
                             granted)
        if skill == dv.OPEN_FOLDER:
            if folder:
                code, verdict, target = self._check_root(folder)
            else:
                code, verdict = self._check_item(target, folder=True)
            if code:
                return refused(code)
            return self._act(number, skill, tier, flags, target, folder, caps, "open_folder", verdict.path, granted)
        if skill == dv.OPEN_APP:
            code, app = self._check_app(target)
            if code:
                return refused(code)
            return self._act(number, skill, tier, flags, app, folder, caps, "launch_app", app.app_id, granted)
        if skill == dv.OPEN_URL:
            url = target.url if isinstance(target, SiteItem) else target
            problem = guard.url_problem(url)
            if problem:
                return refused(dv.SHAPE if problem == guard.URL_SCHEME else dv.SITE_NOT_APPROVED)
            return self._act(number, skill, tier, flags, target, folder, caps, "open_url", url, granted)
        return refused(dv.NOT_YET)

    def _act(self, number: int, skill: str, tier: str, flags: frozenset[str], target: Any, folder: str,
             caps: _Caps, method: str, argument: str, granted: str = dv.INSTANT) -> StepResult:
        if dv.TIERS.index(tier) > dv.TIERS.index(granted):
            return StepResult(number, skill, tier, WAITING, "", frozenset(flags), target, folder)
        if method == "open_script_document" and granted != dv.CARD:
            return StepResult(number, skill, dv.CARD, WAITING, "", frozenset(flags), target, folder)
        if self._cancel is not None and self._cancel.is_set():          # asked again right before the OS call
            return StepResult(number, skill, tier, CANCELLED, STOPPED, frozenset(flags), target, folder)
        if not caps.take(skill):
            logger.info("Desktop guard: refused %s %s", skill, dv.CAP)
            return StepResult(number, skill, tier, REFUSED, dv.CAP, frozenset(flags), target, folder)
        try:
            getattr(self.backend, method)(argument)
        except ValueError:
            logger.warning("Desktop guard: the backend's last check refused %s", skill)
            return StepResult(number, skill, tier, REFUSED, GUARD, frozenset(flags), target, folder)
        except Exception as exc:  # noqa: BLE001 - Windows did not do it: the caption says so
            winerror = int(getattr(exc, "winerror", 0) or 0)
            logger.warning("Desktop: %s failed (%s %s)", skill, type(exc).__name__, winerror or "")
            return StepResult(number, skill, tier, FAILED, OS_FAILED, frozenset(flags), target, folder,
                              winerror=winerror)
        return StepResult(number, skill, tier, DONE, "", frozenset(flags), target, folder)

    # ---- the re-checks ------------------------------------------------------------------------------

    def _check_item(self, item: Any, *, folder: bool) -> tuple[str, paths.PathVerdict | None]:
        """A listed file (or folder) re-checked: the path check in the scope as it is now, the same final path, the
        same identity; a file never secret-shaped."""
        wanted = Place if folder else FileItem
        if not isinstance(item, wanted):
            return dv.PATH_OUTSIDE, None
        verdict = paths.check_path(item.path, self._scope(), self.backend)
        if verdict.reason == paths.R_MISSING:
            return CHANGED, None                          # gone since it was listed: left as it is
        if not verdict.ok:
            return verdict.code or dv.PATH_OUTSIDE, None
        if _key(verdict.path) != _key(item.path):
            return CHANGED, None                          # it resolves elsewhere now
        if not folder and guard.secret_file(verdict.path):
            return dv.EXCLUDED, None
        try:
            now = self.backend.file_id(verdict.path)
        except OSError:
            return CHANGED, None
        recorded = item.identity
        if not isinstance(recorded, FileIdentity) or not isinstance(now, FileIdentity) \
                or now.is_dir != folder or not recorded.same_item(now):
            return CHANGED, None
        return "", verdict

    def _check_root(self, key: str) -> tuple[str, paths.PathVerdict | None, Any]:
        """A known folder of a local route ("open Downloads"): found in the scope as it is now, by its key."""
        scope = self._scope()
        root = next((root for root in scope.roots if root.key == key), None)
        if root is None:
            return dv.PATH_OUTSIDE, None, None
        verdict = paths.check_path(root.path, scope, self.backend)
        if not verdict.ok:
            return verdict.code or dv.PATH_OUTSIDE, None, None
        return "", verdict, Place(verdict.path, (root.label,))

    def _check_app(self, app: Any) -> tuple[str, AppItem | None]:
        """An app re-checked: the Start menu read afresh lists the same id with the same name, its id is of its shape
        and it is on no never-open list."""
        if not isinstance(app, AppItem):
            return dv.APP_NOT_APPROVED, None
        try:
            listed = self._apps()
        except Exception as exc:  # noqa: BLE001 - the Start menu can't be read: nothing is launched
            logger.warning("Desktop: the Start menu could not be read again (%s)", type(exc).__name__)
            return CHANGED, None
        current = desktop_apps.find(listed, app.app_id)
        if current is None or " ".join(current.name.casefold().split()) != " ".join(app.name.casefold().split()):
            return CHANGED, None
        if desktop_apps.app_id_problem(current.app_id) or desktop_apps.never(current.name, current.app_id):
            return dv.APP_NOT_APPROVED, None
        return "", current

    def _log(self, run: DesktopRun) -> None:
        skills: dict[str, int] = {}
        for result in run.done():
            skills[result.skill] = skills.get(result.skill, 0) + 1
        opened = ", ".join(f"{skill}={count}" for skill, count in skills.items()) or "none"
        logger.info("Desktop %s: carried out: %s; opened %s (%.2f s)", run.run_id or "-", run.statuses_text(),
                    opened, run.seconds)


class _Caps:
    """A request's rate caps (SPEC 2.1), counted over the OS opens actually asked for."""

    def __init__(self) -> None:
        self.opens = 0
        self.urls = 0

    def take(self, skill: str) -> bool:
        if skill not in dv.OPEN_SKILLS:
            return True
        if self.opens >= dv.MAX_OPENS or (skill == dv.OPEN_URL and self.urls >= dv.MAX_URLS):
            return False
        self.opens += 1
        self.urls += skill == dv.OPEN_URL
        return True


def _file_tier(path: str, tier: str, flags: frozenset[str]) -> tuple[str, frozenset[str]]:
    """open_file's tier from the file's REAL name now (validate's rules again; only ever promoted): a document that
    can run macros or scripts and a shortcut need a card, hidden characters a click."""
    name = ntpath.basename(path)
    kind = guard.open_kind(name)
    flags = set(flags)
    if kind == guard.OPEN_SCRIPT:
        tier = dv.promote(tier, dv.CARD)
        flags.add(dv.FLAG_SCRIPT)
    elif kind == guard.OPEN_SHORTCUT:
        tier = dv.promote(tier, dv.CARD)
        flags.add(dv.FLAG_SHORTCUT)
    if any(guard.hidden_char(char) for char in name):
        tier = dv.promote(tier, dv.ONE_CLICK)
        flags.add(dv.FLAG_HIDDEN_CHARS)
    return tier, frozenset(flags)


def _others(target: Any, words: str, files: Sequence[FileItem]) -> tuple[FileItem, ...]:
    """The other matches for "Open this instead" (D5): the files the planner was shown whose names hold the owner's
    words, the most words and then the newest first, never the one opened, at most MAX_OTHERS."""
    wanted = query_words(words)
    opened = _key(getattr(target, "path", ""))
    scored = []
    for item in files:
        if not isinstance(item, FileItem) or _key(item.path) == opened:
            continue
        score = name_score(item.name, wanted)
        if score:
            changed = item.identity.mtime_ns if isinstance(item.identity, FileIdentity) else 0
            scored.append((score, changed, item))
    scored.sort(key=lambda found: (-found[0], -found[1]))
    return tuple(item for _score, _changed, item in scored[:MAX_OTHERS])


def _with_others(result: StepResult, others: tuple[FileItem, ...]) -> StepResult:
    return StepResult(result.number, result.skill, result.tier, result.status, result.code, result.flags,
                      result.target, result.folder, others, result.found, result.winerror)


# --------------------------------------------------------------------------
# Jarvis's own words (the HUD's caption) and Live's lines
# --------------------------------------------------------------------------

_NOUNS = {dv.OPEN_FILE: ("the file", "{n} files"), dv.OPEN_FOLDER: ("the folder", "{n} folders"),
          dv.OPEN_APP: ("the app", "{n} apps"), dv.OPEN_URL: ("the web page", "{n} web pages"),
          dv.SHOW_IN_FOLDER: ("the file", "{n} files"), dv.FIND_FILES: ("the search", "{n} searches")}
# What waits for the owner, by why its tier went up (the first that applies).
WAITING_NOUNS = ((dv.FLAG_SCRIPT, "a file that can run macros or scripts"), (dv.FLAG_SHORTCUT, "a shortcut"),
                 (dv.FLAG_HIDDEN_CHARS, "a file with hidden characters in its name"),
                 (dv.FLAG_NOT_NAMED, "an app you didn't name exactly"),
                 (dv.FLAG_SUGGESTED, "a web page you didn't type"))


def _and(items: Sequence[str]) -> str:
    items = [item for item in items if item]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _count(n: int, one: str, many: str) -> str:
    return one if n == 1 else many.format(n=n)


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def _opened_words(done: Sequence[StepResult]) -> str:
    """"Opened the file, your Downloads folder and the app" (kinds and counts; a known folder's own label only)."""
    parts: list[str] = []
    known = [paths.ROOT_LABELS[result.folder] for result in done
             if result.skill == dv.OPEN_FOLDER and result.folder in paths.ROOT_LABELS]
    for skill in (dv.OPEN_FILE, dv.OPEN_FOLDER, dv.OPEN_APP, dv.OPEN_URL):
        count = sum(1 for result in done if result.skill == skill)
        if skill == dv.OPEN_FOLDER:
            parts += [f"your {label} folder" for label in known]
            count -= len(known)
        if count > 0:
            one, many = _NOUNS[skill]
            parts.append(_count(count, one, many))
    clauses = [f"opened {_and(parts)}"] if parts else []
    shown = sum(1 for result in done if result.skill == dv.SHOW_IN_FOLDER)
    if shown:
        clauses.append("showed the file in its folder" if shown == 1 else f"showed {shown} files in their folders")
    return _capital(_and(clauses))


def _found_words(found: Sequence[StepResult]) -> str:
    items = [item for result in found for item in result.found]
    folders = sum(1 for item in items if isinstance(item, Place))
    files = len(items) - folders
    if not items:
        return "Found nothing that matches"
    parts = [_count(files, "1 matching file", "{n} matching files")] if files else []
    if folders:
        parts.append(_count(folders, "1 matching folder", "{n} matching folders"))
    return "Found " + _and(parts)


def _waiting_words(waiting: Sequence[StepResult]) -> str:
    cards = sum(1 for result in waiting if result.tier == dv.CARD)
    if len(waiting) == 1:
        result = waiting[0]
        noun = next((words for flag, words in WAITING_NOUNS if flag in result.flags), "one more step")
        verb = "needs your OK first" if cards else "waits for your click"
        return _capital(f"{noun} {verb}")
    if cards == len(waiting):
        return f"{len(waiting)} steps need your OK first"
    return f"{len(waiting)} steps wait for your click" + (" or OK" if cards else "")


def _not_done_words(results: Sequence[StepResult]) -> str:
    if len(results) == 1:
        result = results[0]
        noun = _NOUNS.get(result.skill, ("that", ""))[0]
        if result.folder in paths.ROOT_LABELS:
            noun = f"your {paths.ROOT_LABELS[result.folder]} folder"
        if result.status == CANCELLED:
            return "Stopped - nothing more was opened"
        if result.code == CHANGED:
            return _capital(f"{noun} changed since I looked - left as it is")
        if result.status == FAILED:
            return f"Windows couldn't open {noun}"
        return f"I didn't open {noun} - {code_words(result.code)}"
    return f"{len(results)} steps couldn't be done - Live says why"


def run_words(run: DesktopRun | None, plan: dv.DesktopPlan | None = None, address: str = "sir") -> str:
    """Jarvis's own sentence for a run (the HUD's caption): what opened, what was found, what waits for the owner and
    what was not done - kinds and counts, a known folder's own label at most; never a name, a path, a title or an
    address, never Claude's text. "" when there is nothing to say (no step was carried out)."""
    if run is None or not run.results:
        return ""
    sentences: list[str] = []
    done, waiting, found, not_done = run.done(), run.waiting(), run.found(), run.not_done()
    if done:
        sentences.append(_opened_words(done))
    if found:
        sentences.append(_found_words(found))
    if waiting:
        words = _waiting_words(waiting)
        if not done and not found:
            words += " - nothing was opened"
        sentences.append(words)
    if not_done:
        sentences.append(_not_done_words(not_done))
    refused = len(plan.refused()) if plan is not None else 0
    if refused:
        sentences.append(f"{refused} step was refused - Live says why" if refused == 1
                         else f"{refused} steps were refused - Live says why")
    return with_address(". ".join(sentences) + ", {sir}.", address)


def run_soft(run: DesktopRun | None, plan: dv.DesktopPlan | None = None) -> bool:
    """The caption takes the soft (amber) tone: something was refused, failed or cancelled, a step needs the owner's
    OK on a card, or nothing opened, was found or waits."""
    if run is None or not run.results:
        return True
    if run.not_done() or (plan is not None and plan.refused()):
        return True
    if any(result.tier == dv.CARD for result in run.waiting()):
        return True
    return not (run.done() or run.found() or run.waiting())


_STATUS_WORDS = {DONE: "opened", WAITING: "waiting for you", FOUND: "found", REFUSED: "refused", FAILED: "failed",
                 CANCELLED: "cancelled"}


def status_words(result: StepResult) -> str:
    if result.status == DONE and result.skill == dv.SHOW_IN_FOLDER:
        return "shown in its folder"
    if result.status == FOUND:
        return f"found {len(result.found)}"
    return _STATUS_WORDS.get(result.status, result.status)


def describe(result: StepResult) -> str:
    """"Step 1 - open_file - instant - opened" / "Step 2 - open_app - one-click - waiting for you" / "Step 3 - open_url
    - instant - refused: changed" (Live; never a name, a path or an address)."""
    head = f"Step {result.number}" if result.number else "On this PC"
    words = status_words(result)
    if result.code:
        words += f": {result.code}"
    if result.winerror:
        words += f" (Windows error {result.winerror})"
    return f"{head} - {result.skill} - {result.tier.replace('_', '-')} - {words}"
