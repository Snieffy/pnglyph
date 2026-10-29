"""
Skeleton extraction and path tracing for the PNGlyph pipeline.

Turns a flat, high-contrast two-tone PNG (optionally with a thin red frame
marking the canvas edge) into a list of polylines in source-pixel space:
[[(x, y), (x, y), ...], ...]. Polarity (white-ink-on-dark vs. black-ink-
on-light) is auto-detected per image via two fixed brightness bars, not
an adaptive per-image threshold -- see load_glyph_mask's own docstring
for why an adaptive (Otsu) version of this was tried and reverted. Either
polarity works without configuration, but the source still needs to
actually BE flat and high-contrast; this doesn't attempt to recover a
soft/anti-aliased/gradient source, only detect and warn about one (see
MaskResult.flatness_warning).

This module has no dependency on any particular character set, font
convention, or downstream tool -- it only knows about pixels and paths.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image
from scipy import ndimage
from skimage.morphology import skeletonize
import cv2
import networkx as nx

# --- Tunable constants -------------------------------------------------

MIN_KEEP_COMPONENT = 10   # px; small edge-touching components below this
                          # span are treated as frame calibration ticks,
                          # not real glyph ink.
MIN_PATH_LENGTH = 15.0    # px; traced paths shorter than this after
                          # simplification are graph-topology noise (e.g.
                          # from a messy pixel cluster at a true diagonal
                          # stroke crossing), not real glyph geometry.
DP_EPSILON = 3.0          # px; Douglas-Peucker simplification tolerance.
AXIS_SNAP_TOL = 3.0       # px; near-horizontal/vertical segments within
                          # this y/x deviation get forced level/plumb.

# Deliberately fixed, not adaptive -- see load_glyph_mask's own docstring
# for why an Otsu-based version of this was tried and reverted. A pixel
# clearing neither bar counts toward neither class.
CONFIDENT_LIGHT_THRESHOLD = 200   # per RGB channel
CONFIDENT_DARK_THRESHOLD = 55     # per RGB channel

# A reasonable starting point, expected to need adjustment once tried
# against a wider range of real reference material -- same caveat
# line_detection.py's own tunable constants carry.
FLATNESS_WARNING_FRACTION = 0.01  # fraction of the image that must be
                                   # confidently-neither (see above)
                                   # before a flatness warning is raised


class TraceWarning(Exception):
    """Raised (or collected) when a source image looks too low-resolution
    or otherwise unreliable to trace confidently."""


@dataclass
class MaskResult:
    mask: np.ndarray
    shape: tuple[int, int]
    inverted: bool               # True: this source was black-ink-on-light and got inverted before masking
    flatness_warning: str | None  # None if the source looks cleanly two-tone; else a human-readable warning


def load_glyph_mask(path: str, min_keep_component: float = MIN_KEEP_COMPONENT
                     ) -> MaskResult:
    """Load a source PNG and return a MaskResult.

    Polarity (which of the two dominant tones is ink) is auto-detected
    via two FIXED brightness bars, not one adaptively-computed cut point:
    a pixel is "confidently light" if every RGB channel clears
    CONFIDENT_LIGHT_THRESHOLD, "confidently dark" if every channel is
    under CONFIDENT_DARK_THRESHOLD, and whichever of those two classes
    has FEWER pixels is treated as ink -- background dominates by pixel
    count for any normal thin-stroke glyph, in either polarity. inverted
    is True when that means the dark class won (a black-ink-on-light
    source), so callers that want to tell the user what happened can.

    An adaptive (Otsu) version of this was tried and reverted -- worth
    recording why, since it's not an obvious choice to rule out. Otsu
    computes its cut point from this image's own histogram, which sounds
    strictly better than two fixed bars, and for a genuinely clean,
    two-tone source it usually is. But that same per-image adaptiveness
    is exactly what makes it exposed to something the original fixed
    threshold never was: confirmed directly, with a source combining
    real-world scale imbalance (~2% ink, ~98% background -- an ordinary
    single glyph on a normal canvas) with marginal, overlapping
    brightness ranges between the two classes, Otsu didn't fail
    gracefully -- it picked a badly wrong cut point and produced a mask
    roughly 20x the correct ink area, a confidently-wrong result rather
    than an obviously-broken one. Two fixed bars can't do that: each
    pixel's classification depends only on itself, never on the image's
    overall histogram, so there's no global statistic for a skewed or
    noisy image to mislead. The real cost is that a source whose two
    tones are close to the fixed bars but don't clearly clear them
    traces thin or empty rather than being adaptively rescued -- but
    that failure is safe (visibly wrong, not silently wrong), and it was
    already the honest behavior for a marginal source before any of
    this. This replaced an even earlier version that only ever treated
    light pixels as ink at all -- confirmed as a real, reported gap: a
    black-on-white source used to silently trace as entirely empty, with
    no warning that anything had gone wrong; polarity auto-detection
    fixes that specific problem without reintroducing Otsu's own.

    flatness_warning flags a source that doesn't look like a clean,
    two-tone image to begin with -- a high fraction of its pixels
    clearing NEITHER confident bar. This is a genuinely different
    problem from polarity: it's what a live on-screen capture usually
    looks like even when captured correctly (real font smoothing/anti-
    aliasing, a background that's "basically" one color but not a flat,
    single value) -- confirmed as the actual cause of small deviations
    on otherwise-straight segments traced from screen captures, as
    opposed to a purpose-prepared, genuinely flat PNG. There's no
    algorithmic fix for a source that was never flat to begin with --
    flatness_warning is this module's way of saying so (and pointing at
    fixing the source itself, e.g. in an ordinary image editor, rather
    than silently producing a noisier trace).

    Filters out:
      - small components touching the canvas edge (calibration ticks
        baked into some source sets' frame borders)
      - strict 1px-wide/tall hairlines sitting exactly on the border
        (never legitimate ink; real strokes are always >=2px wide)
    while preserving genuine edge-to-edge ink (e.g. box-drawing
    characters that must reach the cell boundary for seamless tiling).
    """
    im = Image.open(path).convert("RGB")
    arr = np.array(im)
    light = ((arr[:, :, 0] > CONFIDENT_LIGHT_THRESHOLD) &
             (arr[:, :, 1] > CONFIDENT_LIGHT_THRESHOLD) &
             (arr[:, :, 2] > CONFIDENT_LIGHT_THRESHOLD))
    dark = ((arr[:, :, 0] < CONFIDENT_DARK_THRESHOLD) &
            (arr[:, :, 1] < CONFIDENT_DARK_THRESHOLD) &
            (arr[:, :, 2] < CONFIDENT_DARK_THRESHOLD))
    light_count = int(light.sum())
    dark_count = int(dark.sum())
    if light_count >= dark_count:
        white = dark
        inverted = True
    else:
        white = light
        inverted = False

    ambiguous_fraction = 1.0 - (light_count + dark_count) / light.size

    # The outermost 1px ring is excluded from that measurement --
    # confirmed as a real, reported false-positive otherwise: some
    # source sets use a thin, deliberately non-ink-colored frame
    # tracing the full canvas border as a calibration reference (see
    # this module's own header), which the connected-component pass
    # below already excludes from the actual ink mask as a matter of
    # course, but the flatness check ran on the raw pixel classification
    # BEFORE that filtering and had no such exception -- a perfectly
    # clean glyph with an intentional, expected border frame was
    # getting flagged as low-contrast over a feature that was never a
    # contrast problem to begin with.
    border_mask = np.zeros(light.shape, dtype=bool)
    border_mask[0, :] = border_mask[-1, :] = True
    border_mask[:, 0] = border_mask[:, -1] = True
    interior_pixels = int(np.count_nonzero(~border_mask))
    if interior_pixels > 0:
        interior_ambiguous = int(np.count_nonzero((~light & ~dark) & ~border_mask))
        ambiguous_fraction = interior_ambiguous / interior_pixels
    flatness_warning = (
        f"Low contrast: {ambiguous_fraction:.0%} of this image's pixels are "
        f"neither confidently ink nor confidently background -- likely to "
        f"trace with extra noise on otherwise-straight edges, or lose fine "
        f"detail entirely. A flat, sharp-edged source traces most reliably; "
        f"an ordinary image editor (IrfanView and similar free tools all "
        f"work) can increase contrast on a source before importing or "
        f"capturing it."
        if ambiguous_fraction > FLATNESS_WARNING_FRACTION else None
    )

    h, w = white.shape

    lbl, n = ndimage.label(white, structure=np.ones((3, 3)))
    keep = np.zeros_like(white)
    for i in range(1, n + 1):
        ys, xs = np.where(lbl == i)
        touches_edge = (xs.min() == 0 or xs.max() == w - 1 or
                         ys.min() == 0 or ys.max() == h - 1)
        span = max(xs.max() - xs.min(), ys.max() - ys.min())
        if touches_edge and span < min_keep_component:
            continue
        is_hairline_on_edge = (
            (xs.min() == xs.max() and (xs.min() == 0 or xs.max() == w - 1)) or
            (ys.min() == ys.max() and (ys.min() == 0 or ys.max() == h - 1))
        )
        if is_hairline_on_edge:
            continue
        keep[lbl == i] = True
    return MaskResult(mask=keep, shape=(h, w), inverted=inverted, flatness_warning=flatness_warning)


def estimate_resolution_quality(mask: np.ndarray) -> dict:
    """Rough heuristic for whether a source image is high-enough
    resolution to trace reliably. Returns a dict with a 'warning'
    string (or None) and supporting numbers, for the GUI to surface.

    Calibrated against this project's own experience: a ~291x509px
    canvas with ~2px-wide strokes traced cleanly; a low-res font-table
    screenshot with sub-2px strokes did not.
    """
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return {"warning": "No ink detected in this image.", "stroke_px": None}

    # crude stroke-width estimate: for each ink column, count the run
    # length of contiguous ink and take the median of small runs (a
    # reasonable proxy for stroke thickness without fully skeletonizing)
    widths = []
    for col in range(mask.shape[1]):
        colmask = mask[:, col]
        if not colmask.any():
            continue
        runs = np.diff(np.where(np.diff(np.concatenate(([0], colmask.view(np.int8), [0]))))[0])[::2]
        widths.extend(int(r) for r in runs if r < 30)
    stroke_px = float(np.median(widths)) if widths else None

    warning = None
    if stroke_px is not None and stroke_px < 1.5:
        warning = (f"Estimated stroke width is only {stroke_px:.1f}px. "
                   "Skeleton tracing may be unreliable below ~2px -- "
                   "consider a higher-resolution source.")
    return {"warning": warning, "stroke_px": stroke_px}


def _skeleton_to_graph(skel: np.ndarray) -> nx.Graph:
    ys, xs = np.where(skel)
    pts = set(zip(xs.tolist(), ys.tolist()))
    G = nx.Graph()
    G.add_nodes_from(pts)
    for (x, y) in pts:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nb = (x + dx, y + dy)
                if nb in pts:
                    G.add_edge((x, y), nb)
    return G


def _split_into_paths(G: nx.Graph) -> list[list[tuple[int, int]]]:
    """The special (degree != 2, i.e. an open endpoint or branch point)
    vs. no-special (a pure closed loop, every node degree exactly 2)
    decision is made separately for EACH connected component, not once
    for the whole graph. Confirmed as a real, reported bug otherwise:
    an earlier version checked this globally -- if the image had EVEN
    ONE open stroke anywhere (contributing at least one special node
    to the graph as a whole), every OTHER component got walked via the
    special-node strategy too, including ones that are pure closed
    loops with no special nodes of their own at all. A closed loop
    never gets visited by a walk that only ever starts from some
    OTHER, unrelated component's endpoints, so it was silently dropped
    from the traced result entirely -- reproduced directly against a
    real capture (a degree symbol disappeared whenever a triangle was
    also present, traced fine alone) and confirmed to generalize to
    any number of closed loops mixed with any number of open strokes,
    not just the one-of-each case originally reported."""
    paths: list[list[tuple[int, int]]] = []
    visited_edges: set[tuple] = set()

    def edge_key(a, b):
        return (a, b) if a < b else (b, a)

    for comp in nx.connected_components(G):
        sub = G.subgraph(comp)
        special = [n for n in comp if G.degree(n) != 2]
        if special:
            for s in special:
                for nb in sub.neighbors(s):
                    ek = edge_key(s, nb)
                    if ek in visited_edges:
                        continue
                    path = [s, nb]
                    visited_edges.add(ek)
                    prev, cur = s, nb
                    while G.degree(cur) == 2:
                        nbrs = [n for n in sub.neighbors(cur) if n != prev]
                        if not nbrs:
                            break
                        nxt = nbrs[0]
                        ek2 = edge_key(cur, nxt)
                        if ek2 in visited_edges:
                            break
                        visited_edges.add(ek2)
                        path.append(nxt)
                        prev, cur = cur, nxt
                    paths.append(path)
        else:
            start = next(iter(comp))
            path = [start]
            prev, cur = None, start
            while True:
                nbrs = [n for n in sub.neighbors(cur) if n != prev]
                if not nbrs:
                    break
                nxt = nbrs[0]
                if nxt == start:
                    path.append(nxt)
                    break
                path.append(nxt)
                prev, cur = cur, nxt
                if len(path) > len(comp) + 2:
                    break
            paths.append(path)
    return paths


def _simplify_path(path: list[tuple[int, int]], eps: float = DP_EPSILON
                    ) -> list[tuple[float, float]]:
    is_closed = len(path) >= 3 and path[0] == path[-1]
    pts_src = path[:-1] if is_closed else path
    pts = np.array(pts_src, dtype=np.float32).reshape(-1, 1, 2)
    if len(pts) < 3:
        result = [tuple(map(float, p)) for p in pts_src]
    else:
        approx = cv2.approxPolyDP(pts, eps, closed=is_closed)
        result = [tuple(map(float, p[0])) for p in approx]
    if is_closed and result and result[0] != result[-1]:
        result = result + [result[0]]
    return result


def _path_length(path: list[tuple[float, float]]) -> float:
    return sum(((path[i + 1][0] - path[i][0]) ** 2 +
                (path[i + 1][1] - path[i][1]) ** 2) ** 0.5
               for i in range(len(path) - 1))


def _axis_snap(path: list[tuple[float, float]], tol: float = AXIS_SNAP_TOL
               ) -> list[tuple[float, float]]:
    if len(path) < 2:
        return path
    pts = [list(p) for p in path]
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        if abs(y1 - y2) <= tol and abs(x1 - x2) > tol:
            avg_y = (y1 + y2) / 2
            pts[i][1] = avg_y
            pts[i + 1][1] = avg_y
        elif abs(x1 - x2) <= tol and abs(y1 - y2) > tol:
            avg_x = (x1 + x2) / 2
            pts[i][0] = avg_x
            pts[i + 1][0] = avg_x
    return [tuple(p) for p in pts]


def trace_glyph(path: str, min_path_length: float = MIN_PATH_LENGTH,
                axis_snap: bool = True
                ) -> tuple[list[list[tuple[float, float]]], tuple[int, int]]:
    """Full pipeline: PNG path -> (list of polylines in source-px space,
    (height, width)). Each polyline is a list of (x, y) float tuples.
    """
    result = load_glyph_mask(path)
    mask, shape = result.mask, result.shape
    skel = skeletonize(mask)
    G = _skeleton_to_graph(skel)
    raw_paths = _split_into_paths(G)
    simplified = [_simplify_path(p) for p in raw_paths if len(p) >= 2]
    simplified = [p for p in simplified if _path_length(p) >= min_path_length]
    if axis_snap:
        simplified = [_axis_snap(p) for p in simplified]
    return simplified, shape
