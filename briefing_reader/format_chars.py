"""Invisible format characters in text from outside Jarvis: one rule for the whole app (no Qt, no I/O, no logs).

A character is a format character when its Unicode category is Cf: bidi controls, zero-width spaces and joiners, the
soft hyphen, the BOM, the tag characters U+E0001 and U+E0020-E007F, U+0600-0605, U+110BD and the rest - text that a
reader does not see but a program does. Memory notes, mail (Ask's searches, headers and bodies), Ask's context blocks
and the desktop guard drop or refuse them with this rule. The line and paragraph separators (Zl, Zp) are not format
characters: each caller keeps its own handling of them (``str.split()`` already reads them as whitespace; the desktop
guard counts them as hidden too).

Public API: ``format_char(char)``, ``has_format(text)``, ``drop_format(text)``.
"""

from __future__ import annotations

import unicodedata


def format_char(char: str) -> bool:
    """``char`` (one character) is an invisible format character (Unicode category Cf)."""
    return unicodedata.category(char) == "Cf"


def has_format(text: str) -> bool:
    """``text`` holds a format character."""
    return not text.isascii() and any(format_char(char) for char in text)


def drop_format(text: str) -> str:
    """``text`` without its format characters."""
    if text.isascii():
        return text
    return "".join(char for char in text if not format_char(char))
