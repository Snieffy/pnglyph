"""
Legacy codepage -> Unicode decoding for "Assign from text file…" (capture
window) and the character picker's own CP437 section.

Why this isn't just bytes.decode(codepage): a TrueType font maps glyphs
to UNICODE code points, never to codepage bytes, so the only question a
codepage answers here is "which Unicode character does this byte stand
for?". For most bytes Python's own codecs answer that correctly. The
exception is CP437's 0x01-0x1F and 0x7F, which have two conflicting
readings depending on what the bytes were used for:

  - written to DOS video memory (screen text, what most DOS
    apps displayed): visible glyphs -- 0x01 = ☺ U+263A, 0x0A = ◙ U+25D9,
    0x0D = ♪ U+266A, 0x7F = ⌂ U+2302 ...
  - sent to a printer / treated as text: control codes (LF, CR, ESC ...)
    -- what Python's built-in "cp437" codec returns.

Mapping the DISPLAY reading into a font costs nothing: ◙ lives at
U+25D9, and line feed stays U+000A exactly as in any other font. Putting
glyphs at U+0000-001F instead would be useless -- text renderers treat
those as control codes and never draw them. Hence two CP437 entries:
"DOS screen glyphs" (display table below) and "standard" (Python's codec;
a byte in the control range simply has no glyph and is reported as such).

No Qt here -- pure data and decoding.
"""
from __future__ import annotations

import unicodedata

# CP437's display glyphs for 0x00-0x1F (IBM PC character ROM order).
# 0x00 has no glyph (blank cell on screen) and stays None.
_CP437_LOW_GLYPHS = (
    None, "☺", "☻", "♥", "♦", "♣", "♠", "•",
    "◘", "○", "◙", "♂", "♀", "♪", "♫", "☼",
    "►", "◄", "↕", "‼", "¶", "§", "▬", "↨",
    "↑", "↓", "→", "←", "∟", "↔", "▲", "▼",
)
_CP437_7F_GLYPH = "⌂"   # ⌂ HOUSE


def cp437_display_char(byte: int) -> str | None:
    """The character CP437 byte `byte` shows ON SCREEN (DOS video
    memory) -- None only for 0x00, which has no glyph."""
    if byte < 0x20:
        return _CP437_LOW_GLYPHS[byte]
    if byte == 0x7F:
        return _CP437_7F_GLYPH
    return bytes([byte]).decode("cp437")


def cp437_display_set() -> list[int]:
    """Every codepoint CP437 can display, in byte order (0x01-0xFF) --
    the character picker's "CP437 (DOS)" section."""
    out = []
    for b in range(1, 256):
        ch = cp437_display_char(b)
        if ch is not None and ch != " " and ch != " ":
            out.append(ord(ch))
    return out


# (id, label shown in the dialog). Order = order in the dialog's list.
# "utf-8" decodes the file as UTF-8 text (for files written by a modern
# editor, where characters like ☺ can simply be typed directly).
CODEPAGES: list[tuple[str, str]] = [
    ("cp437-display", "CP437 – DOS screen glyphs (0x01–1F, 0x7F as ☺…▼, ⌂)"),
    ("cp437", "CP437 – standard (0x01–1F, 0x7F are control codes)"),
    ("cp850", "CP850 – DOS Western Europe"),
    ("cp852", "CP852 – DOS Central Europe"),
    ("cp1252", "Windows-1252 – Western"),
    ("latin-1", "ISO 8859-1 – Latin-1"),
    ("utf-8", "UTF-8"),
]

# Separators, never counted as a character: they let the text file be
# laid out freely (one row per screen line, spaces between groups...).
# Matched on the DECODED character, and for raw bytes on 0x09/0x0A/
# 0x0D/0x20 before any glyph table applies -- so a CP437 file's line
# breaks are always line breaks, never ◙ / ♪. The rare capture of one of
# those glyphs is corrected by hand in the preview (or typed as the real
# character in a UTF-8 file).
_SEPARATOR_BYTES = {0x09, 0x0A, 0x0D, 0x20}


class DecodedItem:
    """One counted character from the file, in order."""
    __slots__ = ("codepoint", "source", "problem")

    def __init__(self, codepoint: int | None, source: str, problem: str | None = None):
        self.codepoint = codepoint     # None when this entry has no usable character
        self.source = source           # how it appeared in the file, for the preview ("0x0A", "A")
        self.problem = problem         # human-readable reason when codepoint is None

    def __repr__(self) -> str:   # debugging aid only
        cp = f"U+{self.codepoint:04X}" if self.codepoint is not None else None
        return f"DecodedItem({cp}, {self.source!r}, {self.problem!r})"


def decode_assignment_file(data: bytes, codepage: str) -> list[DecodedItem]:
    """Turns the raw bytes of a user-written text file into an ordered
    list of characters -- the n-th entry is meant for the n-th capture.

    Whitespace (space, tab, CR, LF) separates and is never counted; every
    other byte (single-byte codepages) or character (UTF-8) counts as
    exactly one entry, so the count always lines up with what the user
    typed. Entries that can't become a glyph (a control code under
    "cp437", an undefined byte, an invalid UTF-8 sequence) are kept as
    placeholders WITH a problem message rather than dropped, so one bad
    byte doesn't silently shift every following assignment by one."""
    items: list[DecodedItem] = []
    if codepage == "utf-8":
        text = data.decode("utf-8", errors="replace")
        if text.startswith("﻿"):
            text = text[1:]   # BOM
        for ch in text:
            if ch in " \t\r\n":
                continue
            if ch == "�":
                items.append(DecodedItem(None, "?", "invalid UTF-8 byte sequence"))
            elif unicodedata.category(ch) == "Cc":
                items.append(DecodedItem(None, f"U+{ord(ch):04X}", "control character, no glyph"))
            else:
                items.append(DecodedItem(ord(ch), ch))
        return items

    for b in data:
        if b in _SEPARATOR_BYTES:
            continue
        src = f"0x{b:02X}"
        if codepage == "cp437-display":
            ch = cp437_display_char(b)
            if ch is None:
                items.append(DecodedItem(None, src, "0x00 has no glyph"))
                continue
        else:
            try:
                ch = bytes([b]).decode(codepage)
            except UnicodeDecodeError:
                items.append(DecodedItem(None, src, f"undefined in {codepage.upper()}"))
                continue
        if unicodedata.category(ch) == "Cc":
            items.append(DecodedItem(None, src, "control code, no glyph"))
            continue
        items.append(DecodedItem(ord(ch), ch if ch.isprintable() else src))
    return items


def guess_codepage(data: bytes) -> str:
    """Pre-selection for the codepage dialog only -- the user always
    confirms. UTF-8 when the file starts with a UTF-8 BOM, or is valid
    UTF-8 and actually contains multi-byte characters; otherwise the
    DOS screen-glyph CP437 reading (the DOS-program case)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "cp437-display"
    return "utf-8" if any(ord(c) > 0x7F for c in text) else "cp437-display"
