"""TrunkVista logo generator:  python3 build_logo.py OUTDIR  ->  every SVG and PNG of the logo set.
The mark: a trunk (the accent blue) forks at a router node into four links, coloured with the app's usage scale (low -> high), each ending in a node.
The wordmark is outlined (HarfBuzz shaping + fontTools outlines of Inter): the SVGs show the same on every machine. PNGs are drawn with Playwright/Chromium."""
import math, os, sys
import uharfbuzz as hb
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tv_mark import mark, svg, square_vb, SCALE, BLUE, BLUE_LIGHT

OUT = sys.argv[1]; os.makedirs(OUT, exist_ok=True)
FONTS = "/usr/share/fonts/opentype/inter/"
BG, LINE, TEXT, MUTED = "#0b0f14", "#243040", "#e6edf3", "#8b98a8"
def put(name, text): open(os.path.join(OUT, name), "w").write(text); print("wrote", name)

# ---------------------------------------------------------------- the mark alone (transparent), and one-colour versions
body, box = mark(); vb = square_vb(box, 16)
put("trunkvista-mark.svg", svg(512, 512, body, vb))
mono = lambda c: mark([c] * 4, c)[0]
put("trunkvista-mark-mono-dark.svg", svg(512, 512, mono("#0b0f14"), vb))
put("trunkvista-mark-mono-light.svg", svg(512, 512, mono("#ffffff"), vb))

# ---------------------------------------------------------------- app icon (dark tile) and favicon (heavier drawing, no hairline border)
def tile_with(body_, box_, border=True, fill_h=330):
    x0, y0, x1, y1 = box_; s = fill_h / (y1 - y0)
    tx = 256 - s * (x0 + x1) / 2; ty = 256 - s * (y0 + y1) / 2
    t = (f'<defs><linearGradient id="t" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#151e2a"/><stop offset="1" stop-color="{BG}"/></linearGradient></defs>'
         f'<rect width="512" height="512" rx="112" fill="url(#t)"/>')
    if border: t += f'<rect x="3" y="3" width="506" height="506" rx="109" fill="none" stroke="{LINE}" stroke-width="6"/>'
    return t + f'<g transform="translate({tx:.2f},{ty:.2f}) scale({s:.4f})">{body_}</g>'
put("trunkvista-icon.svg", svg(512, 512, tile_with(body, box), "0 0 512 512"))
hb_body, hb_box = mark(heavy=True)
put("favicon.svg", svg(512, 512, tile_with(hb_body, hb_box, border=False, fill_h=372), "0 0 512 512"))

# ---------------------------------------------------------------- the wordmark, outlined
def text_path(text, font_file, size, x0, baseline, tracking=0.0):
    data = open(FONTS + font_file, "rb").read(); face = hb.Face(data); font = hb.Font(face)
    buf = hb.Buffer(); buf.add_str(text); buf.guess_segment_properties(); hb.shape(font, buf, {"kern": True, "liga": False})
    tt = TTFont(FONTS + font_file); gs = tt.getGlyphSet(); order = tt.getGlyphOrder(); k = size / face.upem
    x = x0; parts = []
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions, strict=True):
        pen = SVGPathPen(gs, ntos=lambda v: ("%.2f" % v).rstrip("0").rstrip("."))
        gs[order[info.codepoint]].draw(TransformPen(pen, (k, 0, 0, -k, x + pos.x_offset * k, baseline - pos.y_offset * k)))
        parts.append(pen.getCommands()); x += pos.x_advance * k + tracking * size
    return "".join(parts), x
def lockup(fg, view_fg, trunk, height=170, mark_h=146, size=104, gap=30):
    body_, box_ = mark(trunk=trunk); x0_, y0_, x1_, y1_ = box_
    s = mark_h / (y1_ - y0_); mw = (x1_ - x0_) * s
    mg = f'<g transform="translate({-x0_ * s:.2f},{(height - mark_h) / 2 - y0_ * s:.2f}) scale({s:.4f})">{body_}</g>'
    base = height / 2 + 0.364 * size                         # Inter's cap height is 0.727 em: centre the capitals on the mark
    p1, x1 = text_path("Trunk", "Inter-Bold.otf", size, mw + gap, base, -0.012)
    p2, x2 = text_path("Vista", "Inter-Light.otf", size, x1, base, -0.012)
    return int(math.ceil(x2 + 4)), height, mg + f'<path d="{p1}" fill="{fg}"/><path d="{p2}" fill="{view_fg}"/>'
w, h, b = lockup(TEXT, TEXT, BLUE); put("trunkvista-logo-dark.svg", svg(w, h, b, f"0 0 {w} {h}"))
w, h, b = lockup(BG, "#3a4656", BLUE_LIGHT); put("trunkvista-logo-light.svg", svg(w, h, b, f"0 0 {w} {h}"))
LOCK_W, LOCK_H = w, h

# ---------------------------------------------------------------- social preview (1280 x 640, GitHub: Settings -> Social preview)
dark_lock = svg(LOCK_W, LOCK_H, lockup(TEXT, TEXT, BLUE)[2], f"0 0 {LOCK_W} {LOCK_H}").replace(f'width="{LOCK_W}" height="{LOCK_H}"', f'width="{LOCK_W * 1.12:.0f}" height="{LOCK_H * 1.12:.0f}"')
bands = "".join(f'<i style="background:{c}"></i>' for c in SCALE)
put("social-preview.html", f'''<!doctype html><meta charset=utf-8><style>
html,body{{margin:0}} body{{width:1280px;height:640px;background:radial-gradient(ellipse at 50% 40%,#16202e 0%,{BG} 70%);position:relative;overflow:hidden;font-family:Inter,system-ui,sans-serif}}
.c{{position:absolute;left:0;right:0;top:150px;display:flex;flex-direction:column;align-items:center;gap:34px}}
.t{{color:{MUTED};font-size:34px;letter-spacing:.2px}} .b{{position:absolute;left:0;right:0;bottom:0;height:10px;display:flex}} .b i{{flex:1}}
</style><div class=c>{dark_lock}<div class=t>A weathermap dashboard for Zabbix 7.0</div></div><div class=b>{bands}</div>''')
