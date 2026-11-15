#!/bin/bash
# End-to-end test of the Debian package:  sudo tests/e2e_debian.sh [path/to/trunkvista_X_all.deb]      (default: dist/trunkvista_*_all.deb)
#
#   install  ->  service user, folders, files, the service answers, the first account is made, nothing works without signing in
#   upgrade  ->  a newer package keeps your edited /etc/default/trunkvista, the data, the accounts and the signed-in sessions, and restarts the service
#   update   ->  trunkvista-update installs a newer release from a local fake GitHub (good and tampered ones)
#   remove / reinstall / purge  ->  what stays and what goes
#   first account  ->  with answers preseeded in debconf, and when a version without accounts is upgraded
#   setup  ->  trunkvista-setup connects a (fake) Zabbix, switches the network and HTTPS on and off; the release archive (build_bundle.sh) is unpacked and installed with ./install.sh, install.sh also installs from a (local) release
#   old name  ->  an installation of the package "channelview" (the program's first name) becomes a trunkvista one, with its data, accounts,
#                 HTTPS certificate and settings; needs a package of the old name: E2E_LEGACY_DEB=path, or the commit before the rename in the git history
#
# WARNING: this really installs and purges the package and deletes /var/lib/trunkvista and the trunkvista user afterwards.
# Run it in a CI runner, a throw-away container or a VM - not on the machine that runs your dashboard.
# With systemd running (a CI runner, a VM) the real service is tested; without it (a plain container) the packaged server is started as the
# service user by hand, with MemoryDenyWriteExecute emulated, and only the systemd-specific checks are skipped.
# shellcheck disable=SC2015   # 'cmd && ok || bad' is deliberate: ok() always succeeds, so bad() runs only when cmd failed
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(dirname "$here")"
pass=0; fail=0
ok()  { pass=$((pass + 1)); echo "PASS  $*"; }
bad() { fail=$((fail + 1)); echo "FAIL  $*"; }
check() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$what"; else bad "$what"; fi; }
die() { echo "e2e_debian.sh: $*" >&2; exit 2; }
fixed() { [ "$(eval "$1")" = "$2" ]; }      # fixed 'command' 'expected output'

[ "$(id -u)" = 0 ] || die "run as root (sudo)"
command -v dpkg-deb >/dev/null || die "needs a Debian / Ubuntu system (dpkg-deb not found)"
command -v apt-get >/dev/null || die "needs a Debian / Ubuntu system (apt-get not found)"
deb="${1:-$(find "$repo_root/dist" -maxdepth 1 -name "trunkvista_*_all.deb" 2>/dev/null | sort | tail -n1)}"
[ -f "$deb" ] || die "no .deb given and none in dist/ - run packaging/build_deb.sh first"
deb="$(readlink -f "$deb")"
if dpkg -s trunkvista >/dev/null 2>&1 || [ -e /var/lib/trunkvista ]; then
    [ "${E2E_FORCE:-}" = 1 ] || die "trunkvista is already installed or has data here; refusing to touch it (E2E_FORCE=1 purges it first)"
    apt-get purge -y trunkvista >/dev/null 2>&1; rm -rf /var/lib/trunkvista; deluser --system trunkvista >/dev/null 2>&1
fi
if dpkg -s channelview >/dev/null 2>&1 || [ -e /var/lib/channelview ]; then
    [ "${E2E_FORCE:-}" = 1 ] || die "the old package channelview is installed or has data here; refusing to touch it (E2E_FORCE=1 purges it first)"
    apt-get purge -y channelview >/dev/null 2>&1; rm -rf /var/lib/channelview /etc/channelview; deluser --system channelview >/dev/null 2>&1
fi

export DEBIAN_FRONTEND=noninteractive
PORT=18088
SVC_USER=trunkvista
DATA=/var/lib/trunkvista
DEFAULTS=/etc/default/trunkvista
have_systemd=false; [ -d /run/systemd/system ] && have_systemd=true
work="$(mktemp -d)"; chmod 0755 "$work"
mock_pid=""; zbx_pid=""; rel_pid=""
version="$(dpkg-deb -f "$deb" Version)"
upstream="${version//\~/-}"
bump() { local base="${1%%-*}"; echo "${base%.*}.$(( ${base##*.} + 1 ))"; }
v2="$(bump "$upstream")"; v3="$(bump "$v2")"
echo "package under test: $deb (version $version);  systemd running: $have_systemd;  upgrade test versions: $v2, $v3"

cleanup() {
    [ -z "$mock_pid" ] || kill "$mock_pid" 2>/dev/null
    [ -z "$zbx_pid" ] || kill "$zbx_pid" 2>/dev/null
    [ -z "$rel_pid" ] || kill "$rel_pid" 2>/dev/null
    manual_stop
    apt-get purge -y trunkvista channelview >/dev/null 2>&1
    rm -rf "$DATA" "$work" /etc/trunkvista /var/lib/channelview /etc/channelview /etc/default/channelview
    deluser --system "$SVC_USER" >/dev/null 2>&1; delgroup --system "$SVC_USER" >/dev/null 2>&1
    deluser --system channelview >/dev/null 2>&1; delgroup --system channelview >/dev/null 2>&1
}
trap cleanup EXIT

# ---- the server: the real service under systemd, otherwise started by hand as the service user (like the unit does)
manual_log="$work/manual.log"
manual_stop() { pkill -u "$SVC_USER" -f trunkvista.py 2>/dev/null; sleep 0.5; }
manual_start() {
    manual_stop
    local host port tcert tkey tls_env=()
    host="$(sed -n 's/^TRUNKVISTA_HOST=//p' "$DEFAULTS" | tail -n1)"; port="$(sed -n 's/^TRUNKVISTA_PORT=//p' "$DEFAULTS" | tail -n1)"
    tcert="$(sed -n 's/^TRUNKVISTA_TLS_CERT=//p' "$DEFAULTS" | tail -n1)"; tkey="$(sed -n 's/^TRUNKVISTA_TLS_KEY=//p' "$DEFAULTS" | tail -n1)"
    [ -z "$tcert" ] || tls_env+=("TRUNKVISTA_TLS_CERT=$tcert")
    [ -z "$tkey" ] || tls_env+=("TRUNKVISTA_TLS_KEY=$tkey")
    runuser -u "$SVC_USER" -- env PYTHONUNBUFFERED=1 "${tls_env[@]}" python3 -c '
import ctypes, os, sys
try: ctypes.CDLL(None).prctl(65, 1, 0, 0, 0)      # PR_SET_MDWE: what MemoryDenyWriteExecute=yes does in the unit
except Exception: pass
os.execv(sys.argv[1], sys.argv[1:])' /usr/lib/trunkvista/trunkvista.py --no-browser --host "${host:-127.0.0.1}" --port "${port:-8088}" \
        --data-dir "$DATA" --require-login >"$manual_log" 2>&1 &
    sleep 0.3
}
server_up() { if $have_systemd; then systemctl restart trunkvista; else manual_start; fi; }
wait_http() { local _; for _ in $(seq 1 60); do curl -fsS -o /dev/null "http://127.0.0.1:$1/" 2>/dev/null && return 0; sleep 0.5; done; return 1; }
server_pid() { if $have_systemd; then systemctl show -p MainPID --value trunkvista; else pgrep -u "$SVC_USER" -f trunkvista.py | head -n1; fi; }
server_log() { if $have_systemd; then journalctl -u trunkvista --no-pager -o cat 2>/dev/null; else cat "$manual_log"; fi; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }                  # code [curl options] URL  ->  the HTTP status
signin() { code -c "$3" -X POST "${4:-http://127.0.0.1:8088}/api/login" -H 'Content-Type: application/json' -d "{\"username\":\"$1\",\"password\":\"$2\"}"; }   # signin USER PASSWORD JAR [BASE]
post_json() { code -b "$2" -c "$2" -X POST "$1" -H 'Content-Type: application/json' -H "X-Edit-Token: $3" -d "$4"; }                                      # post_json URL JAR TOKEN JSON
page_token() { curl -s -b "$1" "${2:-http://127.0.0.1:8088}/" | sed -n 's/.*const EDIT_TOKEN = "\([^"]*\)".*/\1/p'; }                                       # the session's request token
apt_install() { apt-get install -y --no-install-recommends -o Dpkg::Options::=--force-confold "$@" >"$work/apt.log" 2>&1 || { cat "$work/apt.log"; return 1; }; }

# ============================================================== install
echo "== install"
apt_install "$deb" && ok "apt-get install ./package.deb succeeds" || bad "install failed"
check "system user exists with home $DATA and no login shell" bash -c "getent passwd $SVC_USER | grep -q ':$DATA:/usr/sbin/nologin\$'"
check "data folder belongs to the service user, mode 700" fixed "stat -c '%U:%G %a' $DATA" "$SVC_USER:$SVC_USER 700"
check "server installed, executable, interpreter fixed to /usr/bin/python3" bash -c "head -n1 /usr/lib/trunkvista/trunkvista.py | grep -qx '#!/usr/bin/python3' && test -x /usr/lib/trunkvista/trunkvista.py"
check "trunkvista --version reports the packaged version" fixed "trunkvista --version | awk '{print \$NF}'" "$upstream"
check "trunkvista-update is installed" test -x /usr/bin/trunkvista-update
check "the service is enabled (symlink in multi-user.target.wants)" test -L /etc/systemd/system/multi-user.target.wants/trunkvista.service
check "the nightly update timer is NOT enabled" bash -c "! test -e /etc/systemd/system/timers.target.wants/trunkvista-update.timer"
check "/etc/default/trunkvista is a conffile" bash -c "dpkg-query -W -f='\${Conffiles}\n' trunkvista | grep -q '$DEFAULTS'"
if command -v systemd-analyze >/dev/null; then
    for u in trunkvista.service trunkvista-update.service trunkvista-update.timer; do
        check "systemd-analyze verify $u" bash -c "out=\$(systemd-analyze verify /lib/systemd/system/$u 2>&1); ! echo \"\$out\" | grep -qiE 'unknown (key|section|lvalue)|failed to|invalid|error'"
    done
fi
if $have_systemd; then
    check "service is active after the installation" bash -c "for i in \$(seq 1 20); do systemctl is-active --quiet trunkvista && exit 0; sleep 0.5; done; exit 1"
else
    echo "(no systemd: starting the packaged server by hand as $SVC_USER)"; manual_start
fi
wait_http 8088 && ok "answers on the default port 8088" || { bad "does not answer on 8088"; server_log | tail -5; }
check "it runs as the service user" fixed "ps -o user:32= -p $(server_pid) | tr -d ' '" "$SVC_USER"
check "GET / returns the page" bash -c "curl -fsS http://127.0.0.1:8088/ | grep -q '<title>'"
check "the page asks for a sign-in and shows nothing of the dashboard" bash -c "curl -fsS http://127.0.0.1:8088/ | grep -q 'id=\"lf\"' && ! curl -fsS http://127.0.0.1:8088/ | grep -q 'id=\"tabs\"'"
check "/api/layout needs a sign-in (401)" fixed "code http://127.0.0.1:8088/api/layout" "401"
check "accounts file stored owner-only, owned by the service user" fixed "stat -c '%U %a' $DATA/users.json" "$SVC_USER 600"
check "no edit token is made any more" bash -c "! test -e $DATA/edit_token"
check "the random first password is kept, owner-only" fixed "stat -c '%U %a' $DATA/initial-admin-password" "$SVC_USER 600"
adm_user="$(sed -n 's/^username: //p' "$DATA/initial-admin-password")"; adm_pw="$(sed -n 's/^password: //p' "$DATA/initial-admin-password")"
check "the first account is called admin, the password is 20 characters" bash -c "[ '$adm_user' = admin ] && [ \"\$(printf %s '$adm_pw' | wc -c)\" = 20 ]"
check "the hash in users.json is not the password" bash -c "! grep -qF '$adm_pw' $DATA/users.json && grep -q 'pbkdf2_sha256' $DATA/users.json"
check "a wrong password is refused (401)" fixed "signin admin wrong-password-here $work/jar0" "401"
check "the first password signs in (200)" fixed "signin admin '$adm_pw' $work/jar1" "200"
check "... but it has to be changed first: data calls answer 403" fixed "code -b $work/jar1 http://127.0.0.1:8088/api/connections" "403"
csrf="$(page_token "$work/jar1")"
new_pw="a new administrator password"
st="$(post_json http://127.0.0.1:8088/api/account/password "$work/jar1" "$csrf" "{\"current\":\"$adm_pw\",\"new\":\"$new_pw\"}")"
[ "$st" = 200 ] && ok "choosing a new password works" || bad "choosing a new password gave $st"
check "the temporary-password file is gone" bash -c "! test -e $DATA/initial-admin-password"
check "the browser stays signed in, and an administrator may edit (200)" fixed "code -b $work/jar1 http://127.0.0.1:8088/api/connections" "200"
check "the temporary password does not work any more (401)" fixed "signin admin '$adm_pw' $work/jar9" "401"
check "no sign-in cookie is sent without signing in (a request without the cookie: 401)" fixed "code http://127.0.0.1:8088/api/connections" "401"
check "a viewer account made with trunkvista-user (never signs out) can look but not edit" bash -c "printf 'viewer password 1' | trunkvista-user add tv --role viewer --idle never --password-stdin >/dev/null && [ \"\$(curl -s -o /dev/null -w '%{http_code}' -c $work/jar_tv -X POST http://127.0.0.1:8088/api/login -H 'Content-Type: application/json' -d '{\"username\":\"tv\",\"password\":\"viewer password 1\"}')\" = 200 ] && [ \"\$(curl -s -o /dev/null -w '%{http_code}' -b $work/jar_tv http://127.0.0.1:8088/api/layout)\" = 200 ] && [ \"\$(curl -s -o /dev/null -w '%{http_code}' -b $work/jar_tv http://127.0.0.1:8088/api/connections)\" = 403 ]"
check "trunkvista-user list shows both accounts" bash -c "trunkvista-user list | grep -q '^admin ' && trunkvista-user list | grep -q '^tv .*never'"
server_log | grep -qF "$adm_pw" && bad "the temporary password is in the log" || ok "the temporary password is not in the log"
server_log | grep -qF "$new_pw" && bad "the new password is in the log" || ok "the new password is not in the log"
server_log | grep -q "\[accounts\] sign-in user=admin" && ok "sign-ins are logged (user and address, never the password)" || bad "no sign-in line in the log"
check "files written by the service belong to it (nothing owned by root in the data folder)" bash -c "! find $DATA -user root | grep -q ."

# ============================================================== administrator's changes, then an upgrade
echo "== upgrade"
echo "keep me" > "$DATA/marker"; chown "$SVC_USER:$SVC_USER" "$DATA/marker"
sed -i "s/^TRUNKVISTA_PORT=.*/TRUNKVISTA_PORT=$PORT/; s#^TRUNKVISTA_UPDATE_REPO=.*#TRUNKVISTA_UPDATE_REPO=test/zbx#" "$DEFAULTS"
server_up; wait_http $PORT && ok "after editing /etc/default/trunkvista and restarting, it listens on port $PORT" || bad "does not answer on $PORT"
pid_before="$(server_pid)"
users_before="$(sha256sum < "$DATA/users.json")"

build_version() {      # build_version VERSION OUTDIR : the repository with that VERSION, built with the same repository name as the installed package
    local v="$1" src="$work/src-$1"
    mkdir -p "$src"; tar --exclude=./dist --exclude=./.git -C "$repo_root" -cf - . | tar -C "$src" -xf -
    sed -i "s/^VERSION = \".*\"/VERSION = \"$v\"/" "$src/trunkvista.py"
    ( cd "$src" && GITHUB_REPOSITORY=test/zbx ./packaging/build_deb.sh >/dev/null ) || return 1
    mkdir -p "$2"; cp "$src/dist/trunkvista_${v}_all.deb" "$src/dist/SHA256SUMS" "$2/"
}
build_version "$v2" "$work/rel-$v2" && ok "built the test package $v2" || bad "could not build the $v2 test package"
apt_install "$work/rel-$v2/trunkvista_${v2}_all.deb" && ok "upgrade to $v2 succeeds without questions (--force-confold)" || bad "upgrade failed"
check "version is now $v2" fixed "dpkg-query -W -f='\${Version}' trunkvista" "$v2"
check "your edited /etc/default/trunkvista is kept (port $PORT, repository test/zbx)" bash -c "grep -qx 'TRUNKVISTA_PORT=$PORT' $DEFAULTS && grep -qx 'TRUNKVISTA_UPDATE_REPO=test/zbx' $DEFAULTS"
check "your data is kept" fixed "cat $DATA/marker" "keep me"
check "your accounts are unchanged" fixed "sha256sum < $DATA/users.json" "$users_before"
if $have_systemd; then
    wait_http $PORT; pid_after="$(server_pid)"
    [ -n "$pid_after" ] && [ "$pid_after" != "$pid_before" ] && ok "the upgrade restarted the service (new process)" || bad "service was not restarted by the upgrade ($pid_before -> $pid_after)"
else
    manual_start
fi
wait_http $PORT && ok "answers on port $PORT with the new version" || bad "does not answer after the upgrade"
check "a browser that was signed in before the upgrade still is (sessions survive the restart)" fixed "code -b $work/jar1 http://127.0.0.1:$PORT/api/connections" "200"
server_log | grep -q "TrunkVista $v2 running" && ok "the running server reports $v2" || bad "server log does not show version $v2"

# ============================================================== update from (fake) GitHub
echo "== trunkvista-update"
build_version "$v3" "$work/rel-$v3" || bad "could not build the $v3 test package"
start_mock() {      # start_mock OUTFILE MOCK_ARGS... : starts the fake GitHub, sets $api and $mock_pid
    local out="$1"; shift
    python3 "$here/mock_github.py" --repo test/zbx --port 0 "$@" >"$out" 2>&1 &
    mock_pid=$!
    for _ in $(seq 1 40); do grep -q '^http' "$out" && break; sleep 0.25; done
    api="$(grep '^http' "$out" | head -n1)"
    [ -n "$api" ] || die "the fake GitHub did not start: $(cat "$out")"
}
start_mock "$work/mock.out" --release "v$v2:$work/rel-$v2/trunkvista_${v2}_all.deb,$work/rel-$v2/SHA256SUMS" \
    --release "v$v3:$work/rel-$v3/trunkvista_${v3}_all.deb,$work/rel-$v3/SHA256SUMS"
upd() { trunkvista-update --api-url "$api" "$@"; }
upd --check --quiet; rc=$?; [ $rc = 10 ] && ok "--check: exit status 10 (update available $v2 -> $v3), repository taken from /etc/default/trunkvista" || bad "--check gave $rc"
out="$(upd --yes 2>&1)"; rc=$?
[ $rc = 0 ] && ok "trunkvista-update --yes succeeds" || { bad "update failed ($rc)"; echo "$out"; }
check "version is now $v3" fixed "dpkg-query -W -f='\${Version}' trunkvista" "$v3"
check "the update kept the edited settings, the data and the accounts" bash -c "grep -qx 'TRUNKVISTA_PORT=$PORT' $DEFAULTS && [ \"\$(cat $DATA/marker)\" = 'keep me' ] && [ \"\$(sha256sum < $DATA/users.json)\" = '$users_before' ]"
check "its download folder was cleaned up" bash -c "! ls /var/cache/trunkvista-update/* 2>/dev/null | grep -q ."
upd --check --quiet; rc=$?; [ $rc = 0 ] && ok "--check: up to date afterwards (exit status 0)" || bad "--check afterwards gave $rc"
if ! $have_systemd; then manual_start; fi
wait_http $PORT && ok "the server answers after the update" || bad "no answer after the update"
check "... and the signed-in browser is still signed in" fixed "code -b $work/jar1 http://127.0.0.1:$PORT/api/connections" "200"

v4="$(bump "$v3")"
if build_version "$v4" "$work/rel-$v4"; then
    mkdir -p "$work/tampered"; cp "$work/rel-$v4/trunkvista_${v4}_all.deb" "$work/tampered/"
    printf 'x' >> "$work/tampered/trunkvista_${v4}_all.deb"       # the file on the server is not the file the checksums were made for
    kill "$mock_pid"; mock_pid=""
    start_mock "$work/mock2.out" --release "v$v4:$work/tampered/trunkvista_${v4}_all.deb,$work/rel-$v4/SHA256SUMS"
    out="$(upd --yes 2>&1)"; rc=$?
    [ $rc = 1 ] && echo "$out" | grep -qi "checksum\|does not match\|sha256" && ok "a release whose .deb does not match SHA256SUMS is refused (exit status 1)" || { bad "tampered release was not refused ($rc)"; echo "$out"; }
    check "... and the installed version is still $v3" fixed "dpkg-query -W -f='\${Version}' trunkvista" "$v3"
else
    bad "could not build the $v4 test package"
fi
kill "$mock_pid" 2>/dev/null; mock_pid=""

# ============================================================== HTTPS, certificate renewal, custom.css
echo "== https"
TLS=/etc/trunkvista/tls
cp "$DEFAULTS" "$work/defaults.before-tls"
cert_fp() { openssl s_client -connect "127.0.0.1:$PORT" </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256 2>/dev/null; }
wait_https() { local _; for _ in $(seq 1 60); do curl -fsS -o /dev/null --cacert "$TLS/server.crt" "https://127.0.0.1:$PORT/" 2>/dev/null && return 0; sleep 0.5; done; return 1; }
trunkvista-cert --name localhost --ip 127.0.0.1 >"$work/cert.out" 2>&1 && ok "trunkvista-cert makes a certificate" || { bad "trunkvista-cert failed"; cat "$work/cert.out"; }
check "key root:trunkvista 640, certificate 644, folder 750" fixed "stat -c '%U:%G %a' $TLS/server.key $TLS/server.crt $TLS | tr '\n' ' '" "root:$SVC_USER 640 root:$SVC_USER 644 root:$SVC_USER 750 "
check "it refuses to overwrite without --force" bash -c "! trunkvista-cert --name localhost >/dev/null 2>&1"
check "the service user can read the key" runuser -u "$SVC_USER" -- head -c 1 "$TLS/server.key"
printf 'TRUNKVISTA_TLS_CERT=%s\nTRUNKVISTA_TLS_KEY=%s\n' "$TLS/server.crt" "$TLS/server.key" >> "$DEFAULTS"
server_up
wait_https && ok "HTTPS answers, certificate verified against server.crt" || { bad "no HTTPS answer"; server_log | tail -5; }
check "the page comes over HTTPS" bash -c "curl -fsS --cacert $TLS/server.crt https://127.0.0.1:$PORT/ | grep -q '<title>'"
check "signing in works over HTTPS" fixed "curl -s -o /dev/null -w '%{http_code}' --cacert $TLS/server.crt -c $work/jar_tls -X POST https://127.0.0.1:$PORT/api/login -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$new_pw\"}'" "200"
check "... and the sign-in cookie is then marked Secure and HttpOnly" bash -c "grep tv_session $work/jar_tls | grep -q '^#HttpOnly_' && [ \"\$(awk '\$6==\"tv_session\"{print \$4}' $work/jar_tls)\" = TRUE ]"
check "plain http on the HTTPS port is redirected (307)" fixed "curl -s -o /dev/null -w '%{http_code} %{redirect_url}' http://127.0.0.1:$PORT/x" "307 https://127.0.0.1:$PORT/x"
check "TLS 1.2 handshake works" bash -c "openssl s_client -connect 127.0.0.1:$PORT -tls1_2 </dev/null 2>&1 | grep -q 'Protocol *: TLSv1.2'"
server_log | grep -q "running at https://127.0.0.1:$PORT/" && ok "the log shows the https address" || bad "log does not show https"
server_log | grep -qF "$new_pw" && bad "the password is in the log" || ok "the password is still not in the log"
# a renewed certificate is used without a restart
fp_before="$(cert_fp)"; pid_before_renew="$(server_pid)"
trunkvista-cert --force --name localhost --ip 127.0.0.1 >/dev/null 2>&1 && ok "trunkvista-cert --force renews it" || bad "renewal failed"
fp_after="$fp_before"
for _ in $(seq 1 40); do fp_after="$(cert_fp)"; [ "$fp_after" != "$fp_before" ] && break; sleep 1; done
[ -n "$fp_before" ] && [ "$fp_after" != "$fp_before" ] && ok "the renewed certificate is served without a restart" || bad "still the old certificate after 40 s"
[ "$(server_pid)" = "$pid_before_renew" ] && ok "... by the same process" || bad "the server process changed"
# custom.css
echo ':root{--accent:#ff00aa}' > "$DATA/custom.css"
https_get() { curl -fsS --cacert "$TLS/server.crt" "$@" "https://127.0.0.1:$PORT/"; }                     # https_get [curl options]
check "custom.css is added to the page" bash -c "$(declare -f https_get); TLS=$TLS; PORT=$PORT; https_get -b $work/jar_tls | grep -q 'id=\"custom-css\"' && https_get -b $work/jar_tls | grep -q -- '--accent:#ff00aa'"
check "... and to the sign-in page" bash -c "$(declare -f https_get); TLS=$TLS; PORT=$PORT; https_get | grep -q 'id=\"custom-css\"'"
rm -f "$DATA/custom.css"
check "... and gone again when the file is" bash -c "$(declare -f https_get); TLS=$TLS; PORT=$PORT; https_get -b $work/jar_tls | grep -q 'EDIT_TOKEN' && ! https_get -b $work/jar_tls | grep -q custom-css"
cp "$work/defaults.before-tls" "$DEFAULTS"
server_up
wait_http $PORT && ok "back on plain HTTP after removing the two settings" || bad "no answer on plain HTTP"

# ============================================================== trunkvista-setup
echo "== trunkvista-setup"
ZPORT=18766
python3 "$repo_root/tests/fake_zabbix.py" --port "$ZPORT" --token e2e-token >"$work/zbx.log" 2>&1 &
zbx_pid=$!
for _ in $(seq 1 20); do curl -s -o /dev/null "http://127.0.0.1:$ZPORT/" 2>/dev/null && break; sleep 0.2; done
check "trunkvista-setup is installed" test -x /usr/bin/trunkvista-setup
cp "$DEFAULTS" "$work/defaults.before-setup"
settle() { if $have_systemd; then wait_http "$PORT"; else server_up; wait_http "$PORT"; fi; }          # (without systemd the script cannot restart the server: do it by hand)
printf 'wrong-token\n' | trunkvista-setup --zabbix-url "http://127.0.0.1:$ZPORT" --token-stdin >"$work/setup.out" 2>&1; rc=$?
[ $rc != 0 ] && ok "a wrong token is refused" || bad "a wrong token was accepted"
check "... with the reason, and nothing was saved" bash -c "grep -q 'not saved' $work/setup.out && python3 -c 'import json; assert not json.load(open(\"$DATA/connections.json\"))[\"profiles\"]'"
printf 'e2e-token\n' | trunkvista-setup --zabbix-url "http://127.0.0.1:$ZPORT" --zabbix-name Lab --token-stdin >"$work/setup.out" 2>&1; rc=$?
[ $rc = 0 ] && ok "trunkvista-setup connects the (fake) Zabbix" || { bad "trunkvista-setup --zabbix-url failed"; cat "$work/setup.out"; }
check "it tried the connection first and says what it found" grep -q 'Connected to Zabbix 7.0.8: 3 monitored hosts' "$work/setup.out"
check "the token is not printed" bash -c "! grep -q 'e2e-token' $work/setup.out"
check "connections.json belongs to the service user, owner-only" fixed "stat -c '%U %a' $DATA/connections.json" "$SVC_USER 600"
settle || { bad "the dashboard does not answer after the setup"; server_log | tail -5; }
signin admin "$new_pw" "$work/jar_setup" "http://127.0.0.1:$PORT" >/dev/null
curl -s -b "$work/jar_setup" "http://127.0.0.1:$PORT/api/connections" >"$work/conns.json"
if python3 - "$work/conns.json" <<'PY'
import json, sys
j = json.load(open(sys.argv[1]))
p = j["profiles"][0]
sys.exit(0 if (p["name"], j["active"] == p["id"], p["has_secret"]) == ("Lab", True, True) and "e2e-token" not in json.dumps(j) else 1)
PY
then ok "the page's Connections list shows it, in use, without the token"; else bad "the page does not show the saved connection"; fi
check "the settings file was not touched by that" cmp -s "$DEFAULTS" "$work/defaults.before-setup"
trunkvista-setup --network --yes >"$work/setup.out" 2>&1; rc=$?
[ $rc = 0 ] && ok "trunkvista-setup --network (with the certificate the HTTPS test above made, for localhost only)" || { bad "trunkvista-setup --network failed"; cat "$work/setup.out"; }
check "it keeps that certificate but says it does not name this machine's address" grep -q 'was not made for' "$work/setup.out"
check "... and the certificate was not replaced" bash -c "openssl x509 -in $TLS/server.crt -noout -ext subjectAltName | grep -qv '$(hostname -I | awk '{print $1}')'"
trunkvista-setup --local --yes >/dev/null 2>&1
rm -f "$TLS/server.crt" "$TLS/server.key"                    # a machine that has none yet
cp "$work/defaults.before-setup" "$DEFAULTS"
trunkvista-setup --network --yes >"$work/setup.out" 2>&1; rc=$?
[ $rc = 0 ] && ok "trunkvista-setup --network (on a machine without a certificate)" || { bad "trunkvista-setup --network failed"; cat "$work/setup.out"; }
check "it listens everywhere and HTTPS is on, with a certificate it made" bash -c "grep -qx 'TRUNKVISTA_HOST=0.0.0.0' $DEFAULTS && grep -qx 'TRUNKVISTA_TLS_CERT=$TLS/server.crt' $DEFAULTS && grep -qx 'TRUNKVISTA_TLS_KEY=$TLS/server.key' $DEFAULTS && test -s $TLS/server.crt"
check "nothing else in the settings changed (only those three lines)" bash -c "[ \"\$(diff $work/defaults.before-setup $DEFAULTS | grep -c '^[<>]')\" = 6 ]"      # the host line (old + new) and the two comment lines switched on (old + new each)
check "it prints the addresses to open, https" grep -q "https://.*:$PORT/" "$work/setup.out"
$have_systemd || server_up
wait_https && ok "the dashboard answers over HTTPS on the network address" || { bad "no HTTPS answer after --network"; server_log | tail -5; }
ext_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ -n "$ext_ip" ]; then
    if curl -fsS -o /dev/null --noproxy '*' --cacert "$TLS/server.crt" "https://$ext_ip:$PORT/" 2>"$work/ext.err"; then ok "... and answers on this machine's own address ($ext_ip), which the certificate names"
    else bad "... and answers on this machine's own address ($ext_ip), which the certificate names"; cat "$work/ext.err"; openssl x509 -in "$TLS/server.crt" -noout -ext subjectAltName; grep -E '^TRUNKVISTA_(HOST|PORT)' "$DEFAULTS"; server_log | tail -3; fi
fi
check "the connection made before is still there" bash -c "python3 -c 'import json; j=json.load(open(\"$DATA/connections.json\")); assert len(j[\"profiles\"])==1'"
cp "$DEFAULTS" "$work/defaults.after-network"
trunkvista-setup --network --yes >"$work/setup.out" 2>&1; rc=$?
[ $rc = 0 ] && cmp -s "$DEFAULTS" "$work/defaults.after-network" && ok "running it again changes nothing" || bad "the second run changed something"
trunkvista-setup --local --yes >"$work/setup.out" 2>&1; rc=$?
[ $rc = 0 ] && ok "trunkvista-setup --local" || { bad "trunkvista-setup --local failed"; cat "$work/setup.out"; }
check "back to this computer only (the certificate settings stay)" bash -c "grep -qx 'TRUNKVISTA_HOST=127.0.0.1' $DEFAULTS && grep -qx 'TRUNKVISTA_TLS_CERT=$TLS/server.crt' $DEFAULTS"
$have_systemd || server_up
wait_https && ok "still served over HTTPS" || bad "no HTTPS answer after --local"
if [ -n "$ext_ip" ]; then check "... and no longer answers on the machine's own address" bash -c "! curl -fsS -o /dev/null --noproxy '*' --max-time 5 --cacert $TLS/server.crt https://$ext_ip:$PORT/"; fi
if $have_systemd; then
    trunkvista-setup --auto-update --yes >/dev/null 2>&1
    check "--auto-update switches the nightly timer on" systemctl is-enabled --quiet trunkvista-update.timer
    trunkvista-setup --no-auto-update --yes >/dev/null 2>&1
    check "--no-auto-update switches it off" bash -c "! systemctl is-enabled --quiet trunkvista-update.timer"
fi
kill "$zbx_pid" 2>/dev/null; zbx_pid=""
cp "$work/defaults.before-setup" "$DEFAULTS"
rm -f "$DATA/connections.json"
server_up
wait_http "$PORT" && ok "back on plain HTTP after removing what the setup added" || bad "no answer on plain HTTP"

# ============================================================== remove, reinstall, purge
echo "== remove / reinstall / purge"
users_before="$(sha256sum < "$DATA/users.json")"
$have_systemd || manual_stop
apt-get remove -y trunkvista >"$work/apt.log" 2>&1 && ok "apt-get remove succeeds" || { bad "remove failed"; cat "$work/apt.log"; }
check "program files are gone" bash -c "! test -e /usr/lib/trunkvista && ! test -e /usr/bin/trunkvista && ! test -e /usr/bin/trunkvista-update && ! test -e /lib/systemd/system/trunkvista.service"
check "your settings, data and the user are kept" bash -c "test -f $DEFAULTS && test -f $DATA/marker && getent passwd $SVC_USER"
if $have_systemd; then
    check "the service is stopped" bash -c "! systemctl is-active --quiet trunkvista"
    check "the service answers no more" bash -c "! curl -fsS -o /dev/null http://127.0.0.1:$PORT/"
fi
apt_install "$deb" && ok "reinstall succeeds" || bad "reinstall failed"
check "reinstall: edited settings kept (port $PORT)" bash -c "grep -qx 'TRUNKVISTA_PORT=$PORT' $DEFAULTS"
check "reinstall: the service is enabled again" test -L /etc/systemd/system/multi-user.target.wants/trunkvista.service
check "reinstall: data and accounts kept, and no second account was made" bash -c "[ \"\$(sha256sum < $DATA/users.json)\" = '$users_before' ] && test -f $DATA/marker && ! test -e $DATA/initial-admin-password"
if $have_systemd; then wait_http $PORT && ok "reinstall: the service is running" || bad "reinstall: no answer"; else manual_start; wait_http $PORT && ok "reinstall: server answers" || bad "reinstall: no answer"; manual_stop; fi
out="$(apt-get purge -y trunkvista 2>&1)"; rc=$?
[ $rc = 0 ] && ok "apt-get purge succeeds" || { bad "purge failed"; echo "$out"; }
check "purge removes /etc/default/trunkvista" bash -c "! test -e $DEFAULTS"
check "purge removes the systemd state (no leftover symlink or mask)" bash -c "! test -e /etc/systemd/system/trunkvista.service && ! test -e /etc/systemd/system/multi-user.target.wants/trunkvista.service"
check "purge keeps the data and says how to remove it" bash -c "test -f $DATA/marker && echo '$out' | grep -q 'rm -rf /var/lib/trunkvista'"
check "purge keeps the HTTPS certificate and says so" bash -c "test -f $TLS/server.key && echo '$out' | grep -q 'rm -rf /etc/trunkvista'"

# ============================================================== the first account on the other paths
echo "== first account: preseeded answers, and an upgrade from a version that had no accounts"
rm -rf "$DATA"; deluser --system "$SVC_USER" >/dev/null 2>&1; delgroup --system "$SVC_USER" >/dev/null 2>&1
printf '%s\n' "trunkvista trunkvista/admin_user string alice" "trunkvista trunkvista/admin_password password alice has a long password" \
    "trunkvista trunkvista/admin_password_again password alice has a long password" | debconf-set-selections
apt_install "$deb" && ok "install with preseeded answers succeeds" || bad "install failed"
check "the account alice exists and is the only one" bash -c "[ \"\$(trunkvista-user list | awk '{print \$1}')\" = alice ]"
check "... as an administrator, without a temporary password file" bash -c "trunkvista-user list | grep -q '^alice  *admin' && ! test -e $DATA/initial-admin-password"
check "the answers' password is not kept in the debconf database" bash -c "! grep -rqs 'alice has a long password' /var/cache/debconf /var/lib/debconf"
if ! $have_systemd; then manual_start; fi
wait_http 8088 && ok "the server answers" || bad "no answer"
check "alice signs in with the preseeded password (200)" fixed "signin alice 'alice has a long password' $work/jar_alice" "200"
check "... and is not asked to change it" bash -c "curl -s -b $work/jar_alice http://127.0.0.1:8088/api/layout | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d[\"can_edit\"] and not d[\"user\"][\"must_change\"]'"
$have_systemd || manual_stop
rm -f "$DATA/users.json" "$DATA/sessions.json"; echo "oldtokenoldtokenoldtoken" > "$DATA/edit_token"; chown "$SVC_USER:$SVC_USER" "$DATA/edit_token"
apt_install --reinstall "$deb" && ok "reinstalling over a data folder without accounts (a version from before sign-in) succeeds" || bad "reinstall failed"
check "an account is made again, with a random password that has to be changed" bash -c "[ \"\$(trunkvista-user list | awk '{print \$1}')\" = alice ] && trunkvista-user list | grep -q 'must change password' && test -f $DATA/initial-admin-password"
check "the old edit token file is removed" bash -c "! test -e $DATA/edit_token"
if ! $have_systemd; then manual_start; fi
wait_http 8088 && ok "the server answers" || bad "no answer"
legacy_pw="$(sed -n 's/^password: //p' "$DATA/initial-admin-password")"
check "the random password signs in" fixed "signin alice '$legacy_pw' $work/jar_legacy" "200"
# an install that cannot make an account must not leave an open dashboard: the service starts with --require-login
$have_systemd || manual_stop
rm -f "$DATA/users.json" "$DATA/initial-admin-password"
if ! $have_systemd; then manual_start; else systemctl restart trunkvista; fi
wait_http 8088 && ok "without any account the server still answers ..." || bad "no answer"
check "... with the sign-in page, never an open dashboard (401)" fixed "code http://127.0.0.1:8088/api/layout" "401"
check "... which says how to make an account" bash -c "curl -fsS http://127.0.0.1:8088/ | grep -q 'trunkvista-user add'"

# ============================================================== an installation under the old name
echo "== migration from the old name (package channelview)"
legacy="${E2E_LEGACY_DEB:-}"
legacy_commit=589d2cb        # the last commit that was made under the old name (ChannelView 1.2.1 plus the sign-in work)
if [ -z "$legacy" ] && command -v git >/dev/null && git -C "$repo_root" cat-file -e "${legacy_commit}^{commit}" 2>/dev/null; then
    mkdir -p "$work/old" && git -C "$repo_root" archive "$legacy_commit" | tar -x -C "$work/old" \
        && (cd "$work/old" && GITHUB_REPOSITORY=test/zbx packaging/build_deb.sh >/dev/null 2>&1) \
        && legacy="$(find "$work/old/dist" -name "channelview_*_all.deb" 2>/dev/null | head -n1)"
fi
if [ -z "$legacy" ] || [ ! -f "$legacy" ]; then
    echo "(skipped: no package of the old name. Set E2E_LEGACY_DEB=/path/channelview_X_all.deb, or run it in a clone that has commit $legacy_commit)"
else
    OLDDATA=/var/lib/channelview; OLDPORT=18089; OLDTLS=/etc/channelview/tls
    $have_systemd || manual_stop
    apt-get purge -y trunkvista >/dev/null 2>&1; rm -rf "$DATA" /etc/trunkvista /etc/default/trunkvista
    deluser --system "$SVC_USER" >/dev/null 2>&1; delgroup --system "$SVC_USER" >/dev/null 2>&1
    printf '%s\n' "channelview channelview/admin_user string oldadmin" "channelview channelview/admin_password password the old admin password" \
        "channelview channelview/admin_password_again password the old admin password" | debconf-set-selections
    apt_install "$legacy" && ok "the old package (channelview $(dpkg-deb -f "$legacy" Version)) installs" || bad "installing the old package failed"
    # an installation the way an administrator would have left it: another account, an own port, HTTPS, custom.css, a changed setting
    printf 'the old viewer password\n' | channelview-user add tv --role viewer --idle never --password-stdin >/dev/null
    printf 'kept\n' > "$OLDDATA/marker"; printf ':root{--accent:#00aaff}\n' > "$OLDDATA/custom.css"
    chown channelview:channelview "$OLDDATA/marker" "$OLDDATA/custom.css"
    sed -i "s/^CHANNELVIEW_PORT=.*/CHANNELVIEW_PORT=$OLDPORT/" /etc/default/channelview
    channelview-cert --name localhost --ip 127.0.0.1 >/dev/null 2>&1
    printf 'CHANNELVIEW_TLS_CERT=%s/server.crt\nCHANNELVIEW_TLS_KEY=%s/server.key\nCHANNELVIEW_UPDATE_CHANNEL=prerelease\n' "$OLDTLS" "$OLDTLS" >> /etc/default/channelview
    key_before="$(sha256sum < "$OLDTLS/server.key")"
    if $have_systemd; then
        systemctl enable channelview-update.timer >/dev/null 2>&1
        systemctl restart channelview
    else
        runuser -u channelview -- env PYTHONUNBUFFERED=1 CHANNELVIEW_TLS_CERT="$OLDTLS/server.crt" CHANNELVIEW_TLS_KEY="$OLDTLS/server.key" \
            /usr/lib/channelview/channelview.py --no-browser --port "$OLDPORT" --data-dir "$OLDDATA" --require-login >"$work/old-server.log" 2>&1 &
    fi
    old_up=false; for _ in $(seq 1 60); do curl -fsS -o /dev/null --cacert "$OLDTLS/server.crt" "https://127.0.0.1:$OLDPORT/" 2>/dev/null && { old_up=true; break; }; sleep 0.5; done
    $old_up && ok "the old server answers over HTTPS on its own port" || bad "the old server does not answer"
    check "the old administrator signs in (the old session cookie is made)" fixed "code --cacert $OLDTLS/server.crt -c $work/jar_old -X POST https://127.0.0.1:$OLDPORT/api/login -H 'Content-Type: application/json' -d '{\"username\":\"oldadmin\",\"password\":\"the old admin password\"}'" "200"
    users_old="$(sha256sum < "$OLDDATA/users.json")"
    $have_systemd || pkill -u channelview -f channelview.py; sleep 0.5

    apt_install "$deb" && ok "installing the new package replaces the old one" || bad "installing over the old package failed"
    check "the installation says what was moved" grep -q 'was called ChannelView before' "$work/apt.log"
    check "the old package is gone (removed, not installed)" bash -c "! dpkg -s channelview 2>/dev/null | grep -q '^Status: install ok installed'"
    check "the old program, commands and service files are gone" bash -c "! test -e /usr/lib/channelview && ! test -e /usr/bin/channelview-user && ! test -e /lib/systemd/system/channelview.service"
    check "the old data folder was moved, not copied" bash -c "! test -e $OLDDATA && test -f $DATA/marker && test -f $DATA/custom.css"
    check "the accounts came along unchanged" fixed "sha256sum < $DATA/users.json" "$users_old"
    check "the data belongs to the new service user" fixed "stat -c '%U:%G %a' $DATA $DATA/users.json | tr '\n' ' '" "$SVC_USER:$SVC_USER 700 $SVC_USER:$SVC_USER 600 "
    check "the old system user and group are gone, the new ones exist" bash -c "! getent passwd channelview && ! getent group channelview && getent passwd $SVC_USER"
    check "the certificate and key moved, the key readable by the new group only (640)" fixed "stat -c '%U:%G %a' /etc/trunkvista/tls/server.key | tr '\n' ' '; test -e /etc/channelview && echo old-etc-left" "root:$SVC_USER 640 "
    check "... and the key is the same one" fixed "sha256sum < /etc/trunkvista/tls/server.key" "$key_before"
    check "the settings were carried over under the new names (port, HTTPS files, update channel)" bash -c "grep -qx 'TRUNKVISTA_PORT=$OLDPORT' $DEFAULTS && grep -qx 'TRUNKVISTA_TLS_CERT=/etc/trunkvista/tls/server.crt' $DEFAULTS && grep -qx 'TRUNKVISTA_TLS_KEY=/etc/trunkvista/tls/server.key' $DEFAULTS && grep -qx 'TRUNKVISTA_UPDATE_CHANNEL=prerelease' $DEFAULTS"
    check "... with nothing of the old names left in the file's settings, and the old file removed" bash -c "! grep -v '^[[:space:]]*#' $DEFAULTS | grep -qi channelview && ! test -e /etc/default/channelview"
    check "the new /etc/default/trunkvista is still a conffile" bash -c "dpkg-query -W -f='\${Conffiles}\n' trunkvista | grep -q '$DEFAULTS'"
    check "no second administrator was made, no random password file" bash -c "[ \"\$(trunkvista-user list | wc -l)\" = 2 ] && ! test -e $DATA/initial-admin-password"
    if $have_systemd; then
        check "the nightly update check that was on stays on (new timer enabled, old one gone)" bash -c "systemctl is-enabled --quiet trunkvista-update.timer && ! systemctl is-enabled --quiet channelview-update.timer"
        wait_https_port() { local _; for _ in $(seq 1 60); do curl -fsS -o /dev/null --cacert "/etc/trunkvista/tls/server.crt" "https://127.0.0.1:$1/" 2>/dev/null && return 0; sleep 0.5; done; return 1; }
        wait_https_port "$OLDPORT" && ok "the service answers over HTTPS on the old port" || bad "no answer from the new service"
    else
        manual_start
        for _ in $(seq 1 60); do curl -fsS -o /dev/null --cacert "/etc/trunkvista/tls/server.crt" "https://127.0.0.1:$OLDPORT/" 2>/dev/null && break; sleep 0.5; done
    fi
    check "the old administrator signs in with the old password" fixed "code --cacert /etc/trunkvista/tls/server.crt -c $work/jar_new -X POST https://127.0.0.1:$OLDPORT/api/login -H 'Content-Type: application/json' -d '{\"username\":\"oldadmin\",\"password\":\"the old admin password\"}'" "200"
    check "... and so does the viewer" fixed "code --cacert /etc/trunkvista/tls/server.crt -c $work/jar_tv -X POST https://127.0.0.1:$OLDPORT/api/login -H 'Content-Type: application/json' -d '{\"username\":\"tv\",\"password\":\"the old viewer password\"}'" "200"
    check "custom.css still reaches the page" bash -c "curl -fsS --cacert /etc/trunkvista/tls/server.crt -b $work/jar_new https://127.0.0.1:$OLDPORT/ | grep -q -- '--accent:#00aaff'"
    check "the cookie is called tv_session now" bash -c "grep -q 'tv_session' $work/jar_new && ! grep -q 'cv_session' $work/jar_new"
    check "the page and the sign-in page carry the new name (except the code that moves the old stored settings)" bash -c "curl -fsS --cacert /etc/trunkvista/tls/server.crt https://127.0.0.1:$OLDPORT/ | grep -q 'Sign in - TrunkVista' && ! curl -fsS --cacert /etc/trunkvista/tls/server.crt -b $work/jar_new https://127.0.0.1:$OLDPORT/ | grep -v 'called ChannelView before' | grep -q ChannelView"
    $have_systemd || manual_stop
    apt-get purge -y channelview >/dev/null 2>&1 && ok "the leftover record of the old package can be purged" || bad "purging the old package record failed"
    check "... which leaves the migrated data alone" bash -c "test -f $DATA/marker && test -f $DEFAULTS && test -f /etc/trunkvista/tls/server.key"
fi

# ---- the same, from what people really have: the last published release of the old name (before accounts existed)
echo "== migration from the last published release of the old name (1.2.1, no accounts yet)"
release_commit=e6eb4f9
last=""
if command -v git >/dev/null && git -C "$repo_root" cat-file -e "${release_commit}^{commit}" 2>/dev/null; then
    mkdir -p "$work/rel" && git -C "$repo_root" archive "$release_commit" | tar -x -C "$work/rel" \
        && (cd "$work/rel" && GITHUB_REPOSITORY=test/zbx packaging/build_deb.sh >/dev/null 2>&1) \
        && last="$(find "$work/rel/dist" -name "channelview_*_all.deb" 2>/dev/null | head -n1)"
fi
if [ -z "$last" ] || [ ! -f "$last" ]; then
    echo "(skipped: needs the git history, commit $release_commit)"
else
    $have_systemd || manual_stop
    apt-get purge -y trunkvista channelview >/dev/null 2>&1; rm -rf "$DATA" /etc/trunkvista "$DEFAULTS" /var/lib/channelview /etc/channelview /etc/default/channelview
    for n in trunkvista channelview; do deluser --system "$n" >/dev/null 2>&1; delgroup --system "$n" >/dev/null 2>&1; done
    apt_install "$last" && ok "the last release of the old name (channelview $(dpkg-deb -f "$last" Version)) installs" || bad "installing the old release failed"
    printf 'kept\n' > /var/lib/channelview/marker; printf 'the-old-edit-token\n' > /var/lib/channelview/edit_token
    chown channelview:channelview /var/lib/channelview/marker /var/lib/channelview/edit_token
    sed -i "s/^CHANNELVIEW_PORT=.*/CHANNELVIEW_PORT=18077/" /etc/default/channelview
    apt_install "$deb" && ok "installing the new package replaces it" || bad "installing over the old release failed"
    check "the old package is gone and its data moved over" bash -c "! dpkg -s channelview 2>/dev/null | grep -q '^Status: install ok installed' && ! test -e /var/lib/channelview && test -f $DATA/marker"
    check "the setting (port) came along under its new name" bash -c "grep -qx 'TRUNKVISTA_PORT=18077' $DEFAULTS && ! test -e /etc/default/channelview"
    check "an administrator was made, with a random password that must be changed" bash -c "test -s $DATA/initial-admin-password && [ \"\$(trunkvista-user list | wc -l)\" = 1 ]"
    check "the edit token of the old release is gone (signing in replaces it)" bash -c "! test -e $DATA/edit_token"
    check "the installation says both: what was moved, and the new account" bash -c "grep -q 'was called ChannelView before' $work/apt.log && grep -q 'created with a random password' $work/apt.log"
    check "the old system user and group are gone" bash -c "! getent passwd channelview && ! getent group channelview"
    apt-get purge -y channelview >/dev/null 2>&1 && ok "the leftover record of the old package can be purged" || bad "purging the old package record failed"
    check "... which leaves the migrated data alone" bash -c "test -f $DATA/marker && test -s $DATA/users.json"
fi

# ============================================================== install.sh
echo "== install.sh (the release archive: wget, tar xzf, cd, sudo ./install.sh)"
$have_systemd || manual_stop
apt-get purge -y trunkvista channelview >/dev/null 2>&1; rm -rf "$DATA" /etc/trunkvista "$DEFAULTS" /var/lib/channelview /etc/channelview /etc/default/channelview
for n in trunkvista channelview; do deluser --system "$n" >/dev/null 2>&1; delgroup --system "$n" >/dev/null 2>&1; done
bundle="$work/bundle"; mkdir -p "$bundle"; chmod 0755 "$bundle"
GITHUB_REPOSITORY=test/zbx "$repo_root/packaging/build_bundle.sh" "$deb" "$bundle" >"$work/bundle.out" 2>&1 && ok "the release archive is built from the package" || { bad "build_bundle.sh failed"; cat "$work/bundle.out"; }
check "the archive and the stand-alone installer name this repository" bash -c "tar xzOf $bundle/trunkvista-latest.tar.gz trunkvista/install.sh | grep -q 'releases/latest/download' && ! grep -q '@REPO@' $bundle/install.sh && cmp -s $bundle/install.sh <(tar xzOf $bundle/trunkvista-latest.tar.gz trunkvista/install.sh)"
unpacked="$work/unpacked"; mkdir -p "$unpacked"; chmod 0755 "$unpacked"
tar xzf "$bundle/trunkvista-latest.tar.gz" -C "$unpacked"
check "it unpacks to a folder trunkvista with the installer, the package, its checksum and a readme" bash -c "cd $unpacked/trunkvista && test -x install.sh && test -f $(basename "$deb") && test -f SHA256SUMS && test -f README.txt"
( cd "$unpacked/trunkvista" && sh ./install.sh </dev/null >"$work/install.out" 2>&1 ); rc=$?
[ $rc = 0 ] && ok "./install.sh installs the package that sits beside it" || { bad "install.sh failed"; cat "$work/install.out"; }
check "it checked the package against the checksum in the archive" grep -q 'Checksum ok.' "$work/install.out"
check "it downloaded nothing" bash -c "! grep -qi 'downloading' $work/install.out"
check "the package is installed (version $version)" fixed "dpkg-query -W -f='\${Version}' trunkvista" "$version"
check "without a terminal it asked nothing: a random administrator password was made" bash -c "test -s $DATA/initial-admin-password && [ \"\$(trunkvista-user list | wc -l)\" = 1 ]"
check "no Zabbix connection was asked for: that is entered in the page" bash -c "python3 /usr/lib/trunkvista/trunkvista.py connection --data-dir $DATA list | grep -q '^No connections'"
check "it says what is next: sign in, then the page asks for the Zabbix server" bash -c "grep -q 'Open that page and sign in' $work/install.out && grep -q 'The page asks for your Zabbix server' $work/install.out"
check "it does not open the network by itself (only this computer)" bash -c "! grep -q '^TRUNKVISTA_HOST=0.0.0.0' $DEFAULTS"
# a damaged or changed package is refused, also when the installer is started by its full path from somewhere else
echo "tampered" >> "$unpacked/trunkvista/$(basename "$deb")"
apt-get purge -y trunkvista >/dev/null 2>&1; rm -rf "$DATA" /etc/trunkvista "$DEFAULTS"
( cd / && sh "$unpacked/trunkvista/install.sh" </dev/null >"$work/install.out" 2>&1 ); rc=$?
[ $rc != 0 ] && ok "a package that does not match the checksum is refused" || bad "install.sh installed a tampered package"
check "... and nothing was installed" bash -c "! dpkg -s trunkvista 2>/dev/null | grep -q '^Status: install ok installed'"

echo "== install.sh on its own (downloads the release; a local copy of GitHub's release files)"
rel="$work/release"; mkdir -p "$rel"; chmod 0755 "$rel"
cp "$deb" "$rel/trunkvista-latest.deb"
( cd "$rel" && sha256sum trunkvista-latest.deb > SHA256SUMS )
RPORT=18799
( cd "$rel" && exec python3 -m http.server "$RPORT" --bind 127.0.0.1 >"$work/rel.log" 2>&1 ) &
rel_pid=$!
for _ in $(seq 1 20); do curl -s -o /dev/null "http://127.0.0.1:$RPORT/SHA256SUMS" 2>/dev/null && break; sleep 0.2; done
TRUNKVISTA_BASE_URL="http://127.0.0.1:$RPORT" sh "$repo_root/packaging/install.sh" </dev/null >"$work/install.out" 2>&1; rc=$?
[ $rc = 0 ] && ok "install.sh installs the package from the release files" || { bad "install.sh failed"; cat "$work/install.out"; }
check "it checked the download" grep -q 'Checksum ok.' "$work/install.out"
check "the package is installed (version $version)" fixed "dpkg-query -W -f='\${Version}' trunkvista" "$version"
echo "tampered" >> "$rel/trunkvista-latest.deb"
apt-get purge -y trunkvista >/dev/null 2>&1; rm -rf "$DATA" /etc/trunkvista "$DEFAULTS"
TRUNKVISTA_BASE_URL="http://127.0.0.1:$RPORT" sh "$repo_root/packaging/install.sh" </dev/null >"$work/install.out" 2>&1; rc=$?
[ $rc != 0 ] && ok "a package that does not match SHA256SUMS is refused" || bad "install.sh installed a tampered package"
check "... and nothing was installed" bash -c "! dpkg -s trunkvista 2>/dev/null | grep -q '^Status: install ok installed'"
kill "$rel_pid" 2>/dev/null; rel_pid=""

echo
echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
