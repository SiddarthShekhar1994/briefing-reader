"""Jarvis on your desktop: the core, without Qt (SPEC "Jarvis on the desktop", sections 2, 5, 13 and 15).

Off unless [desktop] enabled (or JARVIS_DESKTOP_ENABLED) is true. Built so far (Phase 1, part 2):

* ``backend``: ``DesktopBackend``, through which every OS call goes, and ``Win32Backend``, the real one (only its
  known-folder lookup works yet). While the tests run (the test guard) constructing the real backend raises.
* ``paths``: the roots Jarvis may look in, the excluded places and the path check made before a card shows and
  again before each OS call.
* ``guard``: the NEVER list (section 2.4) as constants and checks, and the suspect-name scanner.
* ``refs``: the per-run ref index (f12, d3, a1 ...) the planner sees instead of paths.

Nothing in this package imports Qt, starts a process or a shell (tests/test_desktop_guard.py greps for it), or logs
a path, a name, a title or a typed value.
"""
