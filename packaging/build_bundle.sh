#!/bin/bash
# Make the installer files of a release from a finished package:
#
#   packaging/build_bundle.sh [DEB [OUTDIR]]
#
#   DEB     the package (default: the one dist/trunkvista_*_all.deb)
#   OUTDIR  where the results go (default: the folder of the package)
#
# Results:
#   OUTDIR/install.sh                  the installer with this repository's name in it (for the one-line installer: curl ... | sudo sh)
#   OUTDIR/trunkvista-latest.tar.gz    the same installer, the package and its checksum in a folder trunkvista/ - the four-line installation:
#                                          wget https://github.com/OWNER/REPO/releases/latest/download/trunkvista-latest.tar.gz
#                                          tar xzf trunkvista-latest.tar.gz
#                                          cd trunkvista
#                                          sudo ./install.sh
#
# The same sources give the same archive (sorted names, fixed owner and times, gzip without a name or time).
# Optional environment:
#   GITHUB_REPOSITORY   owner/name of the GitHub repository (GitHub Actions sets it). Default: taken from `git remote origin`.
#   SOURCE_DATE_EPOCH   timestamp for the files in the archive (default: the last commit, else the time of the package).
# Needs: tar (GNU), gzip, sed, sha256sum.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
die() { echo "build_bundle.sh: $*" >&2; exit 1; }

deb="${1:-}"
if [ -z "$deb" ]; then
    set -- "$root"/dist/trunkvista_*_all.deb
    if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then die "expected exactly one dist/trunkvista_*_all.deb (build it with packaging/build_deb.sh) - found: $*"; fi
    deb="$1"
fi
[ -f "$deb" ] || die "$deb is not a file"
debname="$(basename "$deb")"
[[ "$debname" =~ ^trunkvista_[0-9A-Za-z.+-]+_all\.deb$ ]] || die "the package must be called trunkvista_<version>_all.deb (not '$debname'); install.sh looks for that name"
out="${2:-$(dirname "$deb")}"
mkdir -p "$out"

repo="${GITHUB_REPOSITORY:-}"
if [ -z "$repo" ] && command -v git >/dev/null && url="$(git -C "$root" config --get remote.origin.url 2>/dev/null)"; then
    repo="$(printf '%s' "$url" | sed -n 's#.*github\.com[:/]\([A-Za-z0-9_.-]*/[A-Za-z0-9_.-]*\)$#\1#p' | sed 's#\.git$##')"
fi
[[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "the repository must be given as owner/name in GITHUB_REPOSITORY (found '${repo:-nothing}')"

epoch="${SOURCE_DATE_EPOCH:-$(git -C "$root" log -1 --format=%ct 2>/dev/null || true)}"
epoch="${epoch:-$(stat -c %Y "$deb")}"
[[ "$epoch" =~ ^[0-9]+$ ]] || die "SOURCE_DATE_EPOCH must be a number of seconds (found '$epoch')"

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
chmod 0755 "$stage"
pack="$stage/trunkvista"
install -d -m 0755 "$pack"

# the installer: the template with the repository's name (a name that cannot contain / & \ # - checked above)
sed "s#@REPO@#$repo#g" "$root/packaging/install.sh" > "$pack/install.sh"
chmod 0755 "$pack/install.sh"
sh -n "$pack/install.sh"
if grep -q '@REPO@' "$pack/install.sh"; then die "install.sh still holds the placeholder @REPO@"; fi

install -m 0644 "$deb" "$pack/$debname"
( cd "$pack" && sha256sum -- "$debname" > SHA256SUMS )

cat > "$pack/README.txt" <<EOF
TrunkVista - installation (Debian / Ubuntu)

    sudo ./install.sh

It asks one question: the administrator's password (Enter = a random one is made and kept for root to read).
Then open http://127.0.0.1:8088/ , sign in, and enter your Zabbix server (its address and an API token) in the page.

Other PCs or wall displays should open it too:   sudo trunkvista-setup --network      (also switches HTTPS on)
Update later:                                    sudo trunkvista-update
This archive works without internet access: install.sh installs the package that sits beside it, after checking SHA256SUMS.
Other systems (no apt): download trunkvista.py from https://github.com/$repo/releases/latest and run: python3 trunkvista.py
EOF
chmod 0644 "$pack/README.txt"

cp "$pack/install.sh" "$out/install.sh"
chmod 0755 "$out/install.sh"
tar --sort=name --format=gnu --mtime="@$epoch" --owner=0 --group=0 --numeric-owner -C "$stage" -cf - trunkvista | gzip -9n > "$out/trunkvista-latest.tar.gz"
echo "wrote $out/install.sh and $out/trunkvista-latest.tar.gz ($debname for $repo)"
