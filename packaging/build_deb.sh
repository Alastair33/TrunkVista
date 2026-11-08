#!/bin/bash
# Build the Debian / Ubuntu package:   packaging/build_deb.sh        ->  dist/trunkvista_<version>_all.deb  and  dist/SHA256SUMS
#
# The version comes from  VERSION = "x.y.z"  in trunkvista.py (the one place it lives). A pre-release such as 1.2.0-rc.1 becomes the
# Debian version 1.2.0~rc.1, which sorts before 1.2.0; the file is still called trunkvista_1.2.0-rc.1_all.deb (GitHub renames a ~ in
# the name of a release file, which would break the checksum lookup).
# Optional environment:
#   GITHUB_REPOSITORY   owner/name of the GitHub repository (GitHub Actions sets it). Default: taken from `git remote origin`, else OWNER/REPO.
#                       It is written into /etc/default/trunkvista so that trunkvista-update knows where to look.
#   DEB_MAINTAINER      "Name <mail>" for the Maintainer field. Default: a placeholder; the release workflow uses the GitHub user's noreply address.
#   SOURCE_DATE_EPOCH   timestamp for all files (default: the last commit, else trunkvista.py), so the same sources give the same package.
# Needs: dpkg-deb, gzip, sed, sha256sum (any Debian / Ubuntu machine, or a CI runner).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
die() { echo "build_deb.sh: $*" >&2; exit 1; }
command -v dpkg-deb >/dev/null || die "dpkg-deb not found - build on a Debian / Ubuntu machine"

version="$(sed -n 's/^VERSION = "\([^"]*\)".*/\1/p' trunkvista.py | head -n1)"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || die "VERSION in trunkvista.py must look like 1.2.3 or 1.2.3-rc.1 (found '$version')"
deb_version="${version//-/\~}"

repo="${GITHUB_REPOSITORY:-}"
if [ -z "$repo" ] && command -v git >/dev/null && url="$(git config --get remote.origin.url 2>/dev/null)"; then
    repo="$(printf '%s' "$url" | sed -n 's#.*github\.com[:/]\([A-Za-z0-9_.-]*/[A-Za-z0-9_.-]*\)$#\1#p' | sed 's#\.git$##')"
fi
repo="${repo:-OWNER/REPO}"
maintainer="${DEB_MAINTAINER:-trunkvista maintainers <trunkvista@users.noreply.github.com>}"
epoch="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || true)}"
epoch="${epoch:-$(stat -c %Y trunkvista.py)}"
export SOURCE_DATE_EPOCH="$epoch"

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
chmod 0755 "$stage"
d="$root/packaging/deb"
install -d -m 0755 "$stage/DEBIAN" "$stage/usr/lib/trunkvista" "$stage/usr/bin" "$stage/lib/systemd/system" "$stage/etc/default" "$stage/usr/share/doc/trunkvista"

# the server, with a fixed interpreter path (the source has `#!/usr/bin/env python3` for running it from a checkout)
sed '1s|^#!.*|#!/usr/bin/python3|' trunkvista.py > "$stage/usr/lib/trunkvista/trunkvista.py"
chmod 0755 "$stage/usr/lib/trunkvista/trunkvista.py"
ln -s ../lib/trunkvista/trunkvista.py "$stage/usr/bin/trunkvista"
install -m 0755 packaging/trunkvista-update "$stage/usr/bin/trunkvista-update"
install -m 0755 packaging/trunkvista-cert "$stage/usr/bin/trunkvista-cert"
install -m 0755 packaging/trunkvista-user "$stage/usr/bin/trunkvista-user"
install -m 0644 "$d/trunkvista.service" "$d/trunkvista-update.service" "$d/trunkvista-update.timer" "$stage/lib/systemd/system/"
sed "s#@REPO@#$repo#" "$d/trunkvista.default" > "$stage/etc/default/trunkvista"
chmod 0644 "$stage/etc/default/trunkvista"
printf '/etc/default/trunkvista\n' > "$stage/DEBIAN/conffiles"

doc="$stage/usr/share/doc/trunkvista"
sed "s#@REPO@#$repo#" "$d/copyright" > "$doc/copyright"
gzip -9n -c README.md > "$doc/README.md.gz"
gzip -9n -c CHANGELOG.md > "$doc/CHANGELOG.md.gz"
{       # the Debian-format changelog every package carries; the real history is CHANGELOG.md
    echo "trunkvista ($deb_version) unstable; urgency=medium"
    echo
    echo "  * Release $version (history: CHANGELOG.md.gz in this directory)."
    echo
    echo " -- $maintainer  $(date -R -u -d "@$epoch")"
} | gzip -9n > "$doc/changelog.gz"
chmod 0644 "$doc"/*
for s in postinst prerm postrm config; do install -m 0755 "$d/$s" "$stage/DEBIAN/$s"; done
install -m 0644 "$d/templates" "$stage/DEBIAN/templates"

homepage=""
[ "$repo" = "OWNER/REPO" ] || homepage="Homepage: https://github.com/$repo"
size="$(du -sk --exclude=DEBIAN "$stage" | cut -f1)"
{
    echo "Package: trunkvista"
    echo "Version: $deb_version"
    echo "Architecture: all"
    echo "Maintainer: $maintainer"
    echo "Installed-Size: $size"
    echo "Depends: python3 (>= 3.9), adduser, init-system-helpers (>= 1.52), debconf (>= 0.5) | debconf-2.0"
    echo "Conflicts: channelview"            # the program's first name: that package is removed, and postinst takes over its data
    echo "Replaces: channelview"
    echo "Recommends: ca-certificates, openssl"
    echo "Section: net"
    echo "Priority: optional"
    [ -z "$homepage" ] || echo "$homepage"
    echo "Description: weathermap dashboard for Zabbix 7.0"
    echo " TrunkVista is a browser dashboard with tabs, playlists (NOC wall /"
    echo " kiosk mode) and weathermaps: devices joined by interface links,"
    echo " coloured by status and utilisation, with an in / out traffic graph"
    echo " per link."
    echo " ."
    echo " Reads everything from the Zabbix API, runs as a systemd service under its"
    echo " own user and needs nothing but Python 3. Everybody signs in (accounts"
    echo " with an administrator or viewer role; trunkvista-user). Serves HTTPS"
    echo " itself (trunkvista-cert makes a certificate); trunkvista-update"
    echo " installs newer releases from GitHub."
} > "$stage/DEBIAN/control"

find "$stage" -exec touch -h -d "@$epoch" {} +
mkdir -p dist
out="dist/trunkvista_${version}_all.deb"      # not ..._1.2.0~rc.1_...: GitHub would rename the ~ in a release file to .
rm -f "$out"
dpkg-deb --root-owner-group -Zxz --build "$stage" "$out" >/dev/null
(cd dist && sha256sum "$(basename "$out")" > SHA256SUMS)
echo "built $out  (Debian version $deb_version, repository $repo)"
dpkg-deb -I "$out" | sed -n '1,/^ Description/p' | sed 's/^/    /'
