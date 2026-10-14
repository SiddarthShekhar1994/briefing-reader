"""Command-line entry point: ``py -m briefing_reader``.

Parses the arguments, sets up logging and configuration, makes sure only one
instance runs (a later launch asks the running one to come forward and exits),
then starts the Qt application and the controller from :mod:`briefing_reader.ui`.

A windowless ``--run`` start (how the scheduled tasks launch it, with
``pythonw.exe``) hands the window to a detached copy of itself and exits, so
the scheduled task ends within seconds instead of running for as long as the
window is open.

Two more modes serve the scheduled tasks:

* ``--catch-up`` (at logon and unlock) decides quickly, without importing Qt,
  whether a scheduled briefing passed in the last 3 hours without an answer;
  only then does it continue exactly like ``--run am|pm``.
* ``--hotkey-agent`` (at logon) waits for the global hotkey
  (:mod:`briefing_reader.hotkey`), also without Qt.

The Qt half (activation messages, Qt's log output) lives in
:mod:`briefing_reader.activation` and is imported only when a window or an
activation message is needed. Nothing here assumes a console: under
``pythonw`` there is no stdout/stderr and every problem goes to the log file.
"""

from __future__ import annotations

import argparse
import ctypes
import dataclasses
import getpass
import logging
import platform
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from . import APP_NAME, DISPLAY_NAME, __version__
from .runstate import RUNSTATE_FILE, RunState, format_slots, parse_slots, slot_for

logger = logging.getLogger("briefing_reader.main")

# Startup errors: the window shows the first sentence as its headline, the rest underneath.
TOKEN_MISSING = ("Notion token missing. Add NOTION_TOKEN to the .env file in the project folder "
                 "(see README).")
PAGE_ID_MISSING = ("Notion page ID missing. Add BRIEFING_PAGE_ID to the .env file in the project "
                   "folder (see README).")
TOKEN_AND_PAGE_ID_MISSING = ("Notion token and page ID missing. Add NOTION_TOKEN and BRIEFING_PAGE_ID "
                             "to the .env file in the project folder (see README).")
ERROR_ALREADY_EXISTS = 183
# The hotkey agent's message to a running app: open Jarvis (an older app reads "now": false as a
# plain "come forward").
OPEN_ACTIVATION = {"cmd": "activate", "run": None, "now": False, "open": True}
# Launch kinds (as models.LAUNCH_*; spelled out here so this module never imports more than it must).
LAUNCH_OPEN = "open"
LAUNCH_READ = "read"
LAUNCH_SCHEDULED = "scheduled"
_PAGE_ID_RE = re.compile(r"[0-9a-f]{32}")
_PACKAGE_PARENT = Path(__file__).resolve().parent.parent

# Older names of what moved to briefing_reader.activation; resolved on first use (Qt).
_QT_NAMES = {
    "ActivationServer": "ActivationServer",
    "ACTIVATION_TIMEOUT_S": "ACTIVATION_TIMEOUT_S",
    "_forward_to_running_instance": "forward_to_running_instance",
    "_try_send": "try_send",
    "_parse_activation": "parse_activation",
    "_forward_qt_message": "forward_qt_message",
    "_QT_LEVELS": "QT_LEVELS",
}

# The single-instance mutex handle; referenced for the whole process lifetime.
_mutex_handle: int | None = None


def __getattr__(name: str) -> Any:
    """``ActivationServer`` and the other Qt helpers, imported from :mod:`.activation` when asked for."""
    target = _QT_NAMES.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import activation
    return getattr(activation, target)


# --------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="py -3.13 -m briefing_reader",
        description=(f"{DISPLAY_NAME}: a personal AI desktop assistant for Windows - your scheduled "
                     "briefing, Ask Jarvis, web research and the actions you approve."),
        epilog="Without options Jarvis opens with a greeting; the briefing waits in its BRIEFING tab.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", type=str.lower, choices=("am", "pm"),
                      help="wait for this run's briefing: check Notion every minute for up to "
                           "15 minutes until the page is from today and from this run, then show it "
                           "without taking the focus and announce it (used by the scheduled tasks)")
    mode.add_argument("--catch-up", action="store_true",
                      help="if a scheduled briefing passed in the last 3 hours and was not "
                           "answered, announced or viewed, handle it like --run; otherwise exit at "
                           "once (used by the catch-up task at logon and unlock)")
    mode.add_argument("--hotkey-agent", action="store_true",
                      help="listen for the global hotkey from [hotkey] in config.toml "
                           "(used by the hotkey task at logon)")
    mode.add_argument("--ask", action="store_true",
                      help="open Jarvis with the command bar ready, like --open "
                           "([ask] enabled = true in config.toml); a running app opens it there")
    mode.add_argument("--ask-check", action="store_true",
                      help="Ask Jarvis: check that your own Claude Code is installed and signed in to your "
                           "claude.ai plan, and print what Ask can use (no Claude request)")
    mode.add_argument("--ask-text", metavar="TEXT",
                      help="Ask Jarvis once from the command line and print the proposals (one or two "
                           "requests on your Claude plan); with --ask-dry-run only print what would be sent")
    mode.add_argument("--plan-dry-run", nargs="?", const="", metavar="REQUEST",
                      help="the day plan of the productivity bundle: print what its run would get (your "
                           "calendar read now, titles and notes as sizes) and its command line, and stop; no "
                           "Claude request. REQUEST as typed in the app (default: \"plan my day\")")
    mode.add_argument("--followups-check", action="store_true",
                      help="the follow-ups of the productivity bundle: one real scan of your accounts' mail "
                           "headers (read only, the last 14 days, never the text); prints counts only")
    parser.add_argument("--ask-dry-run", action="store_true",
                        help="with --ask-text: print the exact text Jarvis would send to Claude Code "
                             "(email text shown as counts) and stop; no Claude request")
    parser.add_argument("--slots", metavar="am=HH:MM,pm=HH:MM",
                        help="the times of the scheduled runs (default: [schedule] in config.toml)")
    view = parser.add_mutually_exclusive_group()
    view.add_argument("--open", action="store_true",
                      help="open Jarvis: the assistant screen with a greeting; the briefing waits in its "
                           "BRIEFING tab and nothing is read until you press Play (a running app comes "
                           "forward)")
    view.add_argument("--now", action="store_true",
                      help="older name of --open, kept so existing shortcuts and hotkeys keep working; "
                           "it no longer starts reading (use --read)")
    view.add_argument("--read", action="store_true",
                      help="open Jarvis and read the briefing aloud at once (what --now did before)")
    parser.add_argument("--from-file", metavar="PATH",
                        help="load a saved Notion API fixture instead of calling Notion")
    parser.add_argument("--debug", action="store_true", help="write detailed (debug) logging")
    parser.add_argument("--version", action="version", version=f"{DISPLAY_NAME} {__version__}")
    # Internal: marks the copy a scheduled start hands its window to.
    parser.add_argument("--detached", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.ask_dry_run and args.ask_text is None:
        parser.error("--ask-dry-run needs --ask-text")
    view = _view_flag(args)
    if view:
        for flag, used in (("--catch-up", args.catch_up), ("--hotkey-agent", args.hotkey_agent),
                           ("--ask-check", args.ask_check), ("--ask-text", args.ask_text is not None),
                           ("--plan-dry-run", args.plan_dry_run is not None),
                           ("--followups-check", args.followups_check)):
            if used:
                parser.error(f"{view} cannot be combined with {flag}")
        if args.read and args.ask:
            parser.error("--read cannot be combined with --ask (--ask opens Jarvis without reading)")
    try:
        return _run(args)
    except Exception:  # noqa: BLE001 - logged; no console is required to see it
        logger.exception("briefing-reader stopped because of an unexpected error")
        return 1


def _view_flag(args: argparse.Namespace) -> str:
    """"--open", "--now" or "--read" when one was given, else ""."""
    for flag in ("open", "now", "read"):
        if getattr(args, flag, False):
            return f"--{flag}"
    return ""


def launch_kind(args: argparse.Namespace) -> str:
    """How this launch asks for Jarvis: --read READ; --open, --now or --ask OPEN; --run (or a due
    --catch-up, which sets run) SCHEDULED; nothing OPEN."""
    if getattr(args, "read", False):
        return LAUNCH_READ
    if getattr(args, "open", False) or getattr(args, "now", False) or getattr(args, "ask", False):
        return LAUNCH_OPEN
    if getattr(args, "run", None) or getattr(args, "catch_up", False):
        return LAUNCH_SCHEDULED
    return LAUNCH_OPEN


def _run(args: argparse.Namespace) -> int:
    from .config import load_config, setup_logging

    # The hotkey agent runs all day: it must not hold the log open (rotation).
    log_file = setup_logging(debug=args.debug, keep_open=not args.hotkey_agent)
    logger.info("%s %s starting (Python %s on %s)", APP_NAME, __version__,
                platform.python_version(), platform.platform())
    logger.info("Arguments: run=%s open=%s now=%s read=%s catch_up=%s hotkey_agent=%s ask=%s slots=%s "
                "from_file=%s debug=%s detached=%s launch=%s", args.run or "-", bool(getattr(args, "open", False)),
                args.now, bool(getattr(args, "read", False)), args.catch_up, args.hotkey_agent,
                bool(getattr(args, "ask", False)), args.slots or "-", args.from_file or "-", args.debug,
                args.detached, launch_kind(args))
    logger.info("Log file: %s", log_file)

    config = load_config()
    _log_config(config)
    if (getattr(args, "ask_check", False) or getattr(args, "ask_text", None) is not None
            or getattr(args, "plan_dry_run", None) is not None or getattr(args, "followups_check", False)):
        # Ask Jarvis on the command line: no window, no Qt, no single-instance lock. The request
        # text is never logged.
        from .ask import commands

        logger.info("Ask from the command line: check=%s dry_run=%s plan_dry_run=%s followups_check=%s",
                    bool(args.ask_check), bool(args.ask_dry_run), getattr(args, "plan_dry_run", None) is not None,
                    bool(getattr(args, "followups_check", False)))
        return commands.run(args, config, sys.stdout)
    slots = _resolve_slots(args.slots, config.schedule.slots)
    if args.hotkey_agent:
        return _run_hotkey_agent(config, slots)

    name = _instance_name()
    if args.catch_up:
        run = _catch_up_run(slots, config.data_dir, name)
        if run is None:
            return 0
        args.run = run.lower()   # from here on exactly like --run <run>
    elif not _claim_single_instance(name):
        return _forward_to_running(name, _run_name(args), launch=launch_kind(args),
                                   ask=bool(getattr(args, "ask", False)))
    if _should_detach(args):
        _release_single_instance()   # the detached copy takes the lock
        if _start_detached_copy(args, slots):
            return 0
        if not _claim_single_instance(name):
            return _forward_to_running(name, _run_name(args), launch=launch_kind(args))
    return _run_app(args, config, name, _run_name(args), slots)


def _run_name(args: argparse.Namespace) -> str | None:
    return args.run.upper() if args.run else None


def _log_config(config: Any) -> None:
    # Individual safe fields only: the Config object itself holds the token.
    logger.info("Page %s; voice %s, rate %s; polling every %d s for up to %d min; token present: %s",
                config.page_id or "(not set)", config.voice.voice, config.voice.rate,
                config.polling.interval_seconds, config.polling.timeout_minutes,
                "yes" if config.notion_token else "no")


def _resolve_slots(raw: str | None, fallback: Mapping[str, str]) -> dict[str, str]:
    """The scheduled run times: ``--slots`` over ``[schedule]`` in config.toml.

    A bad ``--slots`` is logged and ignored (there may be no console to show an
    argument error to).
    """
    slots = dict(fallback)
    if raw:
        try:
            slots.update(parse_slots(raw))
        except ValueError as exc:
            logger.warning("--slots %r is not usable (%s); using [schedule] from config.toml",
                           raw[:60], exc)
    return slots


def _local_now() -> datetime:
    return datetime.now().astimezone()


# --------------------------------------------------------------------------
# Catch-up
# --------------------------------------------------------------------------

def _catch_up_run(slots: Mapping[str, str], data_dir: Path, name: str) -> str | None:
    """The run a ``--catch-up`` launch asks about (it then holds the lock), or None to exit.

    One INFO line says why. Qt is not imported here: when nothing is due, the
    launch ends within a fraction of a second, and a running app is left
    alone (no focus stealing at every unlock).
    """
    slot = slot_for(_local_now(), slots)
    if slot is None:
        logger.info("Catch-up: no scheduled briefing in the last 3 hours (%s); nothing to do",
                    _describe_slots(slots))
        return None
    state = RunState(data_dir / RUNSTATE_FILE)
    how = state.handled_how(slot.key)
    if how in ("viewed", "announced"):
        logger.info("Catch-up: the %s briefing was already %s; nothing to do", slot.key, how)
        return None
    if how:
        logger.info("Catch-up: the %s briefing was already answered (%s); nothing to do", slot.key, how)
        return None
    if not _claim_single_instance(name):
        logger.info("Catch-up: the %s briefing is not answered, announced or viewed yet, but the app is "
                    "already running; leaving it alone", slot.key)
        return None
    logger.info("Catch-up: the %s briefing (%s) was not answered, announced or viewed; handling it like "
                "--run %s", slot.key, slot.at.strftime("%H:%M"), slot.run.lower())
    return slot.run


def _describe_slots(slots: Mapping[str, str]) -> str:
    try:
        return format_slots(slots) or "no slots"
    except ValueError:
        return "no usable slots"


# --------------------------------------------------------------------------
# Single instance
# --------------------------------------------------------------------------

def _instance_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name available
        user = ""
    user = re.sub(r"[^A-Za-z0-9_-]", "", user) or "user"
    return f"{APP_NAME}-{user}"


def _claim_single_instance(name: str) -> bool:
    """True when this is the only instance (Windows named mutex; elsewhere a socket probe)."""
    if sys.platform != "win32":
        from .activation import try_send
        return not try_send(name, b"")
    global _mutex_handle
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, f"Local\\{name}")
    error = ctypes.get_last_error()
    if not handle:
        logger.warning("Could not create the single-instance mutex (error %d); continuing", error)
        return True
    _mutex_handle = handle
    return error != ERROR_ALREADY_EXISTS


def _release_single_instance() -> None:
    """Give up the lock once the app is done.

    The process can outlive its window for a few seconds (the speech worker
    finishing a section before its audio folder is deleted); a launch in that
    time must start normally instead of asking this closed instance to come
    forward.
    """
    global _mutex_handle
    if _mutex_handle is None:
        return
    handle, _mutex_handle = _mutex_handle, None
    try:
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(handle)
    except Exception as exc:  # noqa: BLE001 - the OS closes it at exit anyway
        logger.debug("Could not release the single-instance mutex: %s", exc)


def _forward_to_running(name: str, run: str | None, *, launch: str, ask: bool = False) -> int:
    """Ask the running app to come forward with this launch's kind (activation.forward_to_running_instance
    builds the message: "open": true, "read": true, or a run alone for a scheduled launch)."""
    from .activation import forward_to_running_instance
    if ask:
        return forward_to_running_instance(name, run, launch=launch, ask=True)
    return forward_to_running_instance(name, run, launch=launch)


# --------------------------------------------------------------------------
# Leaving the scheduled task
# --------------------------------------------------------------------------

def _should_detach(args: argparse.Namespace) -> bool:
    """True for a windowless ``--run`` start: how the scheduled tasks launch the app (pythonw).

    Task Scheduler treats the task as running for as long as its process
    lives: a task that woke the PC keeps it awake, and the task's limits
    apply to the window. A detached copy is outside the task, which then ends
    within seconds. A start with a console (py, or the tasks installed with
    -Console) is left alone, so its log output stays visible.
    """
    return (sys.platform == "win32" and bool(args.run) and not args.detached
            and sys.stdout is None and sys.stderr is None)


def _detached_arguments(args: argparse.Namespace, slots: Mapping[str, str] | None = None) -> list[str]:
    arguments = ["--run", str(args.run), "--detached"]
    if slots:
        arguments += ["--slots", format_slots(slots)]
    if getattr(args, "read", False):
        arguments.append("--read")
    elif getattr(args, "open", False) or getattr(args, "now", False):
        arguments.append("--open")
    if args.from_file:
        arguments += ["--from-file", str(Path(args.from_file).resolve())]
    if args.debug:
        arguments.append("--debug")
    return arguments


def _start_detached_copy(args: argparse.Namespace, slots: Mapping[str, str] | None = None) -> bool:
    """Start this app again outside the scheduled task's process tree; False if that failed."""
    if _start_detached(_detached_arguments(args, slots)):
        logger.info("Handed the window to the detached process; this launch ends now")
        return True
    logger.warning("Could not start a detached copy; the window stays inside this launch")
    return False


def _start_detached(arguments: Sequence[str], executable: str | None = None) -> bool:
    """Start ``<python> -m briefing_reader <arguments>`` detached from this process; False if that failed.

    The child gets a normal priority class: Task Scheduler starts its tasks
    below normal, which a child would otherwise inherit.
    """
    executable = executable or sys.executable
    if not executable:
        return False
    command = [executable, "-m", "briefing_reader", *arguments]
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
             | subprocess.NORMAL_PRIORITY_CLASS)
    # Leave the launcher's job explicitly when it allows that; otherwise the
    # py launcher's job lets children leave silently anyway.
    for creationflags in (flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, flags):
        try:
            child = subprocess.Popen(command, cwd=str(_PACKAGE_PARENT), creationflags=creationflags,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, close_fds=True)
        except OSError as exc:
            logger.debug("Could not start the detached process (flags %#x): %s", creationflags, exc)
            continue
        logger.info("Started a detached process (pid %d): %s", child.pid, " ".join(arguments))
        return True
    return False


def _windowless_python() -> str:
    """pythonw.exe next to this interpreter when there is one (no console window), else this one."""
    exe = Path(sys.executable or "")
    if exe.name.lower() == "python.exe":
        candidate = exe.with_name("pythonw.exe")
        if candidate.is_file():
            return str(candidate)
    return sys.executable


# --------------------------------------------------------------------------
# Hotkey agent
# --------------------------------------------------------------------------

def _run_hotkey_agent(config: Any, slots: Mapping[str, str]) -> int:
    """``--hotkey-agent``: listen for the hotkey until logoff; never imports Qt."""
    from . import hotkey

    if not config.hotkey.enabled:
        logger.info("Hotkey agent: [hotkey] enabled = false in config.toml; not listening")
        return 0
    name = _instance_name()
    start_arguments = ["--open", "--slots", format_slots(slots)] if slots else ["--open"]
    dispatcher = hotkey.PressDispatcher(
        is_app_running=lambda: hotkey.mutex_exists(f"Local\\{name}"),
        activate=lambda: hotkey.send_activation(name, OPEN_ACTIVATION),
        start_app=lambda: _start_detached(start_arguments, executable=_windowless_python()),
    )
    return hotkey.run_agent(config.hotkey.combo, dispatcher)


# --------------------------------------------------------------------------
# Qt application
# --------------------------------------------------------------------------

def _set_app_user_model_id() -> None:
    """Group the window and tray under our own taskbar identity instead of python.exe."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_NAME)
    except Exception as exc:  # noqa: BLE001 - cosmetic only
        logger.debug("Could not set the AppUserModelID: %s", exc)


def _setup_error(config: Any) -> str | None:
    """The startup error for a missing NOTION_TOKEN and/or BRIEFING_PAGE_ID, or None when both are set."""
    has_token = bool(config.notion_token)
    has_page = bool(_PAGE_ID_RE.fullmatch(config.page_id or ""))
    if has_token and has_page:
        return None
    if has_page:
        return TOKEN_MISSING
    return PAGE_ID_MISSING if has_token else TOKEN_AND_PAGE_ID_MISSING


def _build_client(args: argparse.Namespace, config: Any) -> tuple[Any, Any, str | None]:
    """(client, config, startup_error). ``--from-file`` serves a saved fixture instead of Notion.

    Without the token or the page id nothing is fetched: the window shows the
    startup error (a scheduled or catch-up start too), and the log says why.
    """
    from .notion_client import FixtureSession, NotionClient

    if not args.from_file:
        client = NotionClient(config.notion_token, notion_version=config.notion_version)
        error = _setup_error(config)
        if error:
            logger.error("Not fetching the briefing: %s", error)
        return client, config, error
    session = FixtureSession(Path(args.from_file))
    if _PAGE_ID_RE.fullmatch(session.page_id) and session.page_id != config.page_id:
        config = dataclasses.replace(config, page_id=session.page_id)
    logger.info("Using the saved Notion fixture %s (page %s) instead of calling Notion",
                session.path, config.page_id)
    return NotionClient("", session=session, notion_version=config.notion_version), config, None


def _run_app(args: argparse.Namespace, config: Any, name: str, run: str | None,
             slots: Mapping[str, str] | None = None) -> int:
    from PySide6.QtCore import qInstallMessageHandler
    from PySide6.QtWidgets import QApplication

    from . import hud, ui
    from .activation import forward_qt_message
    from .snapshot_engine import prepare_application

    _set_app_user_model_id()
    # Qt's attributes for the page snapshots' web engine: only before the QApplication exists.
    prepare_application()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)
    # The HUD typefaces (bundled in fonts/) before any window is built.
    hud.load_fonts(Path(config.project_root) / "fonts")
    ui.apply_dark_theme(app)
    app.setWindowIcon(ui.app_icon())
    qInstallMessageHandler(forward_qt_message)
    try:
        return _run_controller(args, config, name, run, app, ui, slots or {})
    finally:
        qInstallMessageHandler(None)


def _run_controller(args: argparse.Namespace, config: Any, name: str, run: str | None,
                    app: Any, ui: Any, slots: Mapping[str, str]) -> int:
    from .activation import ActivationServer

    server = ActivationServer(name)
    server.listen()
    try:
        client, config, startup_error = _build_client(args, config)
    except (OSError, ValueError) as exc:
        logger.error("Could not load the fixture %s: %s", args.from_file, exc)
        server.close()
        return 2
    # A --from-file run is a test: it never records answers in the real run state.
    run_state = None if args.from_file else RunState(config.data_dir / RUNSTATE_FILE)
    launch = launch_kind(args)
    controller = ui.AppController(config, client, expected_run=run, now_mode=launch == LAUNCH_READ,
                                  startup_error=startup_error, slots=slots, run_state=run_state,
                                  on_shutdown=start_exit_watchdog, ask_mode=bool(getattr(args, "ask", False)),
                                  launch=launch)
    server.set_handler(controller.handle_activation)
    controller.start()
    rc = app.exec()
    controller.shutdown()
    server.close()
    _release_single_instance()
    _delete_qt_objects(controller.window, controller, server)
    logger.info("Exiting (code %d)", rc)
    return int(rc)


SHUTDOWN_GRACE_S = 30.0


def start_exit_watchdog(grace_s: float = SHUTDOWN_GRACE_S,
                        exit_func: Callable[[int], Any] = os._exit) -> threading.Timer:
    """End the process if shutting down hangs.

    Qt's media player has (rarely) deadlocked in stop(); a frozen process would
    keep the single-instance lock, so every later scheduled launch would defer
    to it. The grace period outlasts the normal audio-folder cleanup (15 s).
    """
    def fire() -> None:
        logger.warning("Shutdown did not finish within %.0f s; ending the process", grace_s)
        for handler in logging.getLogger().handlers:
            try:
                handler.flush()
            except Exception:  # noqa: BLE001 - best effort before a hard exit
                pass
        exit_func(0)

    timer = threading.Timer(grace_s, fire)
    timer.daemon = True
    timer.name = "exit-watchdog"
    timer.start()
    return timer


def _delete_qt_objects(*objects: Any) -> None:
    """Delete Qt objects now, while QApplication still exists (exit-order safety)."""
    import shiboken6

    for obj in objects:
        if shiboken6.isValid(obj):
            shiboken6.delete(obj)


if __name__ == "__main__":
    sys.exit(main())
