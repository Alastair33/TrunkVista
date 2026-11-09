"""packaging/release.sh in a throw-away git repository: version bumps, the CHANGELOG, the checks it makes before touching anything, and the
roll-back when the tests fail.   (Needs git and bash; the script itself runs `python3 -m unittest` and tools/build_demo.py.)"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HAVE_GIT = shutil.which("git") is not None
HAVE_DPKG = shutil.which("dpkg") is not None
ENV = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@example.invalid",
           GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")

CHANGELOG = """# Changelog

## [Unreleased]

- Darker header.

## [1.0.0] - 2026-01-01

First.
"""


@unittest.skipUnless(HAVE_GIT, "git not found")
class ReleaseScript(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo, self.remote = self.tmp / "repo", self.tmp / "remote.git"
        for rel in ("trunkvista.py", "tools/build_demo.py", "tools/demo_shim.js", "packaging/release.sh", "packaging/whats_new.py", ".gitignore"):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / rel, self.repo / rel)
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_fine.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
        text = (self.repo / "trunkvista.py").read_text()
        (self.repo / "trunkvista.py").write_text(re.sub(r'^VERSION = "[^"]*"', 'VERSION = "1.0.0"', text, count=1, flags=re.M))
        (self.repo / "CHANGELOG.md").write_text(CHANGELOG)
        self.git("init", "-q", "-b", "main")
        subprocess.run(["python3", "tools/build_demo.py", "trunkvista_demo.html"], cwd=self.repo, check=True, capture_output=True)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "start")
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True, env=ENV)
        self.git("remote", "add", "origin", str(self.remote))

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True, env=ENV).stdout.strip()

    def release(self, *args):
        r = subprocess.run(["bash", "packaging/release.sh", *args], cwd=self.repo, capture_output=True, text=True, env=ENV, timeout=120)
        return r.returncode, r.stdout + r.stderr

    def version(self):
        return re.search(r'^VERSION = "([^"]*)"', (self.repo / "trunkvista.py").read_text(), re.M).group(1)

    def changelog(self):
        return (self.repo / "CHANGELOG.md").read_text()

    def add_note(self, text):
        c = self.changelog().replace("## [Unreleased]\n", "## [Unreleased]\n\n" + text + "\n", 1)
        (self.repo / "CHANGELOG.md").write_text(c)
        self.git("commit", "-q", "-am", "note")

    README = "# Prog\n\nIntro.\n\n## Release notes\n\n<!-- whats-new:start -->\n<!-- whats-new:end -->\n\n## Install\n\nText.\n"

    def add_readme(self, text=None):
        (self.repo / "README.md").write_text(self.README if text is None else text)
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "readme")

    def readme(self):
        return (self.repo / "README.md").read_text()

    def assert_untouched(self, head):
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("tag"), "")

    def test_patch_turns_unreleased_into_the_new_section_and_tags(self):
        rc, out = self.release("patch")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.version(), "1.0.1")
        c = self.changelog()
        self.assertRegex(c, r"## \[Unreleased\]\n\n## \[1\.0\.1\] - " + date.today().isoformat() + r"\n\n- Darker header\.\n\n## \[1\.0\.0\]")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Release v1.0.1")
        self.assertEqual(self.git("cat-file", "-t", "v1.0.1"), "tag", "an annotated tag")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertIn("git push origin main v1.0.1", out)
        fresh = self.tmp / "fresh_demo.html"
        subprocess.run(["python3", "tools/build_demo.py", str(fresh)], cwd=self.repo, check=True, capture_output=True)
        self.assertEqual((self.repo / "trunkvista_demo.html").read_bytes(), fresh.read_bytes(), "the demo page was rebuilt for the new version")

    def test_minor_and_major(self):
        self.assertEqual(self.release("minor")[0], 0)
        self.assertEqual(self.version(), "1.1.0")
        self.add_note("- Something big.")
        self.assertEqual(self.release("major")[0], 0)
        self.assertEqual(self.version(), "2.0.0")
        self.assertEqual(self.git("tag", "--list").split(), ["v1.1.0", "v2.0.0"])

    def test_nothing_to_release_is_refused_before_anything_changes(self):
        self.assertEqual(self.release("patch")[0], 0)
        head = self.git("rev-parse", "HEAD")
        rc, out = self.release("patch")
        self.assertEqual(rc, 1)
        self.assertIn("nothing under '## [Unreleased]'", out)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.version(), "1.0.1")

    def test_failing_tests_put_everything_back(self):
        (self.repo / "tests" / "test_bad.py").write_text("import unittest\nclass B(unittest.TestCase):\n    def test_bad(self):\n        self.fail('boom')\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "a failing test")
        head = self.git("rev-parse", "HEAD")
        rc, out = self.release("patch")
        self.assertEqual(rc, 1)
        self.assertIn("the tests fail", out)
        self.assert_untouched(head)
        self.assertEqual(self.version(), "1.0.0")
        self.assertIn("- Darker header.", self.changelog().split("## [1.0.0]")[0].split("## [Unreleased]")[1], "the notes are back under [Unreleased]")
        self.assertEqual(self.release("--no-tests", "patch")[0], 0, "the escape hatch exists")

    def test_the_checks_before_touching_anything(self):
        head = self.git("rev-parse", "HEAD")
        for args, text in ((("1.2",), "is not a version"), (("patch", "minor"), "one version only"), (("--bogus", "patch"), "unknown option")):
            rc, out = self.release(*args)
            self.assertEqual((rc, text in out), (1, True), (args, out))
        (self.repo / "dirty.txt").write_text("x")
        rc, out = self.release("patch")
        self.assertEqual((rc, "uncommitted changes" in out), (1, True), out)
        (self.repo / "dirty.txt").unlink()
        self.git("checkout", "-q", "-b", "feature")
        rc, out = self.release("patch")
        self.assertEqual((rc, "releases are made from main" in out), (1, True), out)
        self.assertEqual(self.release("--any-branch", "--no-tests", "patch")[0], 0)
        self.git("checkout", "-q", "main")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("tag", "--list"), "v1.0.1", "only the one made on the feature branch")

    @unittest.skipUnless(HAVE_DPKG, "dpkg compares the versions")
    def test_not_newer_and_existing_tags_are_refused(self):
        self.assertEqual(self.release("--no-tests", "1.0.1")[0], 0)
        self.add_note("- More.")
        rc, out = self.release("1.0.1")
        self.assertEqual((rc, "already exists" in out), (1, True), out)
        rc, out = self.release("0.9.0")
        self.assertEqual((rc, "not newer" in out), (1, True), out)
        self.assertEqual(self.release("--no-tests", "1.1.0-rc.1")[0], 0, "pre-release")
        c = self.changelog()
        self.assertIn("## [1.1.0-rc.1] - ", c)
        self.assertIn("- More.", c.split("## [1.1.0-rc.1]")[0], "the notes are still under [Unreleased]")
        rc, out = self.release("patch")
        self.assertEqual((rc, "give the new version explicitly" in out), (1, True), "patch of a pre-release is ambiguous: " + out)
        self.assertEqual(self.release("--no-tests", "1.1.0")[0], 0, "the final release after the rc")
        self.assertEqual(self.version(), "1.1.0")
        self.assertRegex(self.changelog(), r"## \[Unreleased\]\n\n## \[1\.1\.0\] - [\d-]+\n\n- More\.\n\n## \[1\.1\.0-rc\.1\]")

    def test_the_first_release_needs_no_bump_and_pushing_works(self):
        c = self.changelog().replace("## [Unreleased]\n\n- Darker header.\n", "## [Unreleased]\n")
        (self.repo / "CHANGELOG.md").write_text(c)
        self.git("commit", "-q", "-am", "notes already in the 1.0.0 section")
        head = self.git("rev-parse", "HEAD")
        rc, out = self.release("--push", "1.0.0")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.git("rev-parse", "HEAD"), head, "no release commit: VERSION and CHANGELOG already say 1.0.0")
        self.assertIn("nothing to commit", out)
        remote_tags = subprocess.run(["git", "--git-dir", str(self.remote), "tag"], capture_output=True, text=True).stdout.split()
        self.assertEqual(remote_tags, ["v1.0.0"])
        branches = subprocess.run(["git", "--git-dir", str(self.remote), "branch"], capture_output=True, text=True).stdout
        self.assertIn("main", branches)

    def test_each_release_gets_a_whats_new_segment_in_the_readme(self):
        self.add_readme()
        self.assertEqual(self.release("patch")[0], 0)
        r, today = self.readme(), date.today().isoformat()
        self.assertRegex(r, r"<!-- whats-new:start[^\n]*-->\n### What's new in 1\.0\.1\n\n\*" + today + r"\*\n\n- Darker header\.\n\n"
                            r"### What's new in 1\.0\.0\n\n\*2026-01-01\*\n\nFirst\.\n<!-- whats-new:end -->\n\n## Install")
        self.assertTrue(r.startswith("# Prog\n\nIntro.\n\n## Release notes\n\n<!-- whats-new:start"), "text around the markers is untouched")
        self.assertTrue(r.endswith("## Install\n\nText.\n"))
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertIn("README.md", self.git("show", "--stat", "--format=", "HEAD"), "part of the release commit")
        self.add_note("- Second change.")
        self.assertEqual(self.release("minor")[0], 0)
        r = self.readme()
        self.assertEqual(r.count("### What's new in"), 3)
        self.assertLess(r.index("What's new in 1.1.0"), r.index("What's new in 1.0.1"), "newest first")
        self.assertIn("- Second change.", r.split("What's new in 1.0.1")[0])
        again = subprocess.run(["python3", "packaging/whats_new.py"], cwd=self.repo, capture_output=True, text=True)
        self.assertIn("already up to date", again.stdout)
        subprocess.run(["python3", "packaging/whats_new.py", "--keep", "1"], cwd=self.repo, check=True, capture_output=True)
        self.assertEqual(self.readme().count("### What's new in"), 1, "--keep N limits how many versions are shown")

    def test_pre_releases_stay_out_of_the_readme_and_a_readme_without_markers_is_left_alone(self):
        self.add_readme()
        self.assertEqual(self.release("--no-tests", "1.1.0-rc.1")[0], 0)
        self.assertEqual(self.readme(), self.README, "a pre-release is not announced")
        self.assertEqual(self.release("--no-tests", "1.1.0")[0], 0)
        self.assertIn("### What's new in 1.1.0\n\n*", self.readme())
        self.assertNotIn("rc.1", self.readme())
        plain = "# Prog\n\nNo markers here.\n"
        self.add_readme(plain)
        self.add_note("- Another.")
        rc, out = self.release("--no-tests", "patch")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.readme(), plain)
        self.assertIn("skipped", out)

    def test_failing_tests_put_the_readme_back_too(self):
        self.add_readme()
        (self.repo / "tests" / "test_bad.py").write_text("import unittest\nclass B(unittest.TestCase):\n    def test_bad(self):\n        self.fail('boom')\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "a failing test")
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(self.release("patch")[0], 1)
        self.assert_untouched(head)
        self.assertEqual(self.readme(), self.README)


class ReadmeInSync(unittest.TestCase):
    def test_whats_new_in_the_readme_matches_the_changelog(self):
        """The 'What's new' block of README.md is made from CHANGELOG.md; this fails when one was edited without the other.
        Fix:  python3 packaging/whats_new.py"""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        for name in ("README.md", "CHANGELOG.md"):
            shutil.copy(ROOT / name, tmp / name)
        r = subprocess.run(["python3", str(ROOT / "packaging" / "whats_new.py"), "--root", str(tmp)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        if "<!-- whats-new:start" in (ROOT / "README.md").read_text(encoding="utf-8"):
            self.assertIn("up to date", r.stdout, "README.md is behind CHANGELOG.md - run: python3 packaging/whats_new.py\n" + r.stdout)
        else:      # the short README has no release-notes block (CHANGELOG.md and the Releases page have them): the script must leave it alone
            self.assertIn("skipped", r.stdout)
        self.assertEqual((tmp / "README.md").read_text(), (ROOT / "README.md").read_text())


if __name__ == "__main__":
    unittest.main()
