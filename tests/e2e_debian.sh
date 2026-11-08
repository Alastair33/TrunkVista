#!/bin/bash
# End-to-end test of the Debian package:  sudo tests/e2e_debian.sh [path/to/channelview_X_all.deb]      (default: dist/channelview_*_all.deb)
#
#   install  ->  service user, folders, files, the service answers, the first account is made, nothing works without signing in
#   upgrade  ->  a newer package keeps your edited /etc/default/channelview, the data, the accounts and the signed-in sessions, and restarts the service
#   update   ->  channelview-update installs a newer release from a local fake GitHub (good and tampered ones)
#   remove / reinstall / purge  ->  what stays and what goes
#   first account  ->  with answers preseeded in debconf, and when a version without accounts is upgraded
#
# WARNING: this really installs and purges the package and deletes /var/lib/channelview and the channelview user afterwards.
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
deb="${1:-$(find "$repo_root/dist" -maxdepth 1 -name "channelview_*_all.deb" 2>/dev/null | sort | tail -n1)}"
[ -f "$deb" ] || die "no .deb given and none in dist/ - run packaging/build_deb.sh first"
deb="$(readlink -f "$deb")"
if dpkg -s channelview >/dev/null 2>&1 || [ -e /var/lib/channelview ]; then
    [ "${E2E_FORCE:-}" = 1 ] || die "channelview is already installed or has data here; refusing to touch it (E2E_FORCE=1 purges it first)"
    apt-get purge -y channelview >/dev/null 2>&1; rm -rf /var/lib/channelview; deluser --system channelview >/dev/null 2>&1
fi

export DEBIAN_FRONTEND=noninteractive
PORT=18088
SVC_USER=channelview
DATA=/var/lib/channelview
DEFAULTS=/etc/default/channelview
have_systemd=false; [ -d /run/systemd/system ] && have_systemd=true
work="$(mktemp -d)"; chmod 0755 "$work"
mock_pid=""
version="$(dpkg-deb -f "$deb" Version)"
upstream="${version//\~/-}"
bump() { local base="${1%%-*}"; echo "${base%.*}.$(( ${base##*.} + 1 ))"; }
v2="$(bump "$upstream")"; v3="$(bump "$v2")"
echo "package under test: $deb (version $version);  systemd running: $have_systemd;  upgrade test versions: $v2, $v3"

cleanup() {
    [ -z "$mock_pid" ] || kill "$mock_pid" 2>/dev/null
    manual_stop
    apt-get purge -y channelview >/dev/null 2>&1
    rm -rf "$DATA" "$work" /etc/channelview/tls
    rmdir /etc/channelview 2>/dev/null
    deluser --system "$SVC_USER" >/dev/null 2>&1; delgroup --system "$SVC_USER" >/dev/null 2>&1
}
trap cleanup EXIT

# ---- the server: the real service under systemd, otherwise started by hand as the service user (like the unit does)
manual_log="$work/manual.log"
manual_stop() { pkill -u "$SVC_USER" -f channelview.py 2>/dev/null; sleep 0.5; }
manual_start() {
    manual_stop
    local host port tcert tkey tls_env=()
    host="$(sed -n 's/^CHANNELVIEW_HOST=//p' "$DEFAULTS" | tail -n1)"; port="$(sed -n 's/^CHANNELVIEW_PORT=//p' "$DEFAULTS" | tail -n1)"
    tcert="$(sed -n 's/^CHANNELVIEW_TLS_CERT=//p' "$DEFAULTS" | tail -n1)"; tkey="$(sed -n 's/^CHANNELVIEW_TLS_KEY=//p' "$DEFAULTS" | tail -n1)"
    [ -z "$tcert" ] || tls_env+=("CHANNELVIEW_TLS_CERT=$tcert")
    [ -z "$tkey" ] || tls_env+=("CHANNELVIEW_TLS_KEY=$tkey")
    runuser -u "$SVC_USER" -- env PYTHONUNBUFFERED=1 "${tls_env[@]}" python3 -c '
import ctypes, os, sys
try: ctypes.CDLL(None).prctl(65, 1, 0, 0, 0)      # PR_SET_MDWE: what MemoryDenyWriteExecute=yes does in the unit
except Exception: pass
os.execv(sys.argv[1], sys.argv[1:])' /usr/lib/channelview/channelview.py --no-browser --host "${host:-127.0.0.1}" --port "${port:-8088}" \
        --data-dir "$DATA" --require-login >"$manual_log" 2>&1 &
    sleep 0.3
}
server_up() { if $have_systemd; then systemctl restart channelview; else manual_start; fi; }
wait_http() { local _; for _ in $(seq 1 60); do curl -fsS -o /dev/null "http://127.0.0.1:$1/" 2>/dev/null && return 0; sleep 0.5; done; return 1; }
server_pid() { if $have_systemd; then systemctl show -p MainPID --value channelview; else pgrep -u "$SVC_USER" -f channelview.py | head -n1; fi; }
server_log() { if $have_systemd; then journalctl -u channelview --no-pager -o cat 2>/dev/null; else cat "$manual_log"; fi; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }                  # code [curl options] URL  ->  the HTTP status
signin() { code -c "$3" -X POST "${4:-http://127.0.0.1:8088}/api/login" -H 'Content-Type: application/json' -d "{\"username\":\"$1\",\"password\":\"$2\"}"; }   # signin USER PASSWORD JAR [BASE]
post_json() { code -b "$2" -c "$2" -X POST "$1" -H 'Content-Type: application/json' -H "X-Edit-Token: $3" -d "$4"; }                                      # post_json URL JAR TOKEN JSON
page_token() { curl -s -b "$1" "${2:-http://127.0.0.1:8088}/" | sed -n 's/.*const EDIT_TOKEN = "\([^"]*\)".*/\1/p'; }                                       # the session's request token
apt_install() { apt-get install -y --no-install-recommends -o Dpkg::Options::=--force-confold "$@" >"$work/apt.log" 2>&1 || { cat "$work/apt.log"; return 1; }; }

# ============================================================== install
echo "== install"
apt_install "$deb" && ok "apt-get install ./package.deb succeeds" || bad "install failed"
check "system user exists with home $DATA and no login shell" bash -c "getent passwd $SVC_USER | grep -q ':$DATA:/usr/sbin/nologin\$'"
check "data folder belongs to the service user, mode 750" fixed "stat -c '%U:%G %a' $DATA" "$SVC_USER:$SVC_USER 750"
check "server installed, executable, interpreter fixed to /usr/bin/python3" bash -c "head -n1 /usr/lib/channelview/channelview.py | grep -qx '#!/usr/bin/python3' && test -x /usr/lib/channelview/channelview.py"
check "channelview --version reports the packaged version" fixed "channelview --version | awk '{print \$NF}'" "$upstream"
check "channelview-update is installed" test -x /usr/bin/channelview-update
check "the service is enabled (symlink in multi-user.target.wants)" test -L /etc/systemd/system/multi-user.target.wants/channelview.service
check "the nightly update timer is NOT enabled" bash -c "! test -e /etc/systemd/system/timers.target.wants/channelview-update.timer"
check "/etc/default/channelview is a conffile" bash -c "dpkg-query -W -f='\${Conffiles}\n' channelview | grep -q '$DEFAULTS'"
if command -v systemd-analyze >/dev/null; then
    for u in channelview.service channelview-update.service channelview-update.timer; do
        check "systemd-analyze verify $u" bash -c "out=\$(systemd-analyze verify /lib/systemd/system/$u 2>&1); ! echo \"\$out\" | grep -qiE 'unknown (key|section|lvalue)|failed to|invalid|error'"
    done
fi
if $have_systemd; then
    check "service is active after the installation" bash -c "for i in \$(seq 1 20); do systemctl is-active --quiet channelview && exit 0; sleep 0.5; done; exit 1"
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
check "a viewer account made with channelview-user (never signs out) can look but not edit" bash -c "printf 'viewer password 1' | channelview-user add tv --role viewer --idle never --password-stdin >/dev/null && [ \"\$(curl -s -o /dev/null -w '%{http_code}' -c $work/jar_tv -X POST http://127.0.0.1:8088/api/login -H 'Content-Type: application/json' -d '{\"username\":\"tv\",\"password\":\"viewer password 1\"}')\" = 200 ] && [ \"\$(curl -s -o /dev/null -w '%{http_code}' -b $work/jar_tv http://127.0.0.1:8088/api/layout)\" = 200 ] && [ \"\$(curl -s -o /dev/null -w '%{http_code}' -b $work/jar_tv http://127.0.0.1:8088/api/connections)\" = 403 ]"
check "channelview-user list shows both accounts" bash -c "channelview-user list | grep -q '^admin ' && channelview-user list | grep -q '^tv .*never'"
server_log | grep -qF "$adm_pw" && bad "the temporary password is in the log" || ok "the temporary password is not in the log"
server_log | grep -qF "$new_pw" && bad "the new password is in the log" || ok "the new password is not in the log"
server_log | grep -q "\[accounts\] sign-in user=admin" && ok "sign-ins are logged (user and address, never the password)" || bad "no sign-in line in the log"
check "files written by the service belong to it (nothing owned by root in the data folder)" bash -c "! find $DATA -user root | grep -q ."

# ============================================================== administrator's changes, then an upgrade
echo "== upgrade"
echo "keep me" > "$DATA/marker"; chown "$SVC_USER:$SVC_USER" "$DATA/marker"
sed -i "s/^CHANNELVIEW_PORT=.*/CHANNELVIEW_PORT=$PORT/; s#^CHANNELVIEW_UPDATE_REPO=.*#CHANNELVIEW_UPDATE_REPO=test/zbx#" "$DEFAULTS"
server_up; wait_http $PORT && ok "after editing /etc/default/channelview and restarting, it listens on port $PORT" || bad "does not answer on $PORT"
pid_before="$(server_pid)"
users_before="$(sha256sum < "$DATA/users.json")"

build_version() {      # build_version VERSION OUTDIR : the repository with that VERSION, built with the same repository name as the installed package
    local v="$1" src="$work/src-$1"
    mkdir -p "$src"; tar --exclude=./dist --exclude=./.git -C "$repo_root" -cf - . | tar -C "$src" -xf -
    sed -i "s/^VERSION = \".*\"/VERSION = \"$v\"/" "$src/channelview.py"
    ( cd "$src" && GITHUB_REPOSITORY=test/zbx ./packaging/build_deb.sh >/dev/null ) || return 1
    mkdir -p "$2"; cp "$src/dist/channelview_${v}_all.deb" "$src/dist/SHA256SUMS" "$2/"
}
build_version "$v2" "$work/rel-$v2" && ok "built the test package $v2" || bad "could not build the $v2 test package"
apt_install "$work/rel-$v2/channelview_${v2}_all.deb" && ok "upgrade to $v2 succeeds without questions (--force-confold)" || bad "upgrade failed"
check "version is now $v2" fixed "dpkg-query -W -f='\${Version}' channelview" "$v2"
check "your edited /etc/default/channelview is kept (port $PORT, repository test/zbx)" bash -c "grep -qx 'CHANNELVIEW_PORT=$PORT' $DEFAULTS && grep -qx 'CHANNELVIEW_UPDATE_REPO=test/zbx' $DEFAULTS"
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
server_log | grep -q "ChannelView $v2 running" && ok "the running server reports $v2" || bad "server log does not show version $v2"

# ============================================================== update from (fake) GitHub
echo "== channelview-update"
build_version "$v3" "$work/rel-$v3" || bad "could not build the $v3 test package"
start_mock() {      # start_mock OUTFILE MOCK_ARGS... : starts the fake GitHub, sets $api and $mock_pid
    local out="$1"; shift
    python3 "$here/mock_github.py" --repo test/zbx --port 0 "$@" >"$out" 2>&1 &
    mock_pid=$!
    for _ in $(seq 1 40); do grep -q '^http' "$out" && break; sleep 0.25; done
    api="$(grep '^http' "$out" | head -n1)"
    [ -n "$api" ] || die "the fake GitHub did not start: $(cat "$out")"
}
start_mock "$work/mock.out" --release "v$v2:$work/rel-$v2/channelview_${v2}_all.deb,$work/rel-$v2/SHA256SUMS" \
    --release "v$v3:$work/rel-$v3/channelview_${v3}_all.deb,$work/rel-$v3/SHA256SUMS"
upd() { channelview-update --api-url "$api" "$@"; }
upd --check --quiet; rc=$?; [ $rc = 10 ] && ok "--check: exit status 10 (update available $v2 -> $v3), repository taken from /etc/default/channelview" || bad "--check gave $rc"
out="$(upd --yes 2>&1)"; rc=$?
[ $rc = 0 ] && ok "channelview-update --yes succeeds" || { bad "update failed ($rc)"; echo "$out"; }
check "version is now $v3" fixed "dpkg-query -W -f='\${Version}' channelview" "$v3"
check "the update kept the edited settings, the data and the accounts" bash -c "grep -qx 'CHANNELVIEW_PORT=$PORT' $DEFAULTS && [ \"\$(cat $DATA/marker)\" = 'keep me' ] && [ \"\$(sha256sum < $DATA/users.json)\" = '$users_before' ]"
check "its download folder was cleaned up" bash -c "! ls /var/cache/channelview-update/* 2>/dev/null | grep -q ."
upd --check --quiet; rc=$?; [ $rc = 0 ] && ok "--check: up to date afterwards (exit status 0)" || bad "--check afterwards gave $rc"
if ! $have_systemd; then manual_start; fi
wait_http $PORT && ok "the server answers after the update" || bad "no answer after the update"
check "... and the signed-in browser is still signed in" fixed "code -b $work/jar1 http://127.0.0.1:$PORT/api/connections" "200"

v4="$(bump "$v3")"
if build_version "$v4" "$work/rel-$v4"; then
    mkdir -p "$work/tampered"; cp "$work/rel-$v4/channelview_${v4}_all.deb" "$work/tampered/"
    printf 'x' >> "$work/tampered/channelview_${v4}_all.deb"       # the file on the server is not the file the checksums were made for
    kill "$mock_pid"; mock_pid=""
    start_mock "$work/mock2.out" --release "v$v4:$work/tampered/channelview_${v4}_all.deb,$work/rel-$v4/SHA256SUMS"
    out="$(upd --yes 2>&1)"; rc=$?
    [ $rc = 1 ] && echo "$out" | grep -qi "checksum\|does not match\|sha256" && ok "a release whose .deb does not match SHA256SUMS is refused (exit status 1)" || { bad "tampered release was not refused ($rc)"; echo "$out"; }
    check "... and the installed version is still $v3" fixed "dpkg-query -W -f='\${Version}' channelview" "$v3"
else
    bad "could not build the $v4 test package"
fi
kill "$mock_pid" 2>/dev/null; mock_pid=""

# ============================================================== HTTPS, certificate renewal, custom.css
echo "== https"
TLS=/etc/channelview/tls
cp "$DEFAULTS" "$work/defaults.before-tls"
cert_fp() { openssl s_client -connect "127.0.0.1:$PORT" </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256 2>/dev/null; }
wait_https() { local _; for _ in $(seq 1 60); do curl -fsS -o /dev/null --cacert "$TLS/server.crt" "https://127.0.0.1:$PORT/" 2>/dev/null && return 0; sleep 0.5; done; return 1; }
channelview-cert --name localhost --ip 127.0.0.1 >"$work/cert.out" 2>&1 && ok "channelview-cert makes a certificate" || { bad "channelview-cert failed"; cat "$work/cert.out"; }
check "key root:channelview 640, certificate 644, folder 750" fixed "stat -c '%U:%G %a' $TLS/server.key $TLS/server.crt $TLS | tr '\n' ' '" "root:$SVC_USER 640 root:$SVC_USER 644 root:$SVC_USER 750 "
check "it refuses to overwrite without --force" bash -c "! channelview-cert --name localhost >/dev/null 2>&1"
check "the service user can read the key" runuser -u "$SVC_USER" -- head -c 1 "$TLS/server.key"
printf 'CHANNELVIEW_TLS_CERT=%s\nCHANNELVIEW_TLS_KEY=%s\n' "$TLS/server.crt" "$TLS/server.key" >> "$DEFAULTS"
server_up
wait_https && ok "HTTPS answers, certificate verified against server.crt" || { bad "no HTTPS answer"; server_log | tail -5; }
check "the page comes over HTTPS" bash -c "curl -fsS --cacert $TLS/server.crt https://127.0.0.1:$PORT/ | grep -q '<title>'"
check "signing in works over HTTPS" fixed "curl -s -o /dev/null -w '%{http_code}' --cacert $TLS/server.crt -c $work/jar_tls -X POST https://127.0.0.1:$PORT/api/login -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$new_pw\"}'" "200"
check "... and the sign-in cookie is then marked Secure and HttpOnly" bash -c "grep cv_session $work/jar_tls | grep -q '^#HttpOnly_' && [ \"\$(awk '\$6==\"cv_session\"{print \$4}' $work/jar_tls)\" = TRUE ]"
check "plain http on the HTTPS port is redirected (307)" fixed "curl -s -o /dev/null -w '%{http_code} %{redirect_url}' http://127.0.0.1:$PORT/x" "307 https://127.0.0.1:$PORT/x"
check "TLS 1.2 handshake works" bash -c "openssl s_client -connect 127.0.0.1:$PORT -tls1_2 </dev/null 2>&1 | grep -q 'Protocol *: TLSv1.2'"
server_log | grep -q "running at https://127.0.0.1:$PORT/" && ok "the log shows the https address" || bad "log does not show https"
server_log | grep -qF "$new_pw" && bad "the password is in the log" || ok "the password is still not in the log"
# a renewed certificate is used without a restart
fp_before="$(cert_fp)"; pid_before_renew="$(server_pid)"
channelview-cert --force --name localhost --ip 127.0.0.1 >/dev/null 2>&1 && ok "channelview-cert --force renews it" || bad "renewal failed"
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

# ============================================================== remove, reinstall, purge
echo "== remove / reinstall / purge"
users_before="$(sha256sum < "$DATA/users.json")"
$have_systemd || manual_stop
apt-get remove -y channelview >"$work/apt.log" 2>&1 && ok "apt-get remove succeeds" || { bad "remove failed"; cat "$work/apt.log"; }
check "program files are gone" bash -c "! test -e /usr/lib/channelview && ! test -e /usr/bin/channelview && ! test -e /usr/bin/channelview-update && ! test -e /lib/systemd/system/channelview.service"
check "your settings, data and the user are kept" bash -c "test -f $DEFAULTS && test -f $DATA/marker && getent passwd $SVC_USER"
if $have_systemd; then
    check "the service is stopped" bash -c "! systemctl is-active --quiet channelview"
    check "the service answers no more" bash -c "! curl -fsS -o /dev/null http://127.0.0.1:$PORT/"
fi
apt_install "$deb" && ok "reinstall succeeds" || bad "reinstall failed"
check "reinstall: edited settings kept (port $PORT)" bash -c "grep -qx 'CHANNELVIEW_PORT=$PORT' $DEFAULTS"
check "reinstall: the service is enabled again" test -L /etc/systemd/system/multi-user.target.wants/channelview.service
check "reinstall: data and accounts kept, and no second account was made" bash -c "[ \"\$(sha256sum < $DATA/users.json)\" = '$users_before' ] && test -f $DATA/marker && ! test -e $DATA/initial-admin-password"
if $have_systemd; then wait_http $PORT && ok "reinstall: the service is running" || bad "reinstall: no answer"; else manual_start; wait_http $PORT && ok "reinstall: server answers" || bad "reinstall: no answer"; manual_stop; fi
out="$(apt-get purge -y channelview 2>&1)"; rc=$?
[ $rc = 0 ] && ok "apt-get purge succeeds" || { bad "purge failed"; echo "$out"; }
check "purge removes /etc/default/channelview" bash -c "! test -e $DEFAULTS"
check "purge removes the systemd state (no leftover symlink or mask)" bash -c "! test -e /etc/systemd/system/channelview.service && ! test -e /etc/systemd/system/multi-user.target.wants/channelview.service"
check "purge keeps the data and says how to remove it" bash -c "test -f $DATA/marker && echo '$out' | grep -q 'rm -rf /var/lib/channelview'"
check "purge keeps the HTTPS certificate and says so" bash -c "test -f $TLS/server.key && echo '$out' | grep -q 'rm -rf /etc/channelview'"

# ============================================================== the first account on the other paths
echo "== first account: preseeded answers, and an upgrade from a version that had no accounts"
rm -rf "$DATA"; deluser --system "$SVC_USER" >/dev/null 2>&1; delgroup --system "$SVC_USER" >/dev/null 2>&1
printf '%s\n' "channelview channelview/admin_user string alice" "channelview channelview/admin_password password alice has a long password" \
    "channelview channelview/admin_password_again password alice has a long password" | debconf-set-selections
apt_install "$deb" && ok "install with preseeded answers succeeds" || bad "install failed"
check "the account alice exists and is the only one" bash -c "[ \"\$(channelview-user list | awk '{print \$1}')\" = alice ]"
check "... as an administrator, without a temporary password file" bash -c "channelview-user list | grep -q '^alice  *admin' && ! test -e $DATA/initial-admin-password"
check "the answers' password is not kept in the debconf database" bash -c "! grep -rqs 'alice has a long password' /var/cache/debconf /var/lib/debconf"
if ! $have_systemd; then manual_start; fi
wait_http 8088 && ok "the server answers" || bad "no answer"
check "alice signs in with the preseeded password (200)" fixed "signin alice 'alice has a long password' $work/jar_alice" "200"
check "... and is not asked to change it" bash -c "curl -s -b $work/jar_alice http://127.0.0.1:8088/api/layout | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d[\"can_edit\"] and not d[\"user\"][\"must_change\"]'"
$have_systemd || manual_stop
rm -f "$DATA/users.json" "$DATA/sessions.json"; echo "oldtokenoldtokenoldtoken" > "$DATA/edit_token"; chown "$SVC_USER:$SVC_USER" "$DATA/edit_token"
apt_install --reinstall "$deb" && ok "reinstalling over a data folder without accounts (a version from before sign-in) succeeds" || bad "reinstall failed"
check "an account is made again, with a random password that has to be changed" bash -c "[ \"\$(channelview-user list | awk '{print \$1}')\" = alice ] && channelview-user list | grep -q 'must change password' && test -f $DATA/initial-admin-password"
check "the old edit token file is removed" bash -c "! test -e $DATA/edit_token"
if ! $have_systemd; then manual_start; fi
wait_http 8088 && ok "the server answers" || bad "no answer"
legacy_pw="$(sed -n 's/^password: //p' "$DATA/initial-admin-password")"
check "the random password signs in" fixed "signin alice '$legacy_pw' $work/jar_legacy" "200"
# an install that cannot make an account must not leave an open dashboard: the service starts with --require-login
$have_systemd || manual_stop
rm -f "$DATA/users.json" "$DATA/initial-admin-password"
if ! $have_systemd; then manual_start; else systemctl restart channelview; fi
wait_http 8088 && ok "without any account the server still answers ..." || bad "no answer"
check "... with the sign-in page, never an open dashboard (401)" fixed "code http://127.0.0.1:8088/api/layout" "401"
check "... which says how to make an account" bash -c "curl -fsS http://127.0.0.1:8088/ | grep -q 'channelview-user add'"

echo
echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
