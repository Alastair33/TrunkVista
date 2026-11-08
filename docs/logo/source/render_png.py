"""PNG + ICO exports of the SVGs in OUT (Chromium draws them)."""
import os, sys, re
from playwright.sync_api import sync_playwright
from PIL import Image
OUT = sys.argv[1]
def read(n): return open(os.path.join(OUT, n)).read()
def render(p, svg_text, w, h, name, transparent=True, scale=1):
    page = p.new_page(viewport={"width": w, "height": h}, device_scale_factor=scale)
    svg_text = re.sub(r'width="\d+" height="\d+"', f'width="{w}" height="{h}"', svg_text, count=1)
    page.set_content(f'<body style="margin:0;background:transparent">{svg_text}</body>')
    page.screenshot(path=os.path.join(OUT, name), omit_background=transparent, clip={"x": 0, "y": 0, "width": w, "height": h})
    page.close(); print("png", name)
with sync_playwright() as pw:
    b = pw.chromium.launch()
    render(b, read("trunkvista-mark.svg"), 1024, 1024, "trunkvista-mark-1024.png")
    for s in (256, 512, 1024): render(b, read("trunkvista-icon.svg"), s, s, f"trunkvista-icon-{s}.png")
    render(b, read("trunkvista-icon.svg"), 192, 192, "icon-192.png")
    render(b, read("favicon.svg"), 180, 180, "apple-touch-icon.png", transparent=False)
    for s in (16, 32, 48): render(b, read("favicon.svg"), s, s, f"favicon-{s}.png")
    for mode in ("dark", "light"):
        t = read(f"trunkvista-logo-{mode}.svg"); m = re.search(r'viewBox="0 0 (\d+) (\d+)"', t); w, h = int(m[1]), int(m[2])
        render(b, t, 2400, round(2400 * h / w), f"trunkvista-logo-{mode}.png")
    page = b.new_page(viewport={"width": 1280, "height": 640}); page.goto("file://" + os.path.abspath(os.path.join(OUT, "social-preview.html"))); page.wait_for_timeout(300)
    page.screenshot(path=os.path.join(OUT, "social-preview.png")); page.close(); print("png social-preview.png")
    b.close()
imgs = [Image.open(os.path.join(OUT, f"favicon-{s}.png")).convert("RGBA") for s in (16, 32, 48)]
imgs[2].save(os.path.join(OUT, "favicon.ico"), sizes=[(16, 16), (32, 32), (48, 48)], append_images=imgs[:2]); print("ico favicon.ico")
os.remove(os.path.join(OUT, "social-preview.html"))
