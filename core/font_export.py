"""
Turns a Project (source PNGs + calibration + per-glyph edit recipes +
font settings) into an actual .ttf file.
"""
from __future__ import annotations

import os

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import newTable
from fontTools.ttLib.tables.O_S_2f_2 import Panose
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

from . import tracing, geometry
from .project import Project, GlyphRecipe, auto_vertical_metrics


class ExportError(Exception):
    pass


def _apply_edits(paths, recipe: GlyphRecipe):
    # import_scale applied FIRST, before edits -- see GlyphRecipe.
    # import_scale's own docstring for why the structural, import-time
    # resize needed to fit an oversized source to the canvas is tracked
    # separately from edits at all (so it never triggers the "edited"
    # marker in the glyph list for a resize the user never actually
    # touched themselves), and why it occupies this same position edits
    # itself always applied it at back when it was still the first
    # entry in a freshly-imported glyph's own edits list.
    if recipe.import_scale != 1.0:
        paths = geometry.scale_uniform(paths, factor=recipe.import_scale, origin=[0.0, 0.0])
    for step in recipe.edits:
        fn = getattr(geometry, step.op, None)
        if fn is None:
            raise ExportError(f"Unknown edit op '{step.op}' for U+{recipe.codepoint:04X}")
        paths = fn(paths, **step.args)
    return paths


def _glyph_name_for_codepoint(cp: int) -> str:
    if cp == 0x20:
        return "space"
    return f"uni{cp:04X}" if cp <= 0xFFFF else f"u{cp:06X}"


class FontExporter:
    def __init__(self, project: Project):
        self.project = project
        m = project.metrics
        if None in (m.cap_top_px, m.baseline_px, m.advance_px):
            raise ExportError(
                "Metrics are incomplete (cap height / baseline / advance "
                "width). Run calibration or set them manually before exporting.")

        self.cap_px = m.baseline_px - m.cap_top_px
        self.baseline_px = m.baseline_px

        em = project.font.em
        # Design-space CAP is fixed at 68% of EM, a typical convention;
        # everything else derives from the source-pixel measurements.
        self.cap_design = round(em * 0.68)
        self.scale = self.cap_design / self.cap_px

        self.xh_design = (round((m.baseline_px - m.xheight_top_px) * self.scale)
                           if m.xheight_top_px is not None else None)
        self.des_design = (-round((m.descender_bottom_px - m.baseline_px) * self.scale)
                            if m.descender_bottom_px is not None else None)
        self.adv_design = round(m.advance_px * self.scale)
        # Ascent/descent: use an explicit override if the user set one,
        # otherwise fall back to the same auto-derived formula the
        # Advanced Settings dialog previews (see auto_vertical_metrics'
        # own docstring for why sharing this one implementation matters).
        auto_ascent, auto_descent = auto_vertical_metrics(project.font, m)
        self.ascent = project.font.ascent_override or auto_ascent
        self.descent = project.font.descent_override or auto_descent

        self.half_width_px = project.font.weight_ratio * self.cap_px / 2

    # -- coordinate transform --------------------------------------------

    def to_design_space(self, x: float, y: float) -> tuple[float, float]:
        return (x * self.scale, (self.baseline_px - y) * self.scale)

    # -- per-glyph build ---------------------------------------------------

    def build_glyph_outline(self, recipe: GlyphRecipe):
        """Trace, edit, and thicken one glyph. Returns a shapely geometry
        (or None for an empty glyph like space)."""
        if recipe.source_file:
            # Staging-aware (see Project.resolve_source_path's own
            # docstring) -- a font build needs to reflect whatever's
            # CURRENTLY in effect for this glyph, staged replacement or
            # original, exactly like tracing/display already do.
            src_path = self.project.resolve_source_path(recipe)
            paths, _shape = tracing.trace_glyph(src_path)
        else:
            # drawn from scratch -- no source PNG to trace; _apply_edits
            # below builds the real geometry from recipe.edits alone
            # (e.g. the add_path steps freehand drawing records)
            paths = []
        paths = _apply_edits(paths, recipe)
        return geometry.thicken(paths, self.half_width_px,
                                 cap_style=self.project.font.cap_style)

    def geom_to_ttglyph(self, geom):
        pen = TTGlyphPen(None)
        if geom is None or geom.is_empty:
            return pen.glyph()
        polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
        for poly in polys:
            if poly.is_empty or len(poly.exterior.coords) < 3:
                continue
            ext = [self.to_design_space(x, y) for x, y in poly.exterior.coords]
            holes = [[self.to_design_space(x, y) for x, y in interior.coords]
                     for interior in poly.interiors]
            design_poly = orient(Polygon(ext, holes), sign=-1.0)  # exterior CW, holes CCW

            ext_pts = list(design_poly.exterior.coords)[:-1]
            pen.moveTo((round(ext_pts[0][0]), round(ext_pts[0][1])))
            for x, y in ext_pts[1:]:
                pen.lineTo((round(x), round(y)))
            pen.closePath()

            for interior in design_poly.interiors:
                pts = list(interior.coords)[:-1]
                pen.moveTo((round(pts[0][0]), round(pts[0][1])))
                for x, y in pts[1:]:
                    pen.lineTo((round(x), round(y)))
                pen.closePath()
        return pen.glyph()

    # -- full build --------------------------------------------------------

    def export(self, out_path: str, progress_cb=None) -> dict:
        """Build every non-excluded glyph and write the TTF. Returns a
        report dict {built: [...], failed: [(cp, error)], skipped: [...]}."""
        glyph_order = [".notdef", "space"]
        charmap: dict[int, str] = {}
        glyphs = {}
        metrics = {}

        notdef_pen = TTGlyphPen(None)
        box = [(60, 0), (self.adv_design - 60, 0),
               (self.adv_design - 60, self.cap_design), (60, self.cap_design)]
        notdef_pen.moveTo(box[0])
        for pt in box[1:]:
            notdef_pen.lineTo(pt)
        notdef_pen.closePath()
        glyphs[".notdef"] = notdef_pen.glyph()
        metrics[".notdef"] = (self.adv_design, 60)

        glyphs["space"] = TTGlyphPen(None).glyph()
        metrics["space"] = (self.adv_design, 0)
        charmap[0x20] = "space"

        report = {"built": [], "failed": [], "skipped": []}
        items = list(self.project.glyphs.items())
        for i, (cp, recipe) in enumerate(items):
            if progress_cb:
                progress_cb(i, len(items), cp)
            if recipe.excluded or cp == 0x20:
                reason = "excluded" if recipe.excluded else "space (built-in, not a traced glyph)"
                report["skipped"].append((cp, reason))
                continue
            if not recipe.source_file and not recipe.edits:
                # drawn-from-scratch glyph with nothing drawn into it
                # yet -- there's no source PNG to trace and no edits
                # that could have built geometry from nothing, so
                # there's genuinely nothing to export here. Skipped,
                # not failed: this is an expected, normal state for a
                # glyph added via "Add Glyph" before it's been drawn,
                # not something gone wrong.
                report["skipped"].append((cp, "not drawn yet"))
                continue
            try:
                geom = self.build_glyph_outline(recipe)
                g = self.geom_to_ttglyph(geom)
            except Exception as e:
                report["failed"].append((cp, str(e)))
                continue
            gname = _glyph_name_for_codepoint(cp)
            glyphs[gname] = g
            glyph_order.append(gname)
            charmap[cp] = gname
            xmin = min((c[0] for c in g.coordinates), default=0) if g.numberOfContours else 0
            metrics[gname] = (self.adv_design, round(xmin))
            report["built"].append(cp)

        fb = FontBuilder(self.project.font.em, isTTF=True)
        fb.setupGlyphOrder(glyph_order)
        fb.setupCharacterMap(charmap)
        fb.setupGlyf(glyphs)
        fb.setupHorizontalMetrics(metrics)
        fs = self.project.font
        fb.setupHorizontalHeader(ascent=self.ascent, descent=-self.descent, lineGap=fs.line_gap)
        name_fields = {
            "familyName": fs.family_name,
            "styleName": fs.style_name,
            "uniqueFontIdentifier": f"{fs.family_name} {fs.style_name} {fs.version}",
            "fullName": f"{fs.family_name} {fs.style_name}",
            "psName": f"{fs.family_name.replace(' ', '')}-{fs.style_name.replace(' ', '')}",
            "version": f"Version {fs.version}",
        }
        # Optional metadata: only included when actually set, rather than
        # writing empty strings into the font -- an absent field is
        # normal; a blank one can look like a mistake in font tools.
        optional = {
            "copyright": fs.copyright, "manufacturer": fs.manufacturer,
            "designer": fs.designer, "description": fs.description,
            "vendorURL": fs.vendor_url, "designerURL": fs.designer_url,
            "licenseDescription": fs.license, "licenseInfoURL": fs.license_url,
            "trademark": fs.trademark,
        }
        name_fields.update({k: v for k, v in optional.items() if v})
        fb.setupNameTable(name_fields)
        fb.setupOS2(
            sTypoAscender=self.ascent, sTypoDescender=-self.descent, sTypoLineGap=fs.line_gap,
            usWinAscent=self.ascent, usWinDescent=self.descent,
            sxHeight=self.xh_design or round(self.cap_design * 0.7),
            sCapHeight=self.cap_design,
            panose=Panose(bFamilyType=2, bSerifStyle=0, bWeight=5,
                           bProportion=9 if fs.monospace else 0,
                           bContrast=0, bStrokeVariation=0, bArmStyle=0,
                           bLetterForm=0, bMidline=0, bXHeight=0),
        )
        # setupOS2 auto-computes ulUnicodeRange from the cmap, but has
        # NO equivalent for ulCodePageRange -- that field stays zeroed
        # unless set explicitly, and a zeroed ulCodePageRange is a
        # documented, specific cause of exactly this symptom: some
        # Windows GDI-based text paths (Word among them) show the font
        # as selectable/recognized -- which is why Windows' own font
        # preview, likely using a different rendering path, can still
        # look correct -- but treat it as "not suitable for any
        # character set" once actually asked to render with it, and
        # silently substitute a fallback font instead. fontTools ships
        # exactly this fix already, including a fallback-to-Latin-1
        # safety net whose own comment cites this precise MS Word issue.
        fb.font["OS/2"].recalcCodePageRanges(fb.font)
        fb.setupPost()
        # An empty DSIG (digital signature) table -- ulNumSigs=0, no
        # actual cryptographic content needed. Without SOME DSIG table
        # present, Windows doesn't recognize a TrueType font as a full
        # OpenType font at all, which is specifically why the same file
        # can render correctly in a FreeType-based renderer (KiCad) and
        # still fail in Word: this is a Windows/GDI-specific check, not
        # something FreeType enforces. The empty-table workaround is
        # the same one most font tools (e.g. Google Fonts' own gftools)
        # use rather than requiring an actual signing certificate.
        dsig = newTable("DSIG")
        dsig.ulVersion = 1
        dsig.usFlag = 0
        dsig.usNumSigs = 0
        dsig.signatureRecords = []
        fb.font["DSIG"] = dsig
        fb.save(out_path)
        return report


def export_multi_weight(project: Project, out_dir: str, base_name: str,
                         variants: list[tuple[float, str]], progress_cb=None) -> dict:
    """Export several weight variants of the same glyph set in one pass
    -- e.g. [(0.05, "Light"), (0.10, "Regular"), (0.16, "Bold")]. Each
    variant gets its own file, named "{base_name}-{style}.ttf".

    Temporarily overrides weight_ratio/style_name on the SAME project
    object (rather than exporting from copies) since FontExporter reads
    them directly off project.font -- always restored via try/finally
    even if one variant fails partway through, so a batch export never
    leaves the live project's settings altered afterward.

    progress_cb(variant_index, total_variants, style_name, glyph_index,
    glyph_total, cp) -- called for every glyph of every variant, not
    just once per variant, since a multi-variant batch is exactly the
    kind of multi-second operation a progress bar exists for in the
    first place.

    Returns {style_name: report} for each variant that was attempted;
    a variant that raised before producing a report is recorded as
    {"error": str(exception)} instead of a normal report dict.
    """
    orig_weight = project.font.weight_ratio
    orig_style = project.font.style_name
    results: dict[str, dict] = {}
    try:
        for i, (weight_ratio, style_name) in enumerate(variants):
            project.font.weight_ratio = weight_ratio
            project.font.style_name = style_name
            out_path = os.path.join(out_dir, f"{base_name}-{style_name}.ttf")

            def glyph_cb(gi, gtotal, cp, _i=i, _style=style_name):
                if progress_cb:
                    progress_cb(_i, len(variants), _style, gi, gtotal, cp)

            try:
                exporter = FontExporter(project)
                results[style_name] = exporter.export(out_path, progress_cb=glyph_cb)
            except Exception as e:
                results[style_name] = {"error": str(e)}
    finally:
        project.font.weight_ratio = orig_weight
        project.font.style_name = orig_style
    return results
