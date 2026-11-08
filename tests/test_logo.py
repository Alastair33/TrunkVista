"""The logo in the program (header mark, sign-in page, tab icons) must be the one in docs/logo, and the logo files the README shows must exist.
Run:  python3 -m unittest discover -s tests -v"""
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Logo(unittest.TestCase):
    def test_the_program_has_the_logo_of_docs_logo(self):
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "embed_logo.py"), "--check"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_the_standalone_demo_has_the_same_header_mark(self):
        page = (ROOT / "trunkvista.py").read_text(encoding="utf-8")
        demo = (ROOT / "trunkvista_demo.html").read_text(encoding="utf-8")
        mark = re.search(r'<span class="logo">\s*(<svg\b.*?</svg>)', page, re.S).group(1)
        self.assertIn(mark, demo, "rebuild the demo: python3 tools/build_demo.py trunkvista_demo.html")

    def test_the_header_mark_follows_the_accent_colour(self):
        page = (ROOT / "trunkvista.py").read_text(encoding="utf-8")
        mark = re.search(r'<span class="logo">\s*(<svg\b.*?</svg>)', page, re.S).group(1)
        self.assertIn('class="trunk"', mark)
        self.assertIn('class="node"', mark)
        for rule in (".logo .node{fill:var(--accent)}", ".logo .trunk{stroke:var(--accent)}",
                     "#login .llogo .node{fill:var(--accent)}", "#login .llogo .trunk{stroke:var(--accent)}"):
            self.assertIn(rule, page)

    def test_the_files_the_readme_shows_exist(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for path in set(re.findall(r'(?:src|srcset)="(docs/[^"]+)"', readme)) | set(re.findall(r"\]\((docs/[^)#]+)\)", readme)):
            self.assertTrue((ROOT / path).exists(), path + " is in the README but not in the repository")
        for name in ("trunkvista-logo-dark.svg", "trunkvista-logo-light.svg", "trunkvista-mark.svg", "trunkvista-icon.svg", "favicon.svg",
                     "favicon.ico", "social-preview.png", "source/tv_mark.py", "source/build_logo.py", "source/render_png.py"):
            self.assertTrue((ROOT / "docs" / "logo" / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
