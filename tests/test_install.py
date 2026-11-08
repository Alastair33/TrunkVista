"""The short way to a working dashboard: `trunkvista.py connection ...`, the guided `trunkvista-setup` (questions and options), and the
one-line installer `install.sh`.   Run:  python3 -m unittest discover -s tests -v

trunkvista-setup runs here against a made-up /etc/default/trunkvista, data folder and certificate command (it reads their places from the
environment), a fake systemctl, and the fake Zabbix of tests/fake_zabbix.py. What needs a real machine is in tests/e2e_debian.sh."""
import contextlib
import json
import os
import select
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_zabbix import FakeZabbix  # noqa: E402
from test_app import free_port  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "trunkvista.py"
SETUP = ROOT / "packaging" / "trunkvista-setup"
DEFAULT_FILE = (ROOT / "packaging" / "deb" / "trunkvista.default").read_text(encoding="utf-8").replace("@REPO@", "test/repo")
HAVE_OPENSSL = shutil.which("openssl") is not None
TOKEN = "tok-0123456789abcdef"

CERT_STUB = """#!/bin/sh
# stands in for trunkvista-cert: a real self-signed certificate in $TRUNKVISTA_TLS_DIR
mkdir -p "$TRUNKVISTA_TLS_DIR"
echo "cert" >> "$STUB_LOG"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -sha256 -days 2 -subj /CN=localhost \\
    -keyout "$TRUNKVISTA_TLS_DIR/server.key" -out "$TRUNKVISTA_TLS_DIR/server.crt" 2>/dev/null
"""
SYSTEMCTL_STUB = """#!/bin/sh
echo "systemctl $*" >> "$STUB_LOG"
case "$1" in is-enabled) [ -e "$STUB_DIR/timer-on" ] ;; esac
"""


HOSTNAME_STUB = """#!/bin/sh
case "$1" in -I) echo "10.9.8.7 fe80::1" ;; -f) echo "noc.example.com" ;; *) echo "noc" ;; esac
"""


class Box:
    """A throw-away 'machine': settings file, data folder, certificate folder, stub commands, fake Zabbix."""

    def __init__(self, tmp):
        self.dir = Path(tmp)
        self.data, self.defaults, self.tls, self.bin = self.dir / "data", self.dir / "default", self.dir / "tls", self.dir / "bin"
        self.log = self.dir / "stub.log"
        self.data.mkdir()
        self.bin.mkdir()
        self.defaults.write_text(DEFAULT_FILE, encoding="utf-8")
        for name, text in (("cert", CERT_STUB), ("systemctl", SYSTEMCTL_STUB), ("hostname", HOSTNAME_STUB)):
            (self.bin / name).write_text(text, encoding="utf-8")
            (self.bin / name).chmod(0o755)
        self.log.write_text("")

    def env(self, **more):
        e = dict(os.environ, TRUNKVISTA_DEFAULTS=str(self.defaults), TRUNKVISTA_PROG=str(APP), TRUNKVISTA_DATA=str(self.data),
                 TRUNKVISTA_CERT_CMD=str(self.bin / "cert"), TRUNKVISTA_TLS_DIR=str(self.tls), STUB_LOG=str(self.log), STUB_DIR=str(self.dir),
                 PATH=f"{self.bin}:{os.environ['PATH']}")
        e.pop("TRUNKVISTA_FORCE_SYSTEMD", None)
        e.pop("SSH_CONNECTION", None)
        e.pop("SSH_TTY", None)
        e.update(more)
        return e

    def setup(self, *args, stdin="", **env):
        return subprocess.run(["sh", str(SETUP), *args], input=stdin, capture_output=True, text=True, env=self.env(**env), timeout=90)

    def cli(self, *args, stdin="", **env):
        return subprocess.run([sys.executable, str(APP), "connection", "--data-dir", str(self.data), *args], input=stdin, capture_output=True,
                              text=True, env=self.env(**env), timeout=60)

    def add_admin(self):
        r = subprocess.run([sys.executable, str(APP), "user", "--data-dir", str(self.data), "add", "root", "--role", "admin", "--password-stdin"],
                           input="a long enough password\n", capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr

    def value(self, key):
        lines = [x for x in self.defaults.read_text().splitlines() if x.startswith(key + "=")]
        return lines[-1].split("=", 1)[1] if lines else None

    def calls(self):
        return self.log.read_text().splitlines()

    def connections(self):
        f = self.data / "connections.json"
        return json.loads(f.read_text()) if f.exists() else None


@contextlib.contextmanager
def listener(port):
    """Something that accepts connections on `port`: the service that trunkvista-setup waits for after a restart."""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(5)
    try:
        yield
    finally:
        s.close()


class ZabbixCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.box = Box(self.tmp.name)
        self.zbx = FakeZabbix(token=TOKEN)
        self.url = self.zbx.start()
        self.addCleanup(self.zbx.stop)


class ConnectionCommand(ZabbixCase):
    """trunkvista.py connection list | test | add"""

    def test_add_tests_first_and_saves_as_the_connection_in_use(self):
        r = self.box.cli("add", self.url, "--name", "Lab", "--secret-stdin", stdin=TOKEN + "\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Connected to Zabbix 7.0.8: 3 monitored hosts, 2 proxies, 12 interface items found.", r.stdout)
        self.assertIn("Saved 'Lab' as the connection in use.", r.stdout)
        self.assertNotIn(TOKEN, r.stdout + r.stderr)
        j = self.box.connections()
        self.assertEqual((len(j["profiles"]), j["profiles"][0]["name"], j["profiles"][0]["token"], j["active"]), (1, "Lab", TOKEN, j["profiles"][0]["id"]))
        self.assertEqual(stat.S_IMODE((self.box.data / "connections.json").stat().st_mode), 0o600)
        listing = self.box.cli("list").stdout
        self.assertRegex(listing, r"Lab\s+" + self.url + r"\s+token\s+in use")
        self.assertNotIn(TOKEN, listing)

    def test_a_wrong_token_saves_nothing(self):
        r = self.box.cli("add", self.url, "--secret-stdin", stdin="not-the-token\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not saved", r.stderr)
        self.assertIn("rejected the credentials", r.stderr)
        self.assertIsNone(self.box.connections())

    def test_an_address_nobody_answers_on_saves_nothing(self):
        r = self.box.cli("add", f"http://127.0.0.1:{free_port()}", "--secret-stdin", stdin=TOKEN + "\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Cannot reach Zabbix", r.stderr)
        self.assertIsNone(self.box.connections())

    def test_a_bad_address_is_explained(self):
        r = self.box.cli("add", "zabbix.example.com", "--secret-stdin", stdin=TOKEN + "\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("must start with http", r.stderr)

    def test_the_same_server_again_updates_and_keeps_the_name(self):
        self.box.cli("add", self.url, "--name", "Lab", "--secret-stdin", stdin=TOKEN + "\n")
        r = self.box.cli("add", self.url + "/api_jsonrpc.php", "--secret-stdin", stdin=TOKEN + "\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Updated 'Lab'", r.stdout)
        j = self.box.connections()
        self.assertEqual([p["name"] for p in j["profiles"]], ["Lab"])

    def test_the_secret_comes_from_a_file_and_never_from_the_command_line(self):
        f = self.box.dir / "token"
        f.write_text(TOKEN + "\n")
        self.assertEqual(self.box.cli("add", self.url, "--secret-file", str(f)).returncode, 0)
        r = self.box.cli("add", self.url, stdin="")          # no terminal, no file, no stdin option
        self.assertEqual(r.returncode, 1)
        self.assertIn("--secret-stdin or --secret-file", r.stderr)
        self.assertEqual(self.box.cli("add", self.url, "--secret-file", str(self.box.dir / "missing")).returncode, 1)
        self.assertNotIn("--token", self.box.cli("add", "--help").stdout)

    def test_user_and_password_instead_of_a_token(self):
        r = self.box.cli("add", self.url, "--username", "Admin", "--secret-stdin", stdin="zabbix\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        p = self.box.connections()["profiles"][0]
        self.assertEqual((p["auth"], p["username"], p["password"], p["token"]), ("password", "Admin", "zabbix", ""))
        self.assertIn("user Admin", self.box.cli("list").stdout)
        self.assertEqual(self.box.cli("add", self.url, "--username", "Admin", "--secret-stdin", stdin="wrong\n").returncode, 1)

    def test_no_use_leaves_the_connection_in_use_alone(self):
        self.box.cli("add", self.url, "--name", "A", "--secret-stdin", stdin=TOKEN + "\n")
        first = self.box.connections()["active"]
        z2 = FakeZabbix(token="other")
        url2 = z2.start()
        self.addCleanup(z2.stop)
        r = self.box.cli("add", url2, "--name", "B", "--no-use", "--secret-stdin", stdin="other\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        j = self.box.connections()
        self.assertEqual((len(j["profiles"]), j["active"]), (2, first))

    def test_quiet_and_no_test(self):
        r = self.box.cli("add", f"http://127.0.0.1:{free_port()}", "--no-test", "--quiet", "--secret-stdin", stdin="whatever\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("restart", r.stdout)
        self.assertEqual(len(self.box.connections()["profiles"]), 1)

    def test_test_command(self):
        self.assertEqual(self.box.cli("test").returncode, 1)         # nothing saved yet
        self.box.cli("add", self.url, "--name", "Lab", "--secret-stdin", stdin=TOKEN + "\n")
        r = self.box.cli("test")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Connected to Zabbix 7.0.8", r.stdout)
        self.assertEqual(self.box.cli("test", "lab").returncode, 0)
        r = self.box.cli("test", "nosuch")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no connection called", r.stderr)

    def test_the_page_sees_what_the_command_saved(self):
        """connections.json written by the command is the file the server reads: the connection is in use when the server starts."""
        from test_accounts import Browser
        from test_app import Server
        self.box.cli("add", self.url, "--name", "Lab", "--secret-stdin", stdin=TOKEN + "\n")
        self.box.add_admin()
        with Server(self.tmp.name, "--data-dir", str(self.box.data), "--require-login") as s:
            b = Browser(s)
            self.assertEqual(b.login("root", "a long enough password")[0], 200)
            st, j = b.api("GET", "/api/connections")
            self.assertEqual(st, 200)
            self.assertEqual((j["active"] == j["profiles"][0]["id"], j["profiles"][0]["name"], j["profiles"][0]["has_secret"]), (True, "Lab", True))
            self.assertNotIn(TOKEN, json.dumps(j))


class SetupOptions(ZabbixCase):
    """trunkvista-setup without questions (options, or no terminal)."""

    def test_zabbix_is_tried_then_saved(self):
        r = self.box.setup("--zabbix-url", self.url, "--zabbix-name", "Lab", "--token-stdin", "--no-restart", stdin=TOKEN + "\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Connected to Zabbix 7.0.8", r.stdout)
        self.assertIn("TrunkVista is set up.", r.stdout)
        self.assertIn("Zabbix: saved", r.stdout)
        self.assertIn("http://127.0.0.1:8088/", r.stdout)
        self.assertNotIn(TOKEN, r.stdout + r.stderr)
        self.assertEqual(self.box.connections()["profiles"][0]["name"], "Lab")
        self.assertEqual(self.box.defaults.read_text(), DEFAULT_FILE)       # Zabbix only: the settings file is untouched

    def test_a_wrong_token_changes_nothing(self):
        r = self.box.setup("--zabbix-url", self.url, "--token-stdin", "--network", stdin="wrong\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nothing else was changed", r.stderr)
        self.assertIsNone(self.box.connections())
        self.assertEqual(self.box.defaults.read_text(), DEFAULT_FILE)
        self.assertEqual(self.box.calls(), [])

    def test_the_token_comes_from_a_file(self):
        f = self.box.dir / "t"
        f.write_text(TOKEN + "\n")
        r = self.box.setup("--zabbix-url", self.url, "--token-file", str(f), "--yes", "--no-restart")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.box.connections()["profiles"]), 1)

    @unittest.skipUnless(HAVE_OPENSSL, "needs openssl")
    def test_network_listens_everywhere_and_switches_on_https(self):
        self.box.add_admin()
        before = DEFAULT_FILE.splitlines()
        r = self.box.setup("--network", "--yes", "--no-restart")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.box.value("TRUNKVISTA_HOST"), "0.0.0.0")
        self.assertEqual(self.box.value("TRUNKVISTA_TLS_CERT"), f"{self.box.tls}/server.crt")
        self.assertEqual(self.box.value("TRUNKVISTA_TLS_KEY"), f"{self.box.tls}/server.key")
        self.assertEqual(self.box.calls(), ["cert"])
        self.assertIn("https://", r.stdout)
        self.assertIn("SHA-256 fingerprint", r.stdout)
        after = self.box.defaults.read_text().splitlines()
        changed = [x for x in after if x not in before]
        self.assertEqual(len(changed), 3, changed)                      # nothing else was touched: not the comments, not the port, not the update settings
        self.assertEqual(len(after), len(before))
        self.assertIn("TRUNKVISTA_PORT=8088", after)
        self.assertEqual(stat.S_IMODE(self.box.defaults.stat().st_mode), 0o644)
        text = self.box.defaults.read_text()
        r2 = self.box.setup("--network", "--yes", "--no-restart")        # again: nothing to do
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertEqual(self.box.defaults.read_text(), text)
        self.assertEqual(self.box.calls(), ["cert"])                    # the certificate is not made twice

    def test_the_summary_names_the_addresses_to_open(self):
        self.box.add_admin()
        r = self.box.setup("--network", "--no-https", "--yes", "--no-restart")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("http://noc.example.com:8088/", r.stdout)
        self.assertIn("http://10.9.8.7:8088/", r.stdout)
        self.assertNotIn("fe80", r.stdout)                                  # IPv6 link-local addresses are noise here

    @unittest.skipUnless(HAVE_OPENSSL, "needs openssl")
    def test_an_older_certificate_is_kept_but_a_missing_name_is_pointed_out(self):
        self.box.add_admin()
        self.box.setup("--network", "--yes", "--no-restart")                 # (the stub certificate names only localhost)
        r = self.box.setup("--network", "--yes", "--no-restart")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("was not made for: 10.9.8.7", r.stdout)
        self.assertIn("sudo trunkvista-cert --force", r.stdout)
        self.assertEqual(self.box.calls(), ["cert"])                         # not replaced behind your back

    @unittest.skipUnless(HAVE_OPENSSL, "needs openssl")
    def test_local_takes_it_back_to_this_computer(self):
        self.box.add_admin()
        self.box.setup("--network", "--yes", "--no-restart")
        r = self.box.setup("--local", "--yes", "--no-restart")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.box.value("TRUNKVISTA_HOST"), "127.0.0.1")
        self.assertIn("(on this computer)", r.stdout)

    def test_network_without_https_when_you_say_so(self):
        self.box.add_admin()
        r = self.box.setup("--network", "--no-https", "--yes", "--no-restart")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.box.value("TRUNKVISTA_HOST"), "0.0.0.0")
        self.assertIsNone(self.box.value("TRUNKVISTA_TLS_CERT"))
        self.assertEqual(self.box.calls(), [])
        self.assertIn("HTTPS stays off", r.stdout)

    def test_your_own_certificate_is_left_alone(self):
        self.box.add_admin()
        self.box.defaults.write_text(DEFAULT_FILE.replace("#TRUNKVISTA_TLS_CERT=/etc/trunkvista/tls/server.crt", "TRUNKVISTA_TLS_CERT=/etc/ssl/mine.pem"))
        r = self.box.setup("--network", "--yes", "--no-restart")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.box.value("TRUNKVISTA_TLS_CERT"), "/etc/ssl/mine.pem")
        self.assertEqual(self.box.calls(), [])

    def test_the_network_needs_an_account(self):
        r = self.box.setup("--network", "--yes", "--no-restart")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no account exists yet", r.stderr)
        self.assertEqual(self.box.defaults.read_text(), DEFAULT_FILE)

    def test_settings_are_found_in_every_shape(self):
        """KEY=value, a duplicate, the commented line of the stock file, and no line at all."""
        self.box.add_admin()
        self.box.defaults.write_text("# my notes\nTRUNKVISTA_PORT=9000\nTRUNKVISTA_HOST=127.0.0.1\nTRUNKVISTA_HOST=127.0.0.1\n")
        r = self.box.setup("--network", "--no-https", "--yes", "--no-restart")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.box.defaults.read_text(), "# my notes\nTRUNKVISTA_PORT=9000\nTRUNKVISTA_HOST=0.0.0.0\n")
        self.assertIn(":9000/", self.box.setup("--yes", "--no-restart").stdout)
        self.box.defaults.write_text("")
        self.assertEqual(self.box.setup("--network", "--no-https", "--yes", "--no-restart").returncode, 0)
        self.assertEqual(self.box.defaults.read_text(), "TRUNKVISTA_HOST=0.0.0.0\n")

    def test_options_that_make_no_sense_are_refused(self):
        for args, text in ((("--no-https",), "only makes sense with --network"), (("--token-stdin",), "belong to --zabbix-url"),
                           (("--bogus",), "unknown option"), (("--zabbix-url",), "needs a value"),
                           (("--zabbix-url", "http://x", "--token-file", "f", "--token-stdin"), "choose one")):
            r = self.box.setup(*args, "--yes")
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn(text, r.stderr, args)
        self.assertIn("Zabbix:", self.box.setup("--help").stdout)

    def test_not_installed_is_said_plainly(self):
        r = self.box.setup("--yes", TRUNKVISTA_PROG=str(self.box.dir / "missing.py"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("is the trunkvista package installed", r.stderr)

    def test_a_changed_setting_restarts_the_service_and_waits_for_it(self):
        self.box.add_admin()
        port = free_port()
        self.box.defaults.write_text(DEFAULT_FILE.replace("TRUNKVISTA_PORT=8088", f"TRUNKVISTA_PORT={port}"))
        with listener(port):
            r = self.box.setup("--network", "--no-https", "--yes", TRUNKVISTA_FORCE_SYSTEMD="1")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(self.box.calls(), ["systemctl is-enabled --quiet trunkvista-update.timer", "systemctl restart trunkvista"])
            r = self.box.setup("--network", "--no-https", "--yes", TRUNKVISTA_FORCE_SYSTEMD="1")         # nothing changed: no restart
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(self.box.calls().count("systemctl restart trunkvista"), 1)

    def test_a_service_that_does_not_come_up_is_reported(self):
        self.box.add_admin()
        self.box.defaults.write_text(DEFAULT_FILE.replace("TRUNKVISTA_PORT=8088", f"TRUNKVISTA_PORT={free_port()}"))
        t0 = time.time()
        r = self.box.setup("--network", "--no-https", "--yes", TRUNKVISTA_FORCE_SYSTEMD="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not answer", r.stderr)
        self.assertLess(time.time() - t0, 60)

    def test_nightly_updates_are_switched_on_and_off(self):
        r = self.box.setup("--auto-update", "--yes", TRUNKVISTA_FORCE_SYSTEMD="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("systemctl enable --now trunkvista-update.timer", self.box.calls())
        r = self.box.setup("--no-auto-update", "--yes", TRUNKVISTA_FORCE_SYSTEMD="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("systemctl disable --now trunkvista-update.timer", self.box.calls())
        self.assertNotIn("systemctl restart trunkvista", self.box.calls())      # updates alone need no restart

    def test_without_systemd_it_says_what_to_do_by_hand(self):
        self.box.add_admin()
        r = self.box.setup("--network", "--no-https", "--yes", stdin="")
        self.assertIn("restart trunkvista yourself", r.stdout)

    def test_the_summary_mentions_a_password_that_was_made_for_you(self):
        (self.box.data / "initial-admin-password").write_text("username: admin\npassword: x\n")
        self.assertIn("has to be changed at the first sign-in", self.box.setup("--yes", "--no-restart").stdout)


@unittest.skipUnless(os.name == "posix", "needs a pseudo terminal")
class SetupQuestions(ZabbixCase):
    """trunkvista-setup at a terminal: the questions, with the answers typed on a pseudo terminal."""

    def drive(self, script, argv=None, **env):
        import pty
        e = self.box.env(**env)
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe(argv[0] if argv else "sh", argv or ["sh", str(SETUP)], e)
        out, pos, deadline = b"", 0, time.time() + 60

        def read_until(text):
            nonlocal out, pos
            while text.encode() not in out[pos:]:
                if time.time() > deadline:
                    self.fail(f"waited for {text!r}; got:\n{out.decode(errors='replace')}")
                if select.select([fd], [], [], 1)[0]:
                    try:
                        chunk = os.read(fd, 4096)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        self.fail(f"the script ended while waiting for {text!r}; got:\n{out.decode(errors='replace')}")
                    out += chunk
            pos = out.index(text.encode(), pos) + len(text)

        for expect, reply in script:
            read_until(expect)
            os.write(fd, (reply + "\n").encode())
        while True:
            try:
                chunk = os.read(fd, 4096) if select.select([fd], [], [], 5)[0] else b""
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status), out.decode(errors="replace")

    @unittest.skipUnless(HAVE_OPENSSL, "needs openssl")
    def test_three_answers_finish_the_installation(self):
        self.box.add_admin()
        port = free_port()
        self.box.defaults.write_text(DEFAULT_FILE.replace("TRUNKVISTA_PORT=8088", f"TRUNKVISTA_PORT={port}"))
        with listener(port):
            code, out = self.drive([("Zabbix address", self.url), ("API token", TOKEN), ("Choose 1 or 2:", "2"),
                                    ("Install new releases automatically?", "y")], TRUNKVISTA_FORCE_SYSTEMD="1")
        self.assertEqual(code, 0, out)
        self.assertIn("Connected to Zabbix 7.0.8", out)
        self.assertNotIn(TOKEN, out)                               # typing the token was not echoed
        self.assertEqual(len(self.box.connections()["profiles"]), 1)
        self.assertEqual((self.box.value("TRUNKVISTA_HOST"), self.box.value("TRUNKVISTA_TLS_CERT")), ("0.0.0.0", f"{self.box.tls}/server.crt"))
        self.assertIn("systemctl enable --now trunkvista-update.timer", self.box.calls())
        self.assertEqual(self.box.calls()[-1], "systemctl restart trunkvista")
        self.assertIn("TrunkVista is set up.", out)
        self.assertIn(f":{port}/", out)

    def test_enter_alone_skips_zabbix_and_keeps_this_computer_only(self):
        code, out = self.drive([("Zabbix address", ""), ("Choose 1 or 2:", "")])
        self.assertEqual(code, 0, out)
        self.assertIn("Skipped", out)
        self.assertIsNone(self.box.connections())
        self.assertEqual(self.box.defaults.read_text(), DEFAULT_FILE)
        self.assertIn("http://127.0.0.1:8088/", out)

    def test_a_wrong_token_is_explained_and_asked_again(self):
        code, out = self.drive([("Zabbix address", self.url), ("API token", "wrong"), ("Try again?", "y"), ("Zabbix address", ""), ("API token", TOKEN),
                                ("Choose 1 or 2:", "1")])
        self.assertEqual(code, 0, out)
        self.assertIn("rejected the credentials", out)
        self.assertEqual(len(self.box.connections()["profiles"]), 1)

    def test_over_ssh_the_default_is_the_network(self):
        self.box.add_admin()
        code, out = self.drive([("Zabbix address", ""), ("Choose 1 or 2:", "")], SSH_CONNECTION="10.0.0.1 5000 10.0.0.2 22")
        self.assertEqual(code, 0, out)
        self.assertIn("probably a server", out)
        self.assertEqual(self.box.value("TRUNKVISTA_HOST"), "0.0.0.0")

    def test_a_wrong_answer_is_asked_again(self):
        code, out = self.drive([("Zabbix address", ""), ("Choose 1 or 2:", "3"), ("Please type 1 or 2.", ""), ("Choose 1 or 2:", "1")])
        self.assertEqual(code, 0, out)

    @unittest.skipUnless(HAVE_OPENSSL, "needs openssl")
    def test_a_self_signed_zabbix_certificate_can_be_accepted_on_the_spot(self):
        from test_tls import make_cert
        crt, key = make_cert(self.box.dir, "zabbix")
        z = FakeZabbix(token=TOKEN, tls=(str(crt), str(key)))
        url = z.start()
        self.addCleanup(z.stop)
        argv = [sys.executable, str(APP), "connection", "--data-dir", str(self.box.data), "add", url, "--secret-stdin"]
        code, out = self.drive([("", TOKEN), ("Accept it anyway", "n")], argv=argv)           # (--secret-stdin: the first line typed is the token)
        self.assertNotEqual(code, 0, out)
        self.assertIn("not saved", out)
        self.assertIsNone(self.box.connections())
        code, out = self.drive([("", TOKEN), ("Accept it anyway", "y")], argv=argv)
        self.assertEqual(code, 0, out)
        self.assertFalse(self.box.connections()["profiles"][0]["verify_tls"])
        self.assertIn("Connected to Zabbix", out)

    def test_an_existing_connection_is_shown_and_kept_unless_you_say_so(self):
        self.box.cli("add", self.url, "--name", "Lab", "--secret-stdin", stdin=TOKEN + "\n")
        code, out = self.drive([("Add or change a Zabbix server now?", "n"), ("Choose 1 or 2:", "1")])
        self.assertEqual(code, 0, out)
        self.assertIn("Already saved", out)
        self.assertIn("Lab", out)
        self.assertEqual(len(self.box.connections()["profiles"]), 1)


INSTALL = ROOT / "packaging" / "install.sh"
APT_STUB = """#!/bin/sh
echo "apt-get $*" >> "$STUB_LOG"
case "$1" in update) touch "$STUB_DIR/apt-updated"; exit 0 ;; esac
for last; do :; done
[ -f "$last" ] && echo "deb-size $(wc -c < "$last")" >> "$STUB_LOG"
[ -z "${DEBIAN_FRONTEND:-}" ] || echo "frontend $DEBIAN_FRONTEND" >> "$STUB_LOG"
[ -z "${TRUNKVISTA_INSTALLER:-}" ] || echo "from-installer" >> "$STUB_LOG"
[ ! -e "$STUB_DIR/apt-fails" ] || [ -e "$STUB_DIR/apt-updated" ] || exit 100      # (the first try fails until the package lists were refreshed)
"""
SETUP_STUB = """#!/bin/sh
echo "trunkvista-setup ran" >> "$STUB_LOG"
if [ -t 0 ]; then printf 'asked: '; read -r x; echo "got:$x"; fi
"""


class InstallScript(unittest.TestCase):
    """packaging/install.sh against a fake release server, with apt-get / dpkg / trunkvista-setup replaced by stubs."""

    DEB = b"not really a package, but it has a size"

    def setUp(self):
        import functools
        import http.server
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.web, self.bin = self.dir / "web", self.dir / "bin"
        self.web.mkdir()
        self.bin.mkdir()
        self.log = self.dir / "stub.log"
        self.log.write_text("")
        self.publish("trunkvista-latest.deb", self.DEB)
        for name, text in (("apt-get", APT_STUB), ("trunkvista-setup", SETUP_STUB), ("id", "#!/bin/sh\necho 0\n"),
                           ("dpkg", "#!/bin/sh\n:\n"), ("dpkg-query", "#!/bin/sh\nprintf 9.9.9\n")):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)

        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(self.web)))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def publish(self, name, data, sums=True):
        import hashlib
        (self.web / name).write_bytes(data)
        if sums:
            lines = [x for x in (self.web / "SHA256SUMS").read_text().splitlines() if not x.endswith("  " + name)] if (self.web / "SHA256SUMS").exists() else []
            lines.append(f"{hashlib.sha256(data).hexdigest()}  {name}")
            (self.web / "SHA256SUMS").write_text("\n".join(lines) + "\n")

    def env(self, **more):
        e = dict(os.environ, TRUNKVISTA_BASE_URL=self.base, STUB_LOG=str(self.log), STUB_DIR=str(self.dir), PATH=f"{self.bin}:{os.environ['PATH']}")
        e.pop("DEBIAN_FRONTEND", None)
        e.update(more)
        return e

    def run_it(self, *args, **env):
        # (a new session: no controlling terminal, so the script cannot ask the terminal of whoever runs the tests)
        return subprocess.run(["sh", str(INSTALL), *args], capture_output=True, text=True, env=self.env(**env), timeout=60, stdin=subprocess.DEVNULL,
                              start_new_session=True)

    def calls(self):
        return self.log.read_text().splitlines()

    def test_it_downloads_checks_installs_and_says_what_is_next(self):
        r = self.run_it()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Checksum ok.", r.stdout)
        self.assertIn("TrunkVista 9.9.9 is installed.", r.stdout)
        self.assertIn("sudo trunkvista-setup", r.stdout)                  # no terminal here: it does not start the questions
        calls = self.calls()
        self.assertRegex(calls[0], r"^apt-get install -y /.+/trunkvista-latest\.deb$")
        self.assertIn(f"deb-size {len(self.DEB)}", calls)
        self.assertIn("from-installer", calls)                            # the package knows it need not say "run trunkvista-setup": the installer does
        self.assertIn("frontend noninteractive", calls)                   # nobody to ask: apt must not wait for answers
        self.assertNotIn("trunkvista-setup ran", calls)

    def test_a_damaged_download_is_not_installed(self):
        (self.web / "trunkvista-latest.deb").write_bytes(self.DEB + b"x")
        r = self.run_it()
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not match SHA256SUMS", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_package_the_checksums_do_not_list_is_not_installed(self):
        (self.web / "SHA256SUMS").write_text("0" * 64 + "  something-else.deb\n")
        r = self.run_it()
        self.assertEqual(r.returncode, 1)
        self.assertIn("is not listed in SHA256SUMS", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_missing_file_is_explained(self):
        (self.web / "trunkvista-latest.deb").unlink()
        r = self.run_it()
        self.assertEqual(r.returncode, 1)
        self.assertIn("could not download", r.stderr)
        self.assertIn("private repository", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_named_version(self):
        self.publish("trunkvista_1.3.0_all.deb", b"old one")
        r = self.run_it("--version", "1.3.0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(self.calls()[0], r"trunkvista_1\.3\.0_all\.deb$")
        self.assertEqual(self.run_it("--version", "1.3.0;rm").returncode, 1)
        self.assertEqual(self.run_it("--version").returncode, 1)

    def test_apt_is_given_a_second_chance_after_an_update_of_its_lists(self):
        (self.dir / "apt-fails").write_text("")
        r = self.run_it()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c.split()[1] for c in self.calls() if c.startswith("apt-get")], ["install", "update", "install"])

    def test_apt_that_keeps_failing_is_reported(self):
        (self.dir / "apt-fails").write_text("")
        (self.bin / "apt-get").write_text("#!/bin/sh\nexit 100\n")
        r = self.run_it()
        self.assertEqual(r.returncode, 1)
        self.assertIn("apt could not install", r.stderr)

    def test_options(self):
        self.assertEqual(self.run_it("--bogus").returncode, 1)
        self.assertIn("unknown option", self.run_it("--bogus").stderr)
        self.assertIn("curl -fsSL", self.run_it("--help").stdout)

    def test_not_root_and_not_debian_are_said_plainly(self):
        (self.bin / "id").write_text("#!/bin/sh\necho 1000\n")
        r = self.run_it()
        self.assertEqual(r.returncode, 1)
        self.assertIn("run it as root", r.stderr)
        # a machine without apt-get: only the commands the script itself needs are on the path
        farm = self.dir / "farm"
        farm.mkdir()
        for name in ("mktemp", "chmod", "rm", "grep", "sha256sum", "sed", "curl", "cat", "dpkg"):
            src = shutil.which(name)
            if src:
                (farm / name).symlink_to(src)
        (farm / "id").write_text("#!/bin/sh\necho 0\n")
        (farm / "id").chmod(0o755)
        r = subprocess.run([shutil.which("sh"), str(INSTALL)], capture_output=True, text=True, env=self.env(PATH=str(farm)), timeout=60,
                           stdin=subprocess.DEVNULL, start_new_session=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("for Debian and Ubuntu", r.stderr)
        self.assertIn("trunkvista.py", r.stderr)

    def test_the_template_in_the_source_tree_refuses_to_run_without_an_address(self):
        env = self.env()
        env.pop("TRUNKVISTA_BASE_URL")
        r = subprocess.run(["sh", str(INSTALL)], capture_output=True, text=True, env=env, timeout=60, stdin=subprocess.DEVNULL, start_new_session=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("template", r.stderr)

    @unittest.skipUnless(os.name == "posix", "needs a pseudo terminal")
    def test_piped_from_curl_it_still_asks_its_questions_on_the_terminal(self):
        """`curl ... | sudo sh`: standard input is the script, yet apt and trunkvista-setup must be able to ask."""
        import pty
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe("sh", ["sh", "-c", f"cat {INSTALL} | sh"], self.env())
        out, deadline = b"", time.time() + 60
        sent = False
        while time.time() < deadline:
            if select.select([fd], [], [], 1)[0]:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            if b"asked: " in out and not sent:
                os.write(fd, b"hello\n")
                sent = True
        _, status = os.waitpid(pid, 0)
        text = out.decode(errors="replace")
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, text)
        self.assertIn("got:hello", text)
        self.assertNotIn("frontend noninteractive", self.calls())
        self.assertIn("trunkvista-setup ran", self.calls())


if __name__ == "__main__":
    unittest.main()
