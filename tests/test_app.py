"""Basic checks of channelview.py itself: --version, demo mode answers, --init, and the persistent edit token (--token-file) that the
Debian service uses.   Run:  python3 -m unittest discover -s tests -v      (standard library only; starts the server on free local ports)"""
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "channelview.py"


def app_version():
    return re.search(r'^VERSION = "([^"]+)"', APP.read_text(encoding="utf-8"), re.M).group(1)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """The server as a child process; `with Server(...) as s:` waits until it answers and stops it afterwards."""

    def __init__(self, tmp, *args):
        self.port = free_port()
        self.log = Path(tmp) / "server.log"
        self.args = [sys.executable, str(APP), "--no-browser", "--port", str(self.port), *args]

    def __enter__(self):
        self.fh = open(self.log, "w")
        self.p = subprocess.Popen(self.args, stdout=self.fh, stderr=subprocess.STDOUT)
        for _ in range(100):
            if self.p.poll() is not None:
                break
            try:
                urllib.request.urlopen(self.url("/"), timeout=1).read()
                return self
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)
        self.__exit__()
        raise AssertionError("server did not start:\n" + self.log.read_text())

    def __exit__(self, *exc):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(5)
            except subprocess.TimeoutExpired:
                self.p.kill()
        self.fh.close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path, token=None):
        req = urllib.request.Request(self.url(path), headers={"X-Edit-Token": token} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def text(self):
        self.fh.flush()
        return self.log.read_text()


class VersionAndDemo(unittest.TestCase):
    def test_version_flag(self):
        out = subprocess.run([sys.executable, str(APP), "--version"], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, "channelview.py " + app_version())
        self.assertRegex(app_version(), r"^\d+\.\d+\.\d+([-.~][0-9A-Za-z.]+)?$", "VERSION must look like 1.2.3 or 1.2.3-rc.1")

    def test_demo_answers(self):
        with tempfile.TemporaryDirectory() as tmp, Server(tmp, "--demo", "--data-dir", tmp + "/d") as s:
            code, body = s.get("/")
            self.assertEqual(code, 200)
            self.assertIn(b"<title>", body)
            code, body = s.get("/api/layout")
            lay = json.loads(body)
            self.assertTrue(lay["demo"] and lay["layout"]["tabs"])
            self.assertEqual(lay["defaults"]["graph_minutes"], 60)
            tab = lay["layout"]["tabs"][0]["id"]
            code, body = s.get(f"/api/view?tab={tab}&every=30&cid=cunit")
            self.assertEqual(code, 200)
            self.assertIsInstance(json.loads(body), dict)
            self.assertIn(f"ChannelView {app_version()} running", s.text())

    def test_logo_in_header_and_tab_icon(self):
        """The ring logo is inline in the header and the tab icons are data: URIs (the page policy only allows img-src data:)."""
        import base64
        import urllib.parse
        import xml.etree.ElementTree as ET
        with tempfile.TemporaryDirectory() as tmp, Server(tmp, "--demo", "--data-dir", tmp + "/d") as s:
            html = s.get("/")[1].decode()
        m = re.search(r'<span class="logo">(<svg .*?</svg>)ChannelView</span>', html, re.S)
        self.assertTrue(m, "header must carry the inline mark followed by the name, written ChannelView")
        def tags(root, name):   # inline SVG in HTML has no xmlns, the data: URI one has
            return [e for e in root.iter() if e.tag.split("}")[-1] == name]
        header = ET.fromstring(m.group(1))
        self.assertEqual(len(tags(header, "path")), 4, "four link segments")
        self.assertEqual([e.get("class") for e in header.iter() if e.get("class")], ["node"], "centre node takes the page accent via CSS")
        icons = re.findall(r'<link rel="icon" type="([^"]+)"[^>]*href="(data:[^"]+)"', html)
        self.assertEqual({t for t, _ in icons}, {"image/png", "image/svg+xml"})
        for kind, uri in icons:
            if kind == "image/png":
                self.assertEqual(base64.b64decode(uri.split(",", 1)[1])[:8], b"\x89PNG\r\n\x1a\n")
            else:
                svg = ET.fromstring(urllib.parse.unquote(uri.split(",", 1)[1]))
                rects = tags(svg, "rect")
                self.assertEqual(len(rects), 1, "the tile")
                rect = rects[0]
                self.assertEqual((rect.get("width"), rect.get("height")), ("512", "512"), "tile must keep its size or it disappears")
                self.assertEqual(len(tags(svg, "path")), 4)
        self.assertNotIn("&#9670;", html, "old diamond glyph is gone")
        self.assertNotIn('href="data:,"', html)

    def test_devices_tab_style_heatmap_is_kept_and_unknown_styles_fall_back(self):
        """A devices tab can default to the heatmap view; the layout keeps list / cards / heatmap and turns anything else into cards."""
        with tempfile.TemporaryDirectory() as tmp, Server(tmp, "--demo", "--data-dir", tmp + "/d") as s:
            html = s.get("/")[1].decode()
            self.assertIn('id: "dev-mode-heatmap"', html, "the view switch offers the heatmap")
            token = re.search(r'const EDIT_TOKEN = "([^"]+)"', html).group(1)
            layout = json.loads(s.get("/api/layout")[1])["layout"]
            tab = next(t for t in layout["tabs"] if t["type"] == "devices")
            for style, want in (("heatmap", "heatmap"), ("list", "list"), ("cards", "cards"), ("bogus", "cards"), (["heatmap"], "cards"), (None, "cards")):
                tab["style"] = style
                req = urllib.request.Request(s.url("/api/layout"), data=json.dumps({"layout": layout}).encode(),
                                             headers={"Content-Type": "application/json", "X-Edit-Token": token})
                with urllib.request.urlopen(req, timeout=5) as r:
                    self.assertEqual(r.status, 200)
                saved = next(t for t in json.loads(s.get("/api/layout")[1])["layout"]["tabs"] if t["id"] == tab["id"])
                self.assertEqual(saved["style"], want, f"style {style!r}")

    def test_init_writes_owner_only_config_and_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = [sys.executable, str(APP), "--init", "--data-dir", tmp + "/d"]
            subprocess.run(cmd, check=True, capture_output=True)
            cfg = Path(tmp, "d", "config.json")
            self.assertEqual(json.loads(cfg.read_text())["poll_seconds"], 30)
            self.assertEqual(stat.S_IMODE(cfg.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(cfg.parent.stat().st_mode), 0o700)
            again = subprocess.run(cmd, capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0)
            self.assertIn("not overwriting", again.stderr + again.stdout)


def load_app_module():
    """channelview.py as a module, to test its classes directly (the file only starts a server when run as a program)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("channelview_under_test", APP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class Hardening(unittest.TestCase):
    """Things found in a code review: who gets the edit token, idle connections, odd answers from the Zabbix address, file modes."""

    def test_a_request_through_a_reverse_proxy_does_not_get_the_edit_token(self):
        with tempfile.TemporaryDirectory() as tmp, Server(tmp, "--demo", "--data-dir", tmp + "/d") as s:
            def token_in_page(headers):
                req = urllib.request.Request(s.url("/"), headers=headers)
                with urllib.request.urlopen(req, timeout=5) as r:
                    return re.search(r'const EDIT_TOKEN = "([^"]*)"', r.read().decode()).group(1)
            self.assertTrue(token_in_page({}), "opened on this computer: the token is built in")
            for h in ("X-Forwarded-For", "X-Real-IP", "Forwarded", "Via", "X-Forwarded-Host"):
                self.assertEqual(token_in_page({h: "203.0.113.9"}), "", h + ": a proxied visitor is not 'this computer'")
            token = token_in_page({})
            code, _ = s.get("/api/connections", token=token)
            self.assertEqual(code, 200, "the token itself still works wherever it is sent from")

    def test_the_page_may_be_opened_through_ipv6_loopback(self):
        with tempfile.TemporaryDirectory() as tmp, Server(tmp, "--demo", "--data-dir", tmp + "/d") as s:
            for host, want in ((f"[::1]:{s.port}", 200), (f"localhost:{s.port}", 200), (f"127.0.0.1:{s.port}", 200), ("evil.example:80", 403), (f"[::2]:{s.port}", 403)):
                req = urllib.request.Request(s.url("/api/layout"), headers={"Host": host})
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        got = r.status
                except urllib.error.HTTPError as e:
                    got = e.code
                self.assertEqual(got, want, host)

    def test_an_address_that_answers_with_other_json_is_reported_as_a_wrong_url(self):
        import http.server
        import threading
        mod = load_app_module()
        body = {"v": b"[]"}

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body["v"])))
                self.end_headers()
                self.wfile.write(body["v"])

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        client = mod.ZabbixClient({"zabbix": {"url": f"http://127.0.0.1:{srv.server_port}", "token": "t", "timeout": 5}})
        for payload in (b"[]", b'"ok"', b"42", b"null"):
            body["v"] = payload
            with self.assertRaises(mod.ZabbixError, msg=payload) as cm:
                client.version()
            self.assertEqual(cm.exception.kind, "url", payload)
        body["v"] = b'{"jsonrpc":"2.0","result":"7.0.1","id":1}'
        self.assertEqual(client.version(), "7.0.1", "a real answer still works")

    def test_connections_file_is_owner_only_from_the_first_byte(self):
        mod = load_app_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "connections.json")
            conns = mod.Connections(path, {"zabbix": {}, "match": {}})
            real_chmod = os.chmod
            mod.os.chmod = lambda *a, **k: None      # the mode must come from how the file is created, not from a chmod afterwards
            try:
                conns.upsert({"name": "z", "url": "https://zabbix.example", "auth": "token", "token": "SECRET"})
            finally:
                mod.os.chmod = real_chmod
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIn("SECRET", path.read_text())
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["connections.json"], "no temporary file left behind")
            Path(tmp, "connections.tmp").write_text("old")
            os.chmod(Path(tmp, "connections.tmp"), 0o666)      # a leftover with loose rights from an earlier crash
            conns.upsert({"name": "y", "url": "https://zabbix2.example", "auth": "token", "token": "S2"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, "a leftover temporary file does not loosen it")

    def test_a_connection_that_sends_nothing_is_dropped(self):
        import threading
        mod = load_app_module()

        class Quick(mod.Handler):
            timeout = 1
        srv = mod.DashServer(("127.0.0.1", 0), Quick)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=10) as c:
            t0 = time.time()
            self.assertEqual(c.recv(10), b"", "the server closed the silent connection")
            self.assertLess(time.time() - t0, 5)
        self.assertEqual(mod.Handler.timeout, 60, "and the real server waits a minute")


class Theme(unittest.TestCase):
    """The light / dark colour theme lives in the CSS variables of the page; these checks keep the two palettes complete and the page free of stray colours.
    (How it looks and behaves in a browser is covered by tools/ browser suites run by hand; these need no browser.)"""

    @classmethod
    def setUpClass(cls):
        cls.page = load_app_module().PAGE
        cls.css = re.search(r"<style>(.*?)</style>", cls.page, re.S).group(1)

    def palette(self, selector):
        m = re.search(r"^" + re.escape(selector) + r"\{(.*?)\}$", self.css, re.M)
        self.assertTrue(m, selector + " block missing")
        return dict(re.findall(r"(--[a-z0-9-]+|color-scheme):([^;]+)", m.group(1)))

    def test_light_palette_sets_every_variable_the_dark_one_does(self):
        dark, light = self.palette(":root"), self.palette(":root:where([data-theme=light])")
        self.assertEqual(sorted(dark), sorted(light), "a variable missing from one theme would fall back to the other theme's colour")
        self.assertEqual((dark["color-scheme"], light["color-scheme"]), ("dark", "light"), "native controls, scrollbars and form widgets follow")
        self.assertGreater(len(dark), 60)

    def test_every_variable_the_page_uses_is_defined(self):
        defined = set(self.palette(":root"))
        used = set(re.findall(r"var\((--[a-z0-9-]+)", self.page))
        self.assertFalse(used - defined, "undefined variables: %s" % sorted(used - defined))
        script_names = set(re.findall(r'getPropertyValue\("--" \+ k\)', self.page))
        tv = re.search(r"const TV_VARS = \[(.*?)\];", self.page, re.S).group(1)
        for name in re.findall(r'"([a-z0-9-]+)"', tv):
            self.assertIn("--" + name, defined, "the map reads --%s from the theme" % name)
        self.assertTrue(script_names, "the script reads its colours from the theme")

    def test_light_palette_does_not_beat_custom_css(self):
        """:where() keeps the light block at the specificity of :root, so a custom.css that is inlined later and sets :root{--accent:..} wins in both themes."""
        self.assertIn(":root:where([data-theme=light]){", self.css)
        self.assertLess(self.css.index(":root{"), self.css.index(":root:where([data-theme=light]){"), "the light block comes after the defaults")

    def test_no_stray_colours_in_the_css(self):
        """Colours belong in the palette lines (or on a status fill that is the same in both themes). A new rule with a literal #0b0f14 would look right in
        dark mode and wrong in light mode."""
        allowed = {"#fff", "#b91c1c", "#ef4444", "#dc2626", "#08121c", "#00000040",      # red button, text on the severity badge, the thin ring on a heatmap tile
                   "#22c55e", "#a3e635", "#facc15", "#fb923c"}                           # the four arcs of the logo
        stray = []
        for line in self.css.split("\n"):
            if line.startswith(":root{") or line.startswith(":root:where("):
                continue
            for c in re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", line):
                if c.lower() not in allowed:
                    stray.append((c, line[:70]))
        self.assertEqual(stray, [])

    def test_theme_script_runs_before_the_page_and_the_button_is_in_the_header(self):
        head = self.page[: self.page.index("<style>")]
        self.assertIn("var CVT = (function", head, "must run in <head>, before the first paint, or the page flashes the wrong palette")
        self.assertIn('setAttribute("data-theme"', head)
        self.assertIn('"channelview-theme"', head)
        self.assertIn('<button id="btn-theme"', self.page)
        self.assertRegex(self.page, r'k === "t"\) \$\("#btn-theme"\)\.click\(\)', "T switches the theme")
        self.assertIn('mode: "dark"', head, "dark stays the default: a wall display must not turn white by itself when the dashboard is updated")

    def test_text_on_a_coloured_fill_picks_the_better_contrast(self):
        """textOn() used a brightness cut-off that put light text on mid greens and oranges at about 2:1."""
        src = re.search(r"function textOn\(hex\) \{.*?\n\}", self.page, re.S).group(0)
        self.assertIn("0.2126", src, "relative luminance, not perceived brightness")


class EditTokenFile(unittest.TestCase):
    """--token-file: the edit token must survive restarts (a service restarts on every upgrade), be owner-only, and stay out of the log."""

    def test_token_survives_restart_and_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp, "d", "edit_token")
            args = ("--data-dir", tmp + "/d", "--token-file", str(tf))
            with Server(tmp, *args) as s:
                t1 = tf.read_text().strip()
                self.assertGreaterEqual(len(t1), 16)
                self.assertEqual(stat.S_IMODE(tf.stat().st_mode), 0o600)
                self.assertEqual(s.get("/api/connections")[0], 403, "no token")
                self.assertEqual(s.get("/api/connections", "wrong-token-value")[0], 403, "wrong token")
                self.assertEqual(s.get("/api/connections", t1)[0], 200, "stored token")
            with Server(tmp, *args) as s:
                self.assertEqual(tf.read_text().strip(), t1, "same token after a restart")
                self.assertEqual(s.get("/api/connections", t1)[0], 200)
            self.assertFalse(list(tf.parent.glob("edit_token.tmp")), "no temp file left behind")

    def test_damaged_token_file_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp, "d", "edit_token")
            tf.parent.mkdir()
            for junk in ("x\n", "", "has spaces and is long enough to pass the length check\n"):
                tf.write_text(junk)
                with Server(tmp, "--data-dir", tmp + "/d", "--token-file", str(tf)) as s:
                    t = tf.read_text().strip()
                    self.assertRegex(t, r"^[A-Za-z0-9_-]{16,128}$")
                    self.assertEqual(s.get("/api/connections", t)[0], 200)
                    self.assertEqual(stat.S_IMODE(tf.stat().st_mode), 0o600)

    def test_lan_bind_keeps_a_stored_token_out_of_the_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp, "d", "edit_token")
            with Server(tmp, "--host", "0.0.0.0", "--no-login", "--data-dir", tmp + "/d", "--token-file", str(tf)) as s:
                tok, log = tf.read_text().strip(), s.text()
                self.assertNotIn(tok, log)
                self.assertIn(str(tf), log)
                self.assertIn("Listening on 0.0.0.0", log)
        with tempfile.TemporaryDirectory() as tmp:       # without --token-file the one-off token IS printed (that is how it is found)
            with Server(tmp, "--host", "0.0.0.0", "--no-login", "--data-dir", tmp + "/d") as s:
                self.assertRegex(s.text(), r"\?token=[A-Za-z0-9_-]{16,}")


if __name__ == "__main__":
    unittest.main()
