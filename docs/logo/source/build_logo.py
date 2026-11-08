"""TrunkVista logo generator: python3 build_logo.py OUTDIR  -> SVG files (the wordmark is outlined: no font needed to display them)."""
import math, sys, os
import uharfbuzz as hb
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen

OUT = sys.argv[1]; os.makedirs(OUT, exist_ok=True)
FONTS = "/usr/share/fonts/opentype/inter/"
SCALE = ["#22c55e", "#a3e635", "#facc15", "#fb923c"]        # the app's usage scale, low -> high
BG, PANEL, LINE, TEXT, ACCENT = "#0b0f14", "#121923", "#243040", "#e6edf3", "#4aa3ff"

# ------------------------------------------------------------------ the mark: a "C" made of link segments (usage colours) around a node
CX = CY = 256; R_MID, SW = 150, 66; R_IN, R_OUT = R_MID - SW / 2, R_MID + SW / 2
HALF_OPEN, GAP = 36, 11                                      # opening of the C (degrees each side of east); parallel-edged gap between segments (px)
def pt(r, deg): a = math.radians(deg); return CX + r * math.cos(a), CY + r * math.sin(a)
def sector(a0, a1, off0, off1):
    """annulus sector from angle a0 to a1; off0/off1: shift each radial edge so the gap stays parallel (px, 0 = flat end)"""
    def ang(r, a, off, sign): return a + sign * math.degrees(math.asin(off / r)) if off else a
    p = lambda r, a: "%.2f,%.2f" % pt(r, a)
    ao0, ao1 = ang(R_OUT, a0, off0, 1), ang(R_OUT, a1, off1, -1)
    ai0, ai1 = ang(R_IN, a0, off0, 1), ang(R_IN, a1, off1, -1)
    return (f"M{p(R_OUT, ao0)}A{R_OUT},{R_OUT} 0 0 1 {p(R_OUT, ao1)}L{p(R_IN, ai1)}A{R_IN},{R_IN} 0 0 0 {p(R_IN, ai0)}Z")
def mark(colors=SCALE, centre=ACCENT, ids=""):
    a_start, a_end = HALF_OPEN, 360 - HALF_OPEN; n = len(colors); step = (a_end - a_start) / n
    out = []
    for i, c in enumerate(colors):
        a0, a1 = a_start + i * step, a_start + (i + 1) * step
        out.append(f'<path d="{sector(a0, a1, GAP / 2 if i else 0, GAP / 2 if i < n - 1 else 0)}" fill="{c}"/>')
    for ang, c in ((a_start, colors[0]), (a_end, colors[-1])):
        x, y = pt(R_MID, ang); out.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{SW / 2}" fill="{c}"/>')
    out.append(f'<circle cx="{CX}" cy="{CY}" r="40" fill="{centre}"/>')
    return "".join(out)
MARK_BOX = (CX - R_OUT, CY - R_OUT, 2 * R_OUT, 2 * R_OUT)   # square around the ring

def svg(w, h, body, vb=None, title="TrunkVista"):
    vb = vb or f"0 0 {w} {h}"
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" width="{w}" height="{h}" role="img" aria-label="{title}"><title>{title}</title>{body}</svg>\n'
def put(name, text): open(os.path.join(OUT, name), "w").write(text); print("wrote", name, len(text) // 1024 or 1, "KB")

# ------------------------------------------------------------------ app icon tile
TILE = (f'<defs><linearGradient id="t" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#151e2a"/><stop offset="1" stop-color="{BG}"/></linearGradient></defs>'
        f'<rect width="512" height="512" rx="112" fill="url(#t)"/><rect x="3" y="3" width="506" height="506" rx="109" fill="none" stroke="{LINE}" stroke-width="6"/>')
put("trunkvista-icon.svg", svg(512, 512, TILE + mark()))
put("trunkvista-mark.svg", svg(512, 512, mark(), vb="%.0f %.0f %.0f %.0f" % (MARK_BOX[0] - 12, MARK_BOX[1] - 12, MARK_BOX[2] + 24, MARK_BOX[3] + 24)))
put("trunkvista-mark-mono-dark.svg", svg(512, 512, mark(["#0b0f14"] * 4, "#0b0f14"), vb="%.0f %.0f %.0f %.0f" % (MARK_BOX[0] - 12, MARK_BOX[1] - 12, MARK_BOX[2] + 24, MARK_BOX[3] + 24)))
put("trunkvista-mark-mono-light.svg", svg(512, 512, mark(["#ffffff"] * 4, "#ffffff"), vb="%.0f %.0f %.0f %.0f" % (MARK_BOX[0] - 12, MARK_BOX[1] - 12, MARK_BOX[2] + 24, MARK_BOX[3] + 24)))

# ------------------------------------------------------------------ wordmark, outlined with HarfBuzz shaping (kerning) + fontTools outlines
def text_path(text, font_file, size, x0, baseline, tracking=0.0):
    data = open(FONTS + font_file, "rb").read(); face = hb.Face(data); font = hb.Font(face)
    buf = hb.Buffer(); buf.add_str(text); buf.guess_segment_properties(); hb.shape(font, buf, {"kern": True, "liga": False})
    tt = TTFont(FONTS + font_file); gs = tt.getGlyphSet(); order = tt.getGlyphOrder(); upem = face.upem; k = size / upem
    x = x0; parts = []
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
        pen = SVGPathPen(gs, ntos=lambda v: ("%.2f" % v).rstrip("0").rstrip("."))
        gs[order[info.codepoint]].draw(TransformPen(pen, (k, 0, 0, -k, x + pos.x_offset * k, baseline - pos.y_offset * k)))
        parts.append(pen.getCommands()); x += pos.x_advance * k + tracking * size
    return "".join(parts), x
def lockup(fg, view_fg, mark_body, height=160):
    size = 104; base_y = 100; mark_h = 132
    s = mark_h / (MARK_BOX[2] + 24); gx = 0
    mark_g = f'<g transform="translate({-(MARK_BOX[0] - 12) * s:.2f},{(height - mark_h) / 2 - (MARK_BOX[1] - 12) * s:.2f}) scale({s:.4f})">{mark_body}</g>'
    x0 = mark_h + 34
    p1, x1 = text_path("Trunk", "Inter-Bold.otf", size, x0, base_y + 14, -0.012)
    p2, x2 = text_path("Vista", "Inter-Light.otf", size, x1, base_y + 14, -0.012)
    w = int(math.ceil(x2 + 6))
    body = mark_g + f'<path d="{p1}" fill="{fg}"/><path d="{p2}" fill="{view_fg}"/>'
    return w, height, body
w, h, body = lockup(TEXT, TEXT, mark()); put("trunkvista-logo-dark.svg", svg(w, h, body))        # for dark backgrounds
w, h, body = lockup(BG, "#3a4656", mark(SCALE, "#1f7ae0")); put("trunkvista-logo-light.svg", svg(w, h, body))   # for light backgrounds

# ------------------------------------------------------------------ favicon: the same mark, slightly heavier and with a wider opening, no hairline border (reads better at 16-48 px)
R_MID, SW, GAP, HALF_OPEN = 148, 76, 13, 44; R_IN, R_OUT = R_MID - SW / 2, R_MID + SW / 2
put("favicon.svg", svg(512, 512, TILE.split('<rect x="3"')[0] + mark().replace('r="40" fill="%s"' % ACCENT, 'r="42" fill="%s"' % ACCENT)))
