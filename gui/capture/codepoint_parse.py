"""
Shared "character or U+XXXX codepoint" text parsing.

Factored out of main_window.AddGlyphDialog (which used to have its own
copy as a static method) so the capture-session assignment window uses
byte-for-byte the same validation rules rather than a second, possibly
drifting implementation. AddGlyphDialog now delegates to this module too.
"""
from __future__ import annotations


def parse_codepoint(text: str) -> tuple[int | None, str | None]:
    """Returns (codepoint, None) on success, (None, error message) on
    failure, or (None, None) for an empty/not-yet-typed field -- that
    last case isn't an error, just "nothing entered yet"."""
    if not text:
        return None, None
    try:
        if text.upper().startswith("U+"):
            return int(text[2:], 16), None
        if len(text) == 1:
            return ord(text), None
        return int(text, 16), None
    except ValueError:
        return None, "Enter a single character or a U+XXXX codepoint."
