"""TrunkVista mark: a trunk (blue) that forks into four link spokes (usage colours) ending in nodes. 512-unit drawing."""
import math
SCALE = ["#22c55e", "#a3e635", "#facc15", "#fb923c"]      # the app's usage scale, low -> high
BLUE, BLUE_LIGHT = "#4aa3ff", "#1f7ae0"

def donut(cx, cy, ro, ri, fill):
    return (f'<path fill-rule="evenodd" fill="{fill}" d="M{cx-ro:.2f},{cy:.2f}a{ro},{ro} 0 1 0 {2*ro},0a{ro},{ro} 0 1 0 {-2*ro},0Z'
            f'M{cx-ri:.2f},{cy:.2f}a{ri},{ri} 0 1 0 {2*ri},0a{ri},{ri} 0 1 0 {-2*ri},0Z"/>')

class P:   # drawing parameters
    fx, fy = 256, 300          # the fork
    L = 212                    # fork -> centre of a node
    angs = (-61, -21, 21, 61)
    sw, ro, ri = 30, 38, 15    # spoke width, node outer / inner radius
    tw, knob, bottom = 56, 40, 440
HEAVY = dict(sw=40, ro=44, ri=0, tw=68, knob=48)

def mark(colors=SCALE, trunk=BLUE, heavy=False, knob=True, themed=False):
    """themed=True: the trunk and its node carry the classes "trunk" / "node", so the page can colour them with its accent."""
    p = {k: v for k, v in vars(P).items() if not k.startswith("_")}
    if heavy: p.update(HEAVY)
    out = []
    ends = []
    for ang in p["angs"]:
        a = math.radians(ang); ends.append((p["fx"] + p["L"] * math.sin(a), p["fy"] - p["L"] * math.cos(a), a))
    for (tx, ty, a), c in zip(ends, colors):
        # the spoke stops inside the ring of the node, so nothing shows through the hole
        k = p["L"] - (p["ro"] + p["ri"]) / 2 if p["ri"] else p["L"]
        ex, ey = p["fx"] + k * math.sin(a), p["fy"] - k * math.cos(a)
        out.append(f'<path d="M{p["fx"]},{p["fy"]} L{ex:.2f},{ey:.2f}" stroke="{c}" stroke-width="{p["sw"]}" stroke-linecap="{"butt" if p["ri"] else "round"}" fill="none"/>')
    for (tx, ty, a), c in zip(ends, colors):
        out.append(donut(tx, ty, p["ro"], p["ri"], c) if p["ri"] else f'<circle cx="{tx:.2f}" cy="{ty:.2f}" r="{p["ro"]}" fill="{c}"/>')
    cls_trunk, cls_node = (' class="trunk"', ' class="node"') if themed else ("", "")      # (not inside the f-strings: a backslash there is a SyntaxError before Python 3.12)
    out.append(f'<path d="M{p["fx"]},{p["fy"]} L{p["fx"]},{p["bottom"]}" stroke="{trunk}" stroke-width="{p["tw"]}" stroke-linecap="round" fill="none"{cls_trunk}/>')
    if knob: out.append(f'<circle cx="{p["fx"]}" cy="{p["fy"]}" r="{p["knob"]}" fill="{trunk}"{cls_node}/>')
    # box of the drawing
    xs = [e[0] for e in ends]; ys = [e[1] for e in ends]
    box = (min(xs) - p["ro"], min(ys) - p["ro"], max(xs) + p["ro"], p["bottom"] + p["tw"] / 2)
    return "".join(out), box

def svg(w, h, body, vb, title="TrunkVista"):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" width="{w}" height="{h}" role="img" aria-label="{title}"><title>{title}</title>{body}</svg>\n'

def square_vb(box, pad=24):
    x0, y0, x1, y1 = box; w, h = x1 - x0, y1 - y0; s = max(w, h) + 2 * pad
    return "%.1f %.1f %.1f %.1f" % ((x0 + x1) / 2 - s / 2, (y0 + y1) / 2 - s / 2, s, s)
