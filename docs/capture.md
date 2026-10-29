# Capture mode

**Capture Glyphs…** on the toolbar builds glyphs directly from screen-grabbed regions of any on-screen reference — another app, a scanned sheet, anything visible on screen — without needing to prepare individual PNG files by hand first.

## Starting a session

Click **Capture Glyphs…** to open the **Capture** window and switch focus to whatever's currently on screen (PNGlyph's own main window hides for the duration). This one window handles the whole session — grabbing, reviewing, and assigning codepoints all live together here, so there's nothing else to open or switch to:

![The Capture window mid-session: a grabbed capture ready for a codepoint](images/capture-window.png)

- **Captured glyphs (N)** — the group box title is a live count of everything queued in this session so far, with one row per capture below it: a thumbnail and an editable codepoint field, same as the multi-file "Assign codepoints" review window (see [Adding glyphs](adding-glyphs.md#assigning-codepoints-to-files)).
- **Spacing (px)** shows the current fixed-size rectangle, once one exists — carried over from the last time you captured anything (this session or a previous one), so most sessions start with a size already in place.
- **Use canvas size** sets the capture rectangle to match the current project's own canvas size in one click. It's disabled — with a tooltip explaining why — when there's no project, or when the project's canvas size isn't genuinely established yet (for example, a brand-new project created just for this capture session, whose canvas size is only a provisional placeholder until the session commits).
- **Grab section** freezes the current on-screen view for cropping. Hover it for a reminder of the zoom/SHIFT/CTRL gestures below.
- **Commit All Assigned**, **Discard Session**, and **Close (keep pending)** — see "Finishing up" below.

## Grabbing a section

Clicking **Grab section** hides the Capture window, freezes a screenshot, and switches to crosshair positioning over it:

![A capture rectangle positioned over a frozen screenshot, with its live "120 x 90 • 3 captured" label](images/capture-overlay-label.png)

- **Plain click** commits the current fixed-size rectangle at the cursor — the normal, fast path once a size is established.
- **Hold SHIFT** to draw a new rectangle instead of using the fixed size — click one corner, move, click the opposite corner. SHIFT only needs to be held for the first click.
- **CTRL** toggles snapping to a nearby corner, line, or point on/off — it's a toggle, not a held modifier, so it stays as set rather than needing to be held down.
- **Right-click**, or **Escape** with nothing pending, cancels back to the Capture window. Escape with a SHIFT-started corner already placed cancels just that pending rectangle instead.
- Keep your zoom level constant for the whole session — changing it forces resizing captures to match, which can hurt detection.

The rectangle's own dimension label doubles as a live zoom-consistency check (a size that suddenly differs from your last capture is the visible sign of a zoom change) and carries the running capture count too, right where your attention already is while positioning. Capture size and the snap toggle's own on/off state don't get a separate on-screen readout at all — both are already covered by the Grab section button's own tooltip, and the Capture window itself reappears the moment you're not actively capturing.

The Capture window hides completely while you're positioning a capture — it would otherwise sit on top of part of whatever you're trying to grab — and reappears automatically once you're done, whether that's a successful capture, a cancelled grab, or a failed one.

## What makes a good capture

The same requirement [Getting started](getting-started.md#what-makes-a-workable-source-png) sets for a prepared PNG applies here too: tracing needs a genuinely flat, high-contrast, essentially two-tone source. Polarity (which color is ink) is worked out automatically, so a light-on-dark or dark-on-light capture both trace correctly with nothing to configure — but that detection still depends on the source actually being close to two flat tones to begin with, and a live on-screen capture is far more likely to fall short of that than a purpose-prepared PNG, even when it looks clean by eye: real font smoothing and anti-aliasing carry genuine in-between pixel values along every edge, which is exactly what shows up afterward as small, unwanted deviations on what should be straight segments.

A capture that looks likely to trace noisily gets flagged right in its row, in the Capture window itself:

![A capture row showing the low-contrast warning](images/flatness-warning-row.png)

The full explanation is in the warning's own tooltip. It's advisory, not a block — you can still assign a codepoint and commit it regardless — but it's worth checking before spending time on one. Ink and background are classified against two fixed brightness bars rather than something that adapts per image, deliberately: an adaptive version was tried and reverted after it was confirmed to occasionally fail in a way that looked confidently right but wasn't, rather than failing in an obviously visible way — see [Getting started](getting-started.md#what-makes-a-workable-source-png) for the fuller reasoning. The practical result is that there's no clever fallback for a source that isn't genuinely flat — if you can, a source rendered without font smoothing beats a screenshot of normally-rendered text every time, for the same reason a flat, purpose-prepared PNG traces more reliably than a photo of the same letterform, and if a capture keeps warning, running it through an ordinary image editor's contrast/levels tool first (IrfanView and other free tools all work fine) is the fix, not a PNGlyph setting.

## Finishing up

- **Commit All Assigned** adds every row with a valid codepoint to the project. Rows still missing one are simply left pending — you're asked to confirm if any exist, and the window stays open afterward if some rows weren't committed, closes if everything was.
- **Discard Session** throws away everything captured so far, after confirming.
- **Close (keep pending)** leaves everything captured untouched and closes the window — resume later from **Capture Glyphs…** again.

The titlebar X and Escape both do the same thing as **Close (keep pending)** — suspend the session, keep everything queued, nothing lost. There's no separate "end session" action distinct from these three; between them they cover every way a session actually ends.

A capture size persists across sessions, and across restarting PNGlyph entirely, so you don't need to redefine it every time — SHIFT-drawing a new rectangle mid-session redefines it going forward from that point on.
