"""The "desktop" thread (SPEC 6.4; no Qt): the desktop feature's searches and opens run here, off the GUI thread, one
at a time and in the order they were given - never on the calendar's _ActionWorker or the Ask thread.

``DesktopWorker.submit(fn, *args)`` queues a job and returns a concurrent.futures.Future for its result: the app's
side (desktop_ui) turns a Future's end into a Qt signal for the GUI thread; the Ask thread, which carries a planner's
plan out through the desktop (ask.desktop_mode.DesktopJob.carry_out), waits on its Future. The thread starts with the
first job (never at start-up) and is a daemon, like the "ask" thread. ``stop()`` lets the job running finish and
cancels every job still queued; nothing is accepted after it.

Logs carry exception types only.

Public API
==========
``WORKER_NAME``; ``DesktopWorker(name=)``: ``submit(fn, *args, **kwargs)``, ``stop()``, ``join(timeout)``,
``started()``, ``on_thread()``.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

logger = logging.getLogger(__name__)

WORKER_NAME = "desktop"


class DesktopWorker:
    """See the module docstring."""

    def __init__(self, name: str = WORKER_NAME) -> None:
        self._name = name
        self._jobs: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopped = False

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        """Queue ``fn(*args, **kwargs)`` for the thread; its Future (already cancelled after stop())."""
        future: Future = Future()
        with self._lock:
            if self._stopped:
                future.cancel()
                return future
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self._thread.start()
            self._jobs.put((future, fn, args, kwargs))
        return future

    def stop(self) -> None:
        """No new job; the one running ends as it is, every queued one is cancelled."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            running = self._thread is not None
        if running:
            self._jobs.put(None)

    def join(self, timeout: float) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def started(self) -> bool:
        return self._thread is not None

    def on_thread(self) -> bool:
        """The caller runs on this worker's thread."""
        return self._thread is not None and threading.current_thread() is self._thread

    def _run(self) -> None:
        while True:
            item = self._jobs.get()
            if item is None:
                break
            future, fn, args, kwargs = item
            if self._stopped:
                future.cancel()
                continue
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - the Future carries it; the thread goes on
                logger.debug("Desktop: a job ended with %s", type(exc).__name__)
                future.set_exception(exc)
            else:
                future.set_result(result)
        while True:                                          # stopped: what is left is cancelled
            try:
                item = self._jobs.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                item[0].cancel()
