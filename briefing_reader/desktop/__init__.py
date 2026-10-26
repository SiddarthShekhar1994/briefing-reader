"""Jarvis on your desktop: the core, without Qt (SPEC "Jarvis on the desktop", sections 2, 4, 5, 6.4, 13 and 15).

Off unless [desktop] enabled (or JARVIS_DESKTOP_ENABLED) is true. Built so far (Phase 1, parts 2, 7 and 8):

* ``backend``: ``DesktopBackend``, through which every OS call goes, and ``Win32Backend``, the real one (the path
  check's facts, the search index, folder listings, the Recent items, the Start menu, and the opens - each guarded
  once more). While the tests run (the test guard) constructing the real backend raises, and so does every one of its
  methods.
* ``paths``: the roots Jarvis may look in, the excluded places and the path check made before a card shows and
  again before each OS call.
* ``guard``: the NEVER list (section 2.4) as constants and checks, and the suspect-name scanner.
* ``refs``: the per-run ref index (f12, d3, a1 ...) the planner sees instead of paths.
* ``context``: what the planner may be told (names only) and ``build_context`` within SPEC 4.1's caps.
* ``routes``: the local routes handled on this PC with no planner run; ``validate``: the planner's plan checked.
* ``index``: Jarvis's own search of this PC (the Windows Search index, else a bounded walk; the Recent items; the
  places), only ever inside the roots; ``apps``: the Start menu's apps minus the never-open ones.
* ``executor``: a request's steps carried out, each target re-checked right before its one OS call; ``service``: the
  desktop thread's work (a request resolved, a plan carried out); ``worker``: the "desktop" thread itself.

Nothing in this package imports Qt, starts a process or a shell (tests/test_desktop_guard.py greps for it), or logs
a path, a name, a title or a typed value.
"""
