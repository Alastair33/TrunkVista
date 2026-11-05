"""channelview-update against a mock GitHub (tests/mock_github.py): version order, channels, checksums, signatures, package check, private
repositories and redirects.  Nothing is installed: the install step and the root check are replaced.
Run:  python3 -m unittest discover -s tests -v"""
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from mock_github import MockGitHub  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("zbx_update", str(HERE.parent / "packaging" / "channelview-update"))
upd = importlib.util.module_from_spec(importlib.util.spec_from_loader("zbx_update", _loader))
_loader.exec_module(upd)

HAVE_DPKG = shutil.which("dpkg-deb") is not None
HAVE_GPG = shutil.which("gpg") is not None and shutil.which("gpgv") is not None
ENV_KEYS = ("CHANNELVIEW_UPDATE_REPO", "CHANNELVIEW_UPDATE_CHANNEL", "CHANNELVIEW_UPDATE_TOKEN_FILE")


def make_deb(tmp, version, package="channelview", arch="all"):
    """A real (empty) .deb with the given control fields."""
    root = Path(tempfile.mkdtemp(dir=tmp))
    (root / "DEBIAN").mkdir()
    (root / "DEBIAN" / "control").write_text(f"Package: {package}\nVersion: {version}\nArchitecture: {arch}\nMaintainer: t <t@example.com>\nDescription: test\n")
    out = Path(tmp) / f"{package}_{version}_{arch}.deb"
    subprocess.run(["dpkg-deb", "--root-owner-group", "-Zgzip", "--build", str(root), str(out)], check=True, capture_output=True)
    return out.read_bytes()


def sums_for(name, data):
    return f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zu")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.gh = MockGitHub("owner/name")
        self.base = self.gh.start()
        self.addCleanup(self.gh.stop)
        self.installs = []
        self.installed = "1.0.0"
        env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        for p in (mock.patch.dict(os.environ, env, clear=True),
                  mock.patch.object(upd, "installed_version", lambda: self.installed),
                  mock.patch.object(upd, "run_install", self.fake_install),
                  mock.patch.object(upd.os, "geteuid", lambda: 0),
                  mock.patch.object(upd, "DEFAULT_KEYRING", self.tmp + "/no-such-keyring.gpg")):
            p.start()
            self.addCleanup(p.stop)

    def fake_install(self, path, allow_downgrade, reinstall=False):
        """Stands in for apt-get: records the call and 'installs' the version named in the file."""
        self.installs.append((Path(path), allow_downgrade, reinstall))
        self.installed = upd.asset_version({"name": Path(path).name})

    def release(self, version, tag=None, deb=None, sums=True, **kw):
        """version is the Debian version (2.0.0~rc.1); the release file is named like build_deb.sh names it (channelview_2.0.0-rc.1_all.deb)."""
        name = f"channelview_{version.replace('~', '-')}_all.deb"
        data = deb if deb is not None else (make_deb(self.tmp, version) if HAVE_DPKG else b"not a real deb")
        assets = {name: data}
        if sums:
            assets["SHA256SUMS"] = sums_for(name, data)
        self.gh.add_release(tag or "v" + version.replace("~", "-"), assets, **kw)
        return name, data

    def go(self, *args, defaults=None):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--repo", "owner/name", "--api-url", self.base, "--download-dir", self.tmp + "/dl", *args]
        if defaults is not None:
            Path(self.tmp, "defaults").write_text(defaults)
            argv += ["--defaults-file", self.tmp + "/defaults"]
        else:
            argv += ["--defaults-file", self.tmp + "/none"]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = upd.main(argv)
        return rc, out.getvalue(), err.getvalue()


class VersionOrder(unittest.TestCase):
    PAIRS = [("1.0.0", "1.0.1"), ("1.0.9", "1.0.10"), ("1.9.0", "1.10.0"), ("1.0.0~rc.1", "1.0.0"), ("1.0.0~rc.1", "1.0.0~rc.2"), ("1.0.0~rc.9", "1.0.0~rc.10"),
             ("1.0.0", "1.0.0+1"), ("1.0.0", "1:0.1"), ("1.0", "1.0.0"), ("2.0.0~beta", "2.0.0~rc"), ("1.0-1", "1.0-2"), ("1.0.0", "1.0.0")]

    def test_matches_dpkg(self):
        for a, b in self.PAIRS:
            for x, y in ((a, b), (b, a)):
                got = upd.compare_versions(x, y)
                if shutil.which("dpkg"):
                    lt = subprocess.run(["dpkg", "--compare-versions", x, "lt", y]).returncode == 0
                    eq = subprocess.run(["dpkg", "--compare-versions", x, "eq", y]).returncode == 0
                    self.assertEqual(got, -1 if lt else 0 if eq else 1, f"{x} vs {y}")
                else:
                    self.assertEqual(got, -1 if (x, y) == (a, b) and a != b else got)       # no dpkg here: only check it does not crash
        self.assertEqual(upd.compare_versions("1.0.0~rc.1", "1.0.0"), -1)
        self.assertEqual(upd.compare_versions("1.0.10", "1.0.9"), 1)


class Settings(Base):
    def test_defaults_file_parsing_and_placeholder_repo(self):
        Path(self.tmp, "d").write_text("# c\nCHANNELVIEW_UPDATE_REPO='me/app'\n\nCHANNELVIEW_UPDATE_CHANNEL=prerelease\nBROKEN\nCHANNELVIEW_ARGS=\n")
        d = upd.read_defaults(self.tmp + "/d")
        self.assertEqual((d["CHANNELVIEW_UPDATE_REPO"], d["CHANNELVIEW_UPDATE_CHANNEL"], d["CHANNELVIEW_ARGS"]), ("me/app", "prerelease", ""))
        self.assertEqual(upd.read_defaults(self.tmp + "/nope"), {})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = upd.main(["--defaults-file", self.tmp + "/dd", "--api-url", self.base])
        self.assertEqual(rc, 1)
        self.assertIn("no GitHub repository is set", err.getvalue())
        Path(self.tmp, "dd").write_text("CHANNELVIEW_UPDATE_REPO=OWNER/REPO\n")
        with contextlib.redirect_stderr(err):
            self.assertEqual(upd.main(["--defaults-file", self.tmp + "/dd", "--api-url", self.base]), 1)
        self.assertIn("CHANNELVIEW_UPDATE_REPO", err.getvalue())

    def test_repo_comes_from_the_defaults_file(self):
        self.release("1.0.0")
        out = io.StringIO()
        Path(self.tmp, "dd").write_text("CHANNELVIEW_UPDATE_REPO=owner/name\n")
        with contextlib.redirect_stdout(out):
            rc = upd.main(["--check", "--api-url", self.base, "--defaults-file", self.tmp + "/dd"])
        self.assertEqual(rc, 0)
        self.assertIn("Already up to date", out.getvalue())


class Checking(Base):
    def test_up_to_date(self):
        self.release("1.0.0")
        rc, out, _ = self.go("--check")
        self.assertEqual(rc, 0)
        self.assertIn("Already up to date", out)

    def test_check_reports_an_update_with_exit_10_and_quiet_prints_nothing(self):
        self.release("1.0.0")
        self.release("1.1.0")
        rc, out, _ = self.go("--check")
        self.assertEqual((rc, "Update available: 1.0.0 -> 1.1.0" in out), (10, True))
        rc, out, err = self.go("--check", "--quiet")
        self.assertEqual((rc, out, err), (10, "", ""))
        self.assertEqual(self.installs, [])

    def test_a_newer_installed_version_is_left_alone(self):
        self.release("1.0.0")
        self.installed = "1.2.0"
        rc, out, _ = self.go("--yes")
        self.assertEqual((rc, self.installs), (0, []))
        self.assertIn("newer", out)

    def test_stable_channel_ignores_prereleases_and_drafts(self):
        self.release("1.0.0")
        self.release("2.0.0~rc.1", prerelease=True)
        self.release("3.0.0", draft=True)
        rc, out, _ = self.go("--check")
        self.assertEqual(rc, 0, out)

    def test_prerelease_channel_picks_the_highest_version(self):
        self.release("1.0.0")
        self.release("2.0.0~rc.1", prerelease=True)
        rc, out, _ = self.go("--check", "--channel", "prerelease")
        self.assertEqual((rc, "1.0.0 -> 2.0.0~rc.1" in out), (10, True), out)
        self.release("2.0.0")
        rc, out, _ = self.go("--check", "--channel", "prerelease")
        self.assertIn("1.0.0 -> 2.0.0\n", out)
        rc, out, _ = self.go("--check", defaults="CHANNELVIEW_UPDATE_CHANNEL=prerelease\n")
        self.assertEqual(rc, 10, "the channel can come from the defaults file")

    def test_prerelease_files_have_names_that_survive_github(self):
        """GitHub turns a ~ in an asset name into a . - so the file is called ..._2.0.0-rc.1_... while the package says 2.0.0~rc.1."""
        name, _ = self.release("2.0.0~rc.1", prerelease=True)
        self.assertNotIn("~", name)
        self.assertEqual(self.gh.github_name("channelview_2.0.0~rc.1_all.deb"), "channelview_2.0.0.rc.1_all.deb")
        self.assertEqual(upd.asset_version({"name": name}), "2.0.0~rc.1")
        rc, out, err = self.go("--yes", "--channel", "prerelease")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.installed, "2.0.0~rc.1")
        self.assertEqual(len(self.installs), 1)
        self.assertEqual(self.go("--yes", "--version", "2.0.0-rc.1", "--force")[0], 0, "a pre-release can be pinned by its tag")

    def test_no_release_yet_and_wrong_repo(self):
        rc, _, err = self.go("--check")
        self.assertEqual(rc, 1)
        self.assertIn("no stable release", err)
        self.release("1.0.0")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = upd.main(["--repo", "someone/else", "--api-url", self.base, "--defaults-file", self.tmp + "/none"])
        self.assertEqual(rc, 1)
        self.assertIn("404", err.getvalue())

    def test_rate_limit_message(self):
        self.release("1.0.0")
        self.gh.rate_limited = True
        rc, _, err = self.go("--check")
        self.assertEqual(rc, 1)
        self.assertIn("rate limit", err)

    def test_api_url_must_be_http_or_https(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = upd.main(["--repo", "o/n", "--api-url", "file:///etc", "--defaults-file", self.tmp + "/none"])
        self.assertEqual(rc, 1)
        self.assertIn("bad API address", err.getvalue())

    def test_plain_http_is_refused_unless_the_api_address_itself_is_http(self):
        with self.assertRaises(upd.Fail):
            upd.Http("https://api.github.com", "")._request("http://example.com/x.deb", "*/*")
        upd.Http(self.base, "")._request(self.base + "/x", "*/*")      # explicit test / mirror address: allowed


@unittest.skipUnless(HAVE_DPKG, "needs dpkg-deb")
class Installing(Base):
    def test_download_check_and_install(self):
        name, data = self.release("1.1.0")
        rc, out, err = self.go("--yes")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(self.installs), 1)
        path, downgrade, reinstall = self.installs[0]
        self.assertEqual((path.name, downgrade, reinstall), (name, False, False))
        self.assertIn("SHA-256 matches", out)
        self.assertIn("What is new", out)
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o644", "apt's sandbox user must be able to read it")

    def test_download_only_keeps_the_checked_file(self):
        name, data = self.release("1.1.0")
        rc, out, _ = self.go("--download-only")
        self.assertEqual((rc, self.installs), (0, []))
        self.assertEqual(Path(self.tmp, "dl", name).read_bytes(), data)
        self.assertIn("sudo apt install", out)

    def test_temporary_download_is_removed_after_installing(self):
        cache = Path(self.tmp, "cache")
        self.release("1.1.0")
        argv = ["--repo", "owner/name", "--api-url", self.base, "--yes", "--defaults-file", self.tmp + "/none"]
        with mock.patch.object(upd, "CACHE_DIR", str(cache)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(upd.main(argv), 0)
        self.assertEqual(len(self.installs), 1)
        self.assertEqual(list(cache.iterdir()), [], "the dl-* folder is gone")
        self.assertEqual(oct(cache.stat().st_mode & 0o777), "0o755")

    def test_checksum_mismatch_installs_nothing(self):
        name = "channelview_1.1.0_all.deb"
        good = make_deb(self.tmp, "1.1.0")
        self.gh.add_release("v1.1.0", {name: good + b"tampered", "SHA256SUMS": sums_for(name, good)})
        rc, _, err = self.go("--yes")
        self.assertEqual((rc, self.installs), (1, []))
        self.assertIn("does not match", err)

    def test_missing_sums_file_or_line_is_refused(self):
        self.release("1.1.0", sums=False)
        rc, _, err = self.go("--yes")
        self.assertEqual((rc, self.installs), (1, []))
        self.assertIn("no SHA256SUMS", err)
        name = "channelview_1.2.0_all.deb"
        data = make_deb(self.tmp, "1.2.0")
        self.gh.add_release("v1.2.0", {name: data, "SHA256SUMS": sums_for("other.deb", data)})
        rc, _, err = self.go("--yes")
        self.assertEqual((rc, self.installs), (1, []))
        self.assertIn("no line for", err)

    def test_the_file_must_be_the_package_of_that_version(self):
        for version, deb in (("1.1.0", make_deb(self.tmp, "1.1.5")), ("1.2.0", make_deb(self.tmp, "1.2.0", package="other")), ("1.3.0", b"garbage")):
            name = f"channelview_{version}_all.deb"
            self.gh.add_release("v" + version, {name: deb, "SHA256SUMS": sums_for(name, deb)})
            rc, _, err = self.go("--yes", "--version", version)
            self.assertEqual((rc, self.installs), (1, []), version)
            self.assertTrue("not channelview" in err or "not a valid" in err, err)

    def test_pinned_version_older_than_installed_needs_force(self):
        self.release("1.0.1")
        self.release("1.1.0")
        self.installed = "1.1.0"
        rc, out, _ = self.go("--yes", "--version", "1.0.1")
        self.assertEqual((rc, self.installs), (0, []))
        self.assertIn("--force", out)
        rc, out, err = self.go("--yes", "--version", "v1.0.1", "--force")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([(p.name, d, r) for p, d, r in self.installs], [("channelview_1.0.1_all.deb", True, False)])

    def test_force_reinstalls_the_same_version(self):
        self.release("1.0.0")
        rc, _, _ = self.go("--yes", "--force")
        self.assertEqual(rc, 0)
        self.assertEqual([(p.name, d, r) for p, d, r in self.installs], [("channelview_1.0.0_all.deb", False, True)])

    def test_unknown_pinned_version(self):
        self.release("1.0.0")
        rc, _, err = self.go("--yes", "--version", "9.9.9")
        self.assertEqual(rc, 1)
        self.assertIn("no release tagged v9.9.9", err)

    def test_needs_root_and_needs_a_yes_without_a_terminal(self):
        self.release("1.1.0")
        with mock.patch.object(upd.os, "geteuid", lambda: 1000):
            rc, _, err = self.go("--yes")
        self.assertEqual((rc, self.installs), (1, []))
        self.assertIn("needs root", err)
        with mock.patch.object(upd.sys.stdin, "isatty", lambda: False):
            rc, _, err = self.go()
        self.assertEqual((rc, self.installs), (1, []))
        self.assertIn("add --yes", err)

    def test_answering_no_cancels(self):
        self.release("1.1.0")
        with mock.patch.object(upd.sys.stdin, "isatty", lambda: True), mock.patch("builtins.input", lambda prompt="": "n"):
            rc, out, _ = self.go()
        self.assertEqual((rc, self.installs), (0, []))
        self.assertIn("Cancelled", out)

    def test_install_failure_is_reported(self):
        self.release("1.1.0")

        def boom(path, allow_downgrade, reinstall=False):
            raise upd.Fail("the installation failed")
        with mock.patch.object(upd, "run_install", boom):
            rc, _, err = self.go("--yes")
        self.assertEqual(rc, 1)
        self.assertIn("installation failed", err)


@unittest.skipUnless(HAVE_DPKG and HAVE_GPG, "needs dpkg-deb and gpg")
class Signatures(Base):
    def setUp(self):
        super().setUp()
        self.gnupg = tempfile.mkdtemp(prefix="g", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.gnupg, True)
        self.env = dict(os.environ, GNUPGHOME=self.gnupg)
        gpg = lambda *a, **k: subprocess.run(["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase", "", *a], env=self.env, capture_output=True, check=True, **k)
        self.gpg = gpg
        gpg("--quick-generate-key", "Test Releases <t@example.com>", "ed25519", "sign", "never")
        self.keyring = self.tmp + "/release-key.gpg"
        Path(self.keyring).write_bytes(gpg("--export").stdout)

    def signed_release(self, version, armor=False, tamper=False):
        name = f"channelview_{version}_all.deb"
        data = make_deb(self.tmp, version)
        sums = sums_for(name, data)
        sfile = Path(self.tmp, "SHA256SUMS")
        sfile.write_bytes(sums)
        sig = Path(self.tmp, "sig")
        self.gpg(*(["--armor"] if armor else []), "--detach-sign", "--output", str(sig), str(sfile))
        assets = {name: data, "SHA256SUMS": sums + (b"# extra\n" if tamper else b""), ("SHA256SUMS.asc" if armor else "SHA256SUMS.sig"): sig.read_bytes()}
        self.gh.add_release("v" + version, assets)

    def test_valid_signatures_pass_binary_and_armored(self):
        for version, armor in (("1.1.0", False), ("1.2.0", True)):
            self.signed_release(version, armor=armor)
            rc, out, err = self.go("--yes", "--keyring", self.keyring)
            self.assertEqual(rc, 0, out + err)
            self.assertIn("Signature is valid", out)
        self.assertEqual(len(self.installs), 2)

    def test_tampered_sums_are_refused(self):
        self.signed_release("1.1.0", tamper=True)
        rc, _, err = self.go("--yes", "--keyring", self.keyring)
        self.assertEqual((rc, self.installs), (1, []))
        self.assertIn("signature", err)

    def test_a_key_that_did_not_sign_it_is_refused(self):
        self.signed_release("1.1.0")
        other = tempfile.mkdtemp(prefix="g", dir="/tmp")
        self.addCleanup(shutil.rmtree, other, True)
        env = dict(os.environ, GNUPGHOME=other)
        subprocess.run(["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "", "--quick-generate-key", "Other <o@example.com>", "ed25519", "sign", "never"], env=env, capture_output=True, check=True)
        Path(self.tmp, "other.gpg").write_bytes(subprocess.run(["gpg", "--export"], env=env, capture_output=True, check=True).stdout)
        rc, _, err = self.go("--yes", "--keyring", self.tmp + "/other.gpg")
        self.assertEqual((rc, self.installs), (1, []))
        self.assertIn("not valid", err)

    def test_when_a_keyring_exists_an_unsigned_release_is_refused(self):
        self.release("1.1.0")
        rc, _, err = self.go("--yes", "--keyring", self.keyring)
        self.assertEqual((rc, self.installs), (1, []))
        self.assertIn("no SHA256SUMS.sig", err)

    def test_the_default_keyring_path_is_used_when_it_exists(self):
        self.release("1.1.0")
        shutil.copy(self.keyring, self.tmp + "/no-such-keyring.gpg")      # DEFAULT_KEYRING is patched to this path
        rc, _, err = self.go("--yes")
        self.assertEqual((rc, self.installs), (1, []))
        self.assertIn("must be signed", err)


class PrivateRepository(Base):
    def setUp(self):
        super().setUp()
        self.gh.stop()
        self.gh = MockGitHub("owner/name", token="s3cret")
        self.base = self.gh.start()
        self.addCleanup(self.gh.stop)
        Path(self.tmp, "token").write_text("s3cret\n")
        os.chmod(self.tmp + "/token", 0o600)

    def test_without_a_token_it_is_not_found(self):
        self.release("1.1.0")
        rc, _, err = self.go("--check")
        self.assertEqual(rc, 1)
        self.assertIn("CHANNELVIEW_UPDATE_TOKEN_FILE", err)

    def test_the_token_goes_to_the_api_but_never_to_the_other_host(self):
        if not HAVE_DPKG:
            self.skipTest("needs dpkg-deb")
        self.release("1.1.0")
        rc, out, err = self.go("--yes", "--token-file", self.tmp + "/token")
        self.assertEqual(rc, 0, out + err)
        api = [a for kind, path, a in self.gh.seen if kind == "GET" and path.startswith("/repos/")]
        cdn = [a for kind, path, a in self.gh.seen if kind == "CDN"]
        self.assertTrue(api and all(a == "Bearer s3cret" for a in api), api)
        self.assertEqual(len(cdn), 2)
        self.assertEqual(cdn, [None, None], "the CDN host must not see the token")

    def test_public_download_sends_no_token_at_all(self):
        if not HAVE_DPKG:
            self.skipTest("needs dpkg-deb")
        self.gh.stop()
        self.gh = MockGitHub("owner/name")
        self.base = self.gh.start()
        self.release("1.1.0")
        rc, out, err = self.go("--yes")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(all(a is None for _, _, a in self.gh.seen))

    def test_token_file_problems(self):
        rc, _, err = self.go("--check", "--token-file", self.tmp + "/missing")
        self.assertEqual(rc, 1)
        self.assertIn("cannot read the token file", err)
        os.chmod(self.tmp + "/token", 0o644)
        self.release("1.0.0")
        with contextlib.redirect_stderr(io.StringIO()) as e:
            upd.read_token(self.tmp + "/token")
        self.assertIn("can be read by other users", e.getvalue())


if __name__ == "__main__":
    unittest.main()
