"""
Glyph-coverage reporting: how much of the Unicode ranges relevant to a
PCB/EDA silkscreen font (the scope this whole project has targeted --
ASCII, Latin extended, Greek, box-drawing, math symbols -- not an
attempt at full Unicode) does the current project's glyph set actually
cover.
"""
from __future__ import annotations

# (name, start, end) -- inclusive. Deliberately scoped to what a stroke
# font for schematic/PCB silkscreen text would plausibly need, not the
# full Unicode standard.
BLOCKS: list[tuple[str, int, int]] = [
    ("Basic Latin", 0x0020, 0x007E),
    ("Latin-1 Supplement", 0x00A0, 0x00FF),
    ("Latin Extended-A", 0x0100, 0x017F),
    ("Greek and Coptic", 0x0370, 0x03FF),
    ("General Punctuation", 0x2000, 0x206F),
    ("Superscripts and Subscripts", 0x2070, 0x209F),
    ("Number Forms", 0x2150, 0x218F),
    ("Arrows", 0x2190, 0x21FF),
    ("Mathematical Operators", 0x2200, 0x22FF),
    ("Box Drawing", 0x2500, 0x257F),
    ("Block Elements", 0x2580, 0x259F),
    ("Geometric Shapes", 0x25A0, 0x25FF),
    ("Miscellaneous Technical", 0x2300, 0x23FF),
]


def coverage_report(codepoints: set[int]) -> list[dict]:
    """Per-block coverage stats against BLOCKS, plus a final entry for
    any covered codepoints that fell outside all of them."""
    report = []
    accounted_for: set[int] = set()
    for name, start, end in BLOCKS:
        block_range = range(start, end + 1)
        total = len(block_range)
        have = sorted(cp for cp in codepoints if start <= cp <= end)
        accounted_for.update(have)
        missing = sorted(set(block_range) - set(have))
        report.append({
            "name": name, "start": start, "end": end,
            "total": total, "covered": len(have),
            "missing": missing,
        })
    other = sorted(codepoints - accounted_for)
    if other:
        report.append({
            "name": "Other (outside the tracked blocks)", "start": None, "end": None,
            "total": None, "covered": len(other),
            "missing": [],
        })
    return report
