# PNGlyph: PNG glyph images to TrueType fonts
**Version:** 2.0 | **Python:** 3.10+ | **License:** GPLv3 | **GUI:** PySide6 (Qt 6)

A small, standalone tool for turning a set of PNG stroke-skeleton images
(one file per character, named `u<hex>.png`, e.g. `u0041.png` = `A`) into
a real TTF font -- with live visual editing, not a one-shot script. Glyphs
can also be captured straight from the screen, or drawn from scratch.

Everything here came out of a real, iterative font-building project
(reverse-engineering a stroke font from a legacy PCB design program); the
GUI wraps the exact geometry engine validated against that project,
generalized so it works against any similarly-shaped source set.

Sibling project: [KIUT -- Ulticap and Ultiboard designs to KiCad](https://github.com/Snieffy/ulticap-ultiboard-to-kicad-converter).

---

## Contents

### In this README

- [Features](#features)
- [Running it](#running-it)
- [Architecture](#architecture)
- [What's new in 2.0](#whats-new-in-20)
- [License](#license)
- [Contributing](#contributing)

### Using PNGlyph

The same documents open inside the program under **Help**.

| Document | What it covers |
|---|---|
| [Getting started](docs/getting-started.md) | opening a source folder, the glyph list, undo/redo, naming, renaming and saving a project, dots and small marks, your first export |
| [Adding glyphs](docs/adding-glyphs.md) | the Add Glyph dialog and its character table, File vs. Blank, assigning codepoints to one or more files |
| [Capture mode](docs/capture.md) | building glyphs from screen-grabs, the Capture window, the character table, assigning codes from a text file |
| [Selection and editing](docs/selection-and-editing.md) | plain/Shift/Ctrl click, groups vs. polylines, the disambiguation popup, Cut/Copy/Paste, Reassign, Exclude/Include |
| [Drawing](docs/drawing.md) | the pen tool, dots, leaving draw mode, angle-snap while drawing |
| [Snapping](docs/snapping.md) | grid and grid alignment, segments, x-height/baseline, the held-key overrides |
| [Weight and Size](docs/size-and-weight.md) | stroke weight, and the Size panel (Flatten/Stretch, linked X/Y) |
| [Metrics and canvas](docs/metrics-and-canvas.md) | the Canvas panel, vertical metrics, clearance check, line endings |
| [Export](docs/export.md) | producing a `.ttf`, metadata, coverage, multi-weight batch export |
| [Reference](docs/reference.md) | every menu, toolbar command and keyboard shortcut in one place |

---

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
- Screen capture sessions with readable thumbnails, a built-in
  character table (full CP437 set, bundled DejaVu Sans Mono) and
  "Assign from text file" -- codes for a whole session from a text
  file in the source's own codepage (CP437 screen glyphs, CP850, ...)
- Dots: single-point strokes, drawn or traced from small separate marks
- Reassign a glyph to another code (swap or replace), rename a project
  (optionally with its folder)
- Classic text menus (File, Edit, Glyph, View, Help) alongside the icon
  toolbar, which can be hidden
- Grid alignment to any point, for exact grid-step moves
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

[↑ Contents](#contents)

---

## Running it

```
pip install -r requirements.txt
python3 pnglyph.py
```

or, without the requirements file:

```
pip install PySide6 fonttools shapely scikit-image opencv-python-headless \
            scipy networkx numpy Pillow
```

[↑ Contents](#contents)

---

## Architecture

```
core/            Pure Python, no Qt. Fully independent of the GUI.
  tracing.py       PNG -> skeleton -> polylines (incl. dots for small marks)
  geometry.py      thickening (buffering) + edit operations (move,
                   delete, add, flatten/flatten_xy, offset, break_path,
                   join_paths)
  calibration.py   auto-detect the metrics grid from reference glyphs
  project.py       .pnglyph.json save/load, the edit-recipe data model
  coverage.py      glyph-coverage report by Unicode block
  codepages.py     legacy codepage -> Unicode (CP437 screen glyphs, ...)
  font_export.py   Project -> .ttf via fontTools

gui/             PySide6. Only talks to core/ -- no geometry logic here.
  canvas_view.py    QGraphicsView-based live editor (points, guides,
                    ink, selection, snapping, the disambiguation popup)
  main_window.py    glyph browser + canvas + properties panel + toolbar
                    and menus, and the project/glyph dialogs
  codepoint_entry.py  codepoint field + the character table (picker)
  export_dialog.py  Export/Metadata/Coverage tabs, multi-weight batch export
  help_dialog.py    in-app viewer for README.md and docs/
  icons.py          toolbar icon generation
  style.py          shared colors and QSS
  render_utils.py   shared glyph-pixmap rendering (list icons, onion skin)
  fonts/            bundled DejaVu Sans Mono (character table)
  capture/          screen capture: overlay, session store, Capture
                    window, thumbnails, Assign from text file

pnglyph.py       Entry point: `python3 pnglyph.py`
```

Each `core/geometry.py` edit function is a small, pure, independently
testable operation -- the same primitives a GUI drag calls interactively
and a saved recipe step replays non-interactively.

[↑ Contents](#contents)

---

## What's new in 2.0

- **Capture:** a character table beside the capture rows (full CP437 set,
  larger characters, codes already in use shown in amber), readable
  thumbnails, and **Assign from text file** -- codes for a whole session
  from a text file in the source's own codepage.
- **Dots:** single-point strokes can be drawn, and small separate marks
  (the dot of an i, j, !, :) are traced as dots instead of being dropped.
- **Glyphs:** Reassign a glyph to another code (Swap or Replace); the
  character table in the Add Glyph and Reassign dialogs, with a live
  "already in use" check.
- **Projects:** Rename project, optionally renaming its folder too.
- **Menus:** classic text menus alongside the icon toolbar (View -> Icon
  toolbar hides it).
- **Editing:** right-drag pans while a paste is attached; Esc or the
  right-click menu leaves draw mode; the grid can be aligned to any point.

[↑ Contents](#contents)

---

## License

PNGlyph is free software, released under the
[GNU General Public License, version 3](LICENSE) (or, at your option,
any later version) -- the same license as
[KIUT](https://github.com/Snieffy/ulticap-ultiboard-to-kicad-converter).

Copyright (C) 2026 Snieffy

This program is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General
Public License for more details.

The character table uses the bundled **DejaVu Sans Mono** font
(`gui/fonts/DejaVuSansMono.ttf`), distributed under its own free license
-- see `gui/fonts/DejaVu-LICENSE.txt`.

This program was entirely built with Claude AI.

[↑ Contents](#contents)

---

## Contributing

Contributions are welcome! If you find a source set PNGlyph doesn't trace
well, or a font it doesn't export correctly, please open an issue or
submit a pull request.\
As this project is GPLv3, all derivatives must remain open and free.

[↑ Contents](#contents)
