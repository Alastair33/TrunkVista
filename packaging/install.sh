#!/bin/sh
# TrunkVista installer for Debian / Ubuntu - one command from nothing to a dashboard that is running:
#
#   curl -fsSL https://github.com/@REPO@/releases/latest/download/install.sh | sudo sh
#
# It downloads the newest release (never the branch), checks the package against the release's SHA256SUMS, installs it with apt (which asks
# for the administrator's user name and password) and then starts trunkvista-setup, which asks for your Zabbix server and for who may open the
# dashboard (this computer, or other PCs over HTTPS). Read it first if you like: it is this file, and it is short.
#
#   sudo sh install.sh                    the same, from a file you downloaded
#   sudo sh install.sh --no-setup         install only; run  sudo trunkvista-setup  yourself later
#   sudo sh install.sh --version 1.3.0    a release other than the newest
#
# Needs: root (sudo), apt-get, curl or wget, sha256sum. Elsewhere (no apt): download trunkvista.py from the same release and run it with
# Python 3.9+ - see the README. A private repository cannot be downloaded without signing in: use `gh release download` (README, "Download").
#
# Everything is inside main() and main runs on the last line, so that a download cut off half way never runs half a script.

main() {
    set -eu
    repo="@REPO@"
    case "$repo" in "@""REPO@") [ -n "${TRUNKVISTA_BASE_URL:-}" ] || { echo "install.sh: this is the template in the source tree; take install.sh from a release (README, Download)" >&2; return 1; } ;; esac
    base="${TRUNKVISTA_BASE_URL:-https://github.com/$repo/releases/latest/download}"      # (the environment variable is for mirrors and tests)
    deb="trunkvista-latest.deb"
    run_setup=yes

    say() { printf '%s\n' "$*"; }
    fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

    while [ $# -gt 0 ]; do
        case "$1" in
            --no-setup) run_setup=no; shift ;;
            --version)
                [ $# -ge 2 ] || fail "--version needs a number, e.g. --version 1.3.0"
                case "$2" in *[!0-9A-Za-z.-]*|"") fail "'$2' is not a version number" ;; esac
                base="${TRUNKVISTA_BASE_URL:-https://github.com/$repo/releases/download/v$2}"
                deb="trunkvista_$2_all.deb"
                shift 2 ;;
            -h|--help) sed -n '2,15p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; return 0 ;;
            *) fail "unknown option $1 (see --help)" ;;
        esac
    done

    # Piped from curl, standard input is the script itself; apt and trunkvista-setup ask their questions on the terminal.
    if [ ! -t 0 ] && ( : </dev/tty ) 2>/dev/null; then exec </dev/tty; fi

    [ "$(id -u)" = 0 ] || fail "run it as root:   curl -fsSL $base/install.sh | sudo sh"
    if ! command -v apt-get >/dev/null 2>&1 || ! command -v dpkg >/dev/null 2>&1; then
        fail "this installer is for Debian and Ubuntu (it needs apt-get). Elsewhere: download trunkvista.py from $base/ and run it with Python 3.9+: python3 trunkvista.py"
    fi
    command -v sha256sum >/dev/null 2>&1 || fail "sha256sum not found (it is part of coreutils)"
    if command -v curl >/dev/null 2>&1; then
        get() { curl -fsSL --retry 2 --connect-timeout 20 -o "$2" "$1"; }
    elif command -v wget >/dev/null 2>&1; then
        get() { wget -q -T 30 -O "$2" "$1"; }
    else
        fail "neither curl nor wget is installed:  apt-get install -y curl"
    fi

    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    chmod 0755 "$tmp"          # (apt downloads as its own user and wants to read the package)

    say "Downloading $deb and SHA256SUMS ..."
    for f in SHA256SUMS "$deb"; do
        get "$base/$f" "$tmp/$f" || fail "could not download $base/$f
  - no internet access from this machine, or a proxy in the way?
  - a private repository? Then download with the GitHub command-line tool instead (README, \"Download\"), and run:  sudo apt install ./$deb"
    done
    chmod 0644 "$tmp/$deb"

    grep "[ *]$deb\$" "$tmp/SHA256SUMS" > "$tmp/this.sum" || fail "$deb is not listed in SHA256SUMS - not installing it"
    ( cd "$tmp" && sha256sum -c this.sum >/dev/null 2>&1 ) || fail "the checksum of $deb does not match SHA256SUMS - the download is damaged or was changed; not installing it"
    say "Checksum ok."

    # Questions (the administrator's password) are asked only with a terminal; an unattended installation makes a random password.
    if [ -t 0 ]; then
        frontend="${DEBIAN_FRONTEND:-}"
    else
        frontend=noninteractive
    fi
    # (TRUNKVISTA_INSTALLER: the package then leaves out its "run trunkvista-setup" hint, because this script does that next)
    install_it() {
        if [ -n "$frontend" ]; then
            TRUNKVISTA_INSTALLER=1 DEBIAN_FRONTEND="$frontend" apt-get install -y "$tmp/$deb"
        else
            TRUNKVISTA_INSTALLER=1 apt-get install -y "$tmp/$deb"
        fi
    }
    if ! install_it; then
        say "Installing did not work; refreshing the package lists once and trying again ..."
        apt-get update || true
        install_it || fail "apt could not install the package (see above)"
    fi

    say ""
    say "TrunkVista $(dpkg-query -W -f='${Version}' trunkvista 2>/dev/null || true) is installed."
    if [ "$run_setup" = yes ] && [ -t 0 ] && [ -t 1 ]; then
        say ""
        trunkvista-setup
    else
        say "Next:  sudo trunkvista-setup      (connects Zabbix and decides who may open the dashboard)"
    fi
}

main "$@"; exit $?
