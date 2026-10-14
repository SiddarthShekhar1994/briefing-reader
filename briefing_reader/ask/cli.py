"""The Claude Code CLI for Ask Jarvis: where it is, what it supports, how it is signed in, how it runs.

    locate_claude(environ)      ClaudeExe (path, source): JARVIS_CLAUDE_EXE (.env or the
                                environment), then claude.exe on PATH, then the standalone install
                                (%USERPROFILE%\\.local\\bin\\claude.exe), then the newest copy the
                                Claude desktop app bundles (%APPDATA%\\Claude\\claude-code\\<v>\\<id>,
                                or the same folder inside the app's MSIX package data)
    probe(exe, runner, env)     version and the flags --help lists (cached per path, size, time)
    auth_status(exe, runner, env)  `claude auth status --json`: Ask runs only when it says
                                loggedIn, authMethod "claude.ai" and apiProvider "firstParty"
    clean_env(environ)          the child's environment, from an allowlist: the Windows basics
                                (SystemRoot, PATH, TEMP, USERPROFILE, APPDATA...), the proxy and CA
                                variables, CLAUDE_CODE_GIT_BASH_PATH; everything else (API keys,
                                auth tokens, base URLs, provider switches, the parent session's
                                own, NOTION_TOKEN and every other .env value) removed; three
                                switches set
    build_argv(...)             the exact command line: a list, never a shell; the prompt goes to
                                stdin and never into argv
    build_research_argv(...)    web research's: the same, but its only tools are WebSearch and
                                WebFetch (--tools and --allowedTools), its own prompt, schema and
                                settings (ask.research)
    prepare_work_folder(path)   %LOCALAPPDATA%\\briefing-reader\\ask (web research: ...\\research),
                                created empty; anything in it (a CLAUDE.md, .mcp.json, .claude)
                                refuses the run
    SubprocessRunner            CREATE_NO_WINDOW, a kill-on-close Job Object, stdout lines through
                                a queue (so every wait has a timeout), a 4 KB stderr tail, kill()

Nothing here reads Claude's credential files or prints what `auth status` says beyond the three
fields above. Never ``--bare`` (API keys only), ``--resume`` / ``--continue``, ``--debug`` or a
``--dangerously-*`` flag. Logs name the CLI's source kind and version only, never its path (it
holds the Windows user name).
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

EXE_ENV = "JARVIS_CLAUDE_EXE"
ASSETS = Path(__file__).resolve().parent / "assets"
PROMPT_FILE = ASSETS / "planner_prompt.md"
SETTINGS_FILE = ASSETS / "ask_settings.json"
SCHEMA_FILE = ASSETS / "schema.json"
WORK_FOLDER = "ask"
# Web research (ask.research): its own system prompt, answer schema, settings and empty folder.
RESEARCH_PROMPT_FILE = ASSETS / "research_prompt.md"
RESEARCH_SETTINGS_FILE = ASSETS / "research_settings.json"
RESEARCH_SCHEMA_FILE = ASSETS / "research_schema.json"
RESEARCH_WORK_FOLDER = "research"
WEB_SEARCH_TOOL, WEB_FETCH_TOOL = "WebSearch", "WebFetch"
# The productivity bundle's planner modes: each its own system prompt and answer schema; every other
# part of the command line (no tools, no MCP, the deny-all settings, the Ask work folder) is Ask's.
PLAN_PROMPT_FILE = ASSETS / "plan_prompt.md"
PLAN_SCHEMA_FILE = ASSETS / "plan_schema.json"
PREP_PROMPT_FILE = ASSETS / "prep_prompt.md"      # meeting prep: a brief before a meeting
PREP_SCHEMA_FILE = ASSETS / "prep_schema.json"
NUDGE_PROMPT_FILE = ASSETS / "nudge_prompt.md"    # follow-ups: a drafted reply or nudge in one thread
NUDGE_SCHEMA_FILE = ASSETS / "nudge_schema.json"
MODE_PROMPTS = {"plan": PLAN_PROMPT_FILE, "prep": PREP_PROMPT_FILE, "nudge": NUDGE_PROMPT_FILE}
MODE_SCHEMAS = {"plan": PLAN_SCHEMA_FILE, "prep": PREP_SCHEMA_FILE, "nudge": NUDGE_SCHEMA_FILE}

SOURCE_ENV = "JARVIS_CLAUDE_EXE"
SOURCE_PATH = "PATH"
SOURCE_STANDALONE = "standalone install"
SOURCE_DESKTOP = "Claude desktop app"

# Flags Ask needs; a Claude Code whose --help lacks one is not supported. --max-turns and
# --system-prompt-file work but --help does not list them, so they are not probed.
REQUIRED_FLAGS = ("--print", "--output-format", "--verbose", "--model", "--json-schema", "--tools",
                  "--strict-mcp-config", "--mcp-config", "--setting-sources", "--settings",
                  "--disallowedTools", "--disable-slash-commands", "--permission-mode",
                  "--permission-prompts", "--no-session-persistence")
HARDENING_FLAGS = ("--safe-mode", "--restricted")
# Flags web research needs besides REQUIRED_FLAGS (probed, not required by Ask): the allow rules
# --permission-mode dontAsk needs for WebSearch / WebFetch (it denies anything not pre-approved).
RESEARCH_FLAGS = ("--allowedTools",)
# Never passed, whatever a later version offers: --bare reads API keys only, the others resume
# saved sessions, write debug files or skip permissions.
FORBIDDEN_FLAGS = ("--bare", "--resume", "--continue", "-c", "-r", "--debug", "--debug-file",
                   "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
                   "--fork-session", "--from-pr", "--teleport", "--remote-control", "--cloud",
                   "--betas", "--max-budget-usd", "--add-dir", "--plugin-dir", "--plugin-url",
                   "--agents", "--chrome", "--ide")
EMPTY_MCP_CONFIG = '{"mcpServers":{}}'

# The child's environment is built from this allowlist (compared upper-case): what Windows, Node
# and Claude Code need to start, find the user's folders and reach the network. Nothing else is
# passed: API keys and auth tokens are "always used when present" in -p mode and bill at API rates;
# base URLs and provider switches change who answers; the parent Claude session's own variables
# would leak into the child; Jarvis's .env values (NOTION_TOKEN...) are none of its business; and
# a switch added in a later Claude Code version is left out without anyone having to name it.
CHILD_ENV_ALLOWED = frozenset({
    "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "PATHEXT", "COMSPEC", "OS", "TEMP", "TMP",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "ALLUSERSPROFILE",
    "PUBLIC", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)",
    "COMMONPROGRAMW6432", "USERNAME", "USERDOMAIN", "COMPUTERNAME", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432", "PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL",
    "PROCESSOR_REVISION", "LANG", "LC_ALL", "TZ", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR",
    # Where Git Bash is (Claude Code on Windows looks for it at start-up): a path, never a credential.
    "CLAUDE_CODE_GIT_BASH_PATH",
})
# The provider / credential variables --ask-check names when it removed them (all are removed
# anyway; these are the ones that would change who pays).
_MONEY_ENV_RE = re.compile(r"^(?:ANTHROPIC_|CLAUDE|AWS_|GOOGLE_|VERTEX|BEDROCK)", re.IGNORECASE)
# ... and these are set.
CHILD_ENV = {"ENABLE_CLAUDEAI_MCP_SERVERS": "false", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
             "DISABLE_AUTOUPDATER": "1"}

QUICK_TIMEOUT_S = 15.0       # --version, --help, auth status
LINE_CAP_BYTES = 4 * 1024 * 1024
STDERR_TAIL_BYTES = 4096
QUICK_OUTPUT_CAP = 256 * 1024
CREATE_NO_WINDOW = 0x08000000
_VERSION_RE = re.compile(r"(?<![\d.])(\d{1,4}\.\d{1,4}\.\d{1,6})(?![\d.])")
_FLAG_RE = re.compile(r"(?<![\w-])(--[A-Za-z][A-Za-z0-9-]*)")
_WORD_RE = re.compile(r"[A-Za-z0-9._-]{1,40}")


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ClaudeExe:
    path: Path
    source: str          # SOURCE_*: logged; the path never is
    version_hint: str = ""   # the desktop app's version folder ("" when unknown)


@dataclass(frozen=True)
class Probe:
    ok: bool
    version: str = ""                         # "2.1.293"
    missing: tuple[str, ...] = ()             # required flags --help does not list
    hardening: bool = False                   # --safe-mode and --restricted are both there
    message: str = ""                         # why not ok (safe to show and log)
    research_missing: tuple[str, ...] = ()    # RESEARCH_FLAGS --help does not list (Ask stays ok)


@dataclass(frozen=True)
class AuthStatus:
    ok: bool
    logged_in: bool = False
    method: str = ""           # authMethod as a plain word ("claude.ai"; "other" when odd)
    provider: str = ""         # apiProvider as a plain word ("firstParty")
    problem: str = ""          # "" | "not_signed_in" | "not_subscription" | "error"
    message: str = ""          # safe to show and log (no address, no path)


@dataclass(frozen=True)
class Completed:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


# --------------------------------------------------------------------------
# Finding claude.exe
# --------------------------------------------------------------------------

def _version_key(text: str) -> tuple[int, ...]:
    match = _VERSION_RE.search(text or "")
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def _usable_exe(path: Path) -> bool:
    """An absolute path to an existing .exe (never a .cmd / .bat / .ps1 shim: those run through a
    shell, where the argv quoting rules differ)."""
    try:
        return path.is_absolute() and path.suffix.casefold() == ".exe" and path.is_file()
    except OSError:
        return False


def desktop_copies(environ: Mapping[str, str]) -> list[Path]:
    """Every claude.exe the Claude desktop app bundles, newest version first. The app is an MSIX
    package, so outside it %APPDATA%\\Claude is found under
    %LOCALAPPDATA%\\Packages\\Claude_<id>\\LocalCache\\Roaming\\Claude."""
    roots: list[Path] = []
    appdata = (environ.get("APPDATA") or "").strip()
    if appdata:
        roots.append(Path(appdata) / "Claude" / "claude-code")
    local = (environ.get("LOCALAPPDATA") or "").strip()
    if local:
        packages = Path(local) / "Packages"
        try:
            found = sorted(packages.glob("Claude_*")) if packages.is_dir() else []
        except OSError:
            found = []
        roots += [package / "LocalCache" / "Roaming" / "Claude" / "claude-code" for package in found]
    copies: list[tuple[tuple[int, ...], str, Path]] = []
    for root in roots:
        try:
            versions = list(root.iterdir()) if root.is_dir() else []
        except OSError:
            continue
        for version in versions:
            key = _version_key(version.name)
            if not key:
                continue
            try:
                builds = list(version.iterdir()) if version.is_dir() else []
            except OSError:
                continue
            for build in builds:
                exe = build / "claude.exe"
                if _usable_exe(exe):
                    copies.append((key, str(exe), exe))
    copies.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in copies]


def on_path(environ: Mapping[str, str]) -> Path | None:
    """The first claude.exe in a PATH folder (absolute folders only; unlike shutil.which on
    Windows, never the current folder, and never a .cmd / .ps1 shim)."""
    for folder in (environ.get("PATH") or "").split(os.pathsep):
        folder = folder.strip().strip('"')
        if not folder or not Path(folder).is_absolute():
            continue
        candidate = Path(folder) / "claude.exe"
        if _usable_exe(candidate):
            return candidate
    return None


def locate_claude(environ: Mapping[str, str] | None = None) -> ClaudeExe | None:
    """The claude.exe Ask runs (see the module docs for the order), or None when there is none."""
    environ = os.environ if environ is None else environ
    configured = (environ.get(EXE_ENV) or "").strip().strip('"').strip()
    if configured:
        path = Path(os.path.expandvars(configured))
        if _usable_exe(path):
            return ClaudeExe(path, SOURCE_ENV)
        logger.warning("Ask: %s is set but is not a claude.exe that exists; looking elsewhere", EXE_ENV)
    found = on_path(environ)
    if found is not None:
        return ClaudeExe(found, SOURCE_PATH)
    profile = (environ.get("USERPROFILE") or "").strip()
    if profile:
        standalone = Path(profile) / ".local" / "bin" / "claude.exe"
        if _usable_exe(standalone):
            return ClaudeExe(standalone, SOURCE_STANDALONE)
    copies = desktop_copies(environ)
    if copies:
        return ClaudeExe(copies[0], SOURCE_DESKTOP, ".".join(map(str, _version_key(copies[0].parent.parent.name))))
    return None


# --------------------------------------------------------------------------
# The child's environment and command line
# --------------------------------------------------------------------------

def clean_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The allowlisted variables of ``environ`` (default os.environ) plus CHILD_ENV. No credential
    variable (Claude's or Jarvis's own) ever reaches the child."""
    environ = os.environ if environ is None else environ
    env = {name: value for name, value in environ.items() if name.upper() in CHILD_ENV_ALLOWED}
    env.update(CHILD_ENV)
    return env


def removed_env(environ: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """(every variable name clean_env leaves out, the provider / credential ones among them)."""
    kept = clean_env(environ)
    removed = sorted(name for name in environ if name not in kept and name.upper() not in CHILD_ENV)
    return removed, [name for name in removed if _MONEY_ENV_RE.match(name)]


def compact_json(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=True, sort_keys=False)


def build_argv(exe: Path | str, *, model: str, max_turns: int, schema: str, hardened: bool,
               prompt_file: Path = PROMPT_FILE, settings_file: Path = SETTINGS_FILE) -> list[str]:
    """The planner's exact command line. No tools (``--tools ""``), no MCP servers (an empty
    ``--mcp-config`` with ``--strict-mcp-config``), no settings files (``--setting-sources ""``;
    ``--settings`` adds only Jarvis's own deny list), no slash commands, nothing saved, a turn cap;
    ``hardened`` adds --safe-mode --restricted. The prompt is never in argv: it goes to stdin."""
    if not model or model.startswith("-") or any(char.isspace() for char in model):
        raise ValueError("not a model name")
    argv = [str(exe), "-p", "--output-format", "stream-json", "--verbose", "--model", model,
            "--system-prompt-file", str(prompt_file), "--json-schema", schema,
            "--tools", "", "--mcp-config", EMPTY_MCP_CONFIG, "--strict-mcp-config",
            "--setting-sources", "", "--settings", str(settings_file),
            "--disallowedTools", "mcp__*", "--disable-slash-commands",
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--max-turns", str(int(max_turns)), "--no-session-persistence"]
    if hardened:
        argv += list(HARDENING_FLAGS)
    if set(argv) & set(FORBIDDEN_FLAGS):   # never, whatever changes above
        raise ValueError("a forbidden flag")
    return argv


def build_research_argv(exe: Path | str, *, model: str, max_turns: int, schema: str, hardened: bool,
                        web_fetch: bool, prompt_file: Path = RESEARCH_PROMPT_FILE,
                        settings_file: Path = RESEARCH_SETTINGS_FILE) -> list[str]:
    """Web research's exact command line: like build_argv, but its only tools are WebSearch and
    (``web_fetch``) WebFetch, allowed by name (``--allowedTools``: what dontAsk needs; anything else
    is denied, never prompted), with research_settings.json's allow / deny rules, its own system
    prompt and answer schema. --restricted keeps WebFetch because --tools names it. The prompt
    (the owner's question) is never in argv: it goes to stdin."""
    if not model or model.startswith("-") or any(char.isspace() for char in model):
        raise ValueError("not a model name")
    tools = ",".join((WEB_SEARCH_TOOL, WEB_FETCH_TOOL) if web_fetch else (WEB_SEARCH_TOOL,))
    argv = [str(exe), "-p", "--output-format", "stream-json", "--verbose", "--model", model,
            "--system-prompt-file", str(prompt_file), "--json-schema", schema,
            "--tools", tools, "--allowedTools", tools,
            "--mcp-config", EMPTY_MCP_CONFIG, "--strict-mcp-config",
            "--setting-sources", "", "--settings", str(settings_file),
            "--disallowedTools", "mcp__*", "--disable-slash-commands",
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--max-turns", str(int(max_turns)), "--no-session-persistence"]
    if hardened:
        argv += list(HARDENING_FLAGS)
    if set(argv) & set(FORBIDDEN_FLAGS):   # never, whatever changes above
        raise ValueError("a forbidden flag")
    return argv


def prepare_work_folder(path: Path, *, what: str = "Ask", folder: str = WORK_FOLDER) -> str:
    """Create ``path`` (the folder the CLI runs in) when missing; "" when it is an empty folder,
    else why ``what`` ("Ask", "Web research") refuses to run there (a CLAUDE.md, .mcp.json or
    .claude would add instructions, servers or settings). The message names no path but
    %LOCALAPPDATA%\\briefing-reader\\<folder>."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        entries = [entry.name for entry in path.iterdir()]
    except OSError as exc:
        return f"{what}'s work folder could not be prepared ({exc.strerror or type(exc).__name__})"
    if entries:
        return (f"{what}'s work folder (%LOCALAPPDATA%\\briefing-reader\\{folder}) must be empty but holds "
                f"{len(entries)} item(s); {what} will not run until you empty it")
    return ""


# --------------------------------------------------------------------------
# Runners
# --------------------------------------------------------------------------

class OutputTooLong(Exception):
    """A stdout line over LINE_CAP_BYTES."""


class Process(Protocol):
    def read_line(self, timeout: float) -> str | None:
        """The next non-empty stdout line, "" when none came within ``timeout`` seconds, None at
        the end of the output; OutputTooLong for a line over the cap."""

    def kill(self) -> None: ...

    def wait(self, timeout: float) -> int | None: ...

    def stderr_tail(self) -> str: ...

    def close(self) -> None: ...


class Runner(Protocol):
    def start(self, argv: Sequence[str], *, stdin_text: str, env: Mapping[str, str], cwd: Path) -> Process: ...

    def run(self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path | None,
            timeout: float) -> Completed: ...


_EOF = object()
_TOO_LONG = object()


class _JobObject:
    """A Windows Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: closing it (or Jarvis ending)
    ends the CLI and anything it started."""

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JobObjectExtendedLimitInformation = 9

    def __init__(self) -> None:
        self.handle: int | None = None
        if sys.platform != "win32":
            return
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount")]

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32 = kernel32
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(handle, self._JobObjectExtendedLimitInformation,
                                                ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(handle)
            return
        self.handle = handle

    def assign(self, process_handle: int) -> bool:
        if self.handle is None:
            return False
        return bool(self._kernel32.AssignProcessToJobObject(self.handle, process_handle))

    def terminate(self) -> None:
        if self.handle is not None:
            self._kernel32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle is not None:
            self._kernel32.CloseHandle(handle)


_job_warning_logged = False


class SubprocessProcess:
    """One running CLI (SubprocessRunner.start). Thread-safe kill(); read_line() on one thread."""

    def __init__(self, argv: Sequence[str], *, stdin_text: str, env: Mapping[str, str], cwd: Path) -> None:
        global _job_warning_logged
        flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, cwd=str(cwd), env=dict(env),
                                      creationflags=flags, close_fds=True)
        self._job = _JobObject()
        handle = getattr(self._proc, "_handle", None)
        if handle is None or not self._job.assign(int(handle)):
            if not _job_warning_logged:
                logger.info("Ask: the CLI could not be put in a Job Object; Cancel ends the process itself")
                _job_warning_logged = True
        self._lines: queue.Queue[Any] = queue.Queue()
        self._tail = bytearray()
        self._tail_lock = threading.Lock()
        self._killed = False
        payload = stdin_text.encode("utf-8")
        threading.Thread(target=self._write, args=(payload,), name="ask-stdin", daemon=True).start()
        threading.Thread(target=self._read_out, name="ask-stdout", daemon=True).start()
        threading.Thread(target=self._read_err, name="ask-stderr", daemon=True).start()

    def _write(self, payload: bytes) -> None:
        stdin = self._proc.stdin
        try:
            if stdin is not None:
                stdin.write(payload)
                stdin.flush()
        except OSError:
            pass   # the process ended early; its exit code and stdout say why
        finally:
            try:
                if stdin is not None:
                    stdin.close()
            except OSError:
                pass

    def _read_out(self) -> None:
        stdout = self._proc.stdout
        try:
            while stdout is not None:
                raw = stdout.readline(LINE_CAP_BYTES + 1)
                if not raw:
                    break
                if len(raw) > LINE_CAP_BYTES and not raw.endswith(b"\n"):
                    self._lines.put(_TOO_LONG)
                    break
                text = raw.decode("utf-8", errors="replace").strip()
                if text:
                    self._lines.put(text)
        except (OSError, ValueError):
            pass
        finally:
            self._lines.put(_EOF)

    def _read_err(self) -> None:
        stderr = self._proc.stderr
        try:
            while stderr is not None:
                chunk = stderr.read1(4096) if hasattr(stderr, "read1") else stderr.read(4096)
                if not chunk:
                    break
                with self._tail_lock:
                    self._tail += chunk
                    del self._tail[:-STDERR_TAIL_BYTES]
        except (OSError, ValueError):
            pass

    def read_line(self, timeout: float) -> str | None:
        try:
            item = self._lines.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return ""
        if item is _EOF:
            self._lines.put(_EOF)   # every later read sees the end too
            return None
        if item is _TOO_LONG:
            raise OutputTooLong()
        return str(item)

    def kill(self) -> None:
        self._killed = True
        try:
            self._job.terminate()
        except OSError:
            pass
        try:
            self._proc.kill()
        except OSError:
            pass

    def wait(self, timeout: float) -> int | None:
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def stderr_tail(self) -> str:
        with self._tail_lock:
            return bytes(self._tail).decode("utf-8", errors="replace")

    def close(self) -> None:
        if self._proc.poll() is None:
            self.kill()
            self.wait(5)
        self._job.close()
        for pipe in (self._proc.stdout, self._proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass


class SubprocessRunner:
    """The real runner (tests use fakes; nothing in the test suite starts claude.exe)."""

    def start(self, argv: Sequence[str], *, stdin_text: str, env: Mapping[str, str], cwd: Path) -> Process:
        return SubprocessProcess(argv, stdin_text=stdin_text, env=env, cwd=cwd)

    def run(self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path | None,
            timeout: float) -> Completed:
        flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            done = subprocess.run(list(argv), stdin=subprocess.DEVNULL, capture_output=True, env=dict(env),
                                  cwd=str(cwd) if cwd is not None else None, timeout=timeout,
                                  creationflags=flags, close_fds=True)
        except subprocess.TimeoutExpired:
            return Completed(None, "", "", timed_out=True)
        return Completed(done.returncode, done.stdout[:QUICK_OUTPUT_CAP].decode("utf-8", errors="replace"),
                         done.stderr[-STDERR_TAIL_BYTES:].decode("utf-8", errors="replace"))


# --------------------------------------------------------------------------
# Probe and sign-in
# --------------------------------------------------------------------------

_probe_cache: dict[tuple[str, int, int], Probe] = {}
_probe_lock = threading.Lock()


def _file_key(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
        return str(path), stat.st_size, stat.st_mtime_ns
    except OSError:
        return str(path), -1, -1


def help_flags(text: str) -> frozenset[str]:
    """Every --flag a --help text names."""
    return frozenset(_FLAG_RE.findall(text or ""))


def check_help(help_text: str, version: str) -> Probe:
    """What ``help_text`` (claude --help) says this version supports."""
    flags = help_flags(help_text)
    missing = tuple(flag for flag in REQUIRED_FLAGS if flag not in flags)
    if not missing and "dontAsk" not in help_text:
        missing = ("--permission-mode dontAsk",)
    hardening = all(flag in flags for flag in HARDENING_FLAGS)
    research_missing = tuple(flag for flag in RESEARCH_FLAGS if flag not in flags)
    if missing:
        return Probe(False, version, missing, hardening,
                     f"Claude Code {version or '(unknown version)'} is not supported by Ask yet (it lacks "
                     f"{', '.join(missing)})", research_missing)
    return Probe(True, version, (), hardening, research_missing=research_missing)


def probe(exe: Path, runner: Runner, env: Mapping[str, str], cwd: Path | None = None) -> Probe:
    """``claude --version`` and ``--help`` (no model request), cached per file path, size and time."""
    key = _file_key(exe)
    with _probe_lock:
        cached = _probe_cache.get(key)
    if cached is not None:
        return cached
    version_run = runner.run([str(exe), "--version"], env=env, cwd=cwd, timeout=QUICK_TIMEOUT_S)
    match = _VERSION_RE.search(version_run.stdout or "")
    version = match.group(1) if match else ""
    help_run = runner.run([str(exe), "--help"], env=env, cwd=cwd, timeout=QUICK_TIMEOUT_S)
    if help_run.timed_out or version_run.timed_out:
        return Probe(False, version, message="Claude Code did not answer --help in time")
    if help_run.returncode not in (0, None) and not help_run.stdout:
        return Probe(False, version, message=f"Claude Code --help failed (exit {help_run.returncode})")
    result = check_help(help_run.stdout, version)
    if result.ok:
        with _probe_lock:
            _probe_cache[key] = result
    return result


def _plain_word(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return value if _WORD_RE.fullmatch(value) else "other"


def parse_auth_status(stdout: str) -> AuthStatus:
    """``claude auth status --json`` output -> AuthStatus. Only loggedIn, authMethod and
    apiProvider are read; everything else in it (an address, an organization, folders) is
    ignored and never kept."""
    try:
        data = json.loads(stdout or "")
    except ValueError:
        return AuthStatus(False, problem="error", message="Claude Code's sign-in status could not be read")
    if not isinstance(data, dict):
        return AuthStatus(False, problem="error", message="Claude Code's sign-in status could not be read")
    logged_in = data.get("loggedIn") is True
    method = _plain_word(data.get("authMethod"))
    provider = _plain_word(data.get("apiProvider"))
    if not logged_in:
        return AuthStatus(False, False, method, provider, "not_signed_in",
                          "Run: claude auth login --claudeai (Claude Code is not signed in)")
    if method != "claude.ai" or provider != "firstParty":
        what = method or "an unknown method"
        via = f" through {provider}" if provider and provider != "firstParty" else ""
        return AuthStatus(False, True, method, provider, "not_subscription",
                          f"Run: claude auth login --claudeai (Claude Code is signed in with {what}{via}, not your "
                          "claude.ai plan; Ask runs only on a claude.ai sign-in)")
    return AuthStatus(True, True, method, provider)


def auth_status(exe: Path, runner: Runner, env: Mapping[str, str], cwd: Path | None = None) -> AuthStatus:
    """``claude auth status --json`` with the cleaned environment (no model request). Its exit
    code is not trusted either way: only what it prints decides."""
    done = runner.run([str(exe), "auth", "status", "--json"], env=env, cwd=cwd, timeout=QUICK_TIMEOUT_S)
    if done.timed_out:
        return AuthStatus(False, problem="error", message="Claude Code did not report its sign-in in time")
    return parse_auth_status(done.stdout)


def forget_probes() -> None:
    """Drop the cached probes (tests; a new install is noticed through its file time anyway)."""
    with _probe_lock:
        _probe_cache.clear()


# Added in code (schema.json stays as it is) to a run that may suggest a note: [productivity]
# memory_suggestions on and no <mail> in its stdin (an Ask's run 1 without mail; never run 2).
REMEMBER_PROPERTY = {"type": "array", "maxItems": 1, "items": {"type": "string", "maxLength": 160},
                     "description": "At most one short fact worth keeping in the owner's memory (see REMEMBER); "
                                    "usually empty."}


def schema_text(*, allow_search: bool, allow_web: bool = False, allow_remember: bool = False,
                path: Path = SCHEMA_FILE) -> str:
    """The --json-schema value: schema.json compact; without ``allow_search`` the gmail_search
    property is removed (the second planner run may not ask to read mail again), without
    ``allow_web`` the web_research property (only the first run may hand a request to web
    research, and only when [research] allows it); ``allow_remember`` adds REMEMBER_PROPERTY as
    "remember" (without it the text is exactly as before)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not allow_search:
        data.get("properties", {}).pop("gmail_search", None)
    if not allow_web:
        data.get("properties", {}).pop("web_research", None)
    if allow_remember:
        data.setdefault("properties", {})["remember"] = dict(REMEMBER_PROPERTY)
    return compact_json(data)


def mode_schema_text(mode: str, *, allow_remember: bool = False) -> str:
    """A planner mode's --json-schema value (MODE_SCHEMAS, compact): never gmail_search or
    web_research (a mode never searches mail or the web, so it has no second run and no hand-off);
    ``allow_remember`` adds REMEMBER_PROPERTY as "remember" (a run whose stdin has no <mail>).
    KeyError for a mode without a schema."""
    data = json.loads(MODE_SCHEMAS[mode].read_text(encoding="utf-8"))
    properties = data.setdefault("properties", {})
    properties.pop("gmail_search", None)
    properties.pop("web_research", None)
    if allow_remember:
        properties["remember"] = dict(REMEMBER_PROPERTY)
    return compact_json(data)


def research_schema_text(path: Path = RESEARCH_SCHEMA_FILE) -> str:
    """Web research's --json-schema value: research_schema.json compact."""
    return compact_json(json.loads(path.read_text(encoding="utf-8")))


def flags_of(argv: Iterable[str]) -> list[str]:
    return [item for item in argv if item.startswith("-")]

