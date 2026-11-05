#!/usr/bin/env python3
"""Keep the "What's new" part of README.md in step with CHANGELOG.md.

    python3 packaging/whats_new.py [--keep N]

Every released version in CHANGELOG.md ("## [1.2.3] - 2026-01-31") becomes a segment headed "What's new in 1.2.3" between the two markers

    <!-- whats-new:start ... -->
    <!-- whats-new:end -->

in README.md, newest first, the latest N (default 3; older ones stay in CHANGELOG.md). The block is rebuilt from the CHANGELOG each time, so
running it twice changes nothing, and what to edit is always the CHANGELOG. Pre-releases (1.2.3-rc.1) and "[Unreleased]" are not shown.
packaging/release.sh runs this after it has written the new version's section. Without a README.md or without the markers it does nothing
(it says so) - a release is never held up by it.   Standard library only.
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START = "<!-- whats-new:start - made from CHANGELOG.md by packaging/whats_new.py (release.sh runs it); edit CHANGELOG.md, not this block -->"
END = "<!-- whats-new:end -->"
SECTION = re.compile(r"^## \[(\d+\.\d+\.\d+)\][ \t]*-[ \t]*(\S+)[ \t]*\n(.*?)(?=^## \[|\Z)", re.S | re.M)
BLOCK = re.compile(r"<!-- whats-new:start[^\n]*?-->\n.*?" + re.escape(END), re.S)


def segments(changelog, keep):
    out = []
    for version, day, body in SECTION.findall(changelog)[:keep]:
        body = body.strip("\n")
        if body.strip():
            out.append(f"### What's new in {version}\n\n*{day}*\n\n{body}\n")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--keep", type=int, default=3, metavar="N", help="how many versions to show (default 3)")
    ap.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    args = ap.parse_args()
    root = Path(args.root)
    readme, changelog = root / "README.md", root / "CHANGELOG.md"
    if not readme.is_file() or not changelog.is_file():
        print("whats_new: README.md or CHANGELOG.md not found - skipped")
        return 0
    text = readme.read_text(encoding="utf-8")
    if len(BLOCK.findall(text)) != 1:
        print(f"whats_new: README.md needs exactly one pair of {END[:17]}... markers - skipped")
        return 0
    segs = segments(changelog.read_text(encoding="utf-8"), max(args.keep, 1))
    if not segs:
        print("whats_new: no released version in CHANGELOG.md - skipped")
        return 0
    block = START + "\n" + "\n".join(segs) + END
    new = BLOCK.sub(lambda _m: block, text, count=1)
    if new == text:
        print("whats_new: README.md already up to date")
    else:
        readme.write_text(new, encoding="utf-8")
        print(f"whats_new: README.md now shows {len(segs)} version(s), newest {re.search(r'in (\S+)', segs[0]).group(1)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
