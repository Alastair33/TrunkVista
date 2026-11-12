"""Accounts: password hashing and policy, the user store, sessions (idle sign-out, void-after-password-change), throttling, the HTTP endpoints
(who sees what, who may change what), the sign-in page, and the `user` command.
Run:  python3 -m unittest discover -s tests -v       (standard library only; the password hashes are made cheap here, so it stays fast)"""
import contextlib
import http.client
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from test_app import APP, Server, free_port, load_app_module

M = load_app_module()
M.PW_ITER = 1000                     # the smallest the verifier accepts; the real figure is 600 000 and would make every test seconds long


class Clock:
    """A clock the tests move by hand."""

    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def seed(data_dir, *users):
    """users.json written directly (cheap hashes): each user is (name, password, role[, idle_minutes[, must_change]])."""
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    rec = {}
    for u in users:
        name, pw, role = u[:3]
        idle = u[3] if len(u) > 3 else None
        rec[name.lower()] = {"username": name, "hash": M.hash_password(pw, 1000), "role": role, "idle_minutes": M.check_idle(idle, role),
                             "disabled": False, "must_change": bool(u[4]) if len(u) > 4 else False, "created": time.time(), "valid_from": 0.0, "last_login": 0.0}
    (d / "users.json").write_text(json.dumps({"version": 1, "users": rec}))
    os.chmod(d / "users.json", 0o600)


class Passwords(unittest.TestCase):
    def test_hash_and_verify(self):
        h = M.hash_password("correct horse battery")
        self.assertTrue(h.startswith("pbkdf2_sha256$"))
        self.assertTrue(M.verify_password("correct horse battery", h))
        self.assertFalse(M.verify_password("correct horse batterY", h))
        self.assertFalse(M.verify_password("", h))
        self.assertNotEqual(h, M.hash_password("correct horse battery"), "every hash has its own salt")
        self.assertNotIn("correct horse", h)

    def test_unusable_stored_values_never_verify_and_never_hang(self):
        good = M.hash_password("secret value 1")
        algo, n, salt, dk = good.split("$")
        for bad in ("", "x", "a$b$c", "pbkdf2_sha256$abc$" + salt + "$" + dk, f"pbkdf2_sha256$999${salt}${dk}",
                    f"pbkdf2_sha256$99999999999${salt}${dk}", f"md5${n}${salt}${dk}", f"pbkdf2_sha256${n}$%%%${dk}", None, 5):
            self.assertFalse(M.verify_password("secret value 1", bad), repr(bad))

    def test_password_policy(self):
        ok = M.check_new_password
        self.assertEqual(ok("a long enough one"), "")
        self.assertIn("at least", ok("short"))
        self.assertIn("at most", ok("x" * 201))
        self.assertIn("only spaces", ok(" " * 12))
        self.assertIn("user name", ok("Alice-Smith1", "alice-smith1"))
        self.assertIn("text", ok(12345678901))
        self.assertIn("text", ok(None))

    def test_weak_passwords_of_enough_length_are_refused(self):
        ok = M.check_new_password
        for weak in ("1234567890", "0987654321", "qwertyuiop", "abcdefghijkl", "aaaaaaaaaaaa", "1212121212", "Password123!", "P@ssw0rd2024", "administrator",
                     "TrunkVista1", "zabbix12345", "qazwsxedcrfv"):
            self.assertNotEqual(ok(weak), "", weak)
        self.assertIn("user name", ok("Alice-Smith1", "alice-smith1"))
        self.assertNotEqual(ok("alice20242024", "alice"), "", "the user name with digits added")
        for fine in ("a long enough one", "correct horse battery", "k7#Vm2qLx9pR", "my password is long", "blue-Heron-4-Sunsets"):
            self.assertEqual(ok(fine, "alice"), "", fine)

    def test_idle_minutes(self):
        c = M.check_idle
        self.assertEqual(c(None, "admin"), M.DEFAULT_IDLE["admin"])
        self.assertEqual(c("", "viewer"), M.DEFAULT_IDLE["viewer"])
        self.assertEqual((c("never"), c("NEVER"), c("0"), c(0)), (0, 0, 0, 0))
        self.assertEqual((c(30), c("30")), (30, 30))
        for bad in ("soon", -1, True, M.IDLE_MAX + 1, 1.5e99, [], {}):
            with self.assertRaises(M.AccountError, msg=repr(bad)):
                c(bad)


class UserStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "users.json"
        self.us = M.UserStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_get_and_the_file(self):
        self.assertFalse(self.us.any())
        self.us.add("Alice", "a password of mine", "admin")
        self.assertTrue(self.us.any())
        u = self.us.get("aLiCe")
        self.assertEqual((u["username"], u["role"], u["idle_minutes"], u["disabled"], u["must_change"]), ("Alice", "admin", 60, False, False))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        raw = self.path.read_text()
        self.assertNotIn("a password of mine", raw)
        self.assertIn("pbkdf2_sha256$", raw)
        self.assertEqual(self.us.add("bob", "another password!", "viewer", "never"), None)
        self.assertEqual(self.us.get("bob")["idle_minutes"], 0)
        self.assertEqual([x["username"] for x in self.us.list()], ["Alice", "bob"])

    def test_what_is_refused(self):
        self.us.add("alice", "a password of mine", "admin")
        for args, text in ((("alice", "another password!"), "already exists"), (("al ice", "another password!"), "user name"),
                           (("-x", "another password!"), "user name"), (("", "another password!"), "user name"), (("x" * 65, "another password!"), "user name"),
                           (("bob", "short"), "at least"), (("bob", "another password!", "root"), "role"), (("bob", "another password!", "viewer", "x"), "Auto-logout")):
            with self.assertRaises(M.AccountError, msg=str(args)) as cm:
                self.us.add(*args)
            self.assertIn(text, str(cm.exception))
        self.assertEqual([x["username"] for x in self.us.list()], ["alice"])

    def test_there_is_always_one_enabled_administrator_and_a_refusal_changes_nothing(self):
        self.us.add("alice", "a password of mine", "admin")
        self.us.add("bob", "another password!", "viewer")
        for call in (lambda: self.us.delete("alice"), lambda: self.us.update("alice", role="viewer"), lambda: self.us.update("alice", disabled=True)):
            with self.assertRaises(M.AccountError) as cm:
                call()
            self.assertIn("one enabled administrator", str(cm.exception))
            # ... and what is held in memory is still what is on disk (a half-applied change used to stay visible until the file changed)
            self.assertEqual(self.us.get("alice")["role"], "admin")
            self.assertFalse(self.us.get("alice")["disabled"])
            self.assertIsNotNone(self.us.get("alice"))
            self.assertEqual(json.loads(self.path.read_text())["users"]["alice"]["role"], "admin")
        self.us.update("bob", role="admin")
        self.us.update("alice", disabled=True)                    # fine now: bob is an administrator
        with self.assertRaises(M.AccountError):
            self.us.delete("bob")
        self.us.update("alice", disabled=False)
        self.us.delete("bob")
        self.assertEqual([x["username"] for x in self.us.list()], ["alice"])

    def test_unknown_user(self):
        self.us.add("alice", "a password of mine", "admin")
        for call in (lambda: self.us.update("zed", role="admin"), lambda: self.us.set_password("zed", "a password of mine"),
                     lambda: self.us.signout_everywhere("zed"), lambda: self.us.delete("zed")):
            with self.assertRaises(M.AccountError):
                call()

    def test_password_change_voids_older_sessions_and_clears_the_flag(self):
        self.us.add("alice", "a password of mine", "admin", must_change=True)
        self.assertTrue(self.us.get("alice")["must_change"])
        self.assertEqual(self.us.get("alice")["valid_from"], 0.0)
        before = time.time()
        self.us.set_password("alice", "a new password!!")
        u = self.us.get("alice")
        self.assertFalse(u["must_change"])
        self.assertGreaterEqual(u["valid_from"], before)
        self.assertTrue(M.verify_password("a new password!!", u["hash"]))
        self.assertFalse(M.verify_password("a password of mine", u["hash"]))
        with self.assertRaises(M.AccountError):
            self.us.set_password("alice", "alice")            # (too short as well; the name rule is covered by check_new_password)

    def test_a_second_process_changing_the_file_is_noticed(self):
        self.us.add("alice", "a password of mine", "admin")
        other = M.UserStore(self.path)                                  # what the `user` command does
        other.add("bob", "another password!", "viewer")
        self.assertIsNotNone(self.us.get("bob"))
        other.update("bob", disabled=True)
        self.assertTrue(self.us.get("bob")["disabled"])
        self.path.unlink()                                              # a file that vanishes means no accounts (Auth keeps requiring a sign-in anyway)
        self.assertFalse(self.us.any())

    def test_a_damaged_file_keeps_the_last_good_state_and_blocks_changes(self):
        self.us.add("alice", "a password of mine", "admin")
        self.path.write_text("{ not json")
        self.assertIsNotNone(self.us.get("alice"), "still the last good content")
        self.assertIn("cannot be read", self.us.error)
        with self.assertRaises(M.AccountError) as cm:
            self.us.add("bob", "another password!")
        self.assertIn("damaged", str(cm.exception))
        self.assertEqual(self.path.read_text(), "{ not json", "not overwritten")
        self.path.write_text(json.dumps({"version": 1, "users": {}}))
        self.assertFalse(self.us.any())
        self.assertEqual(self.us.error, "", "readable again")

    def test_odd_records_are_tamed_not_trusted(self):
        h = M.hash_password("a password of mine")
        self.path.write_text(json.dumps({"users": {
            "a": {"username": "a", "hash": h, "role": "root", "idle_minutes": "soon"},       # unknown role -> viewer, bad idle -> default
            "b": {"username": "b", "hash": 5},                                               # no usable hash: dropped
            "c d": {"username": "c d", "hash": h, "role": "admin"},                          # illegal name: dropped
            "e": "text"}}))
        self.assertEqual([(u["username"], u["role"], u["idle_minutes"]) for u in self.us.list()], [("a", "viewer", M.DEFAULT_IDLE["viewer"])])


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.clock = Clock()
        self.users = M.UserStore(self.dir / "users.json", self.clock)
        self.users.add("alice", "a password of mine", "admin", 10)
        self.users.add("tv", "another password!", "viewer", "never")
        self.ss = M.SessionStore(self.dir / "sessions.json", self.users, self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_lookup_and_what_reaches_the_disk(self):
        tok = self.ss.create("Alice", "10.0.0.5")
        s = self.ss.lookup(tok)
        self.assertEqual((s["user"], s["role"], s["must_change"]), ("alice", "admin", False))
        self.assertRegex(s["csrf"], r"^[A-Za-z0-9_-]{20,}$")
        on_disk = (self.dir / "sessions.json").read_text()
        self.assertNotIn(tok, on_disk, "only a hash of the key is stored")
        self.assertIn(self.ss._hash(tok), on_disk)
        self.assertEqual(stat.S_IMODE((self.dir / "sessions.json").stat().st_mode), 0o600)
        self.assertIsNone(self.ss.lookup("x" * 43))
        for junk in ("", None, 5, "short", "has space " * 6, "ä" * 40):
            self.assertIsNone(self.ss.lookup(junk), repr(junk))

    def test_idle_sign_out_counts_only_real_activity(self):
        tok = self.ss.create("alice")                    # 10 minutes
        self.clock.advance(300)
        self.assertIsNotNone(self.ss.lookup(tok, active=False), "the page polling does not count as activity ...")
        self.clock.advance(301)
        self.assertIsNone(self.ss.lookup(tok), "... so 10 minutes after the last real activity it is over")
        tok = self.ss.create("alice")
        self.clock.advance(500)
        self.assertIsNotNone(self.ss.lookup(tok, active=True), "a person touching the page")
        self.clock.advance(500)
        self.assertIsNotNone(self.ss.lookup(tok), "(restarted the idle clock)")
        self.clock.advance(601)
        self.assertIsNone(self.ss.lookup(tok))

    def test_never_means_never(self):
        tok = self.ss.create("tv")
        self.clock.advance(3600 * 24 * 3650)
        self.assertEqual(self.ss.lookup(tok)["user"], "tv")

    def test_a_new_password_a_disabled_account_and_a_deleted_one_end_the_session(self):
        for what in ("password", "disabled", "signout", "deleted"):
            tok = self.ss.create("tv")
            self.assertIsNotNone(self.ss.lookup(tok))
            self.clock.advance(1)
            if what == "password":
                self.users.set_password("tv", "yet another password")
            elif what == "disabled":
                self.users.update("tv", disabled=True)
            elif what == "signout":
                self.users.signout_everywhere("tv")
            else:
                self.users.delete("tv")
            self.assertIsNone(self.ss.lookup(tok), what)
            self.assertIsNone(self.ss.lookup(tok), what + " (and it stays gone)")
            self.clock.advance(1)
            if what != "deleted":
                self.users.update("tv", disabled=False)
                self.assertIsNotNone(self.ss.lookup(self.ss.create("tv")), "a session made after the change works: " + what)
            else:
                self.users.add("tv", "another password!", "viewer", "never")

    def test_sessions_survive_a_restart_and_the_last_activity_is_saved_at_exit(self):
        tok = self.ss.create("alice")
        self.clock.advance(400)
        self.ss.lookup(tok, active=True)
        self.ss.flush()                                  # what main() does when the server stops
        again = M.SessionStore(self.dir / "sessions.json", self.users, self.clock)
        self.assertIsNotNone(again.lookup(tok))
        self.clock.advance(500)
        self.assertIsNotNone(again.lookup(tok), "the activity 500 s ago was remembered, the sign-in 900 s ago was not what counts")
        self.clock.advance(200)
        self.assertIsNone(again.lookup(tok))

    def test_flush_is_not_a_disk_write_per_request(self):
        tok = self.ss.create("alice")
        f = self.dir / "sessions.json"
        m0 = f.stat().st_mtime_ns
        for _ in range(20):
            self.clock.advance(1)
            self.ss.lookup(tok, active=True)
        self.assertEqual(f.stat().st_mtime_ns, m0, "last-activity times are written once a minute, not on every request")
        self.clock.advance(M.SessionStore.FLUSH_EVERY)
        self.ss.lookup(tok, active=True)
        self.assertNotEqual(f.stat().st_mtime_ns, m0)

    def test_at_most_twenty_sessions_per_user_the_oldest_goes(self):
        toks = []
        for _ in range(M.MAX_SESSIONS + 5):
            self.clock.advance(1)
            toks.append(self.ss.create("alice"))
        self.assertEqual(self.ss.count("alice"), M.MAX_SESSIONS)
        self.assertIsNone(self.ss.lookup(toks[0]))
        self.assertIsNotNone(self.ss.lookup(toks[-1]))
        self.assertIsNotNone(self.ss.create("tv"), "another user is not affected")

    def test_delete_and_delete_user(self):
        a, b, c = self.ss.create("alice"), self.ss.create("alice"), self.ss.create("tv")
        self.ss.delete(a)
        self.assertIsNone(self.ss.lookup(a))
        self.assertIsNotNone(self.ss.lookup(b))
        self.ss.delete("not a key")
        self.ss.delete(None)
        self.ss.delete_user("alice")
        self.assertIsNone(self.ss.lookup(b))
        self.assertIsNotNone(self.ss.lookup(c))

    def test_a_damaged_sessions_file_means_everybody_signs_in_again(self):
        (self.dir / "sessions.json").write_text("garbage")
        ss = M.SessionStore(self.dir / "sessions.json", self.users, self.clock)
        self.assertEqual(ss.count("alice"), 0)
        (self.dir / "sessions.json").write_text(json.dumps({"sessions": {"zz": {"user": "alice"}, "0" * 64: {"user": 5, "csrf": "x"}, "1" * 64: "no"}}))
        self.assertEqual(M.SessionStore(self.dir / "sessions.json", self.users, self.clock).count("alice"), 0)


class ThrottleTests(unittest.TestCase):
    def test_blocks_after_the_limit_and_frees_when_the_oldest_failure_ages_out(self):
        c = Clock()
        t = M.Throttle(3, 100, c)
        for _ in range(3):
            self.assertEqual(t.blocked("k"), 0)
            t.fail("k")
            c.advance(10)
        self.assertEqual(t.blocked("k"), 70)               # first failure was 30 s ago, window 100
        self.assertEqual(t.blocked("other"), 0)
        c.advance(70)
        self.assertEqual(t.blocked("k"), 0)

    def test_the_table_stays_bounded(self):
        t = M.Throttle(3, 1e9)
        for i in range(6000):
            t.fail(f"k{i}")
        self.assertLessEqual(len(t.hits), 5000)
        t.fail("newest")
        self.assertIn("newest", t.hits)


class AuthTests(unittest.TestCase):
    def setUp(self):
        quiet = contextlib.redirect_stdout(io.StringIO())          # (the [accounts] audit lines)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.auth = M.Auth(self.tmp.name, clock=self.clock)
        self.auth.users.add("alice", "a password of mine", "admin")
        self.auth.users.add("bob", "another password!", "viewer")

    def tearDown(self):
        self.tmp.cleanup()

    def fails(self, user, pw, ip="10.0.0.1"):
        with self.assertRaises(M.LoginError) as cm:
            self.auth.login(user, pw, ip)
        return cm.exception

    def test_login(self):
        key, u = self.auth.login(" ALICE ", "a password of mine", "10.0.0.1")
        self.assertEqual(u["username"], "alice")
        self.assertEqual(self.auth.sessions.lookup(key)["user"], "alice")
        self.assertGreater(self.auth.users.get("alice")["last_login"], 0)

    def test_every_kind_of_failure_looks_the_same(self):
        self.auth.users.update("bob", disabled=True)
        msgs = {str(self.fails(n, p)) for n, p in (("alice", "wrong password!!"), ("nobody", "a password of mine"), ("bob", "another password!"),
                                                   ("alice", ""), ("alice", None), ("alice", "x" * 5000), ("a b", "x"), (None, None), ("alice", ["a password of mine"]))}
        self.assertEqual(msgs, {"Wrong user name or password."})

    def test_guessing_is_slowed_per_address_and_per_account(self):
        for i in range(10):
            self.fails("alice", "guess number %d" % i)
        e = self.fails("alice", "a password of mine")                # the right one, but this address has had its 10
        self.assertEqual(e.status, 429)
        self.assertGreater(e.retry_after, 0)
        self.assertEqual(self.auth.login("bob", "another password!", "10.0.0.2")[1]["username"], "bob", "another address is fine")
        self.clock.advance(301)
        self.assertEqual(self.auth.login("alice", "a password of mine", "10.0.0.1")[1]["username"], "alice")
        self.assertEqual(self.auth.login("bob", "another password!", "10.0.0.2")[1]["username"], "bob")      # bob signs in from a usual address
        for i in range(20):                                           # many addresses, one account
            self.fails("bob", "guess number %d" % i, ip="10.1.%d.1" % i)
        self.assertEqual(self.fails("bob", "another password!", ip="10.9.9.9").status, 429, "a stranger is held back, right password or not")
        self.assertEqual(self.auth.login("alice", "a password of mine", "10.9.9.9")[1]["username"], "alice", "other accounts are fine")

    def test_strangers_cannot_lock_the_owner_out(self):
        self.auth.login("bob", "another password!", "10.0.0.2")
        for i in range(60):                                           # an attacker with many addresses uses up bob's tries
            self.fails("bob", "guess number %d" % i, ip="10.1.%d.%d" % (i // 250, i % 250 + 1))
        self.assertEqual(self.auth.login("bob", "another password!", "10.0.0.2")[1]["username"], "bob", "bob's usual address still gets in")
        self.assertEqual(self.fails("bob", "another password!", ip="10.7.7.7").status, 429, "an address nobody knows does not")
        self.assertEqual(self.fails("bob", "wrong guess here", ip="10.0.0.2").status, 401, "a wrong password from the usual address is just wrong")

    def test_login_stays_required_once_there_was_an_account(self):
        a = M.Auth(self.tmp.name)
        self.assertTrue(a.required())
        (Path(self.tmp.name) / "users.json").unlink()
        self.assertTrue(a.required(), "removing users.json under a running server must not open it up")
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        self.assertFalse(M.Auth(empty.name).required())
        self.assertTrue(M.Auth(empty.name, require=True).required())

    def test_the_log_never_holds_a_password_or_a_key(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            key, _ = self.auth.login("alice", "a password of mine", "10.0.0.1")
            self.fails("alice", "wrong password!!")
            self.fails("al\nice\x1b[31m", "x")
        out = buf.getvalue()
        self.assertIn("[accounts] sign-in user=alice", out)
        self.assertIn("[accounts] sign-in failed", out)
        for secret in ("a password of mine", "wrong password!!", key):
            self.assertNotIn(secret, out)
        self.assertNotIn("\x1b", out)
        self.assertEqual(len(out.strip().splitlines()), 3, "a hostile user name cannot add log lines")


class InitialPasswordFile(unittest.TestCase):
    def test_removed_when_its_owner_has_chosen_a_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / M.INITIAL_PW_FILE
            f.write_text("username: Admin\npassword: xyz\n")
            M.forget_initial_password(tmp, "bob")
            self.assertTrue(f.exists(), "somebody else's password change leaves it")
            M.forget_initial_password(tmp, "admin")
            self.assertFalse(f.exists())
            M.forget_initial_password(tmp, "admin")           # (nothing there: no error)


class Browser:
    """A minimal browser for the tests: keeps the session cookie and the request token the page carries."""

    def __init__(self, srv):
        self.srv, self.cookie, self.csrf, self.last = srv, "", "", None

    def req(self, method, path, body=None, ctype="application/json", token=True, headers=None):
        h = dict(headers or {})
        if self.cookie:
            h["Cookie"] = "tv_session=" + self.cookie
        if token and self.csrf:
            h.setdefault("X-Edit-Token", self.csrf)
        data = None
        if body is not None:
            data = json.dumps(body) if ctype == "application/json" else body
            h["Content-Type"] = ctype
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=15)
        try:
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            raw = r.read()
            self.last = r
            for k, v in r.getheaders():
                if k.lower() == "set-cookie" and v.startswith("tv_session="):
                    self.cookie = v.split(";", 1)[0].split("=", 1)[1]
            return r.status, raw
        finally:
            c.close()

    def api(self, method, path, body=None, **kw):
        st, raw = self.req(method, path, body, **kw)
        try:
            return st, json.loads(raw)
        except ValueError:
            return st, raw

    def page(self):
        st, raw = self.req("GET", "/")
        m = re.search(rb'const EDIT_TOKEN = "([^"]*)"', raw)
        self.csrf = m.group(1).decode() if m else ""
        return st, raw.decode("utf-8", "replace")

    def login(self, user, pw):
        st, j = self.api("POST", "/api/login", {"username": user, "password": pw}, token=False)
        if st == 200:
            self.page()
        return st, j


ADMIN, VIEWER, TEMP = ("root", "admin password 1", "admin"), ("tv", "viewer password 1", "viewer", "never"), ("temp", "temporary pw 12", "viewer", None, True)


class Endpoints(unittest.TestCase):
    """The server as a child process with accounts present."""

    def start(self, *extra, users=(ADMIN, VIEWER, TEMP)):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name) / "d"
        if users:
            seed(d, *users)
        srv = Server(tmp.name, "--demo", "--data-dir", str(d), *extra)
        srv.__enter__()
        self.addCleanup(srv.__exit__)
        self.dir = d
        return srv

    def test_without_a_session_there_is_only_the_form(self):
        s = self.start()
        b = Browser(s)
        st, html = b.page()
        self.assertEqual(st, 200)
        self.assertIn('id="lf"', html)
        self.assertNotIn("EDIT_TOKEN", html)
        self.assertNotIn("const LAYOUT", html)
        self.assertIn("frame-ancestors 'none'", b.last.getheader("Content-Security-Policy"))
        for path in ("/api/layout", "/api/users", "/api/connections", "/api/data", "/api/status", "/nothing"):
            st, j = b.api("GET", path, token=False)
            self.assertIn(st, (401, 404), path)
            self.assertNotIn("layout", j if isinstance(j, dict) else {}, path)
        st, j = b.api("GET", "/api/layout", token=False)
        self.assertEqual((st, j.get("login")), (401, True))
        for path in ("/api/layout", "/api/refresh", "/api/users/add", "/api/connections/save", "/api/logout", "/api/account/password"):
            self.assertEqual(b.api("POST", path, {}, token=False)[0], 401, path)

    def test_sign_in_rules(self):
        s = self.start()
        b = Browser(s)
        self.assertEqual(b.login("root", "not the password")[0], 401)
        self.assertEqual(b.login("nobody", "admin password 1")[0], 401)
        self.assertEqual(b.api("POST", "/api/login", "username=root&password=admin+password+1", ctype="text/plain", token=False)[0], 415, "a plain form post")
        self.assertEqual(b.api("POST", "/api/login", ["list"], token=False)[0], 400)
        self.assertEqual(b.api("GET", "/api/login", token=False)[0], 401)
        self.assertEqual(b.cookie, "")
        st, j = b.login("ROOT", "admin password 1")
        self.assertEqual(st, 200, j)
        b2 = Browser(s)
        b2.api("POST", "/api/login", {"username": "root", "password": "admin password 1"}, token=False)
        c = [v for k, v in b2.last.getheaders() if k.lower() == "set-cookie"][0]
        self.assertIn("HttpOnly", c)
        self.assertIn("SameSite=Strict", c)
        self.assertNotIn("Secure", c, "plain http, no proxy flag")
        self.assertRegex(c, r"^tv_session=[A-Za-z0-9_-]{43};")
        self.assertNotIn(b2.cookie, (self.dir / "sessions.json").read_text(), "the key itself is not on the disk")
        log = s.text()
        self.assertIn("[accounts] sign-in user=root", log)
        self.assertIn("[accounts] sign-in failed", log)
        for secret in ("admin password 1", "not the password", b2.cookie):
            self.assertNotIn(secret, log)

    def test_the_cookie_is_secure_behind_an_https_proxy(self):
        s = self.start("--secure-cookie")
        b = Browser(s)
        b.api("POST", "/api/login", {"username": "root", "password": "admin password 1"}, token=False)
        c = [v for k, v in b.last.getheaders() if k.lower() == "set-cookie"][0]
        self.assertIn("; Secure", c)

    def test_an_administrator_edits_with_the_session_token_only(self):
        s = self.start()
        b = Browser(s)
        self.assertEqual(b.login("root", "admin password 1")[0], 200)
        self.assertRegex(b.csrf, r"^[A-Za-z0-9_-]{20,}$")
        st, j = b.api("GET", "/api/layout")
        self.assertEqual((st, j["can_edit"], j["auth"], j["user"]["name"], j["user"]["role"]), (200, True, True, "root", "admin"))
        layout = j["layout"]
        self.assertEqual(b.api("POST", "/api/layout", {"layout": layout}, token=False)[0], 403, "no request token")
        self.assertEqual(b.api("POST", "/api/layout", {"layout": layout}, headers={"X-Edit-Token": "x" * 32})[0], 403, "somebody else's token")
        self.assertEqual(b.api("POST", "/api/layout", {"layout": layout})[0], 200)
        self.assertEqual(b.api("GET", "/api/connections")[0], 200)
        st, j = b.api("GET", "/api/users")
        self.assertEqual(st, 200)
        self.assertEqual({u["username"] for u in j["users"]}, {"root", "tv", "temp"})
        self.assertNotIn("hash", json.dumps(j))
        self.assertNotIn("pbkdf2", json.dumps(j))
        # a second browser's token does not work in this one's session
        other = Browser(s)
        other.login("root", "admin password 1")
        self.assertNotEqual(other.csrf, b.csrf)
        self.assertEqual(b.api("POST", "/api/layout", {"layout": layout}, headers={"X-Edit-Token": other.csrf})[0], 403)

    def test_a_viewer_may_look_and_refresh_and_nothing_else(self):
        s = self.start()
        b = Browser(s)
        self.assertEqual(b.login("tv", "viewer password 1")[0], 200)
        st, j = b.api("GET", "/api/layout")
        self.assertEqual((st, j["can_edit"], j["user"]["role"], j["user"]["idle_minutes"]), (200, False, "viewer", 0))
        self.assertEqual(b.api("POST", "/api/refresh", {})[0], 200)
        self.assertEqual(b.api("POST", "/api/layout", {"layout": j["layout"]})[0], 403)
        for path in ("/api/users/add", "/api/users/delete", "/api/connections/save", "/api/connections/delete"):
            self.assertEqual(b.api("POST", path, {"username": "root", "password": "whatever it is 1"})[0], 403, path)
        for path in ("/api/users", "/api/connections"):
            self.assertEqual(b.api("GET", path)[0], 403, path)
        self.assertEqual(b.api("POST", "/api/refresh", {}, token=False)[0], 403, "a state-changing call needs the token even for a viewer")

    def test_a_forced_password_change_blocks_everything_until_it_is_done(self):
        s = self.start()
        b = Browser(s)
        st, j = b.login("temp", "temporary pw 12")
        self.assertEqual((st, j.get("must_change")), (200, True))
        st, j = b.api("GET", "/api/layout")
        self.assertEqual((st, j["must_change"] if "user" not in j else j["user"]["must_change"], j["can_edit"]), (200, True, False))
        self.assertNotIn("layout", j, "no layout until the password is changed")
        st, j = b.api("GET", "/api/data")
        self.assertEqual((st, j.get("must_change")), (403, True))
        self.assertEqual(b.api("POST", "/api/refresh", {})[0], 403)
        self.assertEqual(b.api("POST", "/api/account/password", {"current": "wrong wrong wrong", "new": "my own new password"})[0], 403)
        self.assertEqual(b.api("POST", "/api/account/password", {"current": "temporary pw 12", "new": "temporary pw 12"})[0], 400)
        st, j = b.api("POST", "/api/account/password", {"current": "temporary pw 12", "new": "short"})
        self.assertEqual(st, 400)
        self.assertIn("at least", j["error"])
        old_cookie = b.cookie
        st, j = b.api("POST", "/api/account/password", {"current": "temporary pw 12", "new": "my own new password"})
        self.assertEqual(st, 200, j)
        self.assertNotEqual(b.cookie, old_cookie, "this browser got a fresh session")
        b.page()
        st, j = b.api("GET", "/api/layout")
        self.assertEqual((st, "layout" in j, j["user"]["must_change"]), (200, True, False))
        self.assertEqual(Browser(s).login("temp", "temporary pw 12")[0], 401)
        self.assertEqual(Browser(s).login("temp", "my own new password")[0], 200)

    def test_changing_your_own_password_ends_your_other_sessions(self):
        s = self.start()
        a, other = Browser(s), Browser(s)
        a.login("root", "admin password 1")
        other.login("root", "admin password 1")
        self.assertEqual(other.api("GET", "/api/layout")[0], 200)
        self.assertEqual(a.api("POST", "/api/account/password", {"current": "admin password 1", "new": "a better password 2"})[0], 200)
        self.assertEqual(other.api("GET", "/api/layout")[0], 401)
        a.page()
        self.assertEqual(a.api("GET", "/api/layout")[0], 200)

    def test_guessing_the_current_password_is_throttled(self):
        s = self.start()
        b = Browser(s)
        b.login("root", "admin password 1")
        codes = [b.api("POST", "/api/account/password", {"current": "guess %d guess" % i, "new": "a better password 2"})[0] for i in range(12)]
        self.assertEqual(codes[:10], [403] * 10)
        self.assertEqual(codes[10:], [429, 429])
        self.assertEqual(b.api("POST", "/api/account/password", {"current": "admin password 1", "new": "a better password 2"})[0], 429)

    def test_sign_out(self):
        s = self.start()
        b = Browser(s)
        b.login("tv", "viewer password 1")
        old = b.cookie
        self.assertEqual(b.api("POST", "/api/logout", {})[0], 200)
        c = [v for k, v in b.last.getheaders() if k.lower() == "set-cookie"][0]
        self.assertIn("Max-Age=0", c)
        b.cookie = old                                        # the old key is dead on the server as well, not just forgotten by the browser
        self.assertEqual(b.api("GET", "/api/layout")[0], 401)

    def test_administering_accounts(self):
        s = self.start()
        a, tv = Browser(s), Browser(s)
        a.login("root", "admin password 1")
        tv.login("tv", "viewer password 1")
        add = lambda **kw: a.api("POST", "/api/users/add", kw)
        self.assertEqual(add(username="wall", password="a wall display pw", role="viewer", idle_minutes="never")[0], 200)
        st, j = add(username="wall", password="a wall display pw")
        self.assertEqual(st, 400)
        self.assertIn("already exists", j["error"])
        self.assertEqual(add(username="x y", password="a wall display pw")[0], 400)
        self.assertEqual(add(username="lowpw", password="short")[0], 400)
        self.assertEqual(add(username="odd", password="a wall display pw", idle_minutes="soon")[0], 400)
        self.assertEqual(Browser(s).login("wall", "a wall display pw")[0], 200)
        # the guard rails
        self.assertIn("own account", a.api("POST", "/api/users/update", {"username": "root", "role": "viewer"})[1]["error"])
        self.assertIn("own account", a.api("POST", "/api/users/update", {"username": "ROOT", "disabled": True})[1]["error"])
        self.assertIn("own account", a.api("POST", "/api/users/delete", {"username": "root"})[1]["error"])
        self.assertIn("Account", a.api("POST", "/api/users/password", {"username": "root", "password": "something else 12"})[1]["error"])
        self.assertEqual(a.api("POST", "/api/users/update", {"username": "root", "idle_minutes": 5})[0], 200, "own idle time is fine")
        self.assertEqual(a.api("POST", "/api/users/update", {"username": "ghost", "idle_minutes": 5})[0], 400)
        self.assertEqual(a.api("POST", "/api/users/nonsense", {"username": "tv"})[0], 404)
        for bad in ("false", 0, 1, [], {}):                    # a flag is true or false; the text "false" must not switch anything on
            self.assertEqual(a.api("POST", "/api/users/update", {"username": "tv", "disabled": bad})[0], 400, repr(bad))
            self.assertEqual(add(username="flagged", password="a wall display pw", must_change=bad)[0], 400, repr(bad))
        self.assertFalse({u["username"]: u for u in a.api("GET", "/api/users")[1]["users"]}["tv"]["disabled"])
        self.assertNotIn("flagged", {u["username"] for u in a.api("GET", "/api/users")[1]["users"]})
        # idle time, then an administrator ending a viewer's sessions
        st, j = a.api("POST", "/api/users/update", {"username": "tv", "idle_minutes": 15})
        self.assertEqual({u["username"]: u["idle_minutes"] for u in j["users"]}["tv"], 15)
        self.assertEqual(tv.api("GET", "/api/layout")[0], 200)
        self.assertEqual(a.api("POST", "/api/users/signout", {"username": "tv"})[0], 200)
        self.assertEqual(tv.api("GET", "/api/layout")[0], 401)
        # disabling, a password set by an administrator, deleting
        tv.login("tv", "viewer password 1")
        self.assertEqual(a.api("POST", "/api/users/update", {"username": "tv", "disabled": True})[0], 200)
        self.assertEqual(tv.api("GET", "/api/layout")[0], 401)
        self.assertEqual(Browser(s).login("tv", "viewer password 1")[0], 401, "a disabled account cannot sign in")
        a.api("POST", "/api/users/update", {"username": "tv", "disabled": False})
        self.assertEqual(a.api("POST", "/api/users/password", {"username": "tv", "password": "set by the admin 1", "must_change": True})[0], 200)
        self.assertEqual(Browser(s).login("tv", "viewer password 1")[0], 401)
        b = Browser(s)
        self.assertEqual(b.login("tv", "set by the admin 1")[1].get("must_change"), True)
        self.assertEqual(a.api("POST", "/api/users/delete", {"username": "tv"})[0], 200)
        self.assertEqual(b.api("GET", "/api/layout")[0], 401)
        st, j = a.api("GET", "/api/users")
        self.assertEqual({u["username"] for u in j["users"]}, {"root", "temp", "wall"})
        log = s.text()
        for line in ("account added by=root user=wall", "account deleted by=root user=tv", "signed out everywhere by=root user=tv"):
            self.assertIn(line, log)
        self.assertNotIn("a wall display pw", log)
        self.assertNotIn("set by the admin 1", log)

    def test_the_form_page_and_the_note_when_there_is_no_account_yet(self):
        s = self.start("--require-login", users=())
        b = Browser(s)
        st, html = b.page()
        self.assertEqual(st, 200)
        self.assertIn("No account exists yet", html)
        self.assertIn("trunkvista-user add", html)
        self.assertEqual(b.api("GET", "/api/layout", token=False)[0], 401)
        self.assertEqual(b.login("root", "admin password 1")[0], 401)
        # the first account is made while the server runs (what the installer and the `user` command do): sign-in works at once
        seed(self.dir, ADMIN)
        self.assertEqual(b.login("root", "admin password 1")[0], 200)
        self.assertNotIn("No account exists yet", Browser(s).page()[1])

    def test_login_stays_required_when_the_account_file_disappears(self):
        s = self.start()
        b = Browser(s)
        self.assertEqual(b.login("root", "admin password 1")[0], 200)
        (self.dir / "users.json").unlink()
        self.assertEqual(b.api("GET", "/api/layout")[0], 401, "no user behind the session any more")
        st, html = Browser(s).page()
        self.assertIn('id="lf"', html)

    def test_custom_css_reaches_the_form_too(self):
        s = self.start()
        (self.dir / "custom.css").write_text(":root{--accent:#ff00aa}")
        html = Browser(s).page()[1]
        self.assertIn('id="custom-css"', html)
        self.assertIn("--accent:#ff00aa", html)

    def test_a_foreign_host_name_is_still_refused(self):
        s = self.start()
        b = Browser(s)
        st, raw = b.req("GET", "/", headers={"Host": "evil.example"}, token=False)
        self.assertEqual(st, 403)

    def test_login_off_switch_and_the_refusal_to_listen_on_a_lan_without_an_account(self):
        s = self.start("--no-login")
        self.assertEqual(Browser(s).api("GET", "/api/layout", token=False)[0], 200, "--no-login: the accounts are ignored")
        with tempfile.TemporaryDirectory() as tmp:
            p = subprocess.run([sys.executable, str(APP), "--no-browser", "--demo", "--host", "0.0.0.0", "--port", str(free_port()), "--data-dir", tmp + "/d"],
                               capture_output=True, text=True, timeout=30)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("trunkvista.py user add", p.stdout + p.stderr)
            self.assertIn("--no-login", p.stdout + p.stderr)
        with tempfile.TemporaryDirectory() as tmp:               # with an account the same address is fine
            seed(Path(tmp) / "d", ADMIN)
            with Server(tmp, "--demo", "--host", "0.0.0.0", "--data-dir", tmp + "/d") as s2:
                self.assertEqual(Browser(s2).api("GET", "/api/layout", token=False)[0], 401)


class UserCommand(unittest.TestCase):
    """trunkvista.py user ...  - also against a running server."""

    def run_cli(self, data, *args, stdin=""):
        return subprocess.run([sys.executable, str(APP), "user", "--data-dir", str(data), *args], input=stdin, capture_output=True, text=True, timeout=60)

    def test_add_list_change_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "d"
            r = self.run_cli(d, "list")
            self.assertEqual(r.returncode, 0)
            self.assertIn("No accounts yet", r.stdout)
            r = self.run_cli(d, "add", "alice", "--role", "admin", "--password-stdin", stdin="a password of mine\n")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("a password of mine", r.stdout + r.stderr)
            self.assertEqual(stat.S_IMODE((d / "users.json").stat().st_mode), 0o600)
            r = self.run_cli(d, "add", "wall", "--idle", "never", "--random-password")
            self.assertEqual(r.returncode, 0, r.stderr)
            pw = re.search(r"Password: (\S+)", r.stdout).group(1)
            self.assertEqual(len(pw), 20)
            self.assertTrue(M.verify_password(pw, M.UserStore(d / "users.json").get("wall")["hash"]))
            out = self.run_cli(d, "list").stdout
            self.assertRegex(out, r"alice\s+admin\s+sign-out after 60 min")
            self.assertRegex(out, r"wall\s+viewer\s+sign-out after never")
            self.assertNotIn("pbkdf2", out)
            self.assertEqual(self.run_cli(d, "set", "wall", "--idle", "30", "--role", "viewer").returncode, 0)
            self.assertEqual(M.UserStore(d / "users.json").get("wall")["idle_minutes"], 30)
            self.assertEqual(self.run_cli(d, "set", "wall", "--disable").returncode, 0)
            self.assertTrue(M.UserStore(d / "users.json").get("wall")["disabled"])
            self.assertIn("DISABLED", self.run_cli(d, "list").stdout)
            self.assertEqual(self.run_cli(d, "set", "wall", "--enable").returncode, 0)
            self.assertEqual(self.run_cli(d, "passwd", "wall", "--password-stdin", "--must-change", stdin="new password for tv\n").returncode, 0)
            self.assertTrue(M.UserStore(d / "users.json").get("wall")["must_change"])
            r = self.run_cli(d, "delete", "alice")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("one enabled administrator", r.stderr)
            self.assertEqual(self.run_cli(d, "delete", "wall").returncode, 0)

    def test_mistakes_are_explained(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "d"
            for args, stdin, text in ((("add", "alice", "--password-stdin"), "short\n", "at least"), (("add", "a b", "--password-stdin"), "a password of mine\n", "user name"),
                                      (("add", "alice"), "", "--password-stdin"), (("set", "ghost", "--role", "admin"), "", "No such user"),
                                      (("set", "ghost"), "", "Nothing to change"), (("passwd", "ghost", "--password-stdin"), "a password of mine\n", "No such user"),
                                      (("add", "alice", "--idle", "x", "--password-stdin"), "a password of mine\n", "Auto-logout")):
                r = self.run_cli(d, *args, stdin=stdin)
                self.assertNotEqual(r.returncode, 0, args)
                self.assertIn(text, r.stderr, args)
            self.assertFalse(M.UserStore(d / "users.json").any())
            (d / "users.json").write_text("{broken")
            r = self.run_cli(d, "add", "alice", "--password-stdin", stdin="a password of mine\n")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("cannot be read", r.stderr)
            self.assertEqual((d / "users.json").read_text(), "{broken")

    def test_passwd_clears_the_installers_random_password_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "d"
            self.run_cli(d, "add", "admin", "--role", "admin", "--password-stdin", stdin="initial password 1\n")
            f = d / M.INITIAL_PW_FILE
            f.write_text("username: admin\npassword: initial password 1\n")
            self.run_cli(d, "passwd", "admin", "--password-stdin", "--must-change", stdin="temporary password 2\n")
            self.assertTrue(f.exists(), "a password that must still be changed does not count")
            self.run_cli(d, "passwd", "admin", "--password-stdin", stdin="chosen by the owner 3\n")
            self.assertFalse(f.exists())

    def test_changes_reach_a_running_server_at_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "d"
            self.run_cli(d, "add", "alice", "--role", "admin", "--password-stdin", stdin="a password of mine\n")
            with Server(tmp, "--demo", "--data-dir", str(d)) as s:
                b = Browser(s)
                self.assertEqual(b.login("alice", "a password of mine")[0], 200)
                self.assertEqual(b.api("GET", "/api/layout")[0], 200)
                self.assertEqual(self.run_cli(d, "add", "bob", "--password-stdin", stdin="bob has a password\n").returncode, 0)
                self.assertEqual(Browser(s).login("bob", "bob has a password")[0], 200)
                self.assertEqual(self.run_cli(d, "passwd", "alice", "--password-stdin", stdin="a changed password 4\n").returncode, 0)
                self.assertEqual(b.api("GET", "/api/layout")[0], 401, "the password change ended alice's session")
                self.assertEqual(Browser(s).login("alice", "a password of mine")[0], 401)
                self.assertEqual(Browser(s).login("alice", "a changed password 4")[0], 200)
                self.assertEqual(self.run_cli(d, "signout", "alice").returncode, 0)
                c = Browser(s)
                c.login("bob", "bob has a password")
                self.assertEqual(self.run_cli(d, "set", "bob", "--disable").returncode, 0)
                self.assertEqual(c.api("GET", "/api/layout")[0], 401)


if __name__ == "__main__":
    unittest.main()
