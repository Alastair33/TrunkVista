"""The instructions and the release workflow must agree: people are told to download from the LATEST release, under file names that the
workflow really attaches to every release.   Run:  python3 -m unittest discover -s tests -v"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
ADMIN = (ROOT / "docs" / "administration.md").read_text(encoding="utf-8")      # the long install steps, accounts, HTTPS, updates (the README is the short version)
WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
FIXED_NAMES = ("trunkvista-latest.tar.gz", "install.sh", "trunkvista-latest.deb", "trunkvista.py", "trunkvista_demo.html", "SHA256SUMS")


class DownloadInstructions(unittest.TestCase):
    def test_the_workflow_attaches_every_fixed_name_and_checksums_them(self):
        sums_line = next(line for line in WORKFLOW.splitlines() if "sha256sum" in line and "> SHA256SUMS" in line)
        for name in ("trunkvista.py", "trunkvista_demo.html"):
            self.assertIn(name, sums_line, name + " must be listed in SHA256SUMS")
            self.assertRegex(WORKFLOW, r"cp [^\n]*" + re.escape(name) + r"[^\n]* dist/", name + " must be copied into dist/")
        self.assertIn("*.deb", sums_line, "both .deb files (versioned and fixed name) must be listed in SHA256SUMS")
        self.assertIn("install.sh", sums_line, "install.sh checks nothing of itself, but the list is the list of the release")
        self.assertIn("trunkvista-latest.tar.gz", sums_line, "the archive of the four-line installation is part of the release and its checksums")
        self.assertRegex(WORKFLOW, r"\./packaging/build_bundle\.sh \"\$1\" dist", "the installer and the archive are made by build_bundle.sh, which puts this repository's name into install.sh")
        self.assertIn("dist/trunkvista-latest.deb", WORKFLOW)
        self.assertIn("releases/latest", WORKFLOW, "the release notes must point to the latest release")

    def test_the_readme_sends_people_to_the_latest_release(self):
        self.assertRegex(README, r"(?m)^## Download$")
        self.assertRegex(README, r"\]\(https://github\.com/[\w.-]+/[\w.-]+/releases/latest\)", "a link to the latest release near the top")
        for name in FIXED_NAMES:
            self.assertRegex(README, r"releases/latest/download/" + re.escape(name) + r"\b", name + " needs its permanent address in the README")
        self.assertIn("Download ZIP", README, "the README must say why the ZIP button / a clone is not the way to get it")

    def test_the_installer_is_what_the_readme_says_it_is(self):
        installer = (ROOT / "packaging" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("@REPO@", installer, "install.sh in the source tree is a template; build_bundle.sh fills in the repository")
        self.assertIn("trunkvista-latest.deb", installer)
        self.assertIn("SHA256SUMS", installer)
        four = "wget https://github.com/Alastair33/TrunkVista/releases/latest/download/trunkvista-latest.tar.gz\ntar xzf trunkvista-latest.tar.gz\ncd trunkvista\nsudo ./install.sh\n"
        self.assertEqual(README.count(four), 1, "the four lines are in the quick start")
        self.assertEqual(README.count("releases/latest/download/install.sh | sudo sh"), 1, "the one-liner is in the quick start")
        self.assertIn(four, ADMIN, "the administration guide repeats the four lines")
        quick = README.split("## Quick start", 1)[1].split("\n## ", 1)[0]
        self.assertIn(four, quick)
        self.assertIn("administrator's password", quick)
        self.assertIn("Zabbix", quick)
        self.assertIn("Quick start", README.split("## Download", 1)[0], "the quick start comes before the download details")
        archive_notes = WORKFLOW.split("Release notes from CHANGELOG.md", 1)[1]
        for line in ("wget https://github.com/${GITHUB_REPOSITORY}/releases/latest/download/trunkvista-latest.tar.gz", "tar xzf trunkvista-latest.tar.gz", "cd trunkvista", "sudo ./install.sh"):
            self.assertIn(line, archive_notes, "the release notes carry the same four lines")

    def test_the_install_steps_do_not_name_a_version(self):
        """A hard-coded 'apt install ./trunkvista_1.1.0_all.deb' is stale with the next release and is a download of an old version."""
        install = ADMIN.split("## Install on Debian / Ubuntu", 1)[1].split("\n## ", 1)[0]
        self.assertIn("apt install ./trunkvista-latest.deb", install)
        self.assertNotRegex(install, r"trunkvista_\d[^\s`]*_all\.deb")

    def test_the_names_in_the_readme_are_the_names_the_workflow_makes(self):
        for name in re.findall(r"releases/latest/download/([\w.-]+)", README):
            self.assertIn(name, FIXED_NAMES + ("SHA256SUMS.sig",), name)
            if name != "SHA256SUMS.sig":
                self.assertIn(name, WORKFLOW)


if __name__ == "__main__":
    unittest.main()
