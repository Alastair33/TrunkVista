"""The sign-in page opens with a short intro (logo and name fade in, slide left, the form appears). It must stay CSS-only, skippable and
harmless for people who asked for less motion.   Run:  python3 -m unittest discover -s tests -v"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("trunkvista_under_test", ROOT / "trunkvista.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class LoginIntro(unittest.TestCase):
    page = mod.login_page("")

    def test_brand_and_form_are_both_on_the_page_whatever_the_animation_does(self):
        self.assertRegex(self.page, r'<div class="brand">\s*<svg class="llogo"')
        self.assertIn("<h1>TrunkVista</h1>", self.page)
        for needle in ('id="lf"', 'id="lu"', 'id="lp"', 'id="lgo"'):
            self.assertIn(needle, self.page)

    def test_the_intro_can_be_skipped_or_switched_off(self):
        self.assertIn("prefers-reduced-motion:reduce", self.page)
        self.assertIn(".noint #login .brand,.noint #login .lbox{animation:none}", self.page)
        self.assertIn('addEventListener("keydown", skip', self.page)
        self.assertIn('addEventListener("pointerdown", skip', self.page)
        self.assertIn("[?&]intro(&|$)/.test(location.search)", self.page)      # a repeat visit in the tab skips the intro, unless ?intro
        self.assertNotIn("\x08", self.page, "a backslash-b in the Python source became a backspace character")

    def test_the_animation_needs_nothing_from_outside_and_is_over_in_a_few_seconds(self):
        self.assertNotRegex(self.page, r"(?i)(src|href)=[\"']https?:")
        self.assertNotIn("@import", self.page)
        css = self.page.split("/* Intro:", 1)[1].split("</style>", 1)[0]
        self.assertIn("tv-slide .9s cubic-bezier(.65,0,.25,1) 1.5s both", css)      # slide ends at 2.4 s
        self.assertIn("animation:tv-box .8s ease-out 1.7s both", css)      # 1.7 + .8 = 2.5 s


if __name__ == "__main__":
    unittest.main()
