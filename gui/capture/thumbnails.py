"""
Readable thumbnails for captures.

The old thumbnail was the raw capture scaled down with smooth (averaging)
filtering. A capture is mostly background with thin strokes -- with
1px-wide screen-capture lines, shrinking a few-hundred-pixel capture to
64px averages each stroke pixel with dozens of background pixels, leaving
an almost invisible grey smear (and a lone dot disappeared completely).

Instead this works from the INK MASK (tracing.load_glyph_mask -- the
exact pixels tracing will see, with its own frame/tick filtering already
applied) and shrinks it by MAX-pooling: a thumbnail pixel is ink if ANY
source pixel in its block is ink. A stroke of any width therefore stays
at least one thumbnail pixel wide, and a single dot stays visible. Drawn
light-on-dark to match the thumbnail's dark tile.
"""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor

from core import tracing

_INK_RGB = (232, 232, 232)


def readable_thumbnail(path: str, size: int) -> QPixmap:
    """A size x size-bounded pixmap of the capture at `path` (aspect
    kept). Falls back to the old smooth-scaled image if the mask can't
    be built for any reason -- a thumbnail must never block the row."""
    try:
        mask = tracing.load_glyph_mask(path).mask
        return _mask_to_pixmap(np.asarray(mask, dtype=bool), size)
    except Exception:
        pix = QPixmap(path)
        if not pix.isNull():
            pix = pix.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return pix


def readable_thumbnail_tile(path: str, size: int) -> QPixmap:
    """readable_thumbnail centred on a dark rounded tile -- for places
    with a light background (the text-file preview table), where the
    light ink would otherwise be nearly invisible. Same tile colour as
    the Capture window's own thumbnail labels."""
    tile = QPixmap(size, size)
    tile.fill(Qt.transparent)
    inner = readable_thumbnail(path, size - 4)
    p = QPainter(tile)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#1e1e1e"))
    p.drawRoundedRect(QRectF(0, 0, size, size), 4, 4)
    if not inner.isNull():
        p.drawPixmap((size - inner.width()) // 2, (size - inner.height()) // 2, inner)
    p.end()
    return tile


def _mask_to_pixmap(mask: np.ndarray, size: int) -> QPixmap:
    h, w = mask.shape
    if h == 0 or w == 0:
        return QPixmap()
    longest = max(h, w)
    if longest > size:
        # Max-pool by an integer factor: pad up to a multiple of it,
        # then a block is ink if any of its pixels is.
        f = math.ceil(longest / size)
        ph, pw = math.ceil(h / f) * f, math.ceil(w / f) * f
        padded = np.zeros((ph, pw), dtype=bool)
        padded[:h, :w] = mask
        small = padded.reshape(ph // f, f, pw // f, f).any(axis=(1, 3))
        # Then thicken by one pixel (right and down): a 1px line becomes
        # 2px and a lone dot a 2x2 square -- still faithful in shape,
        # but no longer a single easily-missed pixel on a 64px tile.
        grown = small.copy()
        grown[1:, :] |= small[:-1, :]
        grown[:, 1:] |= small[:, :-1]
        grown[1:, 1:] |= small[:-1, :-1]
        small = grown
    else:
        # Smaller than the tile: enlarge by a whole-number factor with
        # no smoothing, so pixels stay crisp squares.
        f = max(1, size // longest)
        small = np.repeat(np.repeat(mask, f, axis=0), f, axis=1)
    sh, sw = small.shape
    rgba = np.zeros((sh, sw, 4), dtype=np.uint8)
    rgba[small] = (*_INK_RGB, 255)
    img = QImage(rgba.data, sw, sh, sw * 4, QImage.Format_RGBA8888).copy()
    return QPixmap.fromImage(img)
