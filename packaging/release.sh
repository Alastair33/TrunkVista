#!/bin/bash
# Prepare a release:   packaging/release.sh [--push] [--any-branch] [--no-tests] patch | minor | major | X.Y.Z | X.Y.Z-rc.1
#
#   patch   1.4.2 -> 1.4.3   a fix or a small design change
#   minor   1.4.2 -> 1.5.0   a new feature
#   major   1.4.2 -> 2.0.0   something that changes how it is used
#   X.Y.Z-rc.1               a pre-release: only servers on the "prerelease" channel get it (try it on one server first); the notes stay under
#                            [Unreleased], so the final X.Y.Z release afterwards still has them
#
# What it does, in order (and stops at the first problem, before changing anything):
#   1. checks the version, a clean git tree, the branch (main / master), that the tag is new, and that there is something to release:
#      the notes under "## [Unreleased]" in CHANGELOG.md (or a "## [X.Y.Z]" section you wrote yourself)
#   2. sets  VERSION = "X.Y.Z"  in channelview.py, turns [Unreleased] into the "## [X.Y.Z] - date" section and rebuilds channelview_demo.html
#   3. runs the unit tests (a failure puts everything back)
#   4. commits ("Release vX.Y.Z", only if something changed) and creates the annotated tag vX.Y.Z
#   5. with --push: pushes the branch and the tag. GitHub Actions (.github/workflows/release.yml) then builds the .deb and publishes the release.
#      Without --push it prints the command, so you can look at the commit first.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
die() { echo "release.sh: $*" >&2; exit 1; }

push=false; any_branch=false; run_tests=true; version=""; bump=""
for arg in "$@"; do
    case "$arg" in
        --push) push=true ;;
        --any-branch) any_branch=true ;;
        --no-tests) run_tests=false ;;
        -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) die "unknown option $arg" ;;
        patch|minor|major) [ -z "$version$bump" ] || die "one version only"; bump="$arg" ;;
        *) [ -z "$version$bump" ] || die "one version only"; version="${arg#v}" ;;
    esac
done
[ -n "$version$bump" ] || die "usage: packaging/release.sh [--push] patch | minor | major | X.Y.Z"

current="$(sed -n 's/^VERSION = "\([^"]*\)".*/\1/p' channelview.py | head -n1)"
[ -n "$current" ] || die 'channelview.py has no VERSION = "..." line'
if [ -n "$bump" ]; then
    [[ "$current" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || die "the current version is $current; '$bump' needs a plain X.Y.Z - give the new version explicitly"
    case "$bump" in
        patch) version="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.$((BASH_REMATCH[3] + 1))" ;;
        minor) version="${BASH_REMATCH[1]}.$((BASH_REMATCH[2] + 1)).0" ;;
        major) version="$((BASH_REMATCH[1] + 1)).0.0" ;;
    esac
fi
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || die "'$version' is not a version like 1.2.3 or 1.2.3-rc.1"
tag="v$version"

command -v git >/dev/null || die "git not found"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "this is not a git repository (run: git init, then add your GitHub repository as 'origin')"
[ -z "$(git status --porcelain)" ] || die "the working tree has uncommitted changes - commit or stash them first:
$(git status --short)"
branch="$(git symbolic-ref --short -q HEAD || true)"
[ -n "$branch" ] || die "HEAD is detached - check out your main branch first"
if ! $any_branch && [ "$branch" != main ] && [ "$branch" != master ]; then
    die "you are on branch '$branch'; releases are made from main (use --any-branch to override)"
fi
git rev-parse -q --verify "refs/tags/$tag" >/dev/null && die "the tag $tag already exists"

if [ "$current" != "$version" ]; then
    # refuse to go backwards or sideways by accident: the new version must sort after the old one
    if command -v dpkg >/dev/null && dpkg --compare-versions "${current//-/\~}" ge "${version//-/\~}"; then
        die "version $version is not newer than the current $current"
    fi
fi

# the release notes: an existing "## [X.Y.Z]" section, else the entries under "## [Unreleased]" become that section
version_re="$(printf '%s' "$version" | sed 's/[.]/\\./g')"
if ! grep -qE "^## \[$version_re\]" CHANGELOG.md; then
    python3 - "$version" "$(date +%F)" <<'EOF' || die "CHANGELOG.md has no '## [$version]' section and nothing under '## [Unreleased]' to release. Write what changed under [Unreleased] first - GitHub shows it as the release notes."
import re, sys
version, day = sys.argv[1:3]
text = open("CHANGELOG.md", encoding="utf-8").read()
m = re.search(r"^## \[Unreleased\][ \t]*\n(.*?)(?=^## \[|\Z)", text, re.S | re.M)
if not m or not m.group(1).strip():
    sys.exit(1)
body = m.group(1).strip("\n")
if "-" in version:      # a pre-release gets a copy of the notes; they stay under [Unreleased] for the final release
    out = text[:m.start()] + "## [Unreleased]\n\n" + body + "\n\n## [" + version + "] - " + day + "\n\n" + body + "\n\n" + text[m.end():]
else:
    out = text[:m.start()] + "## [Unreleased]\n\n## [" + version + "] - " + day + "\n\n" + body + "\n\n" + text[m.end():]
open("CHANGELOG.md", "w", encoding="utf-8").write(out)
EOF
    case "$version" in
        *-*) echo "CHANGELOG.md: the [Unreleased] notes are copied to a [$version] section (they stay under [Unreleased] for the final release)" ;;
        *) echo "CHANGELOG.md: the [Unreleased] notes are now the [$version] section" ;;
    esac
fi

if [ "$current" != "$version" ]; then
    sed -i "s/^VERSION = \".*\"/VERSION = \"$version\"/" channelview.py
    echo "VERSION: $current -> $version"
fi

python3 tools/build_demo.py channelview_demo.html >/dev/null
if $run_tests; then
    echo "running the unit tests ..."
    if ! python3 -m unittest discover -s tests >/dev/null 2>&1; then
        git checkout -- channelview.py channelview_demo.html CHANGELOG.md
        die "the tests fail - nothing was committed and the files are as they were. Run:  python3 -m unittest discover -s tests -v"
    fi
    echo "tests pass"
fi

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then      # tracked files only: running the tests may leave __pycache__ behind
    git add channelview.py channelview_demo.html CHANGELOG.md
    git commit -q -m "Release $tag"
    echo "committed: Release $tag"
else
    echo "nothing to commit (VERSION, CHANGELOG.md and the demo page are already at $version)"
fi
git tag -a "$tag" -m "Release $version"
echo "tagged $tag"

if $push; then
    git push origin "$branch" "$tag"
    echo "pushed. GitHub Actions now builds the package: look under the repository's Actions tab, then Releases."
else
    echo
    echo "To publish:   git push origin $branch $tag"
    echo "To undo:      git tag -d $tag   (and  git reset --hard HEAD~1  if a release commit was made)"
fi
