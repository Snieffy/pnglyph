"""
Auto-detection of the shared metrics grid (cap height, x-height, baseline,
descender depth, advance width) from a set of source PNGs, plus the
box-drawing canonical-line detection used to keep separately-traced
box-drawing glyphs aligned with each other.

Falls back gracefully when the "ideal" reference letters aren't present
in the source set (e.g. a symbol-only or non-Latin set) by widening the
search or returning None for a metric, which the GUI surfaces for manual
entry instead of a silent bad guess.
"""
from __future__ import annotations

import os
from collections import Counter

import numpy as np
from PIL import Image

from . import tracing

# Reference glyphs used when present. Filenames follow the "u<hex>.png"
# convention (u0048.png == 'H'); codepoints chosen because they have
# flat tops/bottoms with no overshoot, so their bounding box cleanly
# marks the metric being measured.
FLAT_CAP_LETTERS = [0x0048, 0x0049, 0x0045, 0x004C, 0x0054]        # H I E L T
FLAT_XHEIGHT_LETTERS = [0x006E, 0x006F, 0x0075, 0x0076, 0x0077, 0x0078]  # n o u v w x
DESCENDER_LETTERS = [0x0067, 0x0070, 0x0071, 0x0079]                # g p q y

BOX_DRAWING_RANGE = range(0x2500, 0x2580)


class CalibrationResult:
    def __init__(self):
        self.cap_top_px: float | None = None
        self.baseline_px: float | None = None
        self.xheight_top_px: float | None = None
        self.descender_bottom_px: float | None = None
        self.advance_px: float | None = None
        self.canvas_size: tuple[int, int] | None = None   # (w, h) mode
        self.box_canonical_y: list[float] = []
        self.box_canonical_x: list[float] = []
        self.warnings: list[str] = []
        self.reference_glyphs_used: dict[str, list[str]] = {}

    @property
    def cap_height_px(self) -> float | None:
        if self.cap_top_px is None or self.baseline_px is None:
            return None
        return self.baseline_px - self.cap_top_px

    def as_dict(self) -> dict:
        return {k: v for k, v in vars(self).items()}


def _codepoint_for_file(fname: str) -> int | None:
    if not (fname.startswith("u") and fname.endswith(".png")):
        return None
    try:
        return int(fname[1:-4], 16)
    except ValueError:
        return None


def _mask_bbox(source_dir: str, fname: str) -> tuple[int, int, int, int] | None:
    """Return (xmin, xmax, ymin, ymax) of the ink in a source PNG."""
    mask = tracing.load_glyph_mask(os.path.join(source_dir, fname)).mask
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())


def calibrate(source_dir: str) -> CalibrationResult:
    """Scan a directory of u<hex>.png source files and auto-detect the
    shared metrics grid. Any metric that can't be measured (missing
    reference glyphs) is left as None for the caller to prompt for
    manually rather than guessed."""
    result = CalibrationResult()
    files = {f: _codepoint_for_file(f) for f in sorted(os.listdir(source_dir))}
    files = {f: cp for f, cp in files.items() if cp is not None}
    if not files:
        result.warnings.append("No u<hex>.png files found in source directory.")
        return result

    # --- canvas size (mode of all file dimensions) ---
    sizes = []
    for f in files:
        with Image.open(os.path.join(source_dir, f)) as im:
            sizes.append(im.size)
    size_counts = Counter(sizes)
    result.canvas_size = size_counts.most_common(1)[0][0]
    # Only sizes that differ MEANINGFULLY from the mode are worth a
    # warning -- an earlier version flagged every non-exact match,
    # which meant a harmless 1px export-rounding artifact triggered
    # the exact same warning as a genuinely different file, with
    # nothing to tell the two apart. SIZE_TOLERANCE_PX draws that line:
    # within it, a size is "effectively the mode"; beyond it, it's a
    # real deviation worth a user's attention.
    SIZE_TOLERANCE_PX = 2
    mode_w, mode_h = result.canvas_size
    deviating = {
        (w, h): n for (w, h), n in size_counts.items()
        if abs(w - mode_w) > SIZE_TOLERANCE_PX or abs(h - mode_h) > SIZE_TOLERANCE_PX
    }
    if deviating:
        mode_count = len(files) - sum(deviating.values())
        # One line per deviating group with right-aligned, padded
        # width/height numbers (see the monospace font main_window.py
        # applies to this specific warning dialog, so the padding here
        # actually lines up on screen instead of just being spaces in
        # a proportional font) -- a comma-separated single line was
        # confirmed hard to scan once more than one or two sizes were
        # involved.
        w_width = max(len(str(w)) for w, h in deviating)
        h_width = max(len(str(h)) for w, h in deviating)
        lines = [
            f"  {w:>{w_width}}x{h:>{h_width}}px  ({n} file{'s' if n != 1 else ''})"
            for (w, h), n in sorted(deviating.items(), key=lambda kv: -kv[1])
        ]
        result.warnings.append(
            f"Source images are not all the same size:\n"
            f"{mode_w}x{mode_h}px = canvas size ({mode_count} out of {len(files)} files).\n"
            f"The rest may not align correctly against it:\n" + "\n".join(lines) + "\n"
        )

    # --- cap height / baseline, from flat-top capital letters ---
    cap_tops, baselines, used = [], [], []
    for cp in FLAT_CAP_LETTERS:
        fname = f"u{cp:04x}.png"
        if fname in files:
            bbox = _mask_bbox(source_dir, fname)
            if bbox:
                cap_tops.append(bbox[2])
                baselines.append(bbox[3])
                used.append(fname)
    if cap_tops:
        result.cap_top_px = float(np.mean(cap_tops))
        result.baseline_px = float(np.mean(baselines))
        result.reference_glyphs_used["cap_height"] = used
    else:
        result.warnings.append(
            "No reference capital letters (H/I/E/L/T) found -- cap height "
            "and baseline need to be set manually.")

    # --- x-height, from flat-top lowercase letters ---
    xh_tops, used = [], []
    for cp in FLAT_XHEIGHT_LETTERS:
        fname = f"u{cp:04x}.png"
        if fname in files:
            bbox = _mask_bbox(source_dir, fname)
            if bbox:
                xh_tops.append(bbox[2])
                used.append(fname)
    if xh_tops:
        result.xheight_top_px = float(np.mean(xh_tops))
        result.reference_glyphs_used["x_height"] = used
    else:
        result.warnings.append(
            "No reference lowercase letters (n/o/u/v/w/x) found -- "
            "x-height needs to be set manually.")

    # --- descender depth ---
    desc_bottoms, used = [], []
    for cp in DESCENDER_LETTERS:
        fname = f"u{cp:04x}.png"
        if fname in files:
            bbox = _mask_bbox(source_dir, fname)
            if bbox:
                desc_bottoms.append(bbox[3])
                used.append(fname)
    if desc_bottoms:
        result.descender_bottom_px = float(np.mean(desc_bottoms))
        result.reference_glyphs_used["descender"] = used
    else:
        result.warnings.append(
            "No reference descender letters (g/p/q/y) found -- "
            "descender depth needs to be set manually.")

    # --- advance width: mode of canvas width across the WHOLE set ---
    widths = [s[0] for s in sizes]
    result.advance_px = float(Counter(widths).most_common(1)[0][0])

    # --- box-drawing canonical grid lines (for cross-glyph alignment) ---
    mid_ys, mid_xs = [], []
    for fname, cp in files.items():
        if cp not in BOX_DRAWING_RANGE:
            continue
        paths, _shape = tracing.trace_glyph(os.path.join(source_dir, fname))
        for p in paths:
            for (x1, y1), (x2, y2) in zip(p, p[1:]):
                if abs(y1 - y2) <= 3 and abs(x1 - x2) > 20:
                    mid_ys.extend([y1, y2])
                if abs(x1 - x2) <= 3 and abs(y1 - y2) > 20:
                    mid_xs.extend([x1, x2])
    result.box_canonical_y = _cluster_centers(mid_ys)
    result.box_canonical_x = _cluster_centers(mid_xs)

    # --- resolution and flatness sanity checks across a sample of the set ---
    sample = list(files)[:10]
    stroke_estimates = []
    flatness_flagged = 0
    for fname in sample:
        mask_result = tracing.load_glyph_mask(os.path.join(source_dir, fname))
        q = tracing.estimate_resolution_quality(mask_result.mask)
        if q["stroke_px"] is not None:
            stroke_estimates.append(q["stroke_px"])
        if mask_result.flatness_warning is not None:
            flatness_flagged += 1
    if stroke_estimates and np.median(stroke_estimates) < 1.5:
        result.warnings.append(
            f"Median estimated stroke width across sampled glyphs is "
            f"{np.median(stroke_estimates):.1f}px. Tracing may be "
            "unreliable below ~2px -- consider a higher-resolution source.")
    if flatness_flagged > len(sample) / 2:
        result.warnings.append(
            f"{flatness_flagged} of {len(sample)} sampled source images look "
            "low-contrast rather than flat/two-tone -- likely to trace with "
            "extra noise on otherwise-straight edges.")

    return result


def _cluster_centers(values: list[float], tol: float = 4.0) -> list[float]:
    """Group nearby values and return each cluster's mean, sorted by
    cluster size (largest/most-common first)."""
    if not values:
        return []
    values = sorted(values)
    clusters: list[list[float]] = [[values[0]]]
    for v in values[1:]:
        if v - clusters[-1][-1] <= tol:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    clusters.sort(key=len, reverse=True)
    return [float(np.mean(c)) for c in clusters]
