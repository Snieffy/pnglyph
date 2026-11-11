"""
Local, on-demand snap detection -- replaces the earlier whole-image grid-
line pre-detection (dropped: auto-detecting a full grid across arbitrary
desktop content proved too unreliable in practice, and the one confirmed
crash so far came from exactly that whole-image Hough call). This module
only ever looks at a small patch around the current cursor position,
triggered by holding CTRL (see overlay.py), which is both cheaper (a
handful of pixels, not a full screen, on every mouse-move) and far less
prone to false positives from unrelated content elsewhere on screen.

Detection is a stepped fallback, from most to least confident:
  1. Corner/crossing -- a real 2D feature (e.g. two drawn grid lines
     actually crossing). Locks BOTH x and y at once.
  2. Line -- one clean, well-formed straight edge, but no crossing
     nearby. Locks only the ONE axis that edge constrains (a vertical
     line only pins x, a horizontal line only pins y).
  3. Point -- no confident corner or line, just a plain local
     intensity-gradient peak. Weakest signal, so it's treated as
     independent PER AXIS the same way the fixed-size cursor's
     left/right/top/bottom edges were checked in the original design --
     it may snap x, y, both, or neither, but never asserts a joint (x,y)
     target the way a real corner does, since a lone gradient peak on
     one axis says nothing reliable about the other.

Every entry point here has a _safe counterpart used by all GUI call
sites: a detection failure degrades to "no snap", it never raises into
a Qt slot (the same principle already applied elsewhere in this
package).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import cv2


@dataclass
class LocalSnapResult:
    x: float | None = None
    y: float | None = None
    kind: str = "none"   # "corner" | "line" | "point" | "none"

    @property
    def found(self) -> bool:
        return self.kind != "none"


# Tunable constants -- reasonable starting points, expected to need
# adjustment once tried against real reference material.
_CORNER_MAX_CANDIDATES = 5
_CORNER_QUALITY = 0.25
_CORNER_MIN_DISTANCE = 5
_CORNER_CENTER_TOLERANCE_FRAC = 0.5   # corner must be within this fraction of the patch's half-size from center

_LINE_MIN_LENGTH_FRAC = 0.4           # relative to the patch's own size, not a whole screen
_LINE_MAX_GAP = 4
_LINE_ANGLE_TOLERANCE_DEG = 4.0

_POINT_PEAK_MIN_RATIO = 2.0           # a gradient peak must exceed this multiple of the patch's mean to count


def detect_local_snap(patch_gray: np.ndarray) -> LocalSnapResult:
    """patch_gray: a small 2D uint8 grayscale patch, cursor assumed to
    be at its center. Returned x/y are relative to the patch's own
    (0, 0) -- the caller offsets them back into full-image coordinates."""
    h, w = patch_gray.shape[:2]
    if h < 6 or w < 6:
        return LocalSnapResult()
    cx, cy = w / 2, h / 2

    # -- tier 1: corner/crossing -----------------------------------------
    corners = cv2.goodFeaturesToTrack(
        patch_gray, maxCorners=_CORNER_MAX_CANDIDATES,
        qualityLevel=_CORNER_QUALITY, minDistance=_CORNER_MIN_DISTANCE)
    if corners is not None:
        pts = corners.reshape(-1, 2)
        dists = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
        best = int(np.argmin(dists))
        if dists[best] <= min(w, h) / 2 * _CORNER_CENTER_TOLERANCE_FRAC:
            px, py = pts[best]
            return LocalSnapResult(x=float(px), y=float(py), kind="corner")

    # -- tier 2: a single clean line -------------------------------------
    edges = cv2.Canny(patch_gray, 50, 150)
    min_len = int(_LINE_MIN_LENGTH_FRAC * max(w, h))
    lines = cv2.HoughLinesP(edges, rho=1, theta=np.pi / 180, threshold=25,
                             minLineLength=min_len, maxLineGap=_LINE_MAX_GAP)
    if lines is not None:
        segs = lines.reshape(-1, 4)   # robust to the (N,1,4) vs (N,4) build-dependent shape (see the earlier fix)
        best_len = -1.0
        best_seg = None
        for (x1, y1, x2, y2) in segs:
            length = float(np.hypot(x2 - x1, y2 - y1))
            if length > best_len:
                best_len, best_seg = length, (x1, y1, x2, y2)
        if best_seg is not None:
            x1, y1, x2, y2 = best_seg
            angle = abs(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
            if angle <= _LINE_ANGLE_TOLERANCE_DEG or angle >= 180 - _LINE_ANGLE_TOLERANCE_DEG:
                return LocalSnapResult(y=float((y1 + y2) / 2), kind="line")
            if abs(angle - 90) <= _LINE_ANGLE_TOLERANCE_DEG:
                return LocalSnapResult(x=float((x1 + x2) / 2), kind="line")
            # a real but non-axis-aligned edge -- not usable for an
            # axis-aligned rectangle snap, fall through to tier 3

    # -- tier 3: independent per-axis gradient peak ("point") ------------
    # Cheap Sobel-based edge strength, summed per column/row -- looking
    # for a single dominant vertical edge (candidate x) and a single
    # dominant horizontal edge (candidate y), each accepted only if it
    # clearly stands out from the patch's own general noise level.
    sobel_x = cv2.Sobel(patch_gray, cv2.CV_32F, 1, 0, ksize=3)
    sobel_y = cv2.Sobel(patch_gray, cv2.CV_32F, 0, 1, ksize=3)
    col_strength = np.abs(sobel_x).sum(axis=0)   # strong vertical edges show up as column peaks
    row_strength = np.abs(sobel_y).sum(axis=1)   # strong horizontal edges show up as row peaks

    result = LocalSnapResult(kind="none")
    if col_strength.mean() > 0 and col_strength.max() >= _POINT_PEAK_MIN_RATIO * col_strength.mean():
        result.x = float(np.argmax(col_strength))
    if row_strength.mean() > 0 and row_strength.max() >= _POINT_PEAK_MIN_RATIO * row_strength.mean():
        result.y = float(np.argmax(row_strength))
    if result.x is not None or result.y is not None:
        result.kind = "point"
    return result


def detect_local_snap_safe(patch_gray: np.ndarray) -> LocalSnapResult:
    """Never raises -- a detection failure degrades to "no snap" rather
    than interrupting the capture flow (see this module's own
    docstring). Every GUI call site uses this, not detect_local_snap
    directly."""
    try:
        return detect_local_snap(patch_gray)
    except Exception:
        logging.getLogger("pnglyph").exception(
            "Local snap detection failed -- continuing without a snap")
        return LocalSnapResult()


def qimage_to_gray(qimage) -> np.ndarray:
    """Converts a QImage (as grabbed from the desktop) into a 2D uint8
    grayscale numpy array, sliced into small patches on demand by
    overlay.py rather than searched all at once. Kept as a thin,
    isolated Qt<->numpy boundary so detect_local_snap above stays
    Qt-free and independently testable."""
    img = qimage.convertToFormat(qimage.Format.Format_Grayscale8)
    width, height = img.width(), img.height()
    ptr = img.constBits()
    buf = bytes(ptr)[:width * height] if hasattr(ptr, "__len__") else bytes(ptr)
    arr = np.frombuffer(buf, dtype=np.uint8)
    bytes_per_line = img.bytesPerLine()
    if bytes_per_line != width:
        arr = arr[: bytes_per_line * height].reshape(height, bytes_per_line)[:, :width]
    else:
        arr = arr.reshape(height, width)
    return np.ascontiguousarray(arr)
