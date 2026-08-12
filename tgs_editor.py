"""
tgs_editor.py

Load a .tgs (gzip-compressed Lottie JSON) Telegram sticker or animated
custom-emoji file -- both are the same 512x512, gzip+Lottie-JSON format, see
find_text_candidates() below -- automatically locate the shape group that
holds the vector text/logo, and replace its paths with freshly-generated
text, while leaving every transform, every other layer, and all animation
completely untouched.

Earlier versions of this module only worked on files built like the
original AnimatedSticker.tgs template, where a human had manually named the
relevant precomp asset "mylogo". Most real .tgs files -- other stickers,
custom emoji, anything not hand-labelled for this bot -- give their layers
and groups no name at all (Bodymovin/After Effects export leaves `nm`
missing, which shows up as `None`), so there is nothing to search for by
name. find_text_candidates() instead scans *every* shape group in the file
(every layer of every precomp asset, plus the top-level layers, recursively
through nested groups) and scores each one on how much it *looks* like text:
several separate closed paths sitting on one baseline (wide, short bounding
box; centers spread out horizontally, not vertically), at a plausible
letter-sized scale relative to the canvas, usually static (not
frame-by-frame morphing) and solid-filled. See _score_candidate() for the
exact signals and weights.

A group's "several separate closed paths" can show up two different ways in
real files, and both have to count as one candidate or detection silently
grabs a fragment of the text instead of the whole thing:
  - flat: the letters are direct 'sh' siblings inside one group (one shared
    baseline transform for the whole word), or
  - per-glyph: each letter is its own nested 'gr' (so it can carry its own
    'tr' for that letter's x-position/kerning), and only a *multi-contour*
    letter -- like "e", with an outer ring and an inner counter-hole -- ends
    up with 2+ 'sh' of its own.
_collect_points() understands both: it treats a direct 'gr' child the same
way it treats a direct 'sh' child, recursing to pull in whatever geometry
lives inside that nested group. Without this, a per-glyph word registers no
candidate for "the whole word" at all -- the only group anywhere with 2+
direct 'sh' children is a single multi-contour *letter* inside it, so that
lone letter gets scored and replaced on its own, leaving the rest of the
original word sitting right next to the new text untouched.
"""
import gzip
import json
import math
import statistics
from pathlib import Path

from text_to_lottie import text_to_lottie_path_shapes

MAX_TGS_BYTES = 64 * 1024  # Telegram's limit for .tgs, same for stickers & animated custom emoji

FONTS_DIR = Path(__file__).parent / "fonts"
DEFAULT_PRIMARY_FONT = str(FONTS_DIR / "Poppins-Bold.ttf")     # geometric look, Latin only
DEFAULT_FALLBACK_FONT = str(FONTS_DIR / "DejaVuSans-Bold.ttf")  # covers Cyrillic & more


def load_tgs(path):
    with open(path, 'rb') as f:
        raw = f.read()
    return json.loads(gzip.decompress(raw).decode('utf-8'))


def load_tgs_bytes(raw: bytes):
    return json.loads(gzip.decompress(raw).decode('utf-8'))


def save_tgs(data, path):
    payload = json.dumps(data, separators=(',', ':')).encode('utf-8')
    compressed = gzip.compress(payload, compresslevel=9, mtime=0)
    with open(path, 'wb') as f:
        f.write(compressed)
    return len(compressed)


def save_tgs_bytes(data) -> bytes:
    payload = json.dumps(data, separators=(',', ':')).encode('utf-8')
    return gzip.compress(payload, compresslevel=9, mtime=0)


# ---------------------------------------------------------------------------
# Small 2D affine transform helper, used only to bring nested shape groups
# into a common coordinate space for *scoring* candidates fairly (a symbol
# reused at a tiny internal scale shouldn't look "huge" just because its raw
# path coordinates are big). It composes each ancestor group's own "tr" item
# as we descend. It deliberately stops at the layer boundary -- it does not
# also fold in the layer's own "ks" transform or any layer-parenting chain,
# which would need graph resolution across the whole file for comparatively
# little benefit here. This is a heuristic ranking aid, not a renderer.
# ---------------------------------------------------------------------------
class Transform2D:
    __slots__ = ('a', 'b', 'c', 'd', 'tx', 'ty')

    def __init__(self, a=1.0, b=0.0, c=0.0, d=1.0, tx=0.0, ty=0.0):
        self.a, self.b, self.c, self.d, self.tx, self.ty = a, b, c, d, tx, ty

    def then(self, outer: "Transform2D") -> "Transform2D":
        """Compose: a point mapped by `self` first, then by `outer`."""
        a, b, c, d = outer.a, outer.b, outer.c, outer.d
        return Transform2D(
            a * self.a + c * self.b, b * self.a + d * self.b,
            a * self.c + c * self.d, b * self.c + d * self.d,
            a * self.tx + c * self.ty + outer.tx,
            b * self.tx + d * self.ty + outer.ty,
        )

    def apply(self, x, y):
        return (self.a * x + self.c * y + self.tx, self.b * x + self.d * y + self.ty)


def _prop_value(prop):
    """First static-or-first-keyframe value of a Lottie animatable property."""
    if not isinstance(prop, dict):
        return None
    k = prop.get('k')
    if isinstance(k, list) and k and isinstance(k[0], dict):
        return k[0].get('s')  # first keyframe's start value
    return k  # already a plain static value


def _transform_from_tr(tr):
    """Build a Transform2D from a shape-group 'tr' item. Falls back to the
    identity for anything missing or oddly-shaped -- only feeds a detection
    heuristic, never the actual file we save."""
    if not tr:
        return Transform2D()
    try:
        p = _prop_value(tr.get('p')) or [0, 0]
        a = _prop_value(tr.get('a')) or [0, 0]
        s = _prop_value(tr.get('s')) or [100, 100]
        r = _prop_value(tr.get('r'))
        r = r if isinstance(r, (int, float)) else 0.0
        sx, sy = s[0] / 100.0, s[1] / 100.0
        rad = math.radians(r)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        scale = Transform2D(sx, 0, 0, sy, -a[0] * sx, -a[1] * sy)  # anchor + scale
        rotate = Transform2D(cos_r, sin_r, -sin_r, cos_r, 0, 0)
        translate = Transform2D(1, 0, 0, 1, p[0], p[1])
        return scale.then(rotate).then(translate)
    except Exception:
        return Transform2D()


def _find_tr(items):
    return next((it for it in items if it.get('ty') == 'tr'), None)


def _sh_points(sh):
    """Vertex list of a 'sh' path item, static or keyframed."""
    ks = sh.get('ks', {})
    if ks.get('a', 0) == 0:
        val = ks.get('k') or {}
    else:
        keyframes = ks.get('k') or []
        val = {}
        if keyframes:
            s = keyframes[0].get('s')
            val = (s[0] if isinstance(s, list) and s else s) or {}
    return val.get('v', []) if isinstance(val, dict) else []


def _collect_points(item, accum):
    """Recursively collect every path vertex reachable from `item` (a 'sh'
    or 'gr' dict), transformed into `accum`'s coordinate space, plus
    whether every path involved is non-animated ('ks.a' == 0 everywhere).

    A direct 'gr' child is treated the same as a direct 'sh' child: both are
    "one shape unit" one level up, and a 'gr' unit contributes whatever
    geometry sits anywhere inside it (via its own nested 'tr', composed into
    `accum` here). This is what lets a per-glyph word -- one nested group
    per letter, e.g. so each letter can carry its own x-position -- be seen
    as one multi-unit candidate the same way a flat word (several sibling
    'sh') already was. See the module docstring for why that matters."""
    ty = item.get('ty')
    if ty == 'sh':
        pts = [accum.apply(x, y) for x, y in _sh_points(item)]
        is_static = item.get('ks', {}).get('a', 0) == 0
        return pts, is_static
    if ty == 'gr':
        own_items = item.get('it', [])
        new_accum = _transform_from_tr(_find_tr(own_items)).then(accum)
        pts = []
        is_static = True
        for sub in own_items:
            if sub.get('ty') in ('sh', 'gr'):
                sub_pts, sub_static = _collect_points(sub, new_accum)
                pts.extend(sub_pts)
                is_static = is_static and sub_static
        return pts, is_static
    return [], True


def _group_raw_bbox(group):
    """Bounding box of everything a group will render, in the group's own
    local coordinate space (before the group's own 'tr' is applied) -- this
    is the space the replacement letters must be generated in, since we
    splice new 'sh' items straight into this same group, keeping its own
    'tr'/fill untouched. Includes nested 'gr' children (each through its own
    relative transform via _collect_points), so this covers both flat
    (sibling 'sh') and per-glyph (nested 'gr' per letter) word layouts."""
    xs, ys = [], []
    for item in group.get('it', []):
        if item.get('ty') in ('sh', 'gr'):
            pts, _ = _collect_points(item, Transform2D())
            for vx, vy in pts:
                xs.append(vx)
                ys.append(vy)
    if not xs:
        raise ValueError("Shape group has no path data to measure.")
    return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------------------
# Auto-detection of the text-shaped group.
# ---------------------------------------------------------------------------
def _range_plateau(x, lo, hi, soft_lo, soft_hi):
    """1.0 for x inside [lo, hi], decaying smoothly to 0 outside it."""
    if lo <= x <= hi:
        return 1.0
    if x < lo:
        return max(0.0, 1.0 - (lo - x) / max(soft_lo, 1e-6))
    return max(0.0, 1.0 - (x - hi) / max(soft_hi, 1e-6))


def _score_candidate(n_sh, bbox, per_shape_bboxes, canvas_h, is_static, has_solid_fill, n_aliases):
    x0, y0, x1, y1 = bbox
    w, h = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)

    # (1) Text sits on one baseline: wide group, centers spread out mostly
    # along X with little vertical scatter -- this is the single strongest
    # signal (real text scores 10-25x here; icons/backgrounds score <2).
    # With only 2-3 shapes this ratio is statistically noisy (two points
    # always define "some" x/y ratio purely by chance), so its weight is
    # scaled down by how many shapes actually back it up.
    xs = [(b[0] + b[2]) / 2.0 for b in per_shape_bboxes]
    ys = [(b[1] + b[3]) / 2.0 for b in per_shape_bboxes]
    std_x = statistics.pstdev(xs) if len(xs) > 1 else 0.0
    std_y = statistics.pstdev(ys) if len(ys) > 1 else 0.0
    baseline_ratio = std_x / (std_y + w * 0.02 + 1e-6)
    baseline_confidence = min(len(per_shape_bboxes) / 6.0, 1.0)
    baseline_score = min(math.log2(baseline_ratio + 1.0) / 3.0, 1.6) * baseline_confidence

    # (2) Group aspect ratio: text is noticeably wider than tall.
    aspect_score = min(math.log2(w / h + 1e-6) / 2.0, 1.3) if w > h else -0.3

    # (3) Individual letters sit at a plausible on-canvas text scale: not a
    # full-frame illustration, not a barely-visible fleck. The lower edge is
    # intentionally sharp -- a repeated decorative speck (sub-1% of canvas
    # height) should lose most of its credit here, not just a little.
    heights = [b[3] - b[1] for b in per_shape_bboxes]
    median_h = statistics.median(heights)
    size_score = _range_plateau(median_h / canvas_h, 0.015, 0.25, 0.01, 0.2)

    # (4) Plausible number of letters/contours for a short word or logotype.
    # A 2-path group is just as consistent with "one small two-tone icon" as
    # with a two-letter word, so it only gets partial credit; a run of many
    # separate closed paths is a much more distinctive "this is a string of
    # characters" signal.
    count_score = _range_plateau(n_sh, 4, 60, 3, 40)

    score = (2.0 * baseline_score + 1.3 * aspect_score
             + 1.6 * size_score + 0.8 * count_score)
    if is_static:
        score += 0.3
    if has_solid_fill:
        score += 0.15
    if n_aliases:
        score += 0.2
    return score


def _walk_shapes(items, accum, layer_label, layer_nm, asset_nm, out):
    for item in items:
        if not isinstance(item, dict) or item.get('ty') != 'gr':
            continue
        own_items = item.get('it', [])
        own_transform = _transform_from_tr(_find_tr(own_items))
        new_accum = own_transform.then(accum)

        # A direct child is one "shape unit" whether it's a bare path ('sh')
        # or a nested group ('gr') that itself resolves to some geometry --
        # e.g. one letter with its own position transform. See
        # _collect_points for why both must count the same way here.
        shape_children = [it for it in own_items if it.get('ty') in ('sh', 'gr')]
        if len(shape_children) >= 2:
            per_shape_bboxes = []
            all_static = True
            for child in shape_children:
                pts, is_static = _collect_points(child, new_accum)
                if not pts:
                    continue
                pxs = [p[0] for p in pts]
                pys = [p[1] for p in pts]
                per_shape_bboxes.append((min(pxs), min(pys), max(pxs), max(pys)))
                all_static = all_static and is_static
            if len(per_shape_bboxes) >= 2:
                bx0 = min(b[0] for b in per_shape_bboxes)
                by0 = min(b[1] for b in per_shape_bboxes)
                bx1 = max(b[2] for b in per_shape_bboxes)
                by1 = max(b[3] for b in per_shape_bboxes)
                has_solid_fill = any(it.get('ty') == 'fl' for it in own_items) and not any(
                    it.get('ty') in ('gf', 'gs') for it in own_items)
                aliases = [n for n in (item.get('nm'), layer_nm, asset_nm) if n]
                out.append({
                    'group': item,
                    'label': layer_label + [f"group(nm={item.get('nm')!r})"],
                    'n_paths': len(per_shape_bboxes),
                    'bbox': (bx0, by0, bx1, by1),
                    'per_shape_bboxes': per_shape_bboxes,
                    'is_static': all_static,
                    'has_solid_fill': has_solid_fill,
                    'aliases': aliases,
                })
        _walk_shapes(own_items, new_accum, layer_label, layer_nm, asset_nm, out)


def find_text_candidates(data):
    """Scan the whole document for shape groups that plausibly hold vector
    text, best-scoring first. Each candidate dict has: 'group' (the actual
    mutable Lottie dict -- mutate this to edit the file), 'label' (human
    breadcrumb for diagnostics), 'n_paths', 'bbox' (local, in the *group's
    own* coordinate space -- what you'd use to fit replacement text), and
    'aliases' (any names found on the group/layer/asset, for /target
    matching)."""
    raw = []
    canvas_h = float(data.get('h') or 512)

    for ai, asset in enumerate(data.get('assets', [])):
        for li, layer in enumerate(asset.get('layers', [])):
            label = [f"asset[{ai}]", f"layer[{li}]"]
            _walk_shapes(layer.get('shapes', []) or [], Transform2D(),
                         label, layer.get('nm'), asset.get('nm'), raw)

    for li, layer in enumerate(data.get('layers', [])):
        label = [f"layer[{li}]"]
        _walk_shapes(layer.get('shapes', []) or [], Transform2D(),
                     label, layer.get('nm'), None, raw)

    for c in raw:
        c['score'] = _score_candidate(
            c['n_paths'], c['bbox'], c['per_shape_bboxes'], canvas_h,
            c['is_static'], c['has_solid_fill'], len(c['aliases']),
        )
    raw.sort(key=lambda c: c['score'], reverse=True)
    return raw


class TextGroupNotFoundError(Exception):
    """Raised when locate_text_group() can't resolve a target.
    .candidates is the full ranked list found in the file (possibly empty);
    .hint is whatever the caller asked for (None means "just auto-pick" and
    means no candidates existed at all -- .hint being set but not found
    means the file does have *some* candidates, just none matching the
    hint, so the caller can show them as alternatives)."""
    def __init__(self, message, candidates=None, hint=None):
        self.candidates = candidates or []
        self.hint = hint
        super().__init__(message)


def locate_text_group(data, hint=None):
    """hint: None (auto-pick the top-scoring candidate), an int/numeric
    string (1-based index into the ranked list), or a name (matched
    case-insensitively against any alias recorded for a candidate --
    group name, layer name, or enclosing precomp-asset name).
    Returns (candidates, chosen_index)."""
    candidates = find_text_candidates(data)
    if not candidates:
        raise TextGroupNotFoundError(
            "No vector shape group in this file looks like text (no group "
            "has 2+ separate closed paths at all -- the text here may be a "
            "raster image, or the sticker may not have a logo/text layer).",
            candidates=[], hint=hint)

    if hint is None or (isinstance(hint, str) and not hint.strip()):
        return candidates, 0

    hint_str = str(hint).strip()
    if hint_str.isdigit():
        idx = int(hint_str)
        if 1 <= idx <= len(candidates):
            return candidates, idx - 1
        raise TextGroupNotFoundError(
            f"Candidate #{idx} doesn't exist (found {len(candidates)}).",
            candidates=candidates, hint=hint)

    name = hint_str.lower()
    for i, c in enumerate(candidates):
        if any(alias.strip().lower() == name for alias in c['aliases']):
            return candidates, i
    raise TextGroupNotFoundError(
        f"No group named {hint_str!r} found in this file.",
        candidates=candidates, hint=hint)


def describe_candidate(c, index):
    x0, y0, x1, y1 = c['bbox']
    w, h = x1 - x0, y1 - y0
    name = f" «{c['aliases'][0]}»" if c['aliases'] else ""
    return f"{index}.{name} ~{w:.0f}×{h:.0f}, {c['n_paths']} контур(ов)"


def replace_text(data, new_text, target=None,
                  primary_font=DEFAULT_PRIMARY_FONT,
                  fallback_font=DEFAULT_FALLBACK_FONT):
    """Replace the vector-path text inside the auto-detected (or explicitly
    targeted, via `target` = name or 1-based index) shape group with newly
    generated glyph outlines for `new_text`. Mutates and returns `data`, plus
    a report dict describing what happened. Raises TextGroupNotFoundError if
    no target can be resolved -- see locate_text_group()."""
    candidates, idx = locate_text_group(data, hint=target)
    chosen = candidates[idx]
    group = chosen['group']

    old_bbox = _group_raw_bbox(group)
    old_cx = (old_bbox[0] + old_bbox[2]) / 2.0
    old_cy = (old_bbox[1] + old_bbox[3]) / 2.0
    old_w = old_bbox[2] - old_bbox[0]
    old_h = old_bbox[3] - old_bbox[1]

    # max_width=old_w keeps a long replacement string from simply being
    # scaled to the original height and left to run however wide it wants
    # (which is what made long text overflow/overlap and look broken). The
    # helper picks whichever of the height-fit or width-fit scale is
    # smaller, so text that's already short enough is unaffected, and text
    # that's too long comes out proportionally smaller instead.
    new_shapes, new_w, new_h = text_to_lottie_path_shapes(
        new_text, primary_font, fallback_font,
        target_center=(old_cx, old_cy), target_height=old_h, max_width=old_w,
    )

    # Sanity check: the generated paths must actually land where the old ones
    # were (within a fraction of the local shape size). This guards against
    # coordinate/axis bugs that would otherwise silently ship a file whose
    # text renders thousands of units off the 512x512 canvas -- structurally
    # valid JSON, but invisible.
    check_xs = [v[0] for sh in new_shapes for v in sh['ks']['k']['v']]
    check_ys = [v[1] for sh in new_shapes for v in sh['ks']['k']['v']]
    new_cx = (min(check_xs) + max(check_xs)) / 2.0
    new_cy = (min(check_ys) + max(check_ys)) / 2.0
    tolerance = max(old_h, 1.0) * 2  # generous: a couple of letter-heights
    drift = ((new_cx - old_cx) ** 2 + (new_cy - old_cy) ** 2) ** 0.5
    if drift > tolerance:
        raise RuntimeError(
            f"Generated text center {(new_cx, new_cy)} drifted too far from "
            f"target {(old_cx, old_cy)} (drift={drift:.1f}, tolerance={tolerance:.1f}). "
            "Refusing to save a file whose text would likely render off-canvas."
        )

    # Keep every non-shape item (fill, stroke, transform...) exactly as-is;
    # only the path list is replaced. This guarantees identical position,
    # scale, rotation and animation to the original. Old shape geometry can
    # be direct 'sh' siblings (flat word) *or* nested per-glyph 'gr' groups
    # (see _collect_points) -- both are wiped, since the candidate was
    # scored as "this whole group is the text". Leaving old 'gr' letters in
    # place here is exactly what previously caused the original word to
    # keep rendering right alongside the newly-inserted replacement text.
    kept_items = [it for it in group['it'] if it.get('ty') not in ('sh', 'gr')]
    group['it'] = new_shapes + kept_items

    report = {
        'old_size': {'w': old_w, 'h': old_h},
        'new_size': {'w': new_w, 'h': new_h},
        'shrunk_to_fit': new_h < old_h - 1e-6,  # width cap kicked in, text sized below full height
        'num_new_contours': len(new_shapes),
        'target_label': ' > '.join(chosen['label']),
        'target_index': idx + 1,
        'num_candidates': len(candidates),
    }
    return data, report
