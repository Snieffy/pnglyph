# Reference

Every toolbar command and keyboard shortcut, in one place, for a quick "what does this do" lookup — see the topic pages for the full explanations behind each one.

## Toolbar

| Icon | Shortcut | Command | Does |
|---|---|---|---|
| ![New](images/icon-new.png) | | **New Project…** | Start a project from scratch, no source PNGs — see [Getting started](getting-started.md). |
| ![Import](images/icon-import.png) | | **Import PNG folder…** | Import a folder of `u<hex>.png` source glyphs — see [Getting started](getting-started.md). |
| ![Save](images/icon-save.png) | | **Save project…** | Save the current project (`.pnglyph.json`). |
| ![Open](images/icon-load.png) | | **Open project…** | Open a previously-saved project. |
| ![Undo](images/icon-undo.png) | Ctrl+Z | **Undo** | Undo the last in-glyph edit — segment-level only, scoped to the current glyph. See [Getting started](getting-started.md#undo-and-redo). |
| ![Redo](images/icon-redo.png) | Ctrl+Y | **Redo** | Redo the last undone edit. |
| ![Add Glyph](images/icon-add-glyph.png) | | **Add Glyph…** | Add a new glyph — blank to draw from scratch, or from one or more source PNGs via a codepoint-assignment review window. See [Adding glyphs](adding-glyphs.md). |
| ![Capture](images/icon-capture.png) | | **Capture Glyphs…** | Build glyphs by screen-grabbing regions of any on-screen reference — see [Capture mode](capture.md). |
| ![Edit Metrics](images/icon-edit-metrics.png) | | **Edit Metrics** | Show the guides without any glyph selected, for adjusting metrics on their own. |
| ![Select all](images/icon-select-all.png) | Ctrl+A | **Select all** | Select every segment in the current glyph. |
| ![Fit to window](images/icon-fit-window.png) | | **Fit to window** | Reset the view to fit the current working area. |
| ![Cut](images/icon-cut.png) | Ctrl+X | **Cut** | Cut the selection — same whole/piece distinction as Delete, see [Selection and editing](selection-and-editing.md). |
| ![Copy](images/icon-copy.png) | Ctrl+C | **Copy** | Copy the selection. |
| ![Paste](images/icon-paste.png) | Ctrl+V | **Paste** | Attaches the clipboard to the cursor; click to place. Replaces any current selection. |
| ![Break](images/icon-break.png) | | **Break** | Break the selected segment(s) off so they can move independently — see [Selection and editing](selection-and-editing.md#join-and-break). |
| ![Join](images/icon-join.png) | | **Join** | Join the selected segments back into one polyline, or into a group if they don't connect end-to-end. |
| ![Angle snap](images/icon-angle-snap.png) | | **Angle snap** | Constrain dragged points and new strokes to 22.5° steps — see [Drawing](drawing.md#angle-snap-while-drawing). |
| ![Draw](images/icon-draw.png) | | **Draw** | Enter/exit the pen tool — see [Drawing](drawing.md). |
| ![Onion Skin](images/icon-onion-skin.png) | | **Onion Skin** | Toggle a faint outline of the nearest already-drawn glyph as a weight/proportion reference. |
| ![Export TTF](images/icon-export.png) | | **Export TTF** | Open the export dialog (spacing, metadata, multi-weight, coverage) — see [Export](export.md). |
| ![Help](images/icon-help.png) | F1 | **Help** | Open this Help viewer. |

## Keyboard-only

Not on the toolbar, but active in specific contexts:

| Key | Where | Does |
|---|---|---|
| **G** | Any drag or draw | Toggles Snap to grid on/off — see [Snapping](snapping.md#held-key-overrides). |
| **S** | Any drag or draw | Toggles Snap to segments on/off. |
| **E** | Any drag or draw | Suspends segment snap's point-priority while it's otherwise active, falling straight through to line-snap — see [Snapping](snapping.md#snap-to-segments). |
| **H** | Any drag or draw | Toggles Snap to x-height/baseline on/off. |
| **Ctrl** | Any drag or draw | Bypasses every snap setting for that one drag or point placement — see [Snapping](snapping.md#bypassing-snap-entirely). |
| **Enter/Return** | Drawing | Finishes the current stroke. |
| **Double-click** | Drawing | Also finishes the current stroke — see [Drawing](drawing.md#the-pen-tool) for a small difference from Enter worth knowing. |
| **Backspace** | Drawing | Removes the last-placed point. |
| **Escape** | Drawing | Discards the entire in-progress stroke. |
| **Delete** | Drawing, or a selection | Discards the in-progress stroke, or deletes the current selection. |
| **Scroll wheel** | Canvas | Zooms in/out, centered on the cursor. |
| **Right-click-drag** | Canvas | Pans the view. |
| **Drag from empty space** | Canvas | Draws a selection box — see [Selection and editing](selection-and-editing.md#drag-box-select) for the Ctrl/Shift modifiers. |
