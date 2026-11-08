# TrunkVista logo

The mark is a **trunk** that forks at a router node into four **links**, each ending in a node: the backbone of a network opening out into a view of it.
The trunk and its node are the app's accent blue, the links use the app's own utilisation colours (low → high: green, lime, amber, orange). The thick
trunk and the thin links echo the wordmark: *Trunk* in bold, *Vista* in light. (It replaces the ring of link segments of the first name, ChannelView.)

## Files

| File | Use |
|---|---|
| `trunkvista-logo-dark.svg` / `.png` | mark + wordmark, for dark backgrounds |
| `trunkvista-logo-light.svg` / `.png` | mark + wordmark, for light backgrounds |
| `trunkvista-mark.svg` / `trunkvista-mark-1024.png` | the mark alone, transparent background |
| `trunkvista-mark-mono-dark.svg`, `trunkvista-mark-mono-light.svg` | one-colour mark (print, stamps, dark or light) |
| `trunkvista-icon.svg`, `trunkvista-icon-512/1024.png`, `icon-192.png`, `apple-touch-icon.png` | app icon on a dark tile |
| `favicon.svg`, `favicon.ico`, `favicon-16/32/48.png` | browser tab icon (a slightly heavier drawing that holds up at 16 px) |
| `social-preview.png` | 1280 × 640, for GitHub: repository → Settings → Social preview |
| `source/` | `tv_mark.py` draws the mark, `build_logo.py OUTDIR` writes every SVG (needs `uharfbuzz`, `fonttools` and the Inter font; the font folder is set at the top of the script), `render_png.py OUTDIR` makes the PNGs and the `.ico` (needs `playwright` and `pillow`). Then `python3 tools/embed_logo.py` puts the mark and the tab icons into `trunkvista.py` |

In the program, the page header and the sign-in page embed the heavier drawing of the mark inline (its trunk and node take the page's `--accent` colour) and the tab icon is `favicon.svg` plus `favicon-32.png`, both embedded in `trunkvista.py` as `data:` URIs because the page's security policy only allows images from `data:`. `tools/embed_logo.py` does the embedding.

The wordmark is converted to outlines, so the SVGs look the same on every machine; no font is needed.

## Colours

| Name | Hex | |
|---|---|---|
| Link green | `#22c55e` | usage 10–25 % |
| Lime | `#a3e635` | usage 25–40 % |
| Amber | `#facc15` | usage 40–55 % |
| Orange | `#fb923c` | usage 55–70 % |
| Node blue | `#4aa3ff` (`#1f7ae0` on light backgrounds) | the app's accent |
| Ink | `#0b0f14` | app background, text on light |
| Mist | `#e6edf3` | text on dark |

## Wordmark

*Trunk* in Inter Bold, *Vista* in Inter Light, tracking −1.2 %. Always written **TrunkVista**: one word, two capitals.

## Rules

- **Clear space:** at least the width of the trunk (about a seventh of the mark's height) on every side.
- **Smallest size:** the mark 16 px (use `favicon.svg`), the full logo 24 px high.
- Don't recolour the links, change their order, add or remove a link, or put the colour mark on a busy or mid-grey background; use a mono version there.
