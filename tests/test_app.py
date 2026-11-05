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
            with Server(tmp, "--host", "0.0.0.0", "--data-dir", tmp + "/d", "--token-file", str(tf)) as s:
                tok, log = tf.read_text().strip(), s.text()
                self.assertNotIn(tok, log)
                self.assertIn(str(tf), log)
                self.assertIn("Listening on 0.0.0.0", log)
        with tempfile.TemporaryDirectory() as tmp:       # without --token-file the one-off token IS printed (that is how it is found)
            with Server(tmp, "--host", "0.0.0.0", "--data-dir", tmp + "/d") as s:
                self.assertRegex(s.text(), r"\?token=[A-Za-z0-9_-]{16,}")


if __name__ == "__main__":
    unittest.main()
