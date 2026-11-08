"""HTTPS (--tls-cert / --tls-key) and custom.css.   Needs the `openssl` command to make test certificates (the tests are skipped without it).
Run:  python3 -m unittest discover -s tests -v"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))      # so that `python3 -m unittest tests.test_tls` works as well as discover
from test_app import APP, Server, free_port      # noqa: E402

HAVE_OPENSSL = shutil.which("openssl") is not None


def make_cert(folder, name="cert", cn="localhost"):
    """A self-signed certificate for localhost / 127.0.0.1; returns (certificate file, key file)."""
    crt, key = Path(folder, name + ".crt"), Path(folder, name + ".key")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-sha256", "-days", "2",
                    "-subj", "/CN=" + cn, "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1", "-keyout", str(key), "-out", str(crt)],
                   check=True, capture_output=True)
    return crt, key


def der(pem_file):
    return ssl.PEM_cert_to_DER_cert(Path(pem_file).read_text())


class TlsServer(Server):
    """Server whose readiness check speaks HTTPS and trusts `crt`."""

    def __init__(self, tmp, crt, *args):
        super().__init__(tmp, *args)
        self.crt = str(crt)

    def ctx(self):
        return ssl.create_default_context(cafile=self.crt)

    def url(self, path):
        return f"https://127.0.0.1:{self.port}{path}"

    def __enter__(self):
        self.fh = open(self.log, "w")
        self.p = subprocess.Popen(self.args, stdout=self.fh, stderr=subprocess.STDOUT)
        for _ in range(100):
            if self.p.poll() is not None:
                break
            try:
                urllib.request.urlopen(self.url("/"), timeout=1, context=self.ctx()).read()
                return self
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)
        self.__exit__()
        raise AssertionError("server did not start:\n" + self.log.read_text())

    def get(self, path, token=None):
        req = urllib.request.Request(self.url(path), headers={"X-Edit-Token": token} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=5, context=self.ctx()) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def raw(self, data, wait=1.0):
        """Send bytes on a plain TCP connection; return what comes back."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as c:
            c.sendall(data)
            c.settimeout(wait)
            out = b""
            try:
                while True:
                    chunk = c.recv(4096)
                    if not chunk:
                        break
                    out += chunk
            except (socket.timeout, OSError):
                pass
            return out


@unittest.skipUnless(HAVE_OPENSSL, "openssl command not found")
class Https(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.crt, self.key = make_cert(self.tmp)
        self.args = ("--demo", "--data-dir", self.tmp + "/d", "--tls-cert", str(self.crt), "--tls-key", str(self.key))

    def test_pages_and_api_over_https_with_modern_tls_and_our_certificate(self):
        with TlsServer(self.tmp, self.crt, *self.args) as s:
            code, body = s.get("/")
            self.assertEqual((code, b"<title>" in body), (200, True))
            self.assertTrue(json.loads(s.get("/api/layout")[1])["layout"]["tabs"])
            with socket.create_connection(("127.0.0.1", s.port), timeout=3) as raw, s.ctx().wrap_socket(raw, server_hostname="localhost") as t:
                self.assertIn(t.version(), ("TLSv1.2", "TLSv1.3"))
                self.assertEqual(t.getpeercert(binary_form=True), der(self.crt), "the certificate we gave it")
            self.assertIn(f"running at https://127.0.0.1:{s.port}/", s.text())

    def test_plain_http_on_the_https_port_is_sent_to_https(self):
        with TlsServer(self.tmp, self.crt, *self.args) as s:
            r = s.raw(f"GET /api/layout?x=1 HTTP/1.1\r\nHost: 127.0.0.1:{s.port}\r\nConnection: close\r\n\r\n".encode())
            head = r.split(b"\r\n\r\n")[0].decode()
            self.assertTrue(head.startswith("HTTP/1.1 307 "), head)
            self.assertIn(f"Location: https://127.0.0.1:{s.port}/api/layout?x=1", head)
            self.assertIn("X-Content-Type-Options: nosniff", head)
            for evil in ("a b", "x.example\x01", "@evil.example/", ""):
                r = s.raw(f"GET / HTTP/1.1\r\nHost: {evil}\r\n\r\n".encode("latin-1"))
                self.assertTrue(r.startswith(b"HTTP/1.1 400 "), (evil, r))
                self.assertNotIn(b"Location", r)

    def test_junk_scanners_and_half_open_connections_do_no_harm(self):
        with TlsServer(self.tmp, self.crt, *self.args) as s:
            s.raw(b"\x16\x03\x01\x00\x10garbage-not-a-client-hello")        # looks like TLS, is not
            s.raw(b"\x00\x01\x02\x03binary junk\r\n\r\n")
            s.raw(b"")                                                      # connects, says nothing, closes
            with socket.create_connection(("127.0.0.1", s.port), timeout=3):
                pass
            self.assertEqual(s.get("/")[0], 200, "still serving afterwards")
            self.assertNotIn("Traceback", s.text(), "handshake failures are not logged as errors")

    def test_the_certificate_may_come_from_the_environment_and_hold_the_key_too(self):
        both = Path(self.tmp, "both.pem")
        both.write_text(self.crt.read_text() + self.key.read_text())
        old = dict(os.environ)
        os.environ["TRUNKVISTA_TLS_CERT"] = str(both)
        try:
            with TlsServer(self.tmp, self.crt, "--demo", "--data-dir", self.tmp + "/d") as s:
                self.assertEqual(s.get("/")[0], 200)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_start_up_refuses_unusable_certificates_with_a_clear_message(self):
        other_crt, other_key = make_cert(self.tmp, "other")

        def run(*args):
            return subprocess.run([sys.executable, str(APP), "--no-browser", "--demo", "--data-dir", self.tmp + "/d", "--port", str(free_port()), *args],
                                  capture_output=True, text=True, timeout=20, env={k: v for k, v in os.environ.items() if not k.startswith("TRUNKVISTA_TLS")})
        r = run("--tls-cert", self.tmp + "/missing.crt")
        self.assertEqual(r.returncode, 1)
        self.assertIn("missing.crt", r.stderr)
        r = run("--tls-cert", str(self.crt), "--tls-key", str(other_key))
        self.assertEqual(r.returncode, 1)
        self.assertIn("Cannot use the TLS certificate", r.stderr)
        r = run("--tls-key", str(self.key))
        self.assertEqual(r.returncode, 1)
        self.assertIn("--tls-cert", r.stderr)
        Path(self.tmp, "empty.crt").write_text("")
        self.assertEqual(run("--tls-cert", self.tmp + "/empty.crt").returncode, 1)


def load_module():
    spec = importlib.util.spec_from_file_location("trunkvista_under_test", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(HAVE_OPENSSL, "openssl command not found")
class CertificateRenewal(unittest.TestCase):
    """A renewed certificate is used without a restart; a broken replacement leaves the old one in place."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.zd = load_module()
        self.zd.TLS_RELOAD_CHECK_SECONDS = 0
        self.crt, self.key = Path(self.tmp, "live.crt"), Path(self.tmp, "live.key")
        self.new = [make_cert(self.tmp, f"gen{i}", cn=f"gen{i}") for i in range(3)]

    def install(self, n_crt, n_key=None):
        self.tick = getattr(self, "tick", 0) + 1
        shutil.copyfile(self.new[n_crt][0], self.crt)
        shutil.copyfile(self.new[n_crt if n_key is None else n_key][1], self.key)
        stamp = time.time_ns() + 10**9 * self.tick                                  # a distinct modification time on every change
        os.utime(self.crt, ns=(stamp, stamp))
        os.utime(self.key, ns=(stamp, stamp))

    def served(self, tls):
        """The DER certificate a client would receive from tls.current() right now."""
        a, b = socket.socketpair()
        box = {}

        def server():
            with contextlib.suppress(Exception):
                box["conn"] = tls.current().wrap_socket(b, server_side=True)
        t = threading.Thread(target=server)
        t.start()
        cctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        cctx.check_hostname, cctx.verify_mode = False, ssl.CERT_NONE
        with cctx.wrap_socket(a) as c:
            got = c.getpeercert(binary_form=True)
        t.join(5)
        if "conn" in box:
            box["conn"].close()
        return got

    def test_renewed_certificate_is_picked_up_and_a_bad_one_is_not(self):
        self.install(0)
        tls = self.zd.TlsContext(self.crt, self.key)
        self.assertEqual(self.served(tls), der(self.new[0][0]))
        self.install(1)
        self.assertEqual(self.served(tls), der(self.new[1][0]), "renewed")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.install(2, n_key=0)                                         # certificate and key from different pairs
            self.assertEqual(self.served(tls), der(self.new[1][0]), "the old certificate stays")
            self.assertEqual(self.served(tls), der(self.new[1][0]), "... and keeps staying, without repeating the complaint")
            self.assertEqual(out.getvalue().count("not usable yet"), 1)
            self.install(2)                                                  # now the matching key arrives
            self.assertEqual(self.served(tls), der(self.new[2][0]))
        self.assertIn("reloaded", out.getvalue())

    def test_a_file_that_is_being_replaced_does_not_break_anything(self):
        self.install(0)
        tls = self.zd.TlsContext(self.crt, self.key)
        self.crt.unlink()                                                    # certbot moves files around; for a moment there is none
        self.assertEqual(self.served(tls), der(self.new[0][0]))


class CustomCss(unittest.TestCase):
    def page(self, css_bytes):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp, "d")
            data.mkdir()
            if css_bytes is not None:
                (data / "custom.css").write_bytes(css_bytes)
            with Server(tmp, "--demo", "--data-dir", str(data)) as s:
                return s.get("/")[1].decode()

    def test_no_file_no_extra_style(self):
        self.assertNotIn('id="custom-css"', self.page(None))

    def test_the_file_is_added_after_the_built_in_styles_and_read_at_every_page_load(self):
        html = self.page(b":root{--accent:#ff00aa}\n")
        self.assertIn('<style id="custom-css">\n:root{--accent:#ff00aa}\n</style>\n</head>', html)
        self.assertLess(html.index("--panel2:"), html.index("#ff00aa"), "after the built-in rules, so it wins")
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "d").mkdir()
            with Server(tmp, "--demo", "--data-dir", tmp + "/d") as s:
                self.assertNotIn("custom-css", s.get("/")[1].decode())
                Path(tmp, "d", "custom.css").write_text("body{outline:1px solid red}")
                self.assertIn("outline:1px solid red", s.get("/")[1].decode(), "no restart needed")

    def test_it_cannot_break_out_of_its_style_element_and_a_huge_file_is_ignored(self):
        html = self.page(b"a{} </style><script>alert(1)</script> </STYLE >")
        self.assertNotIn("</style><script>alert(1)", html)
        self.assertNotIn("</STYLE", html)
        self.assertNotIn('id="custom-css"', self.page(b"a{}" * 200000))


if __name__ == "__main__":
    unittest.main()
