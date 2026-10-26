"""The desktop's work on this PC, as the "desktop" thread does it (SPEC sections 3, 4.1, 5 and 6.4; no Qt).

A ``DesktopService`` belongs to the desktop worker's thread (desktop.worker): every method is called there and only
there, one at a time, so nothing in it is shared with the GUI or the Ask thread. It holds:

* the backend, made there at the first job (``backend_factory``: Win32Backend in the app, a fake in the tests; while
  the test guard is on the real one refuses to be made - then the desktop can't look or open, and says so);
* the scope (paths.load_scope over [desktop] folders and extra_folders, the Jarvis project and its data folder
  excluded), the Start menu (desktop.apps.load) and the folder tree (desktop.index.folder_tree), each kept a while -
  a re-check before an OS call always reads the scope and the Start menu afresh;
* ``resolve(words)``: a typed desktop request - its local route (desktop.routes; a find searches) carried out now
  (desktop.executor), or else the planner's context (``context_for``: the places, the files the owner's words find
  - first in the places his words name -, the Recent items, the files changed in the days his words say, the
  Start-menu apps his words name, within SPEC 4.1's caps; names only reach the planner);
* ``carry_out(plan, words, context, cancel)``: a planner's checked plan carried out (desktop.executor);
* ``click(click)``: the owner's click on a result card (P1.9: desktop.results.Click - the card's own item, re-checked
  and carried out by executor.run_click);
* ``find(query)``: a find_files step's search.

Every run that comes back carries its result cards (``DesktopRun.shown``: desktop.results.present, made here right
after the run - the other matches and the found items re-checked first, the app a file opened in read from the
associations).

Logs carry counts, durations and exception types only.

Public API
==========
``LOCAL``, ``PLANNER``, ``UNAVAILABLE``; ``Resolved(kind, run_id, route, run, context, seconds)``;
``DesktopService(folders=, extra_folders=, excluded=, backend_factory=, sites=, clock=, now=, hour24=)``:
``backend()``, ``scope(fresh=)``, ``apps(fresh=)``, ``tree()``, ``route_context()``, ``resolve(words, planner=,
override=, run_id=)``, ``context_for(words)``, ``carry_out(plan, words, context, cancel)``, ``click(click)``,
``find(query)``, ``executor()``; ``days_in(words)``; ``RECENT_FILES``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from . import apps as desktop_apps
from . import guard, paths
from . import results as desktop_results
from . import routes as desktop_routes
from . import validate as dv
from .backend import DesktopBackend
from .context import MAX_FILES, MAX_PLACES, AppItem, DesktopContext, Place, SiteItem, build_context
from .executor import DesktopRun, Executor, FindQuery
from .index import LocalSearch, TreeFolder, folder_tree, name_score, pick_places, query_words, recent_files

logger = logging.getLogger(__name__)

LOCAL, PLANNER, UNAVAILABLE = "local", "planner", "unavailable"
SCOPE_TTL_S = 60.0                      # the scope is read again after this long (a re-check always reads it anew)
APPS_TTL_S = 600.0                      # the Start menu likewise
TREE_TTL_S = 300.0                      # the folder tree likewise
RECENT_FILES = 30                       # Recent items' files a context takes at most
WITHIN_FILES = 40                       # files changed in the days the words say, at most
# Days back the owner's words ask for ("from yesterday", "this week").
_DAYS = ((("today",), 1), (("yesterday",), 2), (("this", "week"), 7), (("last", "week"), 14), (("past", "week"), 7),
         (("this", "month"), 31), (("last", "month"), 62))


def days_in(words: Any) -> int | None:
    """How many days back the owner's words look ("yesterday": 2; "this week": 7), or None."""
    tokens = guard.words(words, camel=False)
    found = [days for phrase, days in _DAYS
             if any(tuple(tokens[i:i + len(phrase)]) == phrase for i in range(len(tokens) - len(phrase) + 1))]
    return max(found) if found else None


@dataclass(frozen=True)
class Resolved:
    """A typed desktop request, resolved on the desktop thread: a local route carried out (LOCAL: ``route``,
    ``run``), the planner's context (PLANNER: ``context``; None when the planner may not run) or nothing at all
    (UNAVAILABLE: the desktop can't look or open now)."""

    kind: str
    run_id: str = ""
    route: Any = field(default=None, repr=False)
    run: DesktopRun | None = None
    context: DesktopContext | None = field(default=None, repr=False)
    seconds: float = 0.0


class DesktopService:
    """See the module docstring. Every method runs on the desktop worker's thread only."""

    def __init__(self, *, folders: Sequence[str], extra_folders: Sequence[str] = (), excluded: Sequence[str] = (),
                 backend_factory: Callable[[], DesktopBackend], sites: Sequence[SiteItem] = (),
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] | None = None, hour24: bool = False) -> None:
        self._folders = tuple(folders)
        self._extra = tuple(extra_folders)
        self._excluded = tuple(str(path) for path in excluded if path)
        self._factory = backend_factory
        self._sites = tuple(site for site in sites if isinstance(site, SiteItem))
        self._clock = clock
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._hour24 = bool(hour24)
        self._backend: DesktopBackend | None = None
        self._backend_failed = False
        self._scope: tuple[float, paths.Scope] | None = None
        self._apps: tuple[float, tuple[AppItem, ...]] | None = None
        self._tree: tuple[float, list[TreeFolder]] | None = None

    # ---- what it keeps ------------------------------------------------------------------------------

    def backend(self) -> DesktopBackend | None:
        """The backend, made at the first call (on this thread); None when it can't be (logged by its type once)."""
        if self._backend is None and not self._backend_failed:
            try:
                self._backend = self._factory()
            except Exception as exc:  # noqa: BLE001 - RealBackendInTests in the tests; anything else: no desktop
                self._backend_failed = True
                logger.warning("Desktop: Jarvis can't look on this PC (%s)", type(exc).__name__)
        return self._backend

    def scope(self, *, fresh: bool = False) -> paths.Scope:
        """The roots and the excluded places (read again when ``fresh`` or after SCOPE_TTL_S)."""
        backend = self.backend()
        if backend is None:
            return paths.Scope()
        now = self._clock()
        if fresh or self._scope is None or now - self._scope[0] > SCOPE_TTL_S:
            self._scope = (now, paths.load_scope(self._folders, self._extra, backend, excluded=self._excluded))
        return self._scope[1]

    def apps(self, *, fresh: bool = False) -> tuple[AppItem, ...]:
        """The Start-menu apps Jarvis may open (read again when ``fresh`` or after APPS_TTL_S; none when it can't be
        read)."""
        backend = self.backend()
        if backend is None:
            return ()
        now = self._clock()
        if fresh or self._apps is None or now - self._apps[0] > APPS_TTL_S:
            try:
                listed = desktop_apps.load(backend).apps
            except Exception as exc:  # noqa: BLE001 - no Start menu: no apps
                logger.warning("Desktop: the Start menu could not be read (%s)", type(exc).__name__)
                listed = ()
            self._apps = (now, listed)
        return self._apps[1]

    def tree(self) -> list[TreeFolder]:
        """The folders below the roots down to 3 levels (read again after TREE_TTL_S)."""
        backend = self.backend()
        if backend is None:
            return []
        now = self._clock()
        if self._tree is None or now - self._tree[0] > TREE_TTL_S:
            self._tree = (now, folder_tree(self.scope(), backend, clock=self._clock))
        return self._tree[1]

    def executor(self) -> Executor | None:
        backend = self.backend()
        if backend is None:
            return None
        return Executor(backend, scope=lambda: self.scope(fresh=True), apps=lambda: self.apps(fresh=True),
                        find=self.find, clock=self._clock)

    # ---- a request ----------------------------------------------------------------------------------

    def route_context(self) -> DesktopContext:
        """What the local routes may name: the roots (as places), every Start-menu app Jarvis may open, the sites."""
        scope = self.scope()
        places = tuple(Place(root.path, (root.label,)) for root in scope.roots)
        return DesktopContext(places, (), self.apps(), self._sites)

    def resolve(self, words: str, *, planner: bool = True,
                override: Callable[[str], DesktopContext] | None = None, run_id: str = "",
                cancel: threading.Event | None = None) -> Resolved:
        """A typed desktop request (see the module docstring). ``override(words)``: a context to use instead of this
        PC's (the tests' invented ones); ``planner``: whether the planner may run (Ask on); ``cancel``: once set (the
        app stopped waiting), a local route opens nothing."""
        started = self._clock()
        executor = self.executor()
        if executor is None:
            return Resolved(UNAVAILABLE, run_id)
        try:
            routes_context = override(words) if override is not None else self.route_context()
        except Exception as exc:  # noqa: BLE001 - no candidates: the routes that need none still work
            logger.warning("Desktop: the request's candidates could not be read (%s)", type(exc).__name__)
            routes_context = DesktopContext()
        found = desktop_routes.route(words, routes_context, search=self._route_search)
        if found is not None:
            run = self._shown(executor.run_route(found, words=words, run_id=run_id, cancel=cancel), executor, words)
            return Resolved(LOCAL, run_id, found, run, seconds=round(self._clock() - started, 3))
        if not planner:
            return Resolved(PLANNER, run_id, seconds=round(self._clock() - started, 3))
        try:
            context = override(words) if override is not None else self.context_for(words)
        except Exception as exc:  # noqa: BLE001 - the planner is then told of nothing
            logger.warning("Desktop: the request's candidates could not be read (%s)", type(exc).__name__)
            context = DesktopContext()
        if not isinstance(context, DesktopContext):
            context = DesktopContext()
        return Resolved(PLANNER, run_id, context=context, seconds=round(self._clock() - started, 3))

    def _route_search(self, words: Sequence[str]) -> list[Any]:
        """The find route's search: the files whose names hold any of ``words`` (desktop.routes keeps those holding
        all of them)."""
        backend = self.backend()
        if backend is None:
            return []
        search = LocalSearch(self.scope(), backend, clock=self._clock, now=self._now)
        return list(search.search(tuple(words)).files)

    def context_for(self, words: str) -> DesktopContext:
        """The planner's context for the owner's ``words`` (see the module docstring)."""
        backend = self.backend()
        if backend is None:
            return DesktopContext()
        scope = self.scope()
        cache: dict[str, bool] = {}
        searched = query_words(words)
        tree = self.tree()
        every = [Place(root.path, (root.label,)) for root in scope.roots] + [Place(f.path, f.parts) for f in tree]
        named = [place.path for place in dv.places_named(words, every)]
        search = LocalSearch(scope, backend, clock=self._clock, now=self._now, cache=cache)
        result = search.search(searched, folders=named or None, limit=MAX_FILES)
        files = list(result.files)
        files += recent_files(scope, backend, limit=RECENT_FILES, cache=cache)
        days = days_in(words)
        if days:
            files += list(search.search((), folders=named or None, within_days=days, limit=WITHIN_FILES).files)
        places = pick_places(scope, backend, tree, words=searched, files=files, named=named, cap=MAX_PLACES,
                             cache=cache)
        context = build_context(places, files, desktop_apps.match(words, self.apps()), self._sites,
                                looked_in=result.looked_in, found=result.found)
        logger.info("Desktop: context of %s from the %s", ", ".join(f"{name} {count}" for name, count in
                                                                     context.counts().items()),
                    result.source or "Recent items")
        return context

    def carry_out(self, plan: dv.DesktopPlan, words: str, context: DesktopContext | None,
                  cancel: threading.Event | None = None) -> DesktopRun:
        """A planner's checked plan carried out (desktop.executor), the files it was shown kept for D5's other
        matches."""
        executor = self.executor()
        if executor is None:
            return DesktopRun(plan.run_id)
        others = context.files if isinstance(context, DesktopContext) else ()
        return self._shown(executor.run_plan(plan, words=words, others_from=others, cancel=cancel), executor, words)

    def click(self, click: desktop_results.Click) -> DesktopRun:
        """The owner's click on a result card (desktop.results.Click): the card's own item re-checked and carried out
        (executor.run_click; the click stands for at most ``click.granted``), with the cards of what came of it."""
        executor = self.executor()
        if executor is None or not isinstance(click, desktop_results.Click):
            return DesktopRun(getattr(click, "run_id", ""))
        run = executor.run_click(click.skill, click.target, granted=click.granted, run_id=click.run_id,
                                 flags=click.flags, via=click.via, words=click.words, others_from=click.others_from)
        return self._shown(run, executor, click.words)

    def _shown(self, run: DesktopRun, executor: Executor, words: str) -> DesktopRun:
        """``run`` with its result cards (desktop.results.present), made now - right after it."""
        try:
            cards = desktop_results.present(run, executor=executor, scope=self.scope(), backend=executor.backend,
                                            now=self._now(), words=words, hour24=self._hour24)
        except Exception as exc:  # noqa: BLE001 - no cards: the caption still says what happened
            logger.warning("Desktop %s: the result cards could not be made (%s)", run.run_id or "-",
                           type(exc).__name__)
            cards = ()
        return replace(run, shown=cards)

    def find(self, query: FindQuery) -> tuple[Any, ...]:
        """A find_files step's search: the files (or, for kind "folder", the folders) its words find, checked."""
        backend = self.backend()
        if backend is None:
            return ()
        scope = self.scope()
        if query.kind == "folder":
            places = pick_places(scope, backend, self.tree(), words=query.words, cap=MAX_PLACES)
            return tuple(place for place in places
                         if place.depth and name_score(place.parts[-1] if place.parts else "", query.words))
        search = LocalSearch(scope, backend, clock=self._clock, now=self._now)
        return search.search(query.words, folders=query.folders, kind=query.kind,
                             within_days=query.within_days).files
