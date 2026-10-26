"""The NEVER list (SPEC section 2.4) as constants and checks, and the suspect-name scanner (no Qt).

Every constant named in ``NEVER_CONSTANTS`` has its own unit test (tests/test_desktop_guard.py checks that each one
does). validate.py makes these checks on the planner's steps and the executor makes them again right before each OS
call (hard rule 6): no card, setting or voice unlocks any of them. Text is compared as words: NFKC-normalised (so a
fullwidth or other compatibility form reads as its plain letter), hidden characters removed, camelCase and
snake_case split, casefolded ("txtCardNumber" reads "txt card number"), so a run-together label is caught as well.
Cyrillic and Greek look-alike letters are read as Latin only by the suspect scanner (below).

Public API
==========
Hidden characters (one rule for the whole feature: Unicode categories Cf, Zl and Zp - bidi and other format
controls, zero-width characters, tag characters, line and paragraph separators): ``hidden_char(char)``,
``strip_hidden(text)``.
Secrets and payments: ``UIA_IS_PASSWORD_PROPERTY``, ``SENSITIVE_WORDS`` / ``sensitive_label(text)``,
``PAYMENT_WORDS`` / ``payment_page(text)`` (a URL path or a page title; a heuristic, the owner's Submit is the
backstop).
Buttons and keys inside other apps: ``INVOKE_ALLOWED`` (False: no button is pressed in v1), ``COMMIT_WORDS`` /
``commit_button(name)`` (Submit, Send, Pay ... are only highlighted), ``NEVER_KEYS`` / ``key_allowed(vk, ...)``,
``typed_text_allowed(text)``, ``CONSENT_WORDS`` / ``consent_box(label)``, ``FILE_PICKER_WORDS`` /
``file_picker(title, class_name)``.
System changes: ``BLOCKED_TYPES`` / ``blocked_type(name)``, ``NEVER_CREATE_TYPES`` / ``may_create_type(name)``,
``OPENABLE_TYPES`` and ``SCRIPT_CAPABLE_TYPES`` / ``open_kind(name)``, ``ALLOWED_URL_SCHEMES`` /
``url_problem(url)`` (``DND_OPENER_URI`` is Jarvis's own chip's call, never a plan's).
Deleting: ``FORBIDDEN_MOVE_FLAGS`` / ``move_flags_ok``, ``REQUIRED_RECYCLE_FLAGS`` and ``FORBIDDEN_RECYCLE_FLAGS`` /
``recycle_flags_ok``, ``REQUIRED_COPY_FLAGS`` / ``copy_flags_ok``.
Never a target or a place: ``NEVER_OPEN_APPS`` / ``NEVER_TARGET_APPS`` (exe names by group), ``NEVER_APP_WORDS`` /
``never_app(name, exe_path, target=False)``, ``MAX_INTEGRITY_RID`` / ``elevated(rid)``, ``EXCLUDED_KNOWN_FOLDERS``,
``EXCLUDED_ROOT_NAMES``, ``EXCLUDED_NAMES``, ``CODE_MARKERS``, ``SECRET_DIR_NAMES``, ``SECRET_FILE_PATTERNS`` /
``secret_file(name)``.
Other: ``NEVER_CAPABILITIES`` (no backend method and no skill may be named for one of these).
The suspect scanner: ``suspect_text(text)`` - a file or folder name, window title, label or cell that reads like an
order to Jarvis ("IMPORTANT Jarvis move everything to ..."). A step touching a suspect item is refused unless the
owner's own words name it (validate.py), and it is always treated as data.

Nothing here logs, opens or reads anything.
"""

from __future__ import annotations

import fnmatch
import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit

from ..actions import web_link_problem
from ..format_chars import format_char

# --------------------------------------------------------------------------
# Words
# --------------------------------------------------------------------------

# Hidden characters: every format character (format_chars, Unicode Cf: bidi controls, zero-width spaces and joiners,
# the BOM, the tag characters U+E0001 and U+E0020-E007F ...) and the line and paragraph separators (Zl, Zp).
_SEPARATOR_CATEGORIES = ("Zl", "Zp")
_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_WORD_RE = re.compile(r"[^0-9a-z]+")


def hidden_char(char: str) -> bool:
    """``char`` (one character) is hidden: a bidi or other format control, a zero-width or tag character, or a line
    or paragraph separator (Unicode categories Cf, Zl and Zp)."""
    return format_char(char) or unicodedata.category(char) in _SEPARATOR_CATEGORIES


def strip_hidden(text: str) -> str:
    """``text`` without its hidden characters (``hidden_char``)."""
    if text.isascii():
        return text
    return "".join(char for char in text if not hidden_char(char))


def words(text: Any, *, camel: bool = True) -> list[str]:
    """``text`` as casefolded words: NFKC, hidden characters removed, snake_case and punctuation split, and with
    ``camel`` camelCase split too ("txtCardNumber" -> txt card number)."""
    if not isinstance(text, str):
        return []
    plain = strip_hidden(unicodedata.normalize("NFKC", text))
    if camel:
        plain = _CAMEL_RE.sub(" ", plain)
    return [word for word in _NON_WORD_RE.split(plain.casefold()) if word]


def _readings(text: Any) -> tuple[list[str], list[str]]:
    """Both ways of reading ``text``: camelCase split ("CardNumber" -> card number) and not ("PowerShell")."""
    return words(text), words(text, camel=False)


def _has_phrase(tokens: list[str], phrases: tuple[tuple[str, ...], ...]) -> bool:
    for phrase in phrases:
        size = len(phrase)
        if any(tuple(tokens[i:i + size]) == phrase for i in range(len(tokens) - size + 1)):
            return True
    return False


def _says(text: Any, phrases: tuple[tuple[str, ...], ...]) -> bool:
    return any(_has_phrase(tokens, phrases) for tokens in _readings(text))


def _phrases(*texts: str) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(text.split()) for text in texts)


def suffix(name: Any) -> str:
    """The type Windows would see: the last extension, casefolded, with the trailing dots and spaces Windows drops
    removed ("Report.PDF.exe. " -> "exe"); "" for none."""
    if not isinstance(name, str):
        return ""
    plain = unicodedata.normalize("NFKC", name).rstrip(". ")
    base = plain.replace("/", "\\").rsplit("\\", 1)[-1]
    if "." not in base.lstrip("."):
        return ""
    return base.rsplit(".", 1)[-1].casefold()


# --------------------------------------------------------------------------
# Secrets and payments
# --------------------------------------------------------------------------

UIA_IS_PASSWORD_PROPERTY = 30019        # UIA_IsPasswordPropertyId: such a field is never read, typed or copied
# Labels, AutomationIds and HelpText of fields never read, typed or copied (the spec's list plus common spellings).
SENSITIVE_WORDS = _phrases(
    "password", "passwd", "pwd", "passcode", "passphrase", "pin", "pincode", "otp", "one time", "onetime", "totp",
    "2fa", "mfa", "verification code", "cvv", "cvv2", "cvc", "cvc2", "security code", "securitycode", "card number",
    "cardnumber", "card no", "expiry", "expiration", "exp date", "iban", "sort code", "sortcode", "account number",
    "accountnumber", "account no", "routing", "ssn", "social security", "national insurance", "passport", "secret",
    "recovery code", "backup code")
# Pages whose URL path or title has a word starting like this are never filled (SPEC 2.4; a heuristic).
PAYMENT_WORDS = ("pay", "checkout", "billing", "basket", "cart", "order", "wallet", "bank", "transfer")


def sensitive_label(text: Any) -> bool:
    """A field label, AutomationId or HelpText that names a secret, a payment card or an identity number."""
    return _says(text, SENSITIVE_WORDS)


def payment_page(text: Any) -> bool:
    """A URL path or page title that reads like payment, a purchase or a transfer ("check out" counts too)."""
    camel, plain = _readings(text)
    return any(token.startswith(PAYMENT_WORDS) for token in (*camel, *plain)) or _has_phrase(camel, (("check", "out"),))


# --------------------------------------------------------------------------
# Buttons and keys inside other apps
# --------------------------------------------------------------------------

INVOKE_ALLOWED = False                  # v1 presses no button in another app (no UIA Invoke on any element)
# The commit buttons Jarvis only highlights ("Submit is yours"); he never presses them.
COMMIT_WORDS = _phrases("submit", "send", "pay", "pay now", "buy", "purchase", "place order", "order", "confirm",
                        "delete", "remove", "transfer", "sign in", "log in", "post", "publish", "checkout")
VK_TAB, VK_RETURN, VK_ESCAPE, VK_DELETE, VK_V = 0x09, 0x0D, 0x1B, 0x2E, 0x56
# Never sent into another app: Enter, Esc, Tab (Tab-then-Enter), Delete; and no Alt or Win shortcut, no Ctrl+V.
NEVER_KEYS = frozenset({VK_TAB, VK_RETURN, VK_ESCAPE, VK_DELETE})
MAX_TYPED_CHARS = 2000
# Consent, terms and authorise boxes are never ticked.
CONSENT_WORDS = _phrases("consent", "terms", "conditions", "agree", "i agree", "accept", "authorise", "authorize",
                         "authorisation", "authorization", "privacy policy")
# File pickers (Open / Save As / Upload / Attach dialogs) are never used.
FILE_PICKER_WORDS = _phrases("open", "open file", "save as", "upload", "file upload", "choose file", "attach",
                             "attach file", "insert file", "browse", "select file")
_DIALOG_CLASS = "#32770"                # the Windows common dialog window class


def commit_button(name: Any) -> bool:
    """A button that commits (Submit, Send, Pay, Delete ...): shown with an amber bracket, never pressed."""
    return _says(name, COMMIT_WORDS)


def key_allowed(vk: int, *, ctrl: bool = False, alt: bool = False, win: bool = False) -> bool:
    """Whether a key may be sent into another app: never Enter, Esc, Tab or Delete, never an Alt or Win shortcut,
    never Ctrl+V."""
    if alt or win or vk in NEVER_KEYS:
        return False
    return not (ctrl and vk == VK_V)


def typed_text_allowed(text: Any) -> bool:
    """Typed values carry no line break, tab, Esc or other control or format character, at most 2,000 characters."""
    if not isinstance(text, str) or len(text) > MAX_TYPED_CHARS:
        return False
    return not any(unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp") for char in text)


def consent_box(label: Any) -> bool:
    return _says(label, CONSENT_WORDS)


def file_picker(title: Any, class_name: Any = "") -> bool:
    """An Open / Save As / Upload / Attach dialog: a common dialog with such a title, or such a title anywhere that
    says file ("Choose File to Upload")."""
    if not _says(title, FILE_PICKER_WORDS):
        return False
    tokens = words(title)
    if class_name == _DIALOG_CLASS:
        return True
    return bool({"file", "upload"} & set(tokens)) or _has_phrase(tokens, (("save", "as"),))


# --------------------------------------------------------------------------
# System changes: types and addresses
# --------------------------------------------------------------------------

# Never opened, zipped or renamed to (the spec's list, then other types Windows runs or installs).
BLOCKED_TYPES = frozenset({
    "exe", "msi", "bat", "cmd", "ps1", "psm1", "vbs", "vbe", "js", "jse", "wsf", "wsh", "hta", "reg", "scr", "com",
    "cpl", "msc", "jar", "py", "pyw", "appx", "msix", "appinstaller", "application", "appref-ms", "library-ms",
    "search-ms", "settingcontent-ms", "url",
    "pif", "scf", "msp", "mst", "msu", "inf", "gadget", "ps1xml", "psc1", "psd1", "ws", "wsc", "chm", "hlp", "xbap",
    "diagcab", "appxbundle", "msixbundle"})
# Never created by Jarvis (a rename or a new file): shortcuts and internet shortcuts.
NEVER_CREATE_TYPES = frozenset({"lnk", "url"})
# What open_file opens at once (SPEC 2.3); anything else not blocked is "show in folder" only.
OPENABLE_TYPES = frozenset({
    "docx", "odt", "rtf", "txt", "md", "csv", "pdf", "xlsx", "ods", "pptx", "odp",
    "jpg", "jpeg", "png", "gif", "bmp", "webp", "heic", "tif", "tiff",
    "mp3", "m4a", "wav", "flac", "ogg",
    "mp4", "mov", "mkv", "avi", "webm"})
# Documents that can run macros or scripts: opened only from a card click ("can run macros / scripts"). The old
# binary Office types (doc, xls, ppt) can carry macros too (owner, 2026-10-21).
SCRIPT_CAPABLE_TYPES = frozenset({"docm", "dotm", "xlsm", "xltm", "xlsb", "xlam", "pptm", "potm", "ppsm", "ppam",
                                  "doc", "xls", "ppt", "html", "htm", "xhtml", "svg", "mht", "mhtml"})
OPEN_DOCUMENT = "document"              # opens at once
OPEN_SCRIPT = "script"                  # opens only from a card click
OPEN_SHORTCUT = "shortcut"              # a .lnk: only when it resolves to an approved app or a document (shown)
OPEN_BLOCKED = "blocked"                # never
OPEN_OTHER = "other"                    # never opened; Show in folder only
ALLOWED_URL_SCHEMES = ("https",)        # file: javascript: data: ms-msdt: search-ms: ms-settings: ... never
# The one fixed Settings page Jarvis's own Do Not Disturb chip opens (Phase 2): never accepted from a plan.
DND_OPENER_URI = "ms-settings:notifications"
URL_SCHEME = "scheme"
URL_ADDRESS = "address"


def blocked_type(name: Any) -> bool:
    return suffix(name) in BLOCKED_TYPES


def may_create_type(name: Any) -> bool:
    """A new name's type may be created: never a blocked type, a shortcut or an internet shortcut."""
    kind = suffix(name)
    return kind not in BLOCKED_TYPES and kind not in NEVER_CREATE_TYPES


def open_kind(name: Any) -> str:
    """How a file may be opened: OPEN_DOCUMENT, OPEN_SCRIPT, OPEN_SHORTCUT, OPEN_BLOCKED or OPEN_OTHER."""
    kind = suffix(name)
    if kind in BLOCKED_TYPES:
        return OPEN_BLOCKED
    if kind in OPENABLE_TYPES:
        return OPEN_DOCUMENT
    if kind in SCRIPT_CAPABLE_TYPES:
        return OPEN_SCRIPT
    if kind == "lnk":
        return OPEN_SHORTCUT
    return OPEN_OTHER


def url_problem(url: Any) -> str:
    """"" for an https address on a public host (actions.web_link_problem: no user name, port 443, no dot
    segments, not this PC or its network); URL_SCHEME for any other scheme, URL_ADDRESS for anything else wrong."""
    if not isinstance(url, str):
        return URL_SCHEME
    try:
        scheme = urlsplit(url.strip()).scheme.casefold()
    except ValueError:
        return URL_ADDRESS
    if scheme not in ALLOWED_URL_SCHEMES:
        return URL_SCHEME
    return URL_ADDRESS if web_link_problem(url) else ""


# --------------------------------------------------------------------------
# Deleting and overwriting
# --------------------------------------------------------------------------

MOVEFILE_REPLACE_EXISTING = 0x1
MOVEFILE_COPY_ALLOWED = 0x2
MOVEFILE_DELAY_UNTIL_REBOOT = 0x4
# A move never overwrites, never crosses a volume (v1) and never waits for a reboot.
FORBIDDEN_MOVE_FLAGS = MOVEFILE_REPLACE_EXISTING | MOVEFILE_COPY_ALLOWED | MOVEFILE_DELAY_UNTIL_REBOOT
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_WANTNUKEWARNING = 0x4000
FOFX_RECYCLEONDELETE = 0x00080000      # defined by value (SPEC 6.2)
# The Recycle Bin only, with Undo, and Windows' own warning when a file would skip the bin.
REQUIRED_RECYCLE_FLAGS = FOFX_RECYCLEONDELETE | FOF_ALLOWUNDO | FOF_WANTNUKEWARNING
FORBIDDEN_RECYCLE_FLAGS = FOF_NOCONFIRMATION
COPY_FILE_FAIL_IF_EXISTS = 0x1
REQUIRED_COPY_FLAGS = COPY_FILE_FAIL_IF_EXISTS


def move_flags_ok(flags: int) -> bool:
    return not int(flags) & FORBIDDEN_MOVE_FLAGS


def recycle_flags_ok(flags: int) -> bool:
    flags = int(flags)
    return flags & REQUIRED_RECYCLE_FLAGS == REQUIRED_RECYCLE_FLAGS and not flags & FORBIDDEN_RECYCLE_FLAGS


def copy_flags_ok(flags: int) -> bool:
    return int(flags) & REQUIRED_COPY_FLAGS == REQUIRED_COPY_FLAGS


# --------------------------------------------------------------------------
# Never a target or a place
# --------------------------------------------------------------------------

# Never opened and never a target (exe names, casefolded), by group.
NEVER_OPEN_APPS: dict[str, frozenset[str]] = {
    "terminal": frozenset({"cmd.exe", "powershell.exe", "powershell_ise.exe", "pwsh.exe", "windowsterminal.exe",
                           "wt.exe", "openconsole.exe", "conhost.exe", "wsl.exe", "wslhost.exe", "bash.exe",
                           "mintty.exe", "git-bash.exe", "git-cmd.exe", "python.exe", "pythonw.exe", "py.exe",
                           "node.exe"}),
    "script_host": frozenset({"wscript.exe", "cscript.exe", "mshta.exe", "rundll32.exe", "regsvr32.exe",
                              "msbuild.exe", "installutil.exe"}),
    "system": frozenset({"regedit.exe", "regedt32.exe", "control.exe", "mmc.exe", "systemsettings.exe",
                         "msconfig.exe", "taskmgr.exe", "sechealthui.exe", "useraccountcontrolsettings.exe",
                         "netsh.exe", "sc.exe", "bcdedit.exe", "diskpart.exe", "manage-bde.exe", "dism.exe",
                         "wusa.exe", "gpedit.exe", "secpol.exe", "compmgmtlauncher.exe", "eventvwr.exe"}),
    "installer": frozenset({"msiexec.exe", "appinstaller.exe", "winget.exe", "setup.exe", "install.exe",
                            "uninstall.exe", "unins000.exe"}),
    "claude": frozenset({"claude.exe"}),
    "password_manager": frozenset({"1password.exe", "bitwarden.exe", "keepass.exe", "keepassxc.exe", "dashlane.exe",
                                   "lastpass.exe", "nordpass.exe", "keeperpasswordmanager.exe", "roboform.exe",
                                   "enpass.exe", "proton pass.exe"}),
    "money": frozenset({"exodus.exe", "ledger live.exe", "trezor suite.exe", "tws.exe", "terminal64.exe",
                        "thinkorswim.exe", "webull desktop.exe"}),
    "remote": frozenset({"mstsc.exe", "msrdc.exe", "anydesk.exe", "teamviewer.exe", "rustdesk.exe", "parsecd.exe",
                         "vncviewer.exe", "quickassist.exe", "vmware.exe", "vmplayer.exe", "virtualbox.exe",
                         "virtualboxvm.exe", "vmconnect.exe"}),
}
# Opened when the owner names them, but never an automation target (in-control mode never works in them).
NEVER_TARGET_APPS: dict[str, frozenset[str]] = {
    "ide": frozenset({"code.exe", "cursor.exe", "windsurf.exe", "devenv.exe", "pycharm64.exe", "idea64.exe",
                      "rider64.exe", "webstorm64.exe", "clion64.exe", "studio64.exe", "eclipse.exe",
                      "sublime_text.exe", "notepad++.exe"}),
    "explorer": frozenset({"explorer.exe"}),
}
# Start-menu names (and window classes' apps) of the same groups, as words.
NEVER_APP_WORDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "terminal": _phrases("terminal", "cmd", "command prompt", "powershell", "windows terminal", "wsl", "ubuntu",
                         "debian", "kali linux", "opensuse", "git bash", "python", "node js",
                         "developer command prompt"),
    "system": _phrases("registry editor", "control panel", "services", "task scheduler", "task manager", "settings",
                       "windows security", "group policy", "local security policy", "system configuration",
                       "computer management", "event viewer", "windows update", "bitlocker", "firewall",
                       "device manager", "disk management", "environment variables", "windows tools",
                       "administrative tools"),
    "installer": _phrases("installer", "install", "setup", "uninstall", "app installer", "winget"),
    "claude": _phrases("claude", "claude code"),
    "password_manager": _phrases("1password", "bitwarden", "keepass", "keepassxc", "dashlane", "lastpass",
                                 "nordpass", "keeper", "roboform", "enpass", "proton pass", "password manager"),
    "money": _phrases("bank", "banking", "trading", "trader", "broker", "brokerage", "crypto", "wallet", "coinbase",
                      "binance", "kraken", "metamask", "exodus", "ledger live", "trezor", "robinhood", "etoro",
                      "trading 212", "interactive brokers", "metatrader", "thinkorswim", "webull", "paypal",
                      "revolut", "monzo"),
    "remote": _phrases("remote desktop", "remote desktop connection", "anydesk", "teamviewer", "rustdesk",
                       "parsec", "vnc", "quick assist", "vmware", "virtualbox", "hyper v", "virtual machine"),
    "jarvis": _phrases("jarvis", "jarvis assistant", "briefing reader"),     # Jarvis's own: never opened again
}
NEVER_TARGET_WORDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "ide": _phrases("visual studio", "visual studio code", "vs code", "vscode", "cursor", "windsurf", "pycharm",
                    "intellij", "rider", "webstorm", "clion", "android studio", "eclipse", "sublime text"),
    "explorer": _phrases("file explorer", "explorer"),
}
SECURITY_MANDATORY_MEDIUM_RID = 0x2000
MAX_INTEGRITY_RID = SECURITY_MANDATORY_MEDIUM_RID     # a window above Medium integrity is elevated: never a target
# Known folders (backend names) whose whole subtree is never a place.
EXCLUDED_KNOWN_FOLDERS = ("windows", "program_files", "program_files_x86", "program_data", "local_appdata",
                          "roaming_appdata")
# Never a place when right below a drive (casefolded).
EXCLUDED_ROOT_NAMES = frozenset({
    "windows", "program files", "program files (x86)", "programdata", "$recycle.bin", "system volume information",
    "recovery", "config.msi", "$winreagent", "$windows.~bt", "$windows.~ws", "windows.old", "perflogs", "msocache",
    "$sysreset", "documents and settings", "onedrivetemp"})
# Never a place at any depth (casefolded).
EXCLUDED_NAMES = frozenset({"appdata", "$recycle.bin", "system volume information", "windowsapps"})
# A folder holding (or named) one of these is a code folder: never a place (this covers the Jarvis project).
CODE_MARKERS = (".git", ".hg", ".svn", ".env", "venv", ".venv", "node_modules")
# Folders of keys and credentials: never a place.
SECRET_DIR_NAMES = frozenset({".ssh", ".gnupg", ".aws"})
# Secret-shaped files: never listed and never touched (casefolded fnmatch patterns; Jarvis's own Google sign-in
# files too).
SECRET_FILE_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "*.ppk", "id_rsa*", "id_dsa*",
                        "id_ecdsa*", "id_ed25519*", "*.kdbx", "*.kdb", "credentials*.json", "token*.json",
                        "client_secret*.json", "google_token*.json", "*client_secret*.json", ".git-credentials",
                        ".netrc", "_netrc", ".npmrc", ".pypirc", "wallet.dat")


def _exe_name(exe_path: Any) -> str:
    if not isinstance(exe_path, str):
        return ""
    return exe_path.replace("/", "\\").rsplit("\\", 1)[-1].strip().casefold()


def never_app(name: Any, exe_path: Any = "", *, target: bool = False) -> str:
    """The group an app belongs to when Jarvis may never open it ("terminal", "password_manager", ...; "" when he
    may). ``target=True`` also refuses the apps that may be opened but never worked in (IDEs, Explorer)."""
    exe = _exe_name(exe_path)
    groups = [NEVER_OPEN_APPS, NEVER_TARGET_APPS] if target else [NEVER_OPEN_APPS]
    for table in groups:
        for group, names in table.items():
            if exe and exe in names:
                return group
    for table in ([NEVER_APP_WORDS, NEVER_TARGET_WORDS] if target else [NEVER_APP_WORDS]):
        for group, phrases in table.items():
            if _says(name, phrases):
                return group
    return ""


def elevated(integrity_rid: int | None) -> bool:
    """A window whose process runs above Medium integrity, or whose level could not be read, is elevated."""
    return integrity_rid is None or int(integrity_rid) > MAX_INTEGRITY_RID


def secret_file(name: Any) -> bool:
    """A secret-shaped file name (.env, a key, a password database, credentials or token JSON ...)."""
    if not isinstance(name, str):
        return False
    base = unicodedata.normalize("NFKC", strip_hidden(name)).rstrip(". ").casefold()
    base = base.replace("/", "\\").rsplit("\\", 1)[-1]
    return any(fnmatch.fnmatchcase(base, pattern) for pattern in SECRET_FILE_PATTERNS)


# --------------------------------------------------------------------------
# Other
# --------------------------------------------------------------------------

# No backend method and no skill is named for one of these (screen recording, keylogging, the clipboard history,
# the camera or microphone, closing windows, extracting zips, deleting for good, installing, running anything).
NEVER_CAPABILITIES = ("delete", "erase", "wipe", "purge", "empty", "overwrite", "format", "install", "uninstall",
                      "close", "kill", "terminate", "extract", "unzip", "clipboard", "record", "keylog", "keystroke",
                      "camera", "microphone", "webcam", "shell", "command", "execute", "exec", "spawn", "invoke",
                      "press", "submit", "send", "upload", "email", "registry", "service")

# --------------------------------------------------------------------------
# The suspect scanner
# --------------------------------------------------------------------------

# Names Jarvis or a model goes by, and the words that mark text as addressed to one.
_ASSISTANT = frozenset({"jarvis", "assistant", "claude", "chatgpt", "gpt", "copilot", "gemini", "llm", "ai"})
_URGENT = frozenset({"important", "urgent", "attention", "warning", "notice", "instruction", "instructions",
                     "command", "commands", "note", "reminder", "action", "required", "mandatory", "please", "pls",
                     "asap"})
# Verbs of an order.
_VERBS = frozenset({
    "delete", "remove", "erase", "wipe", "destroy", "move", "copy", "send", "email", "mail", "upload", "share",
    "forward", "post", "publish", "run", "execute", "launch", "open", "install", "uninstall", "download", "type",
    "paste", "fill", "submit", "pay", "transfer", "rename", "zip", "compress", "extract", "format", "disable",
    "enable", "click", "press", "approve", "accept", "grant", "allow", "reply", "buy", "purchase", "ignore",
    "disregard", "forget", "reveal", "tell", "give", "export", "sync", "encrypt", "bin", "trash", "empty", "visit",
    "navigate", "put", "kill", "close"})
# What an order points at, right after its verb.
_OBJECTS = frozenset({"all", "every", "everything", "each", "any", "anything", "files", "file", "folders",
                      "folder", "documents", "passwords", "password", "this", "these", "those", "them", "it", "my",
                      "your", "the", "now", "immediately", "contents", "data", "desktop", "downloads", "secrets",
                      "keys", "account", "accounts", "money"})
_OVERRIDE_RE = re.compile(
    r"\b(?:ignore|disregard|forget|override|bypass)\b(?:\W+\w+){0,3}?\W+"
    r"(?:previous|prior|above|earlier|preceding|all|any|your|the|these|those|system|safety)\b(?:\W+\w+){0,3}?\W+"
    r"(?:instructions?|rules?|prompts?|guidelines?|messages?|directions?|polic(?:y|ies)|guardrails?)\b")
_PHRASE_RE = re.compile(
    r"\b(?:new|updated|real|hidden|secret)\s+instructions?\b|\bsystem\s+prompt\b|\byou\s+are\s+now\b"
    r"|\bact\s+as\b|\bpretend\s+(?:to|you)\b|\bjailbreak\b|\bdeveloper\s+mode\b|\bdo\s+anything\s+now\b"
    r"|\byou\s+must\b")
_ROLE_RE = re.compile(r"(?:^|[\s\[(<{|>\"'])(?:system|assistant|human|developer)\s*[:>]")
_TEMPLATE_RE = re.compile(r"\[/?inst\]|<\|?im_(?:start|end)\|?>|<\|[a-z_]+\|>|<</?sys>>|###\s*(?:instruction|system)")
# Windows' own copy names ("Budget - Copy (2)", "Copy of Budget") are not orders.
_WINDOWS_COPY_RE = re.compile(r"(?:\s-\s*copy(?:\s*\(\d+\))?(?=\.[^.]*$|$))|(?:^copy\s+of\s+)")
_WORD_RE = re.compile(r"\w+")
# Tag characters U+E0020-E007E are invisible but spell ASCII (U+E0041 is "A"): the scanner reads what they spell too.
_TAG_OFFSET = 0xE0000
_TAG_FIRST, _TAG_LAST = 0xE0020, 0xE007E
# Cyrillic and Greek letters that look like Latin ones, read as those Latin letters (the common look-alikes only).
_LOOKALIKES = str.maketrans(
    "\u0430\u0435\u043e\u0440\u0441\u0443\u0445\u0456\u0458\u0455\u0501\u04cf\u043a"
    "\u0410\u0412\u0415\u041a\u041c\u041d\u041e\u0420\u0421\u0422\u0423\u0425\u0406\u0408\u0405"
    "\u03b1\u03b9\u03ba\u03bd\u03bf\u03c1\u03c4\u03c5\u03c7"
    "\u0391\u0392\u0395\u0396\u0397\u0399\u039a\u039c\u039d\u039f\u03a1\u03a4\u03a5\u03a7",
    "aeopcyxijsdlk" "ABEKMHOPCTYXIJS" "aikvoptux" "ABEZHIKMNOPTYX")
_LOOKALIKE_CHARS = frozenset(chr(code) for code in _LOOKALIKES)


def suspect_text(text: Any) -> bool:
    """True when ``text`` (a name, title, label or cell written by someone else) reads like an order to Jarvis or a
    model: an instruction override ("ignore previous instructions"), a role or prompt marker ("system:", "[INST]"),
    a phrase like "you must" or "new instructions", an assistant's name with an order verb, an urgent word with an
    order verb, or an order verb that opens the text and points at something ("Delete all files", "move everything
    to ..."). What invisible tag characters spell is read as well, Cyrillic and Greek look-alike letters are read as
    Latin, and a word mixing Latin letters with such a look-alike ("Jarvis" with a Cyrillic a) is suspect by itself.
    Ordinary names ("Copy of Budget.xlsx", "Move-in checklist.pdf", "Important dates.docx") pass."""
    if not isinstance(text, str) or not text.strip():
        return False
    spelled = _untag(text)
    return _reads_like_an_order(text) or (spelled != text and _reads_like_an_order(spelled))


def _untag(text: str) -> str:
    """``text`` with its tag characters read as the ASCII they spell."""
    if text.isascii():
        return text
    return "".join(chr(ord(char) - _TAG_OFFSET) if _TAG_FIRST <= ord(char) <= _TAG_LAST else char for char in text)


def _reads_like_an_order(text: str) -> bool:
    plain = unicodedata.normalize("NFKC", strip_hidden(text))
    for word in _WORD_RE.findall(plain):
        if any(char in _LOOKALIKE_CHARS for char in word) and any(char.isascii() and char.isalpha() for char in word):
            return True                          # a Latin word with a Cyrillic or Greek look-alike letter in it
    lowered = _CAMEL_RE.sub(" ", plain.translate(_LOOKALIKES)).casefold()
    if _OVERRIDE_RE.search(lowered) or _PHRASE_RE.search(lowered) or _ROLE_RE.search(lowered) \
            or _TEMPLATE_RE.search(lowered):
        return True
    lowered = _WINDOWS_COPY_RE.sub(" ", lowered)
    tokens = [word for word in _NON_WORD_RE.split(lowered) if word]
    if not tokens:
        return False
    verbs = [i for i, token in enumerate(tokens) if token in _VERBS]
    if not verbs:
        return False
    if _ASSISTANT & set(tokens) or _URGENT & set(tokens[:verbs[0]]):
        return True
    # An order verb at the start (after numbering or a date) that points at something.
    start = 0
    while start < len(tokens) and tokens[start].isdigit():
        start += 1
    if start < len(tokens) and tokens[start] in _VERBS:
        rest = tokens[start + 1:start + 3]
        return any(token in _OBJECTS for token in rest)
    return False


# Every NEVER constant; each one has its own unit test (tests/test_desktop_guard.py: test_never_<name>).
NEVER_CONSTANTS = (
    "UIA_IS_PASSWORD_PROPERTY", "SENSITIVE_WORDS", "PAYMENT_WORDS", "INVOKE_ALLOWED", "COMMIT_WORDS", "NEVER_KEYS",
    "CONSENT_WORDS", "FILE_PICKER_WORDS", "BLOCKED_TYPES", "NEVER_CREATE_TYPES", "SCRIPT_CAPABLE_TYPES",
    "ALLOWED_URL_SCHEMES", "DND_OPENER_URI", "FORBIDDEN_MOVE_FLAGS", "REQUIRED_RECYCLE_FLAGS",
    "FORBIDDEN_RECYCLE_FLAGS", "REQUIRED_COPY_FLAGS", "NEVER_OPEN_APPS", "NEVER_TARGET_APPS", "NEVER_APP_WORDS",
    "NEVER_TARGET_WORDS", "MAX_INTEGRITY_RID", "EXCLUDED_KNOWN_FOLDERS", "EXCLUDED_ROOT_NAMES", "EXCLUDED_NAMES",
    "CODE_MARKERS", "SECRET_DIR_NAMES", "SECRET_FILE_PATTERNS", "NEVER_CAPABILITIES")
