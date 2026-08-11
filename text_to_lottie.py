"""
text_to_lottie.py

Converts a text string into Lottie-format vector shapes ("sh" path items),
using real font outlines (TrueType glyf table, quadratic beziers) converted
to Lottie's cubic-bezier vertex/in-tangent/out-tangent format.

No external rendering dependencies -- pure fontTools + math.
"""
import math
from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import RecordingPen


def _expand_qcurve(start, pts):
    """Expand a TrueType qCurveTo point list (with implied on-curve midpoints)
    into a list of simple quadratic segments: ('quad', p0, ctrl, p1) or ('line', p0, p1)."""
    segments = []
    n = len(pts)
    if n == 1:
        segments.append(('line', start, pts[0]))
        return segments
    cur_on = start
    for i in range(n - 1):
        c = pts[i]
        nxt = pts[i + 1]
        if i == n - 2:
            on = nxt
        else:
            on = ((c[0] + nxt[0]) / 2.0, (c[1] + nxt[1]) / 2.0)
        segments.append(('quad', cur_on, c, on))
        cur_on = on
    return segments


def _quad_to_cubic(p0, c, p1):
    """Degree-elevate a quadratic bezier (p0, c, p1) to cubic control points (c1, c2)."""
    c1 = (p0[0] + 2.0 / 3.0 * (c[0] - p0[0]), p0[1] + 2.0 / 3.0 * (c[1] - p0[1]))
    c2 = (p1[0] + 2.0 / 3.0 * (c[0] - p1[0]), p1[1] + 2.0 / 3.0 * (c[1] - p1[1]))
    return c1, c2


def glyph_to_contours(glyphset, glyph_name):
    """Return glyph outline as a list of contours. Each contour is a list of
    (vertex, in_tangent_offset, out_tangent_offset) with tangents relative to vertex,
    matching Lottie's bezier vertex convention. Handles composite glyphs recursively
    (fontTools' pen-based draw() already resolves components)."""
    rec = RecordingPen()
    glyphset[glyph_name].draw(rec)

    contours = []
    cur_contour = None   # list of dicts: {'pt':.., 'in':.., 'out':..}
    start_pt = None
    last_pt = None

    def new_vertex(pt):
        return {'pt': pt, 'in': (0.0, 0.0), 'out': (0.0, 0.0)}

    for op, args in rec.value:
        if op == 'moveTo':
            cur_contour = [new_vertex(args[0])]
            start_pt = args[0]
            last_pt = args[0]
        elif op == 'lineTo':
            v = new_vertex(args[0])
            cur_contour.append(v)
            last_pt = args[0]
        elif op == 'curveTo':
            # cubic: args = (c1, c2, p1)
            c1, c2, p1 = args
            cur_contour[-1]['out'] = (c1[0] - last_pt[0], c1[1] - last_pt[1])
            v = new_vertex(p1)
            v['in'] = (c2[0] - p1[0], c2[1] - p1[1])
            cur_contour.append(v)
            last_pt = p1
        elif op == 'qCurveTo':
            for seg in _expand_qcurve(last_pt, list(args)):
                if seg[0] == 'line':
                    _, p0, p1 = seg
                    v = new_vertex(p1)
                    cur_contour.append(v)
                    last_pt = p1
                else:
                    _, p0, c, p1 = seg
                    c1, c2 = _quad_to_cubic(p0, c, p1)
                    cur_contour[-1]['out'] = (c1[0] - p0[0], c1[1] - p0[1])
                    v = new_vertex(p1)
                    v['in'] = (c2[0] - p1[0], c2[1] - p1[1])
                    cur_contour.append(v)
                    last_pt = p1
        elif op == 'closePath' or op == 'endPath':
            if cur_contour:
                # if the last point duplicates the start point, merge them
                # (transfer the closing segment's tangent info onto vertex 0)
                if len(cur_contour) > 1 and cur_contour[-1]['pt'] == cur_contour[0]['pt']:
                    cur_contour[0]['in'] = cur_contour[-1]['in']
                    cur_contour.pop()
                contours.append(cur_contour)
            cur_contour = None

    return contours


def get_char_glyph_name(font, ch, fallback_font=None):
    cmap = font.getBestCmap()
    if ord(ch) in cmap:
        return font, cmap[ord(ch)]
    if fallback_font is not None:
        fcmap = fallback_font.getBestCmap()
        if ord(ch) in fcmap:
            return fallback_font, fcmap[ord(ch)]
    return None, None


def layout_text(text, primary_font_path, fallback_font_path=None, tracking=0.0):
    """Lay out `text` left-to-right using advance widths (no kerning).
    Returns (contours, bbox) where contours is a list of per-character contour-lists
    already positioned in a shared coordinate space (font units, Y-down/Lottie
    convention), and bbox is (minx, miny, maxx, maxy) of the full string.
    """
    primary = TTFont(primary_font_path)
    fallback = TTFont(fallback_font_path) if fallback_font_path else None

    upm_primary = primary['head'].unitsPerEm
    # normalize fallback to the same em-square scale as primary
    upm_fallback = fallback['head'].unitsPerEm if fallback else upm_primary

    pen_x = 0.0
    all_contours = []
    any_pt = False
    minx = miny = math.inf
    maxx = maxy = -math.inf

    for ch in text:
        if ch == ' ':
            # space: just advance. Try primary space width, else 0.25 em.
            cmap = primary.getBestCmap()
            if 0x20 in cmap:
                gname = cmap[0x20]
                w = primary.getGlyphSet()[gname].width
            else:
                w = upm_primary * 0.25
            pen_x += w + tracking
            continue

        font, gname = get_char_glyph_name(primary, ch, fallback)
        if gname is None:
            # unsupported character in both fonts -- skip but advance a placeholder width
            pen_x += upm_primary * 0.5 + tracking
            continue

        glyphset = font.getGlyphSet()
        upm = upm_primary if font is primary else upm_fallback
        scale = upm_primary / upm  # normalize fallback glyphs to primary's em size

        contours = glyph_to_contours(glyphset, gname)
        for contour in contours:
            new_contour = []
            for v in contour:
                # Flip Y here (font units are Y-up; Lottie/SVG space is Y-down).
                # This must happen before any fitting/centering math downstream,
                # so every later step operates in one consistent coordinate space.
                pt = (v['pt'][0] * scale + pen_x, -v['pt'][1] * scale)
                intan = (v['in'][0] * scale, -v['in'][1] * scale)
                outtan = (v['out'][0] * scale, -v['out'][1] * scale)
                new_contour.append({'pt': pt, 'in': intan, 'out': outtan})
                any_pt = True
                minx = min(minx, pt[0]); maxx = max(maxx, pt[0])
                miny = min(miny, pt[1]); maxy = max(maxy, pt[1])
            all_contours.append(new_contour)

        width = glyphset[gname].width * scale
        pen_x += width + tracking

    if not any_pt:
        return [], (0, 0, 0, 0)

    return all_contours, (minx, miny, maxx, maxy)


def contours_to_lottie_shapes(contours):
    """Convert contour list (already in Lottie's Y-down convention -- see
    layout_text, which does the Y flip at extraction time) into Lottie 'sh'
    shape items. No further axis flip happens here."""
    shapes = []
    for contour in contours:
        v = []
        i_tan = []
        o_tan = []
        for pt in contour:
            x, y = pt['pt']
            v.append([x, y])
            ix, iy = pt['in']
            i_tan.append([ix, iy])
            ox, oy = pt['out']
            o_tan.append([ox, oy])
        shapes.append({
            "ty": "sh",
            "d": 1,
            "ks": {
                "a": 0,
                "k": {"i": i_tan, "o": o_tan, "v": v, "c": True},
                "ix": 2,
            }
        })
    return shapes


def text_to_lottie_path_shapes(text, primary_font_path, fallback_font_path,
                                target_center, target_height, max_width=None,
                                tracking_em=0.0):
    """High level: lay out `text`, fit to target_center=(cx,cy) and target_height
    (in the destination/local coordinate space), return list of Lottie 'sh' items
    plus the resulting bbox (post-fit, in local space).

    Scaling is uniform (never stretched/squished per-axis), so by default the
    string is sized to match target_height with no limit on the resulting
    width -- fine when the new text is about as long as what it's replacing,
    but a longer string ends up proportionally wider and can run past the
    edge of the sticker. Passing `max_width` caps that: the smaller of the
    height-fit and width-fit scale wins, so a too-long string comes out a
    bit smaller overall (still one line, still normal letter proportions)
    instead of overflowing.
    """
    upm = TTFont(primary_font_path)['head'].unitsPerEm
    contours, (minx, miny, maxx, maxy) = layout_text(
        text, primary_font_path, fallback_font_path, tracking=tracking_em * upm)

    src_h = maxy - miny
    src_w = maxx - minx
    if src_h <= 0:
        raise ValueError("Empty or unrenderable text")

    scale = target_height / src_h
    if max_width and max_width > 0 and src_w > 0:
        scale = min(scale, max_width / src_w)
    src_cx = (minx + maxx) / 2.0
    src_cy = (miny + maxy) / 2.0

    fitted = []
    for contour in contours:
        new_contour = []
        for p in contour:
            x, y = p['pt']
            nx = (x - src_cx) * scale + target_center[0]
            ny = (y - src_cy) * scale + target_center[1]
            new_contour.append({
                'pt': (nx, ny),
                'in': (p['in'][0] * scale, p['in'][1] * scale),
                'out': (p['out'][0] * scale, p['out'][1] * scale),
            })
        fitted.append(new_contour)

    shapes = contours_to_lottie_shapes(fitted)
    out_w = src_w * scale
    out_h = src_h * scale
    return shapes, out_w, out_h
