# Changing the design

[← README](../README.md)


**Quick, on the server – `custom.css`.** Your own style rules, added after the built-in ones. The file lives in the data folder, so updates never touch it, and it is read at every page load (edit, press F5 – no restart).

```
sudo nano /var/lib/trunkvista/custom.css
```
```css
:root { --accent: #ff9f1a; }                    /* colours of the interface, in both themes */
:root[data-theme=dark] { --bg: #05080c; }       /* only in the dark theme (see Light and dark below) */
body  { font-size: 15px; }
```

| Variable | Used for |
|---|---|
| `--bg` | page background, input fields |
| `--panel`, `--panel2` | header, side panels, dialogs, cards |
| `--line` | borders and separators |
| `--text`, `--muted` | normal and secondary text |
| `--accent` | selected items, links, highlights |
| `--ok`, `--bad`, `--warn` | green / red / amber in the interface (badges, buttons) |

This restyles the **interface** (header, panels, buttons, text, fonts, sizes) and, through the variables listed under *Light and dark* below, the frame, labels and graph colours of the map. The status and usage colours – link colours, the usage scale, device status dots – are the same in both themes, are drawn by the program and are changed in a release, see below. Press F12 in the browser to find the element and class names for finer rules. Delete the file to go back to the built-in look. Size limit 256 KB.

**Light and dark.** The button in the header (◐ Auto, ☀ Light, ☾ Dark) or the **T** key switches the theme; it is remembered in that browser. **Dark is the default**, so a wall display does not turn white by itself when you update. *Auto* follows the system setting (and changes with it while the page is open). For a display you cannot click, put the theme in its address – it is remembered like the other choices:

```
http://dashboard:8088/#kiosk=1&play=wall&theme=light      # theme=dark | light | auto
```

`custom.css` works in both themes. A plain `:root { ... }` rule applies to **both**; to change one theme only, use `:root[data-theme=light] { ... }` or `:root[data-theme=dark] { ... }` (these win over the plain rule). Besides the variables above, the map has its own:

| Variable | Used for |
|---|---|
| `--on-accent` | text on accent-coloured buttons |
| `--hl`, `--hover` | selected and hovered rows and menu items |
| `--nodefill`, `--sel` | the inside of a device on the map, and its outline when selected |
| `--halo`, `--iftag`, `--lname` | outline of map labels, interface names beside a link, link name |
| `--mapbg1`, `--mapbg2`, `--mapgrid` | the map background (centre, edge) and the grid shown while editing |
| `--gab`, `--gba` | the two lines of the traffic graph (first device to second, and back) |

The complete list is the first two rules of the page's CSS (`:root` for dark, `:root:where([data-theme=light])` for light): the two palettes define the same names, and a test keeps them that way.

**The logo.** The header shows the mark (a blue trunk that forks into four links in the usage colours, each ending in a node) and the browser tab uses the same drawing as its icon. The trunk and its node follow `--accent`, so a changed accent colour changes them too; to remove the mark from the header, add `.logo svg { display: none; }` to `custom.css`. All logo files (SVG, PNG, favicon, social preview) and the rules for using them are in [`docs/logo`](logo/README.md).

**Bigger changes, for everybody – a release.** The whole page (HTML, CSS, JavaScript) is the `PAGE` text in `trunkvista.py`.

1. Change it and look: `python3 trunkvista.py --demo` (reload the browser after each change).
2. Write what changed under `## [Unreleased]` in `CHANGELOG.md`.
3. Release it ([Patching and releasing](maintainer.md#patching-and-releasing-maintainer)). Every server then offers it with `sudo trunkvista-update`.
