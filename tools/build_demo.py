"""Build the standalone demo page: the real dashboard page + an in-browser backend (tools/demo_shim.js).
Usage:  python3 tools/build_demo.py channelview_demo.html [channelview.py] [demo_shim.js]      (defaults: the files of this repository)"""
import importlib.util
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
py = sys.argv[2] if len(sys.argv) > 2 else str(HERE.parent / "channelview.py")
shimp = Path(sys.argv[3]) if len(sys.argv) > 3 else HERE / "demo_shim.js"
spec = importlib.util.spec_from_file_location("channelview", py)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
page = mod.PAGE
shim = shimp.read_text(encoding="utf-8")
assert "</script" not in shim.lower(), "shim must not contain a closing script tag"
marker = '<script>\n"use strict";\nconst EDIT_TOKEN'
assert page.count(marker) == 1
page = page.replace(marker, "<script>\n" + shim + "</script>\n" + marker)
page = page.replace('const EDIT_TOKEN = "__EDIT_TOKEN__";', 'const EDIT_TOKEN = "demo";')
page, n = re.subn(r"<title>.*?</title>", "<title>ChannelView - standalone demo</title>", page, count=1)
assert n == 1 and "__EDIT_TOKEN__" not in page
Path(sys.argv[1]).write_text(page, encoding="utf-8")
print("wrote", sys.argv[1], len(page), "bytes")
