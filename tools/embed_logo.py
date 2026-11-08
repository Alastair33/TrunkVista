#!/usr/bin/env python3
"""Puts the logo into trunkvista.py: the mark in the page header (and so on the sign-in page), and the two tab icons (data: URIs, because the page's
security policy only allows images from data:).  Run it after docs/logo has been regenerated (docs/logo/source/build_logo.py + render_png.py),
then rebuild the standalone demo:  python3 tools/build_demo.py trunkvista_demo.html

    python3 tools/embed_logo.py          # rewrites trunkvista.py; prints what changed
    python3 tools/embed_logo.py --check  # changes nothing; exit status 1 when trunkvista.py does not have the logo of docs/logo (a test runs this)
"""
import base64
import re
import sys
import urllib.parse
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "docs" / "logo" / "source"))
import tv_mark  # noqa: E402

page = root / "trunkvista.py"
src = page.read_text(encoding="utf-8")
before = src

# 1. the header mark: the heavier drawing (it is only 26 px high), its trunk and node follow the page's accent colour
body, box = tv_mark.mark(heavy=True, themed=True)
svg = f'<svg viewBox="{tv_mark.square_vb(box, 4)}" aria-hidden="true" focusable="false">{body}</svg>'
src, n1 = re.subn(r'(<span class="logo">\s*)<svg\b.*?</svg>', lambda m: m.group(1) + svg, src, count=1, flags=re.S)

# 2. the tab icons
png = base64.b64encode((root / "docs" / "logo" / "favicon-32.png").read_bytes()).decode()
src, n2 = re.subn(r'(<link rel="icon" type="image/png" sizes="32x32" href=")data:image/png;base64,[^"]*', lambda m: m.group(1) + "data:image/png;base64," + png, src, count=1)
fav = (root / "docs" / "logo" / "favicon.svg").read_text(encoding="utf-8")
fav = re.sub(r"<title>.*?</title>", "", fav, flags=re.S)
root_tag = re.match(r"<svg\b[^>]*>", fav).group(0)       # only the root element loses its size and labels: the tile's own width and height must stay
fav = (re.sub(r'\s(?:width|height|role|aria-label)="[^"]*"', "", root_tag) + fav[len(root_tag):]).strip().replace('"', "'")
enc = "data:image/svg+xml," + urllib.parse.quote(fav, safe="/:=',()")
src, n3 = re.subn(r'(<link rel="icon" type="image/svg\+xml" sizes="any" href=")data:image/svg\+xml,[^"]*', lambda m: m.group(1) + enc, src, count=1)

if (n1, n2, n3) != (1, 1, 1):
    sys.exit(f"embed_logo.py: expected to replace the header mark, the PNG icon and the SVG icon once each, replaced {n1}, {n2}, {n3}")
if src == before:
    print("trunkvista.py already has this logo")
elif "--check" in sys.argv[1:]:
    sys.exit("embed_logo.py: trunkvista.py does not have the logo of docs/logo; run  python3 tools/embed_logo.py  and rebuild the demo page")
else:
    page.write_text(src, encoding="utf-8")
    print(f"trunkvista.py: header mark {len(svg)} bytes, tab icons {len(png) + len(enc)} bytes")
