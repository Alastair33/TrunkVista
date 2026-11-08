#!/bin/sh
# TrunkVista installer for Debian / Ubuntu. The only question it leads to is the administrator's password; the Zabbix server and its API
# token are entered in the web page afterwards.
#
#   wget https://github.com/@REPO@/releases/latest/download/trunkvista-latest.tar.gz
#   tar xzf trunkvista-latest.tar.gz
#   cd trunkvista
#   sudo ./install.sh
#
# The archive holds this script, the Debian package and its checksum, so it also works on a machine that cannot reach GitHub: copy the
# archive there. The same script, on its own, downloads the newest release instead (never the branch) and checks it against the release's
# SHA256SUMS:
#
#   curl -fsSL https://github.com/@REPO@/releases/latest/download/install.sh | sudo sh
#   sudo sh install.sh --version 1.3.0         a release other than the newest (this mode only)
#
# Either way the package is installed with apt, which asks for the administrator's password (empty = a random one is made for you).
# Needs: root (sudo), apt-get, sha256sum - and curl or wget when it has to download. Elsewhere (no apt): download trunkvista.py from the
# release and run it with Python 3.9+ - see the README. A private repository cannot be downloaded without signing in: use
# `gh release download` (README, "Download").
#
# Everything is inside main() and main runs on the last line, so that a download cut off half way never runs half a script.

main() {
    set -eu
    repo="@REPO@"
    base=""
    deb=""
    from_dir=""

    say() { printf '%s\n' "$*"; }
    fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

    while [ $# -gt 0 ]; do
        case "$1" in
            --version)
                [ $# -ge 2 ] || fail "--version needs a number, e.g. --version 1.3.0"
                case "$2" in *[!0-9A-Za-z.-]*|"") fail "'$2' is not a version number" ;; esac
                base="https://github.com/$repo/releases/download/v$2"
                deb="trunkvista_$2_all.deb"
                shift 2 ;;
            -h|--help) sed -n '2,/^$/p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; return 0 ;;
            *) fail "unknown option $1 (see --help)" ;;
        esac
    done

    # Run from the unpacked archive (a file called install.sh with a package beside it)? Then nothing is downloaded. Piped from curl, $0 is "sh".
    case "$0" in
        *install.sh)
            if [ -f "$0" ] && [ -z "$deb" ]; then
                dir="$(cd "$(dirname "$0")" && pwd)"
                set -- "$dir"/trunkvista_*_all.deb
                if [ -e "$1" ]; then
                    [ $# -eq 1 ] || fail "more than one package next to install.sh ($*); keep the one you want"
                    from_dir="$dir"
                    deb="$(basename "$1")"
                fi
            fi ;;
    esac
    if [ -z "$from_dir" ]; then
        case "$repo" in "@""REPO@") [ -n "${TRUNKVISTA_BASE_URL:-}" ] || fail "this is the template in the source tree; take install.sh from a release (README, Download)" ;; esac
        [ -n "$base" ] || base="https://github.com/$repo/releases/latest/download"
        base="${TRUNKVISTA_BASE_URL:-$base}"          # (the environment variable is for mirrors and tests)
        [ -n "$deb" ] || deb="trunkvista-latest.deb"
    fi

    # Piped from curl, standard input is the script itself; apt asks its question on the terminal.
    if [ ! -t 0 ] && ( : </dev/tty ) 2>/dev/null; then exec </dev/tty; fi

    [ "$(id -u)" = 0 ] || fail "run it as root:   sudo ./install.sh     (or:  curl -fsSL https://github.com/$repo/releases/latest/download/install.sh | sudo sh)"
    if ! command -v apt-get >/dev/null 2>&1 || ! command -v dpkg >/dev/null 2>&1; then
        fail "this installer is for Debian and Ubuntu (it needs apt-get). Elsewhere: download trunkvista.py from the release and run it with Python 3.9+: python3 trunkvista.py"
    fi
    command -v sha256sum >/dev/null 2>&1 || fail "sha256sum not found (it is part of coreutils)"

    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    chmod 0755 "$tmp"          # (apt downloads as its own user and wants to read the package; a copy here is readable, a file in /root is not)

    if [ -n "$from_dir" ]; then
        cp "$from_dir/$deb" "$tmp/$deb"
        if [ -f "$from_dir/SHA256SUMS" ]; then cp "$from_dir/SHA256SUMS" "$tmp/SHA256SUMS"; else say "No SHA256SUMS next to the package: installing it without a checksum check."; fi
    else
        if command -v curl >/dev/null 2>&1; then
            get() { curl -fsSL --retry 2 --connect-timeout 20 -o "$2" "$1"; }
        elif command -v wget >/dev/null 2>&1; then
            get() { wget -q -T 30 -O "$2" "$1"; }
        else
            fail "neither curl nor wget is installed:  apt-get install -y curl"
        fi
        say "Downloading $deb and SHA256SUMS ..."
        for f in SHA256SUMS "$deb"; do
            get "$base/$f" "$tmp/$f" || fail "could not download $base/$f
  - no internet access from this machine, or a proxy in the way?
  - a private repository? Then download with the GitHub command-line tool instead (README, \"Download\"), and run:  sudo apt install ./$deb
  - no access to GitHub at all? Take trunkvista-latest.tar.gz to the machine on a stick, unpack it and run its install.sh"
        done
    fi
    chmod 0644 "$tmp/$deb"

    if [ -f "$tmp/SHA256SUMS" ]; then
        # (the line of exactly this file name - "grep" would take the dots of the name for any character)
        awk -v f="$deb" '{ n = $2; sub(/^\*/, "", n); if (n == f) print }' "$tmp/SHA256SUMS" > "$tmp/this.sum"
        [ -s "$tmp/this.sum" ] || fail "$deb is not listed in SHA256SUMS - not installing it"
        ( cd "$tmp" && sha256sum -c this.sum >/dev/null 2>&1 ) || fail "the checksum of $deb does not match SHA256SUMS - the file is damaged or was changed; not installing it"
        say "Checksum ok."
    fi

    # The administrator's password is asked only with a terminal; an unattended installation makes a random one (kept for root to read).
    if [ -t 0 ]; then
        frontend="${DEBIAN_FRONTEND:-}"
    else
        frontend=noninteractive
    fi
    install_it() {
        if [ -n "$frontend" ]; then DEBIAN_FRONTEND="$frontend" apt-get install -y -q "$tmp/$deb"; else apt-get install -y -q "$tmp/$deb"; fi
    }
    if ! install_it; then
        say "Installing did not work; refreshing the package lists once and trying again ..."
        apt-get update || true
        install_it || fail "apt could not install the package (see above)"
    fi
    say ""
    say "TrunkVista $(dpkg-query -W -f='${Version}' trunkvista 2>/dev/null || true) is installed."
}

main "$@"; exit $?
