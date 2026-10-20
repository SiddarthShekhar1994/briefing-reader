"""The test suite.

Before any test module creates its QApplication, the Qt attributes the page snapshots' web engine
needs are set (snapshot_engine.prepare_application: QtCore only, nothing is started), exactly as
the app does before its own QApplication.

The desktop feature's test guard is set first: while it is on, constructing the real desktop backend
(desktop.backend.Win32Backend) raises, so no test - and no harness that imports this package - can reach
the real desktop.
"""

import os

os.environ["JARVIS_DESKTOP_TEST_GUARD"] = "1"     # desktop.backend.TEST_GUARD_ENV (test_desktop_guard checks it)

try:
    from briefing_reader.snapshot_engine import prepare_application

    prepare_application()
except Exception:  # noqa: BLE001 - a test run without Qt still runs its Qt-free tests
    pass
