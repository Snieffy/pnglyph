# PNGlyph

A small, standalone tool for turning a set of PNG stroke-skeleton images
(one file per character, named `u<hex>.png`, e.g. `u0041.png` = `A`) into
a real TTF font -- with live visual editing, not a one-shot script. This
program was entirely built with Claude AI.

Everything here came out of a real, iterative font-building project
(reverse-engineering an Ultiboard stroke font); the GUI wraps the exact
geometry engine validated against that project, generalized so it works
against any similarly-shaped source set.

## Documentation

- **[Getting started](docs/getting-started.md)** — opening a source folder, the glyph list, undo/redo, saving/naming a project, your first export
- **[Adding glyphs](docs/adding-glyphs.md)** — the Add Glyph dialog, File vs. Blank, assigning codepoints to one or more files
- **[Capture mode](docs/capture.md)** — building glyphs from on-screen screen-grabs, the Capture window, reviewing and assigning captures
- **[Selection and editing](docs/selection-and-editing.md)** — plain/Shift/Ctrl click, groups vs. polylines, the disambiguation popup, Cut/Copy/Paste, Exclude/Include
- **[Drawing](docs/drawing.md)** — the pen tool, angle-snap while drawing
- **[Snapping](docs/snapping.md)** — grid, segments, x-height/baseline, the held-key overrides
- **[Weight and Size](docs/size-and-weight.md)** — stroke weight, and the Size panel (Flatten/Stretch, linked X/Y)
- **[Metrics and canvas](docs/metrics-and-canvas.md)** — the Canvas panel, vertical metrics, clearance check, line endings
- **[Export](docs/export.md)** — producing a `.ttf`, metadata, coverage, multi-weight batch export
- **[Reference](docs/reference.md)** — every toolbar command and keyboard shortcut in one place

## Features

- Import a folder of `u<hex>.png` files, auto-calibrate cap height /
  x-height / baseline / descender / advance width from reference glyphs
- Glyph browser, searchable, with live-updating icons
- Live canvas: traced skeleton, thickened preview, draggable guide
  lines, draggable points -- edits re-render immediately
- Freehand drawing (pen tool, straight segments only -- no curves or
  arcs, built up from short segments instead) with grid/segment/
  x-height snap and angle-snap, onion-skin reference against a
  neighboring glyph
- Full point/segment/polyline/group selection model, with a
  disambiguation popup for genuinely ambiguous clicks; Cut, Copy, Paste
- Snap to grid, to segments (points and lines, weight-independent),
  and to x-height/baseline, plus G/S/H toggle keys and a held-E
  endpoint-priority override
- Weight (stroke-to-cap-height ratio) and line-ending style (round/flat),
  live, with live-updating list icons and onion skin
- Size panel: independent Flatten/Stretch on X and Y, optionally linked
- Clearance-check triage: flags glyph pairs whose paths may end up
  touching once thickened, filtering out ordinary junctions
  automatically. This is a **filter, not an oracle** -- it still
  surfaces some near-crossing diagonal strokes as false positives.
  Meant to turn "check 185 glyphs by eye" into "check the ~10 it
  flagged."
- Project file format (`.pnglyph.json`): source directory, calibration,
  font settings, and a per-glyph list of *replayable* edit steps (not
  baked-in one-off code) -- re-running the same recipe against updated
  source PNGs reproduces the same fixes, and a diff between two project
  files shows exactly what changed
- TTF export via fontTools, with correct winding, hollow counters,
  monospaced or proportional advance, name-table metadata, a
  glyph-coverage report, and multi-weight batch export

## Architecture

```
core/            Pure Python, no Qt. Fully independent of the GUI.
  tracing.py       PNG -> skeleton -> polylines
  geometry.py      thickening (buffering) + edit operations (move,
                   delete, add, flatten/flatten_xy, offset, break_path,
                   join_paths)
  calibration.py   auto-detect the metrics grid from reference glyphs
  project.py       .pnglyph.json save/load, the edit-recipe data model
  coverage.py      glyph-coverage report by Unicode block
  font_export.py   Project -> .ttf via fontTools

gui/             PySide6. Only talks to core/ -- no geometry logic here.
  canvas_view.py    QGraphicsView-based live editor (points, guides,
                    ink, selection, snapping, the disambiguation popup)
  main_window.py    glyph browser + canvas + properties panel + toolbar
  export_dialog.py  Export/Metadata/Coverage tabs, multi-weight batch export
  icons.py          toolbar icon generation
  style.py          shared colors and QSS
  render_utils.py   shared glyph-pixmap rendering (list icons, onion skin)

pnglyph.py       Entry point: `python3 pnglyph.py`
```

Each `core/geometry.py` edit function is a small, pure, independently
testable operation -- the same primitives a GUI drag calls interactively
and a saved recipe step replays non-interactively.

## Running it

```
pip install PySide6 fonttools shapely scikit-image opencv-python-headless \
            scipy networkx numpy Pillow
python3 pnglyph.py
```

## License

PNGlyph is free software, released under the
[GNU General Public License, version 3](../LICENSE) (or, at your option,
any later version) -- the same license as KIUB and KIUC.

Copyright (C) 2026 Snieffy

This program is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General
Public License for more details.
