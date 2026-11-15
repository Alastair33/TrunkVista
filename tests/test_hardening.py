"""Regression tests for findings of the penetration test (November 2026):
F1 - a saved Zabbix token must never be sent to another address than the one it was saved for;
F2 - connections that never finish their request must not be able to shut everybody else out;
F3 - strangers' wrong guesses must not lock the owner out (tests/test_accounts.py), F5 - weak passwords (same file);
F6 - HSTS on request, F7 - no Python version in the Server header;
F4 - an interface-name regex that can take exponential time must be refused, and a long item name must not freeze the server."""
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from test_app import load_app_module

mod = load_app_module()


class StoredSecretStaysWithItsServer(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.conns = mod.Connections(Path(d.name) / "connections.json", {"zabbix": {}})
        self.saved = self.conns.upsert({"name": "prod", "url": "https://zabbix.example.com/zabbix", "token": "SECRET-TOKEN"})

    def test_a_blank_token_keeps_the_saved_one_for_the_same_server(self):
        for url in ("https://zabbix.example.com/zabbix", "https://ZABBIX.example.com:443/other/path", "https://zabbix.example.com"):
            self.assertEqual(self.conns.candidate({"id": self.saved["id"], "url": url})["token"], "SECRET-TOKEN", url)
        p = self.conns.upsert({"id": self.saved["id"], "name": "renamed", "url": "https://zabbix.example.com/zabbix"})
        self.assertEqual((p["name"], p["token"]), ("renamed", "SECRET-TOKEN"))

    def test_testing_another_address_does_not_get_the_saved_token(self):
        for url in ("https://attacker.example/zabbix", "http://zabbix.example.com/zabbix", "https://zabbix.example.com:8443/zabbix",
                    "https://zabbix.example.com.attacker.example/", "https://zabbix.example.com@attacker.example/", "https://attacker.example/?https://zabbix.example.com"):
            with self.assertRaises(ValueError, msg=url) as e:
                self.conns.candidate({"id": self.saved["id"], "url": url})
            self.assertIn("again", str(e.exception))

    def test_saving_another_address_does_not_keep_the_saved_token(self):
        with self.assertRaises(ValueError):
            self.conns.upsert({"id": self.saved["id"], "url": "https://attacker.example/"})
        self.assertEqual(self.conns.get(self.saved["id"])["url"], "https://zabbix.example.com/zabbix", "nothing was changed")
        p = self.conns.upsert({"id": self.saved["id"], "url": "https://new.example/", "token": "NEW-TOKEN"})      # with the secret typed again it may move
        self.assertEqual((p["url"], p["token"]), ("https://new.example/", "NEW-TOKEN"))

    def test_a_saved_password_is_kept_back_the_same_way(self):
        s = self.conns.upsert({"name": "pw", "url": "http://10.0.0.5/zabbix", "auth": "password", "username": "api", "password": "PW"})
        self.assertEqual(self.conns.candidate({"id": s["id"], "url": "http://10.0.0.5/", "auth": "password", "username": "api"})["password"], "PW")
        with self.assertRaises(ValueError):
            self.conns.candidate({"id": s["id"], "url": "http://10.0.0.6/zabbix", "auth": "password", "username": "api"})
        with self.assertRaises(ValueError):      # token -> password on the same server: the token is not a password
            self.conns.candidate({"id": self.saved["id"], "url": "https://zabbix.example.com/zabbix", "auth": "password", "username": "api"})

    def test_the_file_of_an_older_version_still_loads(self):
        again = mod.Connections(self.conns.path, {"zabbix": {}})
        self.assertEqual(again.get(self.saved["id"])["token"], "SECRET-TOKEN")


class Pong(mod.Handler):
    def do_GET(self):
        self._send(200, b"pong", "text/plain")

    def do_POST(self):
        d = self._body()
        if d is not None:
            self._send(200, b"got it", "text/plain")


def serve(handler, **limits):
    srv = mod.DashServer(("127.0.0.1", 0), handler)
    for k, v in limits.items():
        setattr(srv, k, v)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def recv_all(c):
    out = b""
    while True:
        x = c.recv(4096)
        if not x:
            return out
        out += x


def get(port, timeout=5):
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as c:
        c.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        return recv_all(c)


class ConnectionFlood(unittest.TestCase):
    def start(self, handler=Pong, **limits):
        srv, port = serve(handler, **limits)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv, port

    def hold(self, port, n, start=b"GET / HTTP/1.1\r\nX-a: "):
        socks = []
        for _ in range(n):
            c = socket.create_connection(("127.0.0.1", port), timeout=5)
            c.sendall(start)
            socks.append(c)
            self.addCleanup(c.close)
        return socks

    def wait(self, cond, seconds=5):
        t0 = time.time()
        while not cond() and time.time() - t0 < seconds:
            time.sleep(0.02)
        self.assertTrue(cond())

    def test_one_address_cannot_take_every_connection(self):
        srv, port = self.start(max_per_address=5, max_connections=50)
        held = self.hold(port, 5)
        self.wait(lambda: srv._total == 5)
        reply = get(port)
        self.assertTrue(reply.startswith(b"HTTP/1.1 503"), reply[:60])
        self.assertIn(b"Retry-After", reply)
        self.assertEqual((srv._total, srv.refused), (5, 1), "the refused connection got no thread")
        for c in held:
            c.close()
        self.wait(lambda: srv._total == 0)
        self.assertEqual(srv._per, {})
        self.assertIn(b"pong", get(port), "and when they are gone, everybody gets in again")

    def test_the_total_is_limited_too(self):
        srv, port = self.start(max_per_address=100, max_connections=4)
        self.hold(port, 4)
        self.wait(lambda: srv._total == 4)
        self.assertTrue(get(port).startswith(b"HTTP/1.1 503"))

    def test_headers_that_drip_in_are_cut_off(self):
        class Quick(Pong):
            head_seconds = 1
        srv, port = self.start(Quick)
        c = self.hold(port, 1)[0]
        t0 = time.time()
        closed = False
        while time.time() - t0 < 8 and not closed:
            time.sleep(0.3)
            try:
                c.sendall(b"a")                    # one byte now and then: a plain socket timeout would never fire
                c.settimeout(0.05)
                try:
                    closed = c.recv(10) == b""
                except TimeoutError:
                    pass
            except OSError:
                closed = True
        self.assertTrue(closed, "the server gave up on the request")
        self.assertLess(time.time() - t0, 5)
        self.wait(lambda: srv._total == 0)
        self.assertIn(b"pong", get(port))

    def test_a_body_that_does_not_arrive_is_cut_off(self):
        class Quick(Pong):
            body_seconds = 1
        srv, port = self.start(Quick)
        c = self.hold(port, 1, b"POST / HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: 500\r\n\r\n{")[0]
        c.settimeout(8)
        t0 = time.time()
        reply = c.recv(200)
        self.assertLess(time.time() - t0, 5)
        self.assertTrue(reply == b"" or b" 400 " in reply[:20], reply[:40])
        self.wait(lambda: srv._total == 0)

    def test_a_slow_start_and_a_normal_post_are_not_cut_off(self):
        class Quick(Pong):
            head_seconds = 1
        srv, port = self.start(Quick)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            time.sleep(1.6)                        # idle for longer than head_seconds BEFORE the request begins: that is the idle timeout's business
            c.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            self.assertIn(b"pong", recv_all(c))
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            c.sendall(b'POST / HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n')
            time.sleep(0.2)
            c.sendall(b"{}")
            self.assertIn(b"got it", recv_all(c))
        self.wait(lambda: srv._total == 0)
        self.assertEqual(srv._watched, {}, "nothing is left on the watch list")

    def test_deeply_nested_json_is_a_bad_request_not_a_crash(self):
        srv, port = self.start()
        body = b"[" * 100000 + b"]" * 100000
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            c.sendall(b"POST / HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            self.assertIn(b" 400 ", recv_all(c)[:20])

    def test_the_limits_leave_room_under_the_file_limit_of_a_service(self):
        self.assertLess(mod.DashServer.max_connections + 64, 1024)
        self.assertLessEqual(mod.DashServer.max_per_address, mod.DashServer.max_connections // 4)
        unit = (Path(__file__).resolve().parent.parent / "packaging" / "deb" / "trunkvista.service").read_text()
        self.assertIn("LimitNOFILE=4096", unit)


class ReviewFixes(unittest.TestCase):
    def test_a_malformed_layout_is_a_value_error(self):
        good = mod.starter_layout()
        for label, bad in {"map is a number": {**good, "maps": {"m1": 5}}, "node is a string": {**good, "maps": {"m1": {"nodes": ["x"]}}, "tabs": []},
                           "settings is a list": {**good, "settings": [1]}, "scale is a number": {**good, "settings": {"scale": 7}},
                           "tabs is a number": {**good, "tabs": 3}, "playlist item is a number": {**good, "playlists": [{"id": "p1", "items": [1]}]},
                           "a width of 400 digits": {**good, "maps": {"m1": {"width": 10 ** 400}}, "tabs": []}}.items():
            with self.assertRaises(ValueError, msg=label):
                mod.validate_layout(bad)
        self.assertEqual(mod.validate_layout(good)["tabs"][0]["id"], "t1")

    def test_the_status_has_all_its_keys_before_the_first_poll(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        cfg = mod.load_config(Path(d.name) / "config.json")
        app = mod.App(cfg, mod.Connections(Path(d.name) / "connections.json", cfg), d.name, "demo", False)
        before = set(app.status)
        app.poll_once()
        self.assertEqual(set(app.status), before, "a poll adds no key: the dict is serialised by other threads meanwhile")

    def test_a_redirect_is_reported_not_followed(self):
        import http.server
        seen = []

        class Redirecting(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append(("GET", self.path, self.headers.get("Authorization")))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                seen.append(("POST", self.path, self.headers.get("Authorization")))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirecting)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        z = mod.ZabbixClient({"zabbix": {"url": f"http://127.0.0.1:{srv.server_address[1]}", "token": "SECRET", "timeout": 5}})
        with self.assertRaises(mod.ZabbixError) as e:
            z.call("host.get", {})
        self.assertIn("redirects to", str(e.exception))
        self.assertIn("/elsewhere", str(e.exception))
        self.assertEqual([m for m, _, _ in seen], ["POST"], "the token did not travel on to the redirect's target")


class ResponseHeaders(unittest.TestCase):
    def head(self, handler):
        srv, port = serve(handler)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return get(port).split(b"\r\n\r\n")[0].decode("latin-1")

    def test_the_server_header_names_no_python_version(self):
        h = self.head(Pong)
        self.assertIn("\r\nServer: TrunkVista\r\n", h + "\r\n")
        self.assertNotIn("Python", h)
        self.assertNotIn("Strict-Transport-Security", h, "plain HTTP never gets HSTS")

    def test_hsts_only_when_asked_for_and_only_on_https(self):
        class Proxied(Pong):
            secure_cookie, hsts = True, True

        class AskedButPlain(Pong):
            hsts = True
        self.assertIn("Strict-Transport-Security: max-age=31536000", self.head(Proxied))
        self.assertNotIn("Strict-Transport-Security", self.head(AskedButPlain))
        self.assertNotIn("Strict-Transport-Security", self.head(type("Proxied2", (Pong,), {"secure_cookie": True})), "off unless --hsts")


class ZabbixReplySize(unittest.TestCase):
    def test_an_endless_or_huge_answer_is_refused(self):
        import http.server

        class Big(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"jsonrpc":"2.0","result":"' + b"x" * 5000 + b'","id":1}')

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), Big)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        cfg = {"zabbix": {"url": "http://127.0.0.1:%d/api_jsonrpc.php" % srv.server_address[1], "token": "T" * 20, "timeout": 5, "verify_tls": True}}
        z = mod.ZabbixClient(cfg)
        self.assertEqual(len(z.call("x")), 5000, "within the limit")
        z.reply_max = 1000
        with self.assertRaises(mod.ZabbixError) as cm:
            z.call("x")
        self.assertIn("larger than", str(cm.exception))


class PortAskedFor(unittest.TestCase):
    def run_app(self, *args):
        import subprocess
        import sys
        return subprocess.run([sys.executable, str(Path(mod.__file__)), "--no-browser", "--demo", "--data-dir", self.tmp.name, *args],
                              capture_output=True, text=True, timeout=20)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.busy = socket.socket()
        self.busy.bind(("127.0.0.1", 0))
        self.busy.listen(1)
        self.addCleanup(self.busy.close)

    def test_a_port_that_was_asked_for_and_is_taken_is_an_error(self):
        r = self.run_app("--port", str(self.busy.getsockname()[1]))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("is not free", r.stderr)


class OddZabbixItems(unittest.TestCase):
    def test_server_items_without_a_host_are_skipped_not_fatal(self):
        now = time.time()
        items = [{"key_": "system.cpu.util", "lastvalue": "5", "lastclock": str(int(now))},
                 {"hostid": "", "key_": "system.cpu.util", "lastvalue": "6", "lastclock": str(int(now))},
                 {"hostid": "7", "key_": "system.cpu.util", "lastvalue": "7", "lastclock": str(int(now))}]
        out = mod.metrics_of(items, now, 300)
        self.assertEqual(list(out), ["7"])


class ReleaseToolsRunOnPython39(unittest.TestCase):
    def test_no_backslash_inside_an_fstring_expression(self):
        """Before Python 3.12 that is a SyntaxError, and the program promises 3.9."""
        import ast
        for rel in ("trunkvista.py", "packaging/whats_new.py", "packaging/trunkvista-update", "tools/build_demo.py"):
            tree = ast.parse((Path(mod.__file__).parent / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.FormattedValue):
                    seg = ast.get_source_segment((Path(mod.__file__).parent / rel).read_text(encoding="utf-8"), node.value) or ""
                    self.assertNotIn("\\", seg, f"{rel}: backslash in an f-string expression: {seg[:60]}")


class CatastrophicRegex(unittest.TestCase):
    BAD = (r"^(?P<ifname>(a+)+)$", r"^(?P<ifname>(a|a)*)$", r"^(?P<ifname>(.*a)*)$", r"^(?P<ifname>(?:\w+\s?)+)$", r"^(?P<ifname>(a|aa){1,}b)$",
           r"^(?P<ifname>((a*)*)b)$", r"(?P<ifname>(?:x(?:a+)*)+)")
    GOOD = (mod.DEFAULTS["match"]["name_regex"] if hasattr(mod, "DEFAULTS") else r"^Interface (?P<ifname>[^(:]+?)(?:\((?P<alias>.*?)\))?: ",
            r"^(?P<ifname>[A-Za-z]+\d+(?:/\d+)*)", r"^(?P<ifname>\S+) status$", r"^Port (?P<ifname>\d{1,3}(?:\.\d{1,3}){3})$", r"(?P<ifname>Gi|Te)\d+")

    def test_exponential_patterns_are_refused_when_saved_and_when_loaded(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        conns = mod.Connections(Path(d.name) / "connections.json", {"zabbix": {}})
        for rx in self.BAD:
            with self.assertRaises(mod.re.error, msg=rx):
                mod.compile_name_regex(rx)
            with self.assertRaises(ValueError, msg=rx):
                conns.upsert({"name": "x", "url": "https://z.example.com", "token": "T" * 20, "match": {"name_regex": rx}})

    def test_ordinary_patterns_still_work(self):
        for rx in self.GOOD:
            self.assertIn("ifname", mod.compile_name_regex(rx).groupindex, rx)
        self.assertIn("named group", self._why(r"^(?P<x>a+)$"))

    def _why(self, rx):
        try:
            mod.compile_name_regex(rx)
        except mod.re.error as e:
            return str(e)
        return ""

    def test_a_long_item_name_is_cut_before_matching(self):
        src = mod.ZabbixSource.__new__(mod.ZabbixSource)
        src.m = {"tag_name": "interface", "alias_tag": "alias", "name_regex": ""}
        src.name_rx = mod.compile_name_regex(r"^Interface (?P<ifname>[^(:]+?)(?:\((?P<alias>.*?)\))?: ")
        t0 = time.time()
        name, alias = src._ifname({"name": "Interface " + "x" * 200000 + ": Bits received", "key_": "net.if.in[ifHCInOctets.5]", "tags": []})
        self.assertLess(time.time() - t0, 1.0)
        self.assertEqual(name, "idx:5", "(the cut name no longer matches, so the item falls back to the SNMP index)")
        self.assertEqual(src._ifname({"name": "Interface Gi0/1(uplink): Bits received", "key_": "net.if.in[ifHCInOctets.5]", "tags": []}), ("Gi0/1", "uplink"))


if __name__ == "__main__":
    unittest.main()
