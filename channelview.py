#!/usr/bin/env python3
"""
ChannelView
===========
A weathermap dashboard for Zabbix 7.0, in your browser:
  * Tabs of two kinds: WEATHERMAP (devices joined by interface links) and DEVICES (host cards)
  * Links coloured by status (down / no data) and by utilisation, labelled with % and bps per direction;
    hover a link for a graph of its in / out traffic; the time range (5 min .. 30 days) and the data refresh interval
    (5 s .. 15 min) are picked in the header
  * Playlists that rotate through tabs (NOC wall / kiosk mode)
  * A built-in editor: add devices from Zabbix, drag them around, add links by picking interfaces, bend links around other devices,
    show / hide the interface names on each link and put them above or below the line, switch the problem count (notification)
    on or off for each device, move the usage meter around the map and lay it out horizontally or vertically
  * Demo mode (--demo) with simulated Cisco gear so you can see it working without Zabbix

Run:
    python3 channelview.py --demo                 # try it, no Zabbix needed
    python3 channelview.py                        # then add your Zabbix in the Connections menu (click the status
                                                    # chip at the top, or press C): URL + API token, Test, Save & use
    python3 channelview.py --init                 # optional: write a config.json instead of using the menu

Needs Python 3.9+ and nothing else (standard library only). Works in Chrome and Firefox.

Data flow: ONE background poller talks to the Zabbix API every `poll_seconds` and caches the result;
browsers only ever talk to this program, never to Zabbix, and never see the API token.
Connections saved in the menu live in <data-dir>/connections.json (owner-only); each connection has its own layout file.

HTTPS: --tls-cert FILE --tls-key FILE (or CHANNELVIEW_TLS_CERT / CHANNELVIEW_TLS_KEY) make the server speak HTTPS itself - no Apache or Nginx needed;
a renewed certificate is picked up without a restart. Your own style rules go into <data-dir>/custom.css (kept across updates).

Security: binds to 127.0.0.1 by default. Anything that changes the layout needs the edit token (embedded
for local browsers; for remote ones open http://HOST:PORT/?token=<token printed at start-up>; with --token-file
the token is kept in that file instead - a service uses that - and is not printed).

Install on Debian / Ubuntu as a service, and update it from GitHub releases: see the .deb package and `channelview-update`
(the packaging/ folder of the repository); `channelview.py --version` shows what is installed.
"""
from __future__ import annotations

import argparse
import gzip
import hmac
import json
import math
import os
import re
import secrets
import shutil
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_NAME = "ChannelView"
VERSION = "1.1.1"        # the one place the version lives: packaging/build_deb.sh and the release workflow read it from here
DEFAULT_PORT = 8088
TOKEN_RX = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
MAX_BODY = 2_000_000
ID_RX = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
HOSTID_RX = re.compile(r"^[0-9]{1,12}$")
DOWN_STATES = {2, 6, 7}  # ifOperStatus: down, notPresent, lowerLayerDown
MAX_VIA = 8              # bend points accepted per link (the page keeps one; layouts saved by earlier versions may hold more)
HOST_BATCH = 100         # hosts per host.get / trigger.get request
ITEM_BATCH = 40          # hosts per item.get request (a switch can have hundreds of interface items)
ITEM_LIMIT = 50000       # a reply this big is treated as cut off: the batch is split and asked again
TRIG_LIMIT = 5000
HIST_LIMIT = 50000       # history rows asked for one link graph (two items; the newest rows are kept if there are more)
TREND_LIMIT = 50000      # trend rows (one per item and hour) asked for one link graph
GRAPH_POINTS = 200       # a link graph is thinned to about this many points
GRAPH_STEP = 30          # seconds between the demo's simulated samples
GRAPH_CACHE_S = 15       # one Zabbix query per link and window per this many seconds, however many browsers hover it
GRAPH_RANGES = [5, 10, 30, 60, 180, 360, 720, 1440, 10080, 20160, 43200]      # minutes: the time ranges the header offers (5m .. 30d)
TREND_FROM = 1440        # a range longer than this many minutes is drawn from hourly trends, not from raw history
CUSTOM_CSS_MAX = 256 * 1024                      # bytes of <data-dir>/custom.css that are used
TLS_RELOAD_CHECK_SECONDS = 10                    # a renewed certificate file is noticed at the next connection after this many seconds
REFRESH_CHOICES = [5, 10, 30, 60, 300, 900]      # seconds: the data refresh intervals the header offers (5s .. 15m)

DEFAULT_CONFIG = {
    "zabbix": {
        "url": "",            # e.g. https://zabbix.example.com  (/api_jsonrpc.php is added for you)
        "token": "",          # API token (Users > API tokens). Preferred over username/password.
        "username": "",
        "password": "",
        "verify_tls": True,
        "ca_bundle": "",      # path to a CA file for an internal CA
        "timeout": 20,
    },
    "poll_seconds": 30,
    "stale_seconds": 300,     # interface data older than this is shown as "no data"
    "graph_minutes": 60,      # time range of the in/out graph (shown when you hover a link) until someone picks another one in the header
                              # (the nearest of 5, 10, 30, 60, 180, 360, 720, 1440, 10080, 20160, 43200 minutes)
    "min_severity": 2,        # ignore problems below this (0 n/c, 1 info, 2 warning, 3 average, 4 high, 5 disaster)
    "max_hosts": 5000,        # safety cap for 'Show all monitored hosts' (the page tells you if it is ever reached)
    # How interface items are recognised. Defaults fit the stock "Network Generic Device by SNMP" /
    # Cisco IOS SNMP templates; change them if your custom templates use other keys.
    "match": {
        "in_key": "net.if.in[",
        "out_key": "net.if.out[",
        "status_key": "net.if.status[",
        "speed_key": "net.if.speed[",
        "tag_name": "interface",
        "alias_tag": "description",
        "name_regex": r"^Interface (?P<ifname>[^(:]+?)(?:\((?P<alias>.*?)\))?: ",
    },
}


# --------------------------------------------------------------------------- config

def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config(path: Path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path.exists():
        cfg = deep_merge(cfg, json.loads(path.read_text(encoding="utf-8")))
    z = cfg["zabbix"]
    z["url"] = os.environ.get("ZBX_URL", z["url"])
    z["token"] = os.environ.get("ZBX_TOKEN", z["token"])
    z["username"] = os.environ.get("ZBX_USER", z["username"])
    z["password"] = os.environ.get("ZBX_PASSWORD", z["password"])
    cfg["poll_seconds"] = max(5, int(cfg["poll_seconds"]))
    cfg["max_hosts"] = max(1, min(50000, int(cfg["max_hosts"])))
    cfg["graph_minutes"] = snap_to(int(cfg["graph_minutes"]), GRAPH_RANGES)
    return cfg


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def load_edit_token(path: Path) -> str:
    """The edit token kept in `path`: read it, or - first start, or a file that is empty / damaged - make one and store it owner-only.
    A service needs this: a token made at every start would be gone after a restart or an upgrade."""
    try:
        tok = path.read_text(encoding="utf-8").strip()
        if TOKEN_RX.match(tok):
            return tok
    except FileNotFoundError:
        pass
    except OSError as e:
        sys.exit(f"Cannot read the token file {path}: {e}")
    tok = secrets.token_urlsafe(24)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok + "\n")
        os.replace(tmp, path)
    except OSError as e:
        sys.exit(f"Cannot write the token file {path}: {e}")
    return tok


def snap_to(v, choices):
    """The choice nearest to v (the smaller one on a tie)."""
    return min(choices, key=lambda c: (abs(c - v), c))


def thin(points, t0, t1, n=GRAPH_POINTS):
    """At most about `n` points for a graph: when there are more, each time slice of the window becomes the average of its samples."""
    half_up = lambda x: int(math.floor(x + 0.5))      # (not round(): the standalone page rounds the same way)
    if len(points) <= n:
        return [[int(c), half_up(v)] for c, v in points]
    width, acc = (t1 - t0) / n, {}
    for c, v in points:
        k = min(n - 1, max(0, math.floor((c - t0) / width)))
        a = acc.setdefault(k, [0.0, 0.0, 0])
        a[0] += c
        a[1] += v
        a[2] += 1
    return [[half_up(a[0] / a[2]), half_up(a[1] / a[2])] for _, a in sorted(acc.items())]


def natkey(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


# --------------------------------------------------------------------------- Zabbix API client

class ZabbixError(Exception):
    def __init__(self, msg, auth=False, kind="api"):
        super().__init__(msg)
        self.auth = auth      # session/token problem that a re-login may fix
        self.kind = kind      # url | network | tls | http | auth | api  (drives the hint shown in the Connections menu)


HINTS = {
    "url": "Check the address. Enter the Zabbix web address or the API endpoint, e.g. https://zabbix.example.com/api_jsonrpc.php",
    "network": "The server could not be reached from this computer. Check the host name or IP, the port, your firewall and VPN.",
    "tls": "The HTTPS certificate is not trusted. Use a certificate from a trusted CA, or untick 'Verify TLS certificate' for a self-signed one "
           "(for an internal CA you can also set zabbix.ca_bundle in config.json).",
    "auth": "Zabbix rejected the credentials. For a token: Users > API tokens, and the user's role must allow API access. "
            "For a username/password: check the account is enabled and not temporarily blocked.",
    "http": "The web server answered with an error. Make sure the URL points at your Zabbix frontend (api_jsonrpc.php).",
    "api": "Zabbix returned an API error - see the message above.",
}


class ZabbixClient:
    """Minimal Zabbix 7.0 JSON-RPC client (API token or user/password -> Bearer auth)."""

    def __init__(self, cfg):
        z = cfg["zabbix"]
        url = (z.get("url") or "").strip().rstrip("/")
        if not url:
            raise ZabbixError("No Zabbix connection is configured. Open the Connections menu to add one "
                              "(or set zabbix.url in config.json / ZBX_URL).", kind="url")
        if not re.match(r"^https?://[^\s/]+", url, re.I):
            raise ZabbixError("The URL must start with http:// or https://", kind="url")
        if not url.endswith(".php"):
            url += "/api_jsonrpc.php"
        self.url = url
        self.token = z.get("token") or ""
        self.user, self.password = z.get("username") or "", z.get("password") or ""
        self.timeout = int(z.get("timeout", 20))
        ctx = ssl.create_default_context(cafile=z.get("ca_bundle") or None)
        if not z.get("verify_tls", True):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.ctx = ctx
        self.lock = threading.Lock()
        self._id = 0

    def _post(self, method, params, auth=True):
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": self._id}).encode()
        headers = {"Content-Type": "application/json-rpc"}
        if auth and self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.url, body, headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise ZabbixError(f"HTTP {e.code} from Zabbix ({self.url})", kind="url" if e.code == 404 else "http")
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as e:
            reason = getattr(e, "reason", e)
            tls = isinstance(e, ssl.SSLError) or isinstance(reason, ssl.SSLError) or "CERTIFICATE" in str(reason).upper()
            raise ZabbixError(f"Cannot reach Zabbix: {reason}", kind="tls" if tls else "network")
        except ValueError:
            raise ZabbixError("Zabbix did not return JSON - check the URL", kind="url")
        if "error" in data:
            err = data["error"]
            text = f"{err.get('message', 'error')}: {err.get('data', '')}".strip(": ")
            low = text.lower()
            relogin = "re-login" in low or "not authori" in low or "session" in low
            bad_login = method == "user.login" or "permission" in low or "incorrect user" in low
            raise ZabbixError(f"{method}: {text}", auth=relogin, kind="auth" if (relogin or bad_login) else "api")
        return data.get("result")

    def _login(self):
        self.token = self._post("user.login", {"username": self.user, "password": self.password}, auth=False)

    def call(self, method, params=None):
        with self.lock:
            if not self.token and self.user:
                self._login()
            try:
                return self._post(method, params or {})
            except ZabbixError as e:
                if e.auth and self.user:
                    self._login()
                    return self._post(method, params or {})
                raise

    def version(self):
        with self.lock:
            return self._post("apiinfo.version", {}, auth=False)  # must be called WITHOUT auth in 7.0


class ZabbixSource:
    demo = False

    def __init__(self, cfg):
        self.cfg = cfg
        self.zbx = ZabbixClient(cfg)
        self.m = cfg["match"]
        self.name_rx = re.compile(self.m["name_regex"])

    def version(self):
        return self.zbx.version()

    # ---- editor helpers
    def search_hosts(self, q):
        rows = self.zbx.call("host.get", {"output": ["hostid", "name"], "search": {"name": q, "host": q},
                                          "searchByAny": True, "monitored_hosts": True,
                                          "sortfield": "name", "limit": 50})
        return [{"id": r["hostid"], "name": r["name"]} for r in rows]

    def search_groups(self, q):
        rows = self.zbx.call("hostgroup.get", {"output": ["name"], "search": {"name": q},
                                               "sortfield": "name", "limit": 50})
        return [{"id": r["name"], "name": r["name"]} for r in rows]

    def group_hostids(self, name):
        groups = self.zbx.call("hostgroup.get", {"output": ["groupid"], "filter": {"name": [name]}})
        if not groups:
            return []
        hosts = self.zbx.call("host.get", {"output": ["hostid"], "monitored_hosts": True,
                                           "groupids": [g["groupid"] for g in groups]})
        return [h["hostid"] for h in hosts]

    def all_hostids(self, cap):
        """Every monitored host (by name) - feeds the 'All devices' tab. -> (ids, cut): `cut` is True when there are
        more than `cap` of them, so the page can say so instead of silently hiding hosts."""
        cap = int(cap)
        rows = self.zbx.call("host.get", {"output": ["hostid"], "monitored_hosts": True,
                                          "sortfield": "name", "limit": cap + 1})
        ids = [r["hostid"] for r in rows]
        return ids[:cap], len(ids) > cap

    def probe(self):
        """What the Connections menu's Test button shows: reachability, login, and a first look at the data."""
        z, m = self.zbx, self.m
        version = z.version()                                  # no auth needed
        hosts = int(z.call("host.get", {"countOutput": True, "monitored_hosts": True}))   # needs valid credentials
        try:
            proxies = int(z.call("proxy.get", {"countOutput": True}))
        except ZabbixError:
            proxies = None                                     # e.g. a role that may not read proxies
        items = int(z.call("item.get", {"countOutput": True, "monitored": True,
                                        "search": {"key_": [m["in_key"], m["out_key"]]},
                                        "searchByAny": True, "startSearch": True}))
        rows = z.call("host.get", {"output": ["hostid", "name", "status", "proxyid"], "monitored_hosts": True,
                                   "sortfield": "name", "limit": 8,
                                   "selectInterfaces": ["type", "available", "main"]})
        return {"version": version, "hosts": hosts, "proxies": proxies, "interface_items": items,
                "sample": [{"id": r["hostid"], "name": r["name"], "status": self._host_state(r)} for r in rows]}

    def interfaces_of(self, hostid):
        by = self._interfaces(self._items([hostid]))
        rows = by.get(hostid, {})
        return [{"name": n, "alias": r["alias"], "oper": r["oper"], "speed_bps": r["speed_bps"]}
                for n, r in sorted(rows.items(), key=lambda kv: natkey(kv[0]))]

    # ---- polling
    def _batched(self, method, hostids, size, make, limit=None):
        """Ask Zabbix about `hostids` in batches of `size`, so no single request is huge (a big inventory in one call
        can run into Zabbix's PHP limits). A batch whose reply reaches `limit` may have been cut off, so it is split
        in two and asked again - nothing is silently dropped."""
        out, todo = [], [list(hostids[i:i + size]) for i in range(0, len(hostids), size)]
        while todo:
            chunk = todo.pop()
            rows = self.zbx.call(method, make(chunk))
            if limit and len(rows) >= limit and len(chunk) > 1:
                half = len(chunk) // 2
                todo += [chunk[:half], chunk[half:]]
                continue
            out += rows
        return out

    def _items(self, hostids):
        m = self.m
        return self._batched("item.get", hostids, ITEM_BATCH, lambda ids: {
            "output": ["itemid", "hostid", "name", "key_", "lastvalue", "lastclock", "units", "value_type"],
            "hostids": ids, "monitored": True,
            "search": {"key_": [m["in_key"], m["out_key"], m["status_key"], m["speed_key"]]},
            "searchByAny": True, "startSearch": True,
            "selectTags": ["tag", "value"], "limit": ITEM_LIMIT}, ITEM_LIMIT)

    def _kind(self, key):
        m = self.m
        for kind, pref in (("in", m["in_key"]), ("out", m["out_key"]),
                           ("status", m["status_key"]), ("speed", m["speed_key"])):
            if key.startswith(pref):
                return kind
        return None

    def _ifname(self, it):
        tags = {t.get("tag"): t.get("value") for t in it.get("tags", [])}
        if tags.get(self.m["tag_name"]):
            return tags[self.m["tag_name"]], tags.get(self.m["alias_tag"], "")
        mm = self.name_rx.match(it["name"])
        if mm and mm.groupdict().get("ifname"):
            return mm.group("ifname").strip(), (mm.groupdict().get("alias") or "")
        mi = re.search(r"\.(\d+)\]$", it["key_"])
        return ("idx:" + mi.group(1) if mi else it["key_"]), ""

    @staticmethod
    def _value(it, kind):
        try:
            v = float(it["lastvalue"])
        except (TypeError, ValueError):
            return None
        raw_units = (it.get("units") or "").strip()
        if kind in ("in", "out") and raw_units in ("Bps", "B/s"):  # bytes per second -> bits per second
            v *= 8                                                  # (case matters: "bps" is already bits)
        if kind == "speed":
            v *= {"kbps": 1e3, "mbps": 1e6, "gbps": 1e9}.get(raw_units.lower(), 1)
        return v

    def _interfaces(self, items):
        out = {}
        for it in items:
            kind = self._kind(it["key_"])
            if not kind:
                continue
            name, alias = self._ifname(it)
            rec = out.setdefault(it["hostid"], {}).setdefault(
                name, {"alias": "", "oper": None, "in_bps": None, "out_bps": None, "speed_bps": None, "clock": 0, "ref": {}})
            if alias and not rec["alias"]:
                rec["alias"] = alias
            v = self._value(it, kind)
            if kind == "status":
                rec["oper"] = int(v) if v is not None else None
            else:
                rec[{"in": "in_bps", "out": "out_bps", "speed": "speed_bps"}[kind]] = v
            if kind in ("in", "out"):      # where the in / out history of this interface lives (the link graph reads it with history.get)
                rec["ref"][kind] = {"id": str(it["itemid"]), "vt": _int(it.get("value_type")),
                                    "mul": 8 if (it.get("units") or "").strip() in ("Bps", "B/s") else 1}
            rec["clock"] = max(rec["clock"], int(it.get("lastclock") or 0))
        return out

    def series(self, refs, t0, t1, trends=False):
        """History of the items `refs` point at (see _interfaces) between t0 and t1 -> one [[clock, bits per second], ...] list per ref
        (empty when a ref is None or its item does not hold numbers). Zabbix keeps this for as long as its housekeeping allows.
        trends=True reads the hourly averages (trend.get) instead of every sample - for ranges of days; an item that keeps no trends
        falls back to its history."""
        want = {}
        for r in refs:
            if r and r["vt"] in (0, 3):      # 0 = float, 3 = unsigned integer: the two types a traffic item can have
                want[r["id"]] = r
        got = {i: [] for i in want}
        if trends and want:
            rows = self.zbx.call("trend.get", {"output": ["itemid", "clock", "value_avg"], "itemids": list(want),
                                               "time_from": int(t0), "time_till": int(t1), "limit": TREND_LIMIT})
            for row in rows:
                try:
                    v, c = float(row["value_avg"]) * want[row["itemid"]]["mul"], int(row["clock"]) + 1800      # an hour's average belongs in the middle of it
                except (KeyError, TypeError, ValueError):
                    continue
                if math.isfinite(v):
                    got[row["itemid"]].append([c, v])
            missing = [i for i in want if not got[i]]      # no trends kept for these: read their history instead
        else:
            missing = list(want)
        for vt in (0, 3):
            ids = [i for i in missing if want[i]["vt"] == vt]
            if not ids:
                continue
            rows = self.zbx.call("history.get", {"output": ["itemid", "clock", "value"], "history": vt, "itemids": ids,
                                                 "time_from": int(t0), "time_till": int(t1),
                                                 "sortfield": "clock", "sortorder": "DESC", "limit": HIST_LIMIT})
            for row in rows:
                try:
                    v, c = float(row["value"]) * want[row["itemid"]]["mul"], int(row["clock"])
                except (KeyError, TypeError, ValueError):
                    continue
                if math.isfinite(v):
                    got[row["itemid"]].append([c, v])
        return [sorted(got[r["id"]]) if r and r["id"] in got else [] for r in refs]

    @staticmethod
    def _host_state(h):
        if str(h.get("status")) == "1":
            return "disabled"
        ifs = h.get("interfaces") or []
        snmp = [i for i in ifs if str(i.get("type")) == "2"] or ifs
        av = [str(i.get("available")) for i in snmp]
        if "2" in av:
            return "down"
        if "1" in av:
            return "up"
        return "unknown"

    @staticmethod
    def _host_ip(h):
        """Address Zabbix polls the host on: the main SNMP interface, else the main/first one."""
        ifs = h.get("interfaces") or []
        pick = ([i for i in ifs if str(i.get("type")) == "2" and str(i.get("main")) == "1"]
                or [i for i in ifs if str(i.get("type")) == "2"] or [i for i in ifs if str(i.get("main")) == "1"] or ifs)
        if not pick:
            return ""
        i = pick[0]
        first, second = (i.get("dns"), i.get("ip")) if str(i.get("useip")) == "0" else (i.get("ip"), i.get("dns"))
        return str(first or second or "")

    @staticmethod
    def _inventory(h):
        """(operating system, device model) from the host inventory; blank when inventory is off or the field is empty."""
        inv = h.get("inventory")
        inv = inv if isinstance(inv, dict) else {}      # Zabbix sends [] for a host without inventory

        def pick(*keys):
            for k in keys:
                v = " ".join(str(inv.get(k) or "").split())
                if v:
                    return v[:80]
            return ""
        return pick("os_short", "os", "os_full"), pick("model", "hardware")

    def snapshot(self, hostids, min_sev):
        snap = {"ts": time.time(), "hosts": {}, "interfaces": {}}
        hostids = sorted(set(hostids))
        if not hostids:
            return snap
        z = self.zbx
        hosts = self._batched("host.get", hostids, HOST_BATCH, lambda ids: {
            "output": ["hostid", "name", "status", "proxyid"], "hostids": ids,
            "selectInterfaces": ["type", "available", "main", "ip", "dns", "useip"],
            "selectInventory": ["os", "os_short", "os_full", "model", "hardware"]})
        proxies = {}
        try:
            for p in z.call("proxy.get", {"output": ["proxyid", "name"]}):
                proxies[p["proxyid"]] = p["name"]
        except ZabbixError:
            pass
        trig = self._batched("trigger.get", hostids, HOST_BATCH, lambda ids: {
            "output": ["triggerid", "description", "priority"], "hostids": ids, "monitored": True,
            "only_true": True, "skipDependent": True, "min_severity": int(min_sev),
            "selectHosts": ["hostid"], "expandDescription": True,
            "sortfield": "priority", "sortorder": "DESC", "limit": TRIG_LIMIT}, TRIG_LIMIT)
        per, seen_t = {}, set()
        for t in trig:
            if t["triggerid"] in seen_t:      # a trigger that spans hosts of two batches comes back from both: count it once
                continue
            seen_t.add(t["triggerid"])
            for h in t.get("hosts", []):
                per.setdefault(h["hostid"], []).append({"name": t["description"], "severity": int(t["priority"])})
        for h in hosts:
            probs = sorted(per.get(h["hostid"], []), key=lambda p: -p["severity"])
            snap["hosts"][h["hostid"]] = {
                "id": h["hostid"], "name": h["name"], "status": self._host_state(h), "ip": self._host_ip(h),
                "os": self._inventory(h)[0], "model": self._inventory(h)[1],
                "proxy": proxies.get(h.get("proxyid"), "") if h.get("proxyid") not in (None, "0") else "",
                "severity": probs[0]["severity"] if probs else 0, "problems": len(probs), "top": probs[:5]}
        snap["interfaces"] = self._interfaces(self._items(hostids))
        return snap


# --------------------------------------------------------------------------- demo data source

DEMO_HOSTS = [
    # id, name, proxy, group, role
    ("10001", "core-c9500-1", "Proxy-A", "Cisco core", "core"),
    ("10002", "core-c9500-2", "Proxy-A", "Cisco core", "core"),
    ("10003", "pe-c8300-1", "Proxy-A", "Cisco WAN routers", "router"),
    ("10004", "pe-c8300-2", "Proxy-B", "Cisco WAN routers", "router"),
    ("10005", "ce-c8200l-1", "Proxy-B", "Cisco edge", "edge"),
    ("10006", "ce-c8200l-2", "Proxy-B", "Cisco edge", "edge"),
    ("10007", "ce-c8200l-3", "Proxy-B", "Cisco edge", "edge"),
    ("10008", "dist-c9300-stack", "Proxy-A", "Cisco core", "core"),
]
DEMO_INFO = {      # host inventory the demo "reports": id -> (operating system, device model); ce-c8200l-2 has none, to show the "unknown" group
    "10001": ("Cisco IOS XE 17.9.4", "Catalyst C9500-24Y4C"), "10002": ("Cisco IOS XE 17.9.4", "Catalyst C9500-24Y4C"),
    "10003": ("Cisco IOS XE 17.9.4", "C8300-2N2S-4T2X"), "10004": ("Cisco IOS XE 17.6.5", "C8300-2N2S-4T2X"),
    "10005": ("Cisco IOS XE 17.9.4", "C8200L-1N-4T"), "10006": ("", ""), "10007": ("Cisco IOS XE 17.6.5", "C8200L-1N-4T"),
    "10008": ("Cisco IOS XE 17.6.5", "Catalyst C9300-48P"),
}
DEMO_IFACES = {
    "core": [("Te1/0/%d" % i, 10e9) for i in range(1, 7)],
    "router": [("Gi0/0/0", 1e9), ("Gi0/0/1", 1e9), ("Te0/0/2", 10e9), ("Te0/0/3", 10e9)],
    "edge": [("Gi0/0/0", 1e9), ("Gi0/0/1", 1e9), ("Gi0/1/0", 1e9)],
}
DEMO_LINKS = [  # id, hostA, ifA, hostB, ifB, name
    ("L1", "10001", "Te1/0/1", "10002", "Te1/0/1", "Core interconnect"),
    ("L2", "10001", "Te1/0/2", "10003", "Te0/0/2", "Core1 - PE1"),
    ("L3", "10002", "Te1/0/2", "10004", "Te0/0/2", "Core2 - PE2"),
    ("L4", "10003", "Te0/0/3", "10004", "Te0/0/3", "PE1 - PE2"),
    ("L5", "10003", "Gi0/0/0", "10005", "Gi0/0/0", "Branch 1"),
    ("L6", "10003", "Gi0/0/1", "10006", "Gi0/0/0", "Branch 2 (primary)"),
    ("L7", "10004", "Gi0/0/0", "10007", "Gi0/0/0", "Branch 3"),
    ("L8", "10004", "Gi0/0/1", "10006", "Gi0/0/1", "Branch 2 (backup)"),
    ("L9", "10001", "Te1/0/3", "10008", "Te1/0/1", "Core1 - Dist"),
    ("L10", "10002", "Te1/0/3", "10008", "Te1/0/2", "Core2 - Dist"),
]
DEMO_ACCESS = 120      # extra access switches (acc-sw-001 ..) that only the "All devices" tab lists - so there is something to page through


def access_info(i):
    """Demo inventory of access switch i: (operating system, device model); every 11th one has none (the 'unknown' group)."""
    if i % 11 == 0:
        return "", ""
    return ("Cisco IOS XE 17.6.5" if i % 3 else "Cisco IOS 15.2(7)E4"), ("C9200-48P" if i % 2 else "C2960X-48FPD-L")


DEMO_HOT = {"L6"}      # always busy, shows the red end of the scale
DEMO_FLAP = {"L7"}     # goes down for 45 s every 3 minutes


class DemoSource:
    demo = True

    def __init__(self):
        self.hosts = {}
        self.peer = {}
        for hid, name, proxy, group, role in DEMO_HOSTS:
            self.hosts[hid] = {"name": name, "proxy": proxy, "group": group,
                               "ifs": {n: {"speed": s, "alias": ""} for n, s in DEMO_IFACES[role]}}
        for i in range(1, DEMO_ACCESS + 1):
            self.hosts[str(10100 + i)] = {"name": "acc-sw-%03d" % i, "proxy": "Proxy-A" if i % 2 else "Proxy-B", "group": "Cisco access",
                                          "ifs": {"Gi1/0/%d" % k: {"speed": 1e9, "alias": "user port %d" % k} for k in range(1, 5)}}
        for lid, ha, ia, hb, ib, _ in DEMO_LINKS:
            self.peer[(ha, ia)] = (lid, "a")
            self.peer[(hb, ib)] = (lid, "b")
            self.hosts[ha]["ifs"][ia]["alias"] = "to " + self.hosts[hb]["name"]
            self.hosts[hb]["ifs"][ib]["alias"] = "to " + self.hosts[ha]["name"]

    def version(self):
        return "7.0 (demo)"

    def search_hosts(self, q):
        q = q.lower()
        return [{"id": i, "name": h["name"]} for i, h in sorted(self.hosts.items(), key=lambda kv: kv[1]["name"])
                if q in h["name"].lower()][:50]

    def search_groups(self, q):
        names = sorted({h["group"] for h in self.hosts.values()})
        return [{"id": n, "name": n} for n in names if q.lower() in n.lower()]

    def group_hostids(self, name):
        return [i for i, h in self.hosts.items() if h["group"] == name]

    def all_hostids(self, cap):
        ids = [i for i, _ in sorted(self.hosts.items(), key=lambda kv: kv[1]["name"])]
        return ids[: int(cap)], len(ids) > int(cap)

    def interfaces_of(self, hostid):
        h = self.hosts.get(hostid)
        if not h:
            return []
        return [{"name": n, "alias": m["alias"], "oper": 1, "speed_bps": m["speed"]}
                for n, m in sorted(h["ifs"].items(), key=lambda kv: natkey(kv[0]))]

    @staticmethod
    def _util(key, direction, t):
        h = zlib.crc32(key.encode())
        phase = (h % 628) / 100.0
        base = 0.10 + (h % 45) / 100.0
        wave = 0.5 + 0.5 * math.sin(t / 80.0 + phase + (1.7 if direction == "ba" else 0.0))
        noise = ((zlib.crc32(f"{key}:{direction}:{int(t // 6)}".encode()) % 1000) / 1000.0 - 0.5) * 0.05
        day = 0.85 + 0.15 * math.sin((t - 14400) * (2 * math.pi / 86400.0))      # busier in the afternoon: the daily shape that 7 / 14 / 30 day graphs show
        u = base * (0.45 + 0.9 * wave) * day + noise
        if key in DEMO_HOT:
            u = 0.78 + 0.18 * wave
        return max(0.0, min(0.99, u))

    @staticmethod
    def _flapping(lid, t):
        return lid in DEMO_FLAP and (int(t) // 45) % 4 == 2

    def _if_at(self, hid, name, t):
        """(bits/s in, bits/s out, ifOperStatus) of one simulated interface at time t."""
        meta = self.hosts[hid]["ifs"][name]
        speed, oper = meta["speed"], 1
        acc = int(hid) - 10100 if int(hid) > 10100 else 0      # access switch number (0 = one of the 8 core / WAN devices)
        lk = self.peer.get((hid, name))
        if acc:
            port = int(name[-1])
            if acc % 17 == 0 or (acc + port) % 19 == 0:       # the switch itself is down / this port is down
                return 0.0, 0.0, 2
            return (self._util(f"{hid}/{name}", "ba", t) * speed, self._util(f"{hid}/{name}", "ab", t) * speed, oper)
        if lk:
            lid, side = lk
            if self._flapping(lid, t):
                return 0.0, 0.0, 2
            ab, ba = self._util(lid, "ab", t) * speed, self._util(lid, "ba", t) * speed
            out, inn = (ab, ba) if side == "a" else (ba, ab)
            return inn, out, oper
        return (self._util(f"{hid}/{name}", "ba", t) * speed * 0.06, self._util(f"{hid}/{name}", "ab", t) * speed * 0.06, oper)

    def series(self, refs, t0, t1, trends=False):
        """The simulated history behind the link graph: the same curves the snapshots follow, one sample every GRAPH_STEP seconds;
        trends=True gives one value per hour instead (the average of twelve samples, dated to the middle of the hour), like Zabbix trends."""
        if trends:
            hours = range(int(t0 // 3600 + 1) * 3600, int(t1) - 3599, 3600)
            stamps = [(h, [h + 150 + 300 * k for k in range(12)]) for h in hours]
        else:
            first = int(t0 // GRAPH_STEP + 1) * GRAPH_STEP
            stamps = [(c, [c]) for c in range(first, int(t1) + 1, GRAPH_STEP)]
        out = []
        for r in refs:
            if not r:
                out.append([])
                continue
            k = 0 if r["dir"] == "in" else 1
            pts = []
            for c, xs in stamps:
                acc = 0.0      # (a plain loop, not sum(): newer Pythons add floats more exactly than the standalone page does, and the two must agree)
                for x in xs:
                    acc += self._if_at(r["hid"], r["if"], x)[k]
                pts.append([c + 1800 if trends else c, acc / len(xs)])
            out.append(pts)
        return out

    def snapshot(self, hostids, min_sev):
        t = time.time()
        snap = {"ts": t, "hosts": {}, "interfaces": {}}
        for hid in sorted(set(hostids)):
            h = self.hosts.get(hid)
            if not h:
                continue
            probs = []
            if hid == "10007" and self._flapping("L7", t):
                probs.append({"name": "Interface Gi0/0/0(to pe-c8300-2): Link down", "severity": 4})
            if hid == "10002" and (int(t) // 120) % 3 == 1:
                probs.append({"name": "High CPU utilization (over 90% for 5m)", "severity": 3})
            if hid == "10005" and (int(t) // 200) % 2 == 0:
                probs.append({"name": "High memory utilization (>90% for 5m)", "severity": 2})
            acc = int(hid) - 10100 if int(hid) > 10100 else 0      # access switch number (0 = one of the 8 core / WAN devices)
            status, ip, (os_, model) = "up", "10.0.0.%d" % (int(hid) - 10000), DEMO_INFO.get(hid, ("", ""))
            if acc:
                status, ip, (os_, model) = "down" if acc % 17 == 0 else "up", "10.1.%d.%d" % (acc // 40 + 1, acc % 40 + 1), access_info(acc)
                if status == "down":
                    probs.append({"name": "SNMP agent is not available for 5m", "severity": 4})
                if status == "up" and acc % 9 == 0:
                    probs.append({"name": "High memory utilization (>90% for 5m)", "severity": 2})
                if status == "up" and acc % 23 == 0:
                    probs.append({"name": "High CPU utilization (over 90% for 5m)", "severity": 3})
            probs = [p for p in probs if p["severity"] >= min_sev]
            snap["hosts"][hid] = {"id": hid, "name": h["name"], "status": status, "proxy": h["proxy"], "ip": ip, "os": os_, "model": model,
                                  "severity": max([p["severity"] for p in probs], default=0),
                                  "problems": len(probs), "top": probs}
            ifs = {}
            for name, meta in h["ifs"].items():
                inn, out, oper = self._if_at(hid, name, t)
                ifs[name] = {"alias": meta["alias"], "oper": oper, "in_bps": inn, "out_bps": out,
                             "speed_bps": meta["speed"], "clock": int(t),
                             "ref": {"in": {"hid": hid, "if": name, "dir": "in"}, "out": {"hid": hid, "if": name, "dir": "out"}}}
            snap["interfaces"][hid] = ifs
        return snap


def demo_layout():
    def node(i, host, x, y):
        return {"id": i, "host": host, "hostname": next(h[1] for h in DEMO_HOSTS if h[0] == host),
                "label": "", "x": x, "y": y}

    def link(lid, nid_a, nid_b):
        _, ha, ia, hb, ib, name = next(l for l in DEMO_LINKS if l[0] == lid)
        return {"id": lid, "name": name, "a": {"node": nid_a, "if": ia}, "b": {"node": nid_b, "if": ib},
                "bandwidth_bps": None}

    wan = {"name": "Core & WAN", "width": 1200, "height": 600,
           "nodes": [node("n-c1", "10001", 260, 130), node("n-c2", "10002", 940, 130),
                     node("n-d1", "10008", 600, 290), node("n-p1", "10003", 260, 450),
                     node("n-p2", "10004", 940, 430)],
           "links": [link("L1", "n-c1", "n-c2"), link("L2", "n-c1", "n-p1"), link("L3", "n-c2", "n-p2"),
                     link("L4", "n-p1", "n-p2"), link("L9", "n-c1", "n-d1"), link("L10", "n-c2", "n-d1")]}
    wan["links"][3]["via"] = [{"x": 600, "y": 540}]      # shows a bent link (its bend point moved by hand): L4 is routed underneath the others
    br = {"name": "Branches", "width": 1200, "height": 600,
          "nodes": [node("n-p1", "10003", 260, 130), node("n-p2", "10004", 940, 130),
                    node("n-e1", "10005", 150, 440), node("n-e2", "10006", 600, 440),
                    node("n-e3", "10007", 1050, 440)],
          "links": [link("L5", "n-p1", "n-e1"), link("L6", "n-p1", "n-e2"), link("L7", "n-p2", "n-e3"),
                    link("L8", "n-p2", "n-e2")]}
    return {
        "version": 1,
        "maps": {"m-wan": wan, "m-br": br},
        "tabs": [
            {"id": "t-wan", "name": "Core & WAN", "type": "map", "map": "m-wan"},
            {"id": "t-br", "name": "Branches", "type": "map", "map": "m-br"},
            {"id": "t-rt", "name": "Routers", "type": "devices", "hosts": [], "hostnames": {},
             "groups": ["Cisco WAN routers", "Cisco edge"], "ifaces": 4},
            {"id": "t-core", "name": "Core switches", "type": "devices", "hosts": ["10001", "10002", "10008"],
             "hostnames": {"10001": "core-c9500-1", "10002": "core-c9500-2", "10008": "dist-c9300-stack"},
             "groups": [], "ifaces": 4},
            {"id": "t-all", "name": "All devices", "type": "devices", "hosts": [], "hostnames": {}, "groups": [], "ifaces": 4,
             "all": True, "style": "list"},
        ],
        "playlists": [{"id": "p-noc", "name": "NOC wall", "loop": True,
                       "items": [{"tab": "t-wan", "seconds": 20}, {"tab": "t-br", "seconds": 20},
                                 {"tab": "t-rt", "seconds": 15}, {"tab": "t-core", "seconds": 15}]}],
        "settings": {},
    }


def starter_layout():
    return {"version": 1,
            "maps": {"m1": {"name": "Network", "width": 1600, "height": 900, "nodes": [], "links": []}},
            "tabs": [{"id": "t1", "name": "Network", "type": "map", "map": "m1"}],
            "playlists": [], "settings": {}}


def live_starter_layout():
    """First layout for a real Zabbix connection: an 'All devices' tab so hosts show up straight away,
    plus an empty weathermap to build links on."""
    lay = starter_layout()
    lay["tabs"].insert(0, {"id": "t-all", "name": "All devices", "type": "devices", "hosts": [], "hostnames": {},
                           "groups": [], "ifaces": 4, "all": True, "style": "list"})
    return lay


# --------------------------------------------------------------------------- layout storage / validation

def validate_layout(d):
    """Strict whitelist copy of an incoming layout; raises ValueError with a readable message."""
    if not isinstance(d, dict):
        raise ValueError("layout must be an object")

    def sid(v, what):
        if not isinstance(v, str) or not ID_RX.match(v):
            raise ValueError(f"bad {what} id: {v!r}")
        return v

    def txt(v, n=80):
        return str(v if v is not None else "").strip()[:n]

    def num(v, lo, hi, default):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return default
        return default if math.isnan(f) else max(lo, min(hi, f))

    out = {"version": 1, "maps": {}, "tabs": [], "playlists": [], "settings": {}}
    maps = d.get("maps") or {}
    if not isinstance(maps, dict) or len(maps) > 200:
        raise ValueError("too many maps")
    for mid, m in maps.items():
        sid(mid, "map")
        nodes, ids = [], set()
        for n in (m.get("nodes") or [])[:500]:
            nid = sid(n.get("id"), "node")
            host = txt(n.get("host"), 20)
            if nid in ids or not HOSTID_RX.match(host):
                raise ValueError(f"bad node {nid!r}")
            ids.add(nid)
            node = {"id": nid, "host": host, "hostname": txt(n.get("hostname"), 100),
                    "label": txt(n.get("label"), 60), "x": num(n.get("x"), -5000, 20000, 0),
                    "y": num(n.get("y"), -5000, 20000, 0)}
            if n.get("shape") in ("circle", "triangle"):      # default is square
                node["shape"] = n["shape"]
            if n.get("size") in ("small", "large", "xlarge"):      # default is medium
                node["size"] = n["size"]
            if n.get("notify") is False:      # the problem count on the device is on unless this says false
                node["notify"] = False
            nodes.append(node)
        links, lids = [], set()
        for l in (m.get("links") or [])[:2000]:
            lid = sid(l.get("id"), "link")
            a, b = l.get("a") or {}, l.get("b") or {}
            if lid in lids or a.get("node") not in ids or b.get("node") not in ids:
                raise ValueError(f"bad link {lid!r}")
            lids.add(lid)
            bw = l.get("bandwidth_bps")
            link = {"id": lid, "name": txt(l.get("name")),
                    "a": {"node": a["node"], "if": txt(a.get("if"), 100)},
                    "b": {"node": b["node"], "if": txt(b.get("if"), 100)},
                    "bandwidth_bps": num(bw, 1, 1e13, None) if bw not in (None, "") else None}
            via, raw = [], l.get("via")
            for p in (raw if isinstance(raw, list) else [])[:MAX_VIA]:      # bend points: the line is routed through them, in order
                x, y = (num(p.get(k), -5000, 20000, None) for k in ("x", "y")) if isinstance(p, dict) else (None, None)
                if x is not None and y is not None:
                    via.append({"x": int(math.floor(x + 0.5)), "y": int(math.floor(y + 0.5))})
            if via:
                link["via"] = via
            if l.get("if_show") is False:      # interface tags are shown unless a link hides them; if_pos fixes their side (without it: automatic)
                link["if_show"] = False
            if l.get("if_pos") in ("above", "below"):
                link["if_pos"] = l["if_pos"]
            links.append(link)
        out["maps"][mid] = {"name": txt(m.get("name"), 60), "width": int(num(m.get("width"), 400, 8000, 1600)),
                            "height": int(num(m.get("height"), 300, 8000, 900)), "nodes": nodes, "links": links}
        lg, leg = m.get("legend"), {}      # the usage meter: vertical or horizontal (the default), and where it was dragged to (x, y: 0..100, both or neither)
        if isinstance(lg, dict):
            if lg.get("dir") == "v":
                leg["dir"] = "v"
            lx, ly = lg.get("x"), lg.get("y")
            if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lx, ly)):
                try:
                    px, py = (math.floor(max(0.0, min(100.0, float(v))) * 10 + 0.5) / 10 for v in (lx, ly))
                    if math.isfinite(px) and math.isfinite(py):
                        leg["x"], leg["y"] = px, py
                except (OverflowError, ValueError):
                    pass
        if leg:
            out["maps"][mid]["legend"] = leg
    tab_ids = set()
    for t in (d.get("tabs") or [])[:200]:
        tid = sid(t.get("id"), "tab")
        typ = t.get("type")
        if tid in tab_ids or typ not in ("map", "devices"):
            raise ValueError(f"bad tab {tid!r}")
        tab_ids.add(tid)
        tab = {"id": tid, "name": txt(t.get("name"), 40) or "Tab", "type": typ}
        if typ == "map":
            if t.get("map") not in out["maps"]:
                raise ValueError(f"tab {tid!r} points at a missing map")
            tab["map"] = t["map"]
        else:
            hosts = [txt(h, 20) for h in (t.get("hosts") or [])[:300]]
            if not all(HOSTID_RX.match(h) for h in hosts):
                raise ValueError("bad host id in devices tab")
            names = t.get("hostnames") or {}
            tab.update(hosts=hosts, hostnames={h: txt(names.get(h), 100) for h in hosts},
                       groups=[txt(g, 100) for g in (t.get("groups") or [])[:30]],
                       ifaces=int(num(t.get("ifaces"), 0, 12, 4)),
                       all=bool(t.get("all", False)),      # (older layouts also stored a 'limit' of 40 here - it is ignored now: 'all' means all)
                       style=t.get("style") if t.get("style") in ("list", "heatmap") else "cards")
        out["tabs"].append(tab)
    pl_ids = set()
    for p in (d.get("playlists") or [])[:50]:
        pid = sid(p.get("id"), "playlist")
        if pid in pl_ids:
            raise ValueError("duplicate playlist id")
        pl_ids.add(pid)
        items = [{"tab": i["tab"], "seconds": int(num(i.get("seconds"), 3, 3600, 20))}
                 for i in (p.get("items") or [])[:100] if i.get("tab") in tab_ids]
        out["playlists"].append({"id": pid, "name": txt(p.get("name"), 40) or "Playlist",
                                 "loop": bool(p.get("loop", True)), "items": items})
    scale = (d.get("settings") or {}).get("scale")
    if scale:
        clean = [[num(t, 0, 1000, 0), c] for t, c in scale[:12] if isinstance(c, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", c)]
        if clean:
            out["settings"]["scale"] = sorted(clean)
    return out


class Store:
    """The layout (maps, tabs, playlists) of ONE connection, kept in a JSON file. path=None keeps it in memory only."""

    def __init__(self, path, seed):
        self.path, self.lock = path, threading.Lock()
        self.data = None
        if path is not None and path.exists():
            try:
                self.data = validate_layout(json.loads(path.read_text(encoding="utf-8")))
            except Exception as e:
                bad = path.with_suffix(".corrupt")
                shutil.copy(path, bad)
                print(f"! {path.name} could not be read ({e}); copied to {bad.name} and starting fresh")
        if self.data is None:
            self.data = validate_layout(seed())
            self._write()

    def get(self):
        return self.data

    def put(self, d):
        clean = validate_layout(d)
        with self.lock:
            self.data = clean
            self._write()
        return clean

    def _write(self):
        if self.path is None:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        if self.path.exists():
            shutil.copy(self.path, self.path.with_suffix(".bak"))
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- saved connections

MATCH_KEYS = ("in_key", "out_key", "status_key", "speed_key", "tag_name", "alias_tag", "name_regex")


def profile_cfg(base, prof):
    """The runtime config for one connection profile (base config + the profile's own Zabbix settings)."""
    cfg = json.loads(json.dumps(base))
    z = cfg["zabbix"]
    if not prof.get("readonly"):      # the config.json/env profile already IS cfg["zabbix"]
        z.update(url=prof.get("url", ""), verify_tls=bool(prof.get("verify_tls", True)),
                 token=prof.get("token", "") if prof.get("auth") == "token" else "",
                 username=prof.get("username", "") if prof.get("auth") == "password" else "",
                 password=prof.get("password", "") if prof.get("auth") == "password" else "")
    for k, v in (prof.get("match") or {}).items():
        if k in MATCH_KEYS and isinstance(v, str) and v:
            cfg["match"][k] = v
    return cfg


class Connections:
    """Saved Zabbix connections (<data>/connections.json, owner-only) plus the read-only one that
    config.json / ZBX_* environment variables may define (id 'default'). Secrets never leave this class
    except to build a client; the browser only ever sees `public()` records."""

    def __init__(self, path: Path, cfg):
        self.path, self.cfg, self.lock = path, cfg, threading.Lock()
        self.profiles, self.active = [], ""
        if path.exists():
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
                for raw in (d.get("profiles") or [])[:50]:
                    try:
                        p = self.clean(raw, None)
                        p["id"] = str(raw.get("id") or "")
                        if ID_RX.match(p["id"]) and p["id"] not in ("demo", "default"):
                            self.profiles.append(p)
                    except ValueError:
                        continue
                self.active = str(d.get("active") or "")
            except (ValueError, OSError, AttributeError) as e:
                shutil.copy(path, path.with_suffix(".corrupt"))
                print(f"! {path.name} could not be read ({e}); copied to {path.with_suffix('.corrupt').name}")

    # ---- reading
    def config_profile(self):
        z = self.cfg["zabbix"]
        if not (z.get("url") or "").strip():
            return None
        return {"id": "default", "name": "config.json / environment", "url": z["url"],
                "auth": "token" if z.get("token") else "password", "token": z.get("token", ""),
                "username": z.get("username", ""), "password": z.get("password", ""),
                "verify_tls": bool(z.get("verify_tls", True)), "match": {}, "readonly": True}

    def all(self):
        cp = self.config_profile()
        return ([cp] if cp else []) + self.profiles

    def get(self, cid):
        return next((p for p in self.all() if p["id"] == cid), None)

    @staticmethod
    def public(p):
        secret = p.get("token") if p["auth"] == "token" else p.get("password")
        return {"id": p["id"], "name": p["name"], "url": p["url"], "auth": p["auth"], "username": p.get("username", ""),
                "has_secret": bool(secret), "verify_tls": bool(p.get("verify_tls", True)),
                "match": dict(p.get("match") or {}), "readonly": bool(p.get("readonly"))}

    # ---- validation (a submitted profile -> a clean one; blank secrets keep the stored ones)
    def clean(self, d, existing):
        if not isinstance(d, dict):
            raise ValueError("bad connection")

        def txt(v, n):
            return str(v if v is not None else "").strip()[:n]

        url = txt(d.get("url"), 300)
        if not re.match(r"^https?://[^\s/]+", url, re.I):
            raise ValueError("The URL must start with http:// or https://")
        auth = d.get("auth") if d.get("auth") in ("token", "password") else "token"
        same = bool(existing) and existing.get("auth") == auth
        p = {"id": existing["id"] if existing else "", "url": url, "auth": auth,
             "name": txt(d.get("name"), 60) or (urllib.parse.urlparse(url).hostname or "Zabbix"),
             "verify_tls": bool(d.get("verify_tls", True)), "token": "", "username": "", "password": "", "match": {}}
        if auth == "token":
            p["token"] = txt(d.get("token"), 400) or (existing.get("token", "") if same else "")
        else:
            p["username"] = txt(d.get("username"), 100)
            p["password"] = txt(d.get("password"), 400) or (existing.get("password", "") if same else "")
            if not p["username"]:
                raise ValueError("Enter the Zabbix username")
        for k, v in (d.get("match") or {}).items() if isinstance(d.get("match"), dict) else []:
            if k in MATCH_KEYS and str(v).strip():
                p["match"][k] = txt(v, 300)
        rx = p["match"].get("name_regex")
        if rx:
            try:
                if "ifname" not in re.compile(rx).groupindex:
                    raise ValueError("name_regex needs a named group (?P<ifname>...)")
            except re.error as e:
                raise ValueError(f"name_regex is not a valid regular expression: {e}")
        return p

    def candidate(self, d):
        """Profile to TEST: the submitted fields, falling back to stored secrets for the same id."""
        existing = self.get(d.get("id")) if isinstance(d, dict) and d.get("id") else None
        if existing and existing.get("readonly"):
            return existing
        return self.clean(d, existing)

    # ---- writing
    def upsert(self, d):
        with self.lock:
            cid = d.get("id") if isinstance(d, dict) else None
            existing = next((p for p in self.profiles if p["id"] == cid), None) if cid else None
            if cid and not existing:
                raise ValueError("that connection cannot be changed here (or does not exist)")
            p = self.clean(d, existing)
            p["id"] = existing["id"] if existing else "c-" + secrets.token_hex(4)
            self.profiles = [p if x["id"] == p["id"] else x for x in self.profiles] if existing else self.profiles + [p]
            self._write()
            return p

    def delete(self, cid):
        with self.lock:
            if not any(p["id"] == cid for p in self.profiles):
                raise ValueError("that connection cannot be deleted here (or does not exist)")
            self.profiles = [p for p in self.profiles if p["id"] != cid]
            if self.active == cid:
                self.active = ""
            self._write()

    def set_active(self, cid):
        with self.lock:
            self.active = cid
            self._write()

    def _write(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"active": self.active, "profiles": self.profiles}, indent=1), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)      # holds API tokens / passwords
        except OSError:
            pass
        os.replace(tmp, self.path)


def test_connection(base_cfg, prof):
    """Run the Connections menu's Test button. Always returns a dict (never raises)."""
    try:
        cfg = profile_cfg(base_cfg, prof)
        cfg["zabbix"]["timeout"] = min(int(cfg["zabbix"].get("timeout", 20)), 10)
        return {"ok": True, **ZabbixSource(cfg).probe()}
    except ZabbixError as e:
        return {"ok": False, "error": str(e), "kind": e.kind, "hint": HINTS.get(e.kind, "")}
    except Exception as e:  # never let a test blow up the request
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "kind": "api", "hint": ""}


# --------------------------------------------------------------------------- application core

class App:
    """Holds the ACTIVE connection (a data source + that connection's layout) and the cached poll result.
    activate() swaps the connection while the server keeps running."""

    def __init__(self, cfg, conns, data_dir, start="", persist=True):
        self.cfg, self.conns, self.data = cfg, conns, Path(data_dir)
        self.wake = threading.Event()
        self.swap = threading.Lock()
        self.pollmu = threading.Lock()      # one poll at a time (background loop vs. the Refresh button)
        self.gcache, self.gmu = {}, threading.Lock()      # link graphs asked for lately (see link_graph)
        self.wants, self.wmu = {}, threading.Lock()      # open pages -> (refresh interval they picked, last seen): see note_client
        self.eff_last = cfg["poll_seconds"]
        self.last_refresh = 0.0
        self.active, self.conn_name, self.is_demo = "", "", False
        self.source, self.store = None, Store(None, live_starter_layout)
        self.snap, self.group_ids, self.all_ids, self.all_cut = {"ts": 0, "hosts": {}, "interfaces": {}}, {}, [], False
        self.status = {}
        self.activate(start, persist)

    def custom_css(self):
        """<data-dir>/custom.css: your own style rules, added after the built-in ones. It lives outside the program, so updates keep it.
        Read at every page load (edit, reload the page). Anything unreadable or huge is ignored."""
        try:
            with open(self.data / "custom.css", "rb") as f:
                raw = f.read(CUSTOM_CSS_MAX + 1)
        except OSError:
            return ""
        if len(raw) > CUSTOM_CSS_MAX:
            return ""
        return re.sub(r"(?i)</style", r"<\\/style", raw.decode("utf-8", "replace")).strip()      # cannot end the <style> element early

    def layout_path(self, cid):
        return self.data / ("dashboard-demo.json" if cid == "demo" else "dashboard.json" if cid == "default"
                            else f"dashboard-{cid}.json")

    def activate(self, cid, persist=True):
        """Switch to 'demo', a saved connection id, or '' (nothing configured yet)."""
        with self.swap:
            err, source, name = "", None, ""
            if cid == "demo":
                source, name = DemoSource(), "Demo data"
                store = Store(self.layout_path("demo"), demo_layout)
            elif cid and self.conns.get(cid):
                prof = self.conns.get(cid)
                name = prof["name"]
                store = Store(self.layout_path(cid), live_starter_layout)
                try:
                    source = ZabbixSource(profile_cfg(self.cfg, prof))
                except (ZabbixError, re.error) as e:
                    err = str(e)
            else:
                cid, store = "", Store(None, live_starter_layout)
                err = "No Zabbix connection is configured yet - open the Connections menu to add one."
            self.active, self.conn_name, self.is_demo = cid, name, cid == "demo"
            self.source, self.store = source, store
            with self.gmu:
                self.gcache.clear()
            self.snap, self.group_ids, self.all_ids, self.all_cut = {"ts": 0, "hosts": {}, "interfaces": {}}, {}, [], False
            self.status = {"ok": False, "error": err or "waiting for first poll", "last_ok": 0, "last_try": 0,
                           "seconds": 0, "version": "", "hosts": 0, "demo": self.is_demo, "conn": name}
            if persist:
                self.conns.set_active(cid)
        self.wake.set()

    def connections_info(self):
        return {"mode": "server", "active": self.active, "demo_available": True,
                "profiles": [self.conns.public(p) for p in self.conns.all()],
                "match_defaults": {k: self.cfg["match"][k] for k in MATCH_KEYS}}

    def needed(self, store=None):
        lay = (store or self.store).get()
        ids, groups, want_all = set(), set(), False
        for m in lay["maps"].values():
            ids.update(n["host"] for n in m["nodes"])
        for t in lay["tabs"]:
            if t["type"] == "devices":
                ids.update(t["hosts"])
                groups.update(t["groups"])
                want_all = want_all or bool(t.get("all"))
        return ids, groups, want_all

    def refresh(self):
        """Refresh button: open a fresh Zabbix session (re-login) and poll right now. Throttled to one per 2 s."""
        now = time.time()
        if now - self.last_refresh < 2.0:
            return dict(self.status)
        self.last_refresh = now
        with self.swap:
            if self.active and not self.is_demo:
                prof = self.conns.get(self.active)
                if prof:
                    try:
                        self.source = ZabbixSource(profile_cfg(self.cfg, prof))
                    except (ZabbixError, re.error) as e:
                        self.status.update(ok=False, error=str(e))
                        return dict(self.status)
                    self.status["version"] = ""
        self.poll_once()
        return dict(self.status)

    def poll_once(self):
        with self.pollmu:
            self._poll_once()

    def _poll_once(self):
        source, store = self.source, self.store
        if source is None:
            return
        t0 = time.time()
        self.status["last_try"] = t0
        try:
            ids, groups, want_all = self.needed(store)
            group_ids = {g: source.group_hostids(g) for g in groups}
            for v in group_ids.values():
                ids.update(v)
            all_ids, all_cut = source.all_hostids(self.cfg["max_hosts"]) if want_all else ([], False)
            ids.update(all_ids)
            snap = source.snapshot(sorted(ids), self.cfg["min_severity"])
            version = self.status.get("version") or source.version()
            if source is not self.source:      # the connection was switched while we were polling: drop this result
                return
            self.snap, self.group_ids, self.all_ids, self.all_cut = snap, group_ids, all_ids, all_cut
            self.status.update(ok=True, error="", last_ok=time.time(), seconds=round(time.time() - t0, 2),
                               version=version, hosts=len(snap["hosts"]))
        except Exception as e:  # keep serving the last good data
            if source is self.source:
                self.status.update(ok=False, error=str(e)[:300])

    def loop(self):
        while True:
            self.poll_once()
            self.wake.wait(self.poll_interval())
            self.wake.clear()

    def poll_interval(self):
        """How often Zabbix is polled: the fastest refresh interval any open page asked for (each page picks one in the header and
        announces it with every data request); `poll_seconds` from the config when no page is open."""
        now = time.time()
        with self.wmu:
            for cid in [c for c, (e, t) in self.wants.items() if now - t > max(120, 3 * e)]:      # a page that went quiet no longer counts
                del self.wants[cid]
            return min((e for e, _ in self.wants.values()), default=self.cfg["poll_seconds"])

    def note_client(self, cid, every):
        """A page asks for data every `every` seconds. Unknown values (anything but the header's choices) are ignored."""
        if not isinstance(cid, str) or not ID_RX.match(cid) or every not in REFRESH_CHOICES:
            return
        now = time.time()
        with self.wmu:
            if cid not in self.wants and len(self.wants) >= 64:      # never let ids pile up
                del self.wants[min(self.wants, key=lambda c: self.wants[c][1])]
            self.wants[cid] = (every, now)
        eff = self.poll_interval()
        if eff != self.eff_last:      # the picture changed (a page picked another interval): poll now and then at the new pace
            self.eff_last = eff
            self.wake.set()

    # ---- views
    def resolve_link(self, l, nodes, now):
        na, nb = nodes.get(l["a"]["node"]), nodes.get(l["b"]["node"])
        ifs = self.snap["interfaces"]
        ia = ifs.get(na["host"], {}).get(l["a"]["if"]) if na else None
        ib = ifs.get(nb["host"], {}).get(l["b"]["if"]) if nb else None

        def g(i, k):
            return i.get(k) if i else None

        def first(*vals):
            return next((v for v in vals if v is not None), None)

        ab = first(g(ia, "out_bps"), g(ib, "in_bps"))
        ba = first(g(ia, "in_bps"), g(ib, "out_bps"))
        speeds = [s for s in (g(ia, "speed_bps"), g(ib, "speed_bps")) if s]
        override = l.get("bandwidth_bps")
        speed = override or (min(speeds) if speeds else None)
        opers = [o for o in (g(ia, "oper"), g(ib, "oper")) if o is not None]
        clocks = [c for c in (g(ia, "clock"), g(ib, "clock")) if c]
        newest = max(clocks) if clocks else 0
        if not ia and not ib:
            status = "unknown"
        elif newest and now - newest > self.cfg["stale_seconds"]:
            status = "stale"
        elif any(o in DOWN_STATES for o in opers):
            status = "down"
        elif opers and all(o == 1 for o in opers):
            status = "up"
        elif opers:
            status = "warn"
        elif ab is not None or ba is not None:
            status = "up"
        else:
            status = "unknown"
        pct = lambda v: (v / speed * 100.0) if (v is not None and speed) else None
        return {"id": l["id"], "status": status, "ab_bps": ab, "ba_bps": ba, "ab_pct": pct(ab), "ba_pct": pct(ba),
                "speed": speed, "speed_src": "override" if override else ("zabbix" if speed else ""),
                "clock": newest or None}

    def link_graph(self, tab, lid, minutes=None):
        """In / out traffic of one link over the last `minutes` (one of GRAPH_RANGES; default `graph_minutes`), for the graph in the link's
        hover card: {"from", "to", "minutes", "ab": [[clock, bps], ..], "ba": [..]}. None when the tab or link does not exist.
        The history comes from the data source (Zabbix history.get, or hourly trend.get for ranges over a day; the demo simulates both),
        not from this program."""
        if tab["type"] != "map":
            return None
        lay = self.store.get()
        m = lay["maps"][tab["map"]]
        l = next((x for x in m["links"] if x["id"] == lid), None)
        if l is None:
            return None
        source = self.source
        if source is None:
            raise ZabbixError(self.status.get("error") or "not connected")
        nodes = {n["id"]: n for n in m["nodes"]}
        ifs = self.snap["interfaces"]

        def end(side):
            n = nodes.get(l[side]["node"])
            return ifs.get(n["host"], {}).get(l[side]["if"]) if n else None

        def pick(*cands):      # same preference as resolve_link: the first end that has a current value, else the first that has an item
            cands = [(i, k) for i, k in cands if i and (i.get("ref") or {}).get(k)]
            ok = next((c for c in cands if c[0].get(c[1] + "_bps") is not None), None)
            c = ok or (cands[0] if cands else None)
            return (c[0]["ref"][c[1]], c[0].get(c[1] + "_bps")) if c else (None, None)
        ia, ib = end("a"), end("b")
        (ref_ab, now_ab), (ref_ba, now_ba) = pick((ia, "out"), (ib, "in")), pick((ia, "in"), (ib, "out"))
        refs = [ref_ab, ref_ba]
        if minutes not in GRAPH_RANGES:
            minutes = self.cfg["graph_minutes"]
        key = json.dumps([self.active, refs, minutes], sort_keys=True)
        now = time.time()
        with self.gmu:
            hit = self.gcache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        t1 = int(now)
        t0 = t1 - minutes * 60
        trends = minutes > TREND_FROM
        ab, ba = source.series(refs, t0, t1, trends)
        if trends:      # trends lag by up to an hour: close the gap to the present with the current value
            ab, ba = ab + ([[t1, now_ab]] if ab and now_ab is not None else []), ba + ([[t1, now_ba]] if ba and now_ba is not None else [])
        out = {"from": t0, "to": t1, "minutes": minutes, "ab": thin(ab, t0, t1), "ba": thin(ba, t0, t1)}
        with self.gmu:
            if source is self.source:
                for k in [k for k, (exp, _) in self.gcache.items() if exp <= now]:
                    del self.gcache[k]
                if len(self.gcache) >= 256:      # many links hovered within seconds: drop the oldest entries
                    for k in sorted(self.gcache, key=lambda k: self.gcache[k][0])[:64]:
                        del self.gcache[k]
                self.gcache[key] = (now + GRAPH_CACHE_S, out)
        return out

    def view(self, tab):
        now = time.time()
        lay = self.store.get()
        out = {"ts": now, "status": self.status, "kind": tab["type"]}
        if tab["type"] == "map":
            m = lay["maps"][tab["map"]]
            nodes = {n["id"]: n for n in m["nodes"]}
            out["nodes"] = []
            for n in m["nodes"]:
                h = self.snap["hosts"].get(n["host"]) or {}
                out["nodes"].append({"id": n["id"], "name": h.get("name", ""), "status": h.get("status", "unknown"),
                                     "severity": h.get("severity", 0), "problems": h.get("problems", 0),
                                     "proxy": h.get("proxy", ""), "top": h.get("top", [])})
            out["links"] = [self.resolve_link(l, nodes, now) for l in m["links"]]
        else:
            ids = list(tab["hosts"])
            for g in tab["groups"]:
                ids += self.group_ids.get(g, [])
            if tab.get("all"):
                ids += self.all_ids
                out["truncated"] = self.all_cut      # more monitored hosts exist than max_hosts allows
                out["max_hosts"] = self.cfg["max_hosts"]
            seen, cards = set(), []
            for hid in ids:
                if hid in seen:
                    continue
                seen.add(hid)
                h = self.snap["hosts"].get(hid)
                if not h:
                    cards.append({"id": hid, "name": tab["hostnames"].get(hid, hid), "status": "unknown", "ip": "",
                                  "os": "", "model": "", "severity": 0, "problems": 0, "proxy": "", "top": [], "ifaces": [],
                                  "if_total": 0, "if_down": 0})
                    continue
                ifs = []
                for name, r in (self.snap["interfaces"].get(hid) or {}).items():
                    peak = max([v for v in (r["in_bps"], r["out_bps"]) if v is not None], default=None)
                    pct = (peak / r["speed_bps"] * 100.0) if (peak is not None and r["speed_bps"]) else None
                    ifs.append({"name": name, "alias": r["alias"], "oper": r["oper"], "in_bps": r["in_bps"],
                                "out_bps": r["out_bps"], "speed": r["speed_bps"], "pct": pct})
                up = sorted((i for i in ifs if i["oper"] in (1, None)),
                            key=lambda i: (i["pct"] is None, -(i["pct"] or 0)))
                cards.append({"id": hid, "name": h["name"], "status": h["status"], "ip": h.get("ip", ""),
                              "os": h.get("os", ""), "model": h.get("model", ""), "severity": h["severity"],
                              "problems": h["problems"], "proxy": h["proxy"], "top": h["top"][:3],
                              "ifaces": up[: tab["ifaces"]], "if_total": len(ifs),
                              "if_down": sum(1 for i in ifs if i["oper"] in DOWN_STATES)})
            cards.sort(key=lambda c: c["name"].lower())
            out["cards"] = cards
        return out


# --------------------------------------------------------------------------- web UI

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ChannelView</title>
<link rel="icon" type="image/png" sizes="32x32" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAF60lEQVR4nLRXfWwURRT/zX7dd7mDflxLW4qigdZCEQJI0EKMVlD+QRsIAhqCRI0xURINRg0xBjQaiUqiMUiMhD9ICGBAQZGEinwUSGhCWyqlFCqUftjrQe+ud3u3u87e3e7t3u0VIvgumzczb+a937x5780cBwN5q+q8nCy+RaDMA0gdHSpWFAWEEIBy2oDeB+3D0B9bPkAbLZSfFhlha/BqS1CzSbRGUXn1YoUo2+lAWXKppkNXlcX/o5xSL1HIusHr7Yd0AMUVUxtkBYdV5Ar9EcPOcvh9kkORlgxev3SIjKus9fFyvI3iKE1iTLpT84vF3u6XnKBXVJhajpXEl2inVJ8iy2mk6b4iG5ADJU5gbjmP6mIOleNY+BwE/SEZV4ZlHPgriisBacz1un4FZYIireEYGnCKBkw7s+SAuV/ik+H1yPiivhCzygRY0cYnXGj+W8S20xEc6RZN6630U5rH2j0TPqdCb55QAsPIWLc0hu3vhlDpl7GvicczD9uRj8qpV5bV2DGrlMPPl2JIyNZ609xDJkx8SMljG26HjD0fhzBjiqwjn7++AEdf9MMlMHkg6AGA3lsSVu8dRlu/bKlfdQTr8IzflBnLnJVdkLF3c1g3rpJ6duEYQXe3gLpSAfmJJD+PnUGZh8W+9miOfiWtj1ObSjpx1YBROcfK+OH9iMm4Ro0LRbywIYQzN+K40BdH74iE6SU85lQIePMxF5w8Mc2vrxLgolZCCej6Fb1oyeCSAYJUR+MrnxZRXyflGG86z+Ltr224EZTRNRzRd3SiR8SfPTEcuDiKn1aNR6FqMU1Hu2IIxRVDfdCyIGWPIzBGfSpaVzwZzzF+5ByDNR856ByDG/XoTq3vHEpg4feDeGW2G42POHD8agxfnQrl6E9tOs19/smKFhkqMo9TQufuiMl4JAbUrnIhHGVSygzz77Wf8pUB4fQHc89912EOoVFjcadn5y7EhNoG2pQQaPsdifBQWnlKrp91lv5sOWcsEiqvmZx79idbGVMxcZZNQ83ru8HbPUn5pGc34sK2ZYgOdiFT1Mx6c3lKzgEZZCrrukHw2S7OYJ6guZ2Bni2UCuuW6sZV4pwFKJr9PHp++VTXk+HEpD+bc1pUatCuCw7wHl5Xrs6NMqMmtzG8LdtJYDgbtM2Y3wey9Xsh3dezQOMTp3BY/KrbpLznYhxtTaI+b7ijCWWPrzXNCbT9Zo7u9Baz9WfLuez7uv9qbgpWTuPQeiymz7vVcQyt36xE8ZxGEIbF4Nk9uNV50vr+vwMn44oqFGORLpnM4YP9RSYA4dsSPmwYRCyizTMekNVFc/dyJlkekQnEvisiBq4lTABcBSxe/sRLd6ukymkyILXAMq+H3wb3ezXw7VwAz+aZ4GZ4TfLs9azNWbAJML7Z6IUTlDDzKYcJREkVh6rpPC6fjWE0pCCbktdPmQPeLbPATxkHYmPBFjmoi3nEjvUZ9JuJgY4skxvnDo9iwCIWqufbsWFnIZiJDgj1JWAqXPTaZMDV+mBvnATv1rlgfOZbUqAy4mTNRcnAOatBUC/teCeAN74rhNvLmhQ2H4zA8VwFHEsqYEVK1j7FlgCUcMLSeDKlU8iMZ5rq97TH8eXafxAKZiqjTF83x/dEYFtUivyUznH6JW5GEPqx01K/loqszeF+jXK3VbSODEk4tT8EXiAon8qj9Y8ozg0VwL7AP5Z5+uJWENndjdtbWoBg3CjJ5v3E7fPT/wNouFO++vwM7G4GkfWPQpjmzQtAvDCEkW87IF0L3bEO0MavtBLKLfSOb9Dck69yBW6qRyGB2XIefLUP3AMFYItplBfwkAMxSL1hRJv66J5GDU/yzHor/bS8txC711/FE/k8HfMakKXK6P/bD3JgZrKJaChos7suUTzLM47MrljZdO9y+uBfcTvY35zMMTEa7uAE5xkKbBEF6Bn7Hr9HTl/r9FW3PBwcOKja1pM8HotcdtqFHQphRBoT9BEGeuErLuStYRrdjRwD9DtBiLKLR2L1SDDQqkn/BQAA///MfZM/AAAABklEQVQDAHmd+IaNIOJ7AAAAAElFTkSuQmCC">
<link rel="icon" type="image/svg+xml" sizes="any" href="data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%20512%20512'%3E%3Cdefs%3E%3ClinearGradient%20id='t'%20x1='0'%20y1='0'%20x2='0'%20y2='1'%3E%3Cstop%20offset='0'%20stop-color='%23151e2a'/%3E%3Cstop%20offset='1'%20stop-color='%230b0f14'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect%20width='512'%20height='512'%20rx='112'%20fill='url(%23t)'/%3E%3Cpath%20d='M389.80,385.21A186.0,186.0%200%200%201%20192.39,430.79L220.89,360.25A110.0,110.0%200%200%200%20335.13,332.41Z'%20fill='%2322c55e'/%3E%3Cpath%20d='M180.34,425.92A186.0,186.0%200%200%201%2070.11,262.50L146.19,262.50A110.0,110.0%200%200%200%20208.84,355.38Z'%20fill='%23a3e635'/%3E%3Cpath%20d='M70.11,249.50A186.0,186.0%200%200%201%20180.34,86.08L208.84,156.62A110.0,110.0%200%200%200%20146.19,249.50Z'%20fill='%23facc15'/%3E%3Cpath%20d='M192.39,81.21A186.0,186.0%200%200%201%20389.80,126.79L335.13,179.59A110.0,110.0%200%200%200%20220.89,151.75Z'%20fill='%23fb923c'/%3E%3Ccircle%20cx='362.46'%20cy='358.81'%20r='38.0'%20fill='%2322c55e'/%3E%3Ccircle%20cx='362.46'%20cy='153.19'%20r='38.0'%20fill='%23fb923c'/%3E%3Ccircle%20cx='256'%20cy='256'%20r='42'%20fill='%234aa3ff'/%3E%3C/svg%3E">
<style>
:root{--bg:#0b0f14;--panel:#121923;--panel2:#18212d;--line:#243040;--text:#e6edf3;--muted:#8b98a8;--accent:#4aa3ff;--ok:#22c55e;--bad:#ef4444;--warn:#f59e0b}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;overflow:hidden}
#wrap{display:grid;grid-template-columns:minmax(0,1fr);grid-template-rows:auto minmax(0,1fr);height:100vh}      /* minmax(0,..): a wide map must not stretch the page (and push the header buttons off screen) */
body.editing #wrap{grid-template-columns:minmax(0,1fr) 390px}
header{grid-column:1/-1;display:flex;align-items:center;gap:14px;padding:8px 14px;background:var(--panel);border-bottom:1px solid var(--line)}
.logo{display:inline-flex;align-items:center;gap:9px;font-weight:700;letter-spacing:.02em;white-space:nowrap}
.logo svg{width:26px;height:26px;flex:none;display:block}
.logo .node{fill:var(--accent)}
#tabs{display:flex;gap:4px;overflow-x:auto;flex:1;min-width:0}
.tab{background:transparent;border:1px solid transparent;color:var(--muted);padding:6px 12px;border-radius:6px;cursor:pointer;white-space:nowrap}
.tab:hover{color:var(--text)}
.tab.on{color:var(--text);background:var(--panel2);border-color:var(--line);box-shadow:inset 0 -2px 0 var(--accent)}
button,select,input{font:inherit;color:var(--text)}
button{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:5px 10px;cursor:pointer}
button:hover{border-color:var(--accent)}
button.on{background:var(--accent);color:#04121f;border-color:var(--accent)}
.hsel{display:flex;align-items:center;gap:5px;color:var(--muted);font-size:12px;white-space:nowrap}
.hsel select{padding:3px 6px;font-size:12px}
button.x{padding:1px 7px;background:transparent;border-color:transparent;color:var(--muted)}
button.x:hover{color:var(--bad)}
button.primary{background:var(--accent);color:#04121f;border-color:var(--accent)}
select,input{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:5px 8px;min-width:0}
input:focus,select:focus{outline:1px solid var(--accent)}
#pl{display:flex;align-items:center;gap:6px}
#pl-bar{width:90px;height:6px;background:var(--line);border-radius:3px;overflow:hidden}
#pl-fill{height:100%;width:0;background:var(--accent)}
.chip{font-size:12px;padding:3px 10px;border-radius:999px;border:1px solid var(--line);color:var(--muted);white-space:nowrap;max-width:420px;overflow:hidden;text-overflow:ellipsis}
.chip.ok{color:#86efac;border-color:#14532d}
.chip.bad{color:#fca5a5;border-color:#7f1d1d}
.badge{font-size:11px;padding:2px 8px;border-radius:4px;background:#3b2f0b;color:#fcd34d;border:1px solid #6b5416}
#main{position:relative;min-width:0;min-height:0;overflow:auto}
svg.map{width:100%;height:100%;display:block;background:radial-gradient(circle at 50% 40%,#0f1722,#0b0f14)}
svg text{font-family:inherit}
body.editing svg.map .node{cursor:move}
.legend{position:absolute;left:12px;bottom:12px;background:#0b0f14d9;border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:11px;max-width:80%}
.legend .bars{display:flex;margin:4px 0}
.legend .lg{padding:2px 7px;font-weight:600;white-space:nowrap}
.legend .lg:first-child{border-radius:4px 0 0 4px}.legend .lg:last-child{border-radius:0 4px 4px 0}
.legend label{color:var(--muted);cursor:pointer}
.legend.vert{max-width:190px}
.legend.vert .bars{flex-direction:column}
.legend.vert .lg{text-align:center}
.legend.vert .lg:first-child{border-radius:4px 4px 0 0}.legend.vert .lg:last-child{border-radius:0 0 4px 4px}
.legend.vert .muted{white-space:normal}
.legend.editable{border-color:var(--accent);cursor:move;touch-action:none;user-select:none}
.legend .lg-bar{display:flex;align-items:center;gap:6px;margin:-2px 0 6px;padding-bottom:5px;border-bottom:1px dashed var(--line);color:var(--muted)}
.legend .lg-bar .grip{flex:1;white-space:nowrap}
.legend .lg-bar button{padding:1px 7px;font-size:11px;line-height:1.5}
.stamp{position:absolute;right:12px;bottom:10px;color:var(--muted);font-size:11px}
#tip{position:fixed;z-index:50;pointer-events:none;background:#05080cf2;border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:12px;max-width:380px}
#tip .tip-h{font-weight:700;margin-bottom:3px}
#tip .tgraph{margin-top:7px;width:340px}
#tip .tgraph.loading{min-height:150px;display:flex;align-items:center;justify-content:center}
#tip .tg-h{display:flex;justify-content:space-between;color:var(--muted);font-size:11px;margin-bottom:2px}
#tip .tg-row{font-size:11px;margin-top:5px}
#tip .tg-st{padding-left:18px;white-space:nowrap}
#tip .tg-sw{display:inline-block;width:12px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}
#tip svg{display:block}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;padding:16px}
.card{background:var(--panel);border:1px solid var(--line);border-left:5px solid var(--ok);border-radius:10px;padding:12px 14px}
.card h3{margin:0 0 4px;font-size:15px;display:flex;align-items:center;gap:8px}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block;flex:none}
.meta{color:var(--muted);font-size:12px;display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.prob{font-size:12px;padding:3px 8px;border-radius:4px;margin:3px 0;background:#1d2633;border-left:3px solid var(--warn)}
.if{display:grid;grid-template-columns:92px 1fr auto;gap:8px;align-items:center;font-size:12px;margin:4px 0}
.if .bar{height:8px;border-radius:4px;background:var(--line);overflow:hidden}
.if .bar i{display:block;height:100%}
.empty{padding:40px;color:var(--muted);text-align:center}
.muted{color:var(--muted)}.err{color:#fca5a5}.pad{padding:4px 8px}
#drawer{background:var(--panel);border-left:1px solid var(--line);overflow-y:auto;padding:0 0 40px}
.drawer-h{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;position:sticky;top:0;background:var(--panel);border-bottom:1px solid var(--line);z-index:2}
details.sec{border-bottom:1px solid var(--line)}
details.sec>summary{cursor:pointer;padding:9px 14px;font-weight:600;color:var(--muted);text-transform:uppercase;font-size:11px;letter-spacing:.07em}
details.sec>div{padding:2px 14px 12px}
.row{display:flex;align-items:center;gap:6px;margin:4px 0}
.row.wrap{flex-wrap:wrap}
.row.cur{background:#16263a;border-radius:6px}
.row input,.row select{flex:1}
.grow{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.link{cursor:pointer}.link:hover{color:var(--accent)}
.fld{display:block;margin:6px 0}.fld>span{display:block;font-size:11px;color:var(--muted);margin-bottom:2px}
.fld input,.fld select{width:100%}
.ifc{display:block}.ifc select,.ifc input{width:100%}
.search .results{max-height:160px;overflow:auto;border:1px solid var(--line);border-radius:6px;margin-top:3px}
.search .results:empty{display:none}
.search input{width:100%}
.hit{padding:5px 8px;cursor:pointer}.hit:hover{background:var(--panel2)}
.hint{font-size:12px;color:var(--muted);margin:6px 0}
body.kiosk header,body.kiosk #drawer{display:none}
body.kiosk #wrap{grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(0,1fr)}
body.kiosk.idle{cursor:none}
#hud{position:fixed;left:12px;top:10px;z-index:20;background:#0b0f14b0;border:1px solid var(--line);border-radius:8px;padding:4px 10px;font-size:12px;color:var(--muted);display:none}
body.kiosk #hud{display:flex;align-items:center;gap:10px}
#hud{padding:4px 12px 4px 4px}#hud button{padding:3px 11px;font-size:12px}
@media(max-width:900px){body.editing #wrap{grid-template-columns:minmax(0,1fr);grid-template-rows:auto minmax(0,1fr) 50vh}}
@media(max-width:1600px){header{flex-wrap:wrap;gap:8px 14px}#tabs{order:9;flex:1 1 100%}}      /* below this the tab strip would get under ~250px next to the playlist, the two display selects and the status chip: it takes a row of its own */
@media(max-width:900px){.chip{max-width:240px}}
.devbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:12px 16px 0}
.devbar input[type=search]{width:300px;max-width:100%}
.devbar .inl2{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:var(--text);cursor:pointer;white-space:nowrap}
.devbar .grow{font-size:12px;min-width:260px}      /* the summary wraps to its own line before it would be cut off */
.seg{display:inline-flex}.seg button{border-radius:0;margin-left:-1px}.seg button:first-child{border-radius:6px 0 0 6px;margin-left:0}.seg button:last-child{border-radius:0 6px 6px 0}
body.kiosk .devbar{visibility:hidden;height:46px;padding:0;overflow:hidden;flex-wrap:nowrap}      /* keeps room for the Back button, so it never covers the first row */
.dwrap{padding:12px 16px 16px;overflow-x:auto}
table.dt{width:100%;min-width:1120px;border-collapse:separate;border-spacing:0;font-size:13px;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.dt th{text-align:left;font-weight:600;font-size:12px;color:var(--muted);padding:9px 10px;background:var(--panel2);border-bottom:1px solid var(--line);cursor:pointer;user-select:none;white-space:nowrap}
.dt th:hover{color:var(--text)}.dt th .ar{margin-left:5px;color:var(--accent);font-size:10px}
.dt td{padding:7px 10px;border-bottom:1px solid #1a2330;vertical-align:middle}
.dt tr:last-child td{border-bottom:0}
.bend{fill:var(--accent);stroke:#fff;stroke-width:2;cursor:move}.bend:hover{fill:#7cc0ff}
.bend.idle{fill:rgba(11,17,24,.6);stroke:#d6ecff;stroke-width:1.6}.bend.idle:hover{opacity:1;fill:var(--accent)}
.dt tr.drow{cursor:pointer}.dt tr.drow:hover td{background:#16202c}
.dt td.nm{font-weight:600;white-space:nowrap}
.dt td.cl{max-width:210px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}
.prob .sevn{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.04em;margin-right:6px}
.dt .twisty{display:inline-block;width:14px;color:var(--muted);font-size:11px}
.dt .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px;vertical-align:baseline}
.dt .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;white-space:nowrap}
.dt .st{display:inline-block;padding:0 9px;border-radius:999px;font-size:11px;border:1px solid var(--line);line-height:18px}
.dt .pcount{display:inline-block;min-width:20px;text-align:center;padding:0 6px;border-radius:999px;font-size:11px;font-weight:700;color:#08121c;line-height:18px;margin-right:8px}
.dt .ptxt{display:inline-block;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom;color:var(--muted);font-size:12px}
.dt .ifn{display:inline-block;min-width:76px}
.dt .pbar{display:inline-block;width:64px;height:8px;border-radius:4px;background:var(--line);overflow:hidden;vertical-align:middle;margin:0 8px}
.dt .pbar i{display:block;height:100%}
.dt .pv{font-size:12px;color:var(--muted);white-space:nowrap}
.dt tr.det td{background:#0d141d;padding:8px 14px 12px 32px}
.dt .dif{max-width:520px;margin-top:6px}
.ctx{position:fixed;z-index:80;min-width:200px;max-width:300px;background:var(--panel);border:1px solid var(--line);border-radius:10px;box-shadow:0 12px 32px #000b;padding:5px}
.ctx-h{padding:6px 10px 7px;font-size:11px;color:var(--muted);border-bottom:1px solid var(--line);margin-bottom:4px;text-transform:uppercase;letter-spacing:.04em}
.ctx-h b{display:block;color:var(--text);font-size:13px;text-transform:none;letter-spacing:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ctx button{display:flex;gap:10px;align-items:center;width:100%;text-align:left;background:transparent;border:0;border-radius:6px;padding:8px 10px;font-size:13px}
.ctx button:hover,.ctx button:focus{background:#16263a;outline:none}
.ctx button.danger{color:#fca5a5}.ctx button.danger:hover,.ctx button.danger:focus{background:#2a1214}
.ctx .ci{width:16px;text-align:center}
.ctx-sep{height:1px;background:var(--line);margin:5px 2px}
.ctx-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:4px 10px}
.ctx-l{font-size:12px;color:var(--muted)}
.ctx .opts{display:flex;gap:4px}
.ctx .opts button{width:auto;min-width:32px;justify-content:center;padding:5px 8px;border:1px solid var(--line);border-radius:6px;font-size:12px;background:#0f1722}
.ctx .opts button:hover,.ctx .opts button:focus{border-color:var(--accent);background:#16263a}
.ctx .opts button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#04121f;font-weight:700}
#mdl{position:fixed;inset:0;z-index:90;background:#05080ccc;display:flex;align-items:center;justify-content:center;padding:16px}
.mbox{background:var(--panel);border:1px solid var(--line);border-radius:12px;width:min(480px,100%);max-height:calc(100vh - 32px);display:flex;flex-direction:column;box-shadow:0 20px 60px #000a}
.mbox .mh{display:flex;justify-content:space-between;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line)}
.mbox .mb{padding:14px 18px 6px;overflow-y:auto}
.mbox .mb p{margin:0 0 10px}
.mbox .mb .row{align-items:flex-start}.mbox .mb .row .fld{flex:1}
.mbox .mb ul{margin:6px 0 0;padding-left:20px}.mbox .mb li{margin:3px 0;font-size:12px}
.mbox .mf{display:flex;justify-content:flex-end;gap:8px;padding:10px 18px 16px}
button.dangerbtn{background:#b91c1c;border-color:#ef4444;color:#fff}button.dangerbtn:hover{background:#dc2626}
.devbar select{padding:4px 8px}
.devbar .seg button{padding:4px 10px}
.pager{display:flex;gap:5px;align-items:center;justify-content:center;flex-wrap:wrap;padding:2px 16px 24px}
.pager button{min-width:34px;padding:4px 9px}
.pager button:disabled{opacity:.4;cursor:default;border-color:var(--line)}
.pager .gap{color:var(--muted);padding:0 3px}
.pager .pinfo{margin-left:10px;font-size:12px;color:var(--muted)}
.dnote{margin:12px 16px 0}
.ghead{cursor:pointer;user-select:none}
tr.ghead td{background:#101a26;font-size:13px;padding:7px 12px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
tr.ghead:hover td{background:#16202c}
div.ghead{display:flex;align-items:center;gap:8px;padding:14px 16px 0;font-size:14px}
div.ghead + .cards,div.ghead + .heat{padding-top:10px}
.heat{display:grid;grid-template-columns:repeat(auto-fill,minmax(132px,1fr));gap:6px;padding:12px 16px}
.hm{border-radius:6px;padding:7px 9px;min-height:56px;display:flex;flex-direction:column;justify-content:space-between;gap:3px;overflow:hidden;cursor:pointer;box-shadow:inset 0 0 0 1px #00000040}
.hm:hover{box-shadow:inset 0 0 0 2px #ffffffb0}
.hm:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.hm b{font-size:12.5px;line-height:1.2;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.hm span{font-size:12px;opacity:.85;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hmlegend{display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px;padding:12px 16px 0;font-size:12px}
.hmlegend .lg{display:inline-flex;align-items:center;gap:6px;color:var(--text)}
.hmlegend .lg i{width:12px;height:12px;border-radius:3px;display:inline-block;box-shadow:inset 0 0 0 1px #00000040}
.ghead .twisty{display:inline-block;width:14px;color:var(--muted);font-size:11px}
.ghead .dot{display:inline-block;width:9px;height:9px;border-radius:50%}
#btn-refresh{font-size:16px;line-height:1;padding:4px 10px}
#btn-refresh span{display:inline-block}
#btn-refresh.spin span{animation:zspin .8s linear infinite}
#btn-refresh.alert{border-color:var(--bad);color:#fca5a5}
@keyframes zspin{to{transform:rotate(360deg)}}
body.kiosk #btn-refresh{display:none}
#chip{cursor:pointer}#chip:hover{border-color:var(--accent)}
.cta{position:fixed;top:64px;left:50%;transform:translateX(-50%);z-index:15;display:flex;align-items:center;gap:10px;background:#0b1626f2;border:1px solid #1e4a7a;border-radius:999px;padding:5px 6px 5px 16px;font-size:13px;box-shadow:0 4px 18px #0009;white-space:nowrap}
body.kiosk .cta{display:none}
#conn{position:fixed;inset:0;z-index:60;background:#05080ccc;display:flex;align-items:center;justify-content:center;padding:16px}
#conn .dlg{background:var(--panel);border:1px solid var(--line);border-radius:12px;width:min(980px,100%);max-height:calc(100vh - 32px);display:flex;flex-direction:column;box-shadow:0 20px 60px #000a}
#conn .dlg-h{display:flex;justify-content:space-between;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line)}
#conn .dlg-b{display:grid;grid-template-columns:250px 1fr;min-height:0;overflow:hidden;flex:1}
#conn .plist{border-right:1px solid var(--line);overflow-y:auto;padding:10px}
.pitem{display:block;width:100%;text-align:left;border:1px solid transparent;background:transparent;padding:8px 10px;border-radius:8px;margin-bottom:3px}
.pitem:hover{background:var(--panel2);border-color:transparent}
.pitem.on{background:#16263a;border-color:#244a73}
.pitem small{display:block;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}
.pitem .use{float:right;font-size:11px;color:#86efac}
#conn .pform{overflow-y:auto;padding:14px 20px 20px}
#conn .acts{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 4px}
.box{border-radius:8px;padding:10px 12px;margin:10px 0;font-size:13px;line-height:1.5}
.box.okb{background:#0c2417;border:1px solid #14532d;color:#bbf7d0}
.box.badb{background:#2a1214;border:1px solid #7f1d1d;color:#fecaca}
.box.warnb{background:#2b2108;border:1px solid #6b5416;color:#fde68a}
.box.infob{background:#0e1b2b;border:1px solid #1e3a5f;color:#bcd6f5}
.box pre{margin:6px 0 10px;padding:8px 10px;background:#05080c;border:1px solid var(--line);border-radius:6px;overflow:auto;font-size:12px;color:#d1d9e2;white-space:pre;user-select:all}
.box ol{margin:6px 0 0;padding-left:20px}.box li{margin:6px 0}
.samples{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.samples span{background:#0b0f14;border:1px solid var(--line);border-radius:999px;padding:2px 10px;font-size:12px;color:var(--text);display:inline-flex;align-items:center}
.radios{display:flex;gap:18px;margin:4px 0 2px;flex-wrap:wrap}.radios label{cursor:pointer}
.radios input{width:auto;vertical-align:middle}
.inl{display:flex;align-items:flex-start;gap:8px;margin:8px 0;cursor:pointer}.inl input{flex:none;width:auto;margin-top:3px}
.pform h3{margin:0 0 8px}
@media(max-width:760px){#conn .dlg-b{grid-template-columns:1fr;overflow-y:auto}#conn .plist{border-right:0;border-bottom:1px solid var(--line);max-height:34vh}#conn .pform{overflow:visible}}
</style>
</head>
<body>
<div id="wrap">
  <header>
    <span class="logo"><svg viewBox="61 61 390 390" aria-hidden="true" focusable="false"><path d="M404.05,363.56A183.0,183.0 0 0 1 204.71,431.66L225.12,368.85A117.0,117.0 0 0 0 350.65,324.77Z" fill="#22c55e"/><path d="M194.24,428.27A183.0,183.0 0 0 1 73.08,261.50L139.13,261.50A117.0,117.0 0 0 0 214.65,365.45Z" fill="#a3e635"/><path d="M73.08,250.50A183.0,183.0 0 0 1 194.24,83.73L214.65,146.55A117.0,117.0 0 0 0 139.13,250.50Z" fill="#facc15"/><path d="M204.71,80.34A183.0,183.0 0 0 1 404.05,148.44L350.65,187.23A117.0,117.0 0 0 0 225.12,143.15Z" fill="#fb923c"/><circle cx="377.35" cy="344.17" r="33.0" fill="#22c55e"/><circle cx="377.35" cy="167.83" r="33.0" fill="#fb923c"/><circle cx="256" cy="256" r="40" class="node"/></svg>ChannelView</span>
    <span class="badge" id="demo" hidden>DEMO DATA</span>
    <nav id="tabs"></nav>
    <div id="pl" hidden>
      <select id="pl-select"></select>
      <button id="pl-prev" title="Previous (&larr;)">&#9198;</button>
      <button id="pl-play" title="Play / pause (space)">&#9654;</button>
      <button id="pl-next" title="Next (&rarr;)">&#9197;</button>
      <div id="pl-bar"><div id="pl-fill"></div></div>
    </div>
    <label class="hsel" id="range-wrap" title="Time range of the in / out traffic graph that shows when you hover a link" hidden><span>Graph</span><select id="range-select"></select></label>
    <label class="hsel" title="How often the data is refreshed, and how often Zabbix is polled for it"><span>Refresh</span><select id="refresh-select"></select></label>
    <span class="chip" id="chip" role="button" tabindex="0" title="Connections to Zabbix (C)">connecting&hellip;</span>
    <button id="btn-refresh" title="Refresh now: reconnect to Zabbix and reload the data (R)" aria-label="Refresh"><span>&#x21BB;</span></button>
    <span class="chip" id="viewonly" hidden>view only</span>
    <button id="btn-edit" hidden>Edit</button>
    <button id="btn-kiosk" title="Kiosk mode (K)">Kiosk</button>
    <button id="btn-full" title="Fullscreen (F)">&#x26F6;</button>
  </header>
  <div id="main"></div>
  <aside id="drawer" hidden></aside>
</div>
<div id="hud"><button id="hud-back" type="button" title="Leave kiosk mode (Esc)">← Back</button><span id="hud-text"></span></div>
<div id="tip" hidden></div>
<div id="conn" hidden></div>
<div id="mdl" hidden></div>
<div id="cta" class="cta" hidden></div>
<script>
"use strict";
const EDIT_TOKEN = "__EDIT_TOKEN__";
let DEMO = false;   // set from /api/layout at boot
const $ = (s, r = document) => r.querySelector(s);
const SVGNS = "http://www.w3.org/2000/svg";

function el(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false) continue;
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}
function svg(tag, attrs = {}, ...kids) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "text") e.textContent = v;
    else e.setAttribute(k, v);
  }
  for (const c of kids.flat(Infinity)) if (c) e.append(c);
  return e;
}
async function api(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.body ? "POST" : "GET",
    headers: { "Content-Type": "application/json", "X-Edit-Token": EDIT_TOKEN },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let j = null;
  try { j = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error((j && j.error) || "HTTP " + r.status);
  return j;
}

// ---------- formatting & colours
const fmtBps = v => v == null ? "n/a" : v >= 1e9 ? (v / 1e9).toFixed(2) + " Gbps" : v >= 1e6 ? (v / 1e6).toFixed(v >= 1e8 ? 0 : 1) + " Mbps" : v >= 1e3 ? (v / 1e3).toFixed(0) + " kbps" : Math.round(v) + " bps";
const fmtPct = p => p == null ? "n/a" : p < 10 ? p.toFixed(1) + "%" : Math.round(p) + "%";
const ago = ts => { if (!ts) return "never"; const s = Math.max(0, Math.round(Date.now() / 1000 - ts)); return s < 90 ? s + "s ago" : s < 5400 ? Math.round(s / 60) + "m ago" : Math.round(s / 3600) + "h ago"; };
// Link usage colours, from the usage % each step starts at. The first step also covers anything below it (an idle link is light green); the last one has no upper end.
const SCALE_DEFAULT = [[1, "#86efac"], [10, "#22c55e"], [25, "#a3e635"], [40, "#facc15"], [55, "#fb923c"], [70, "#f97316"]];
const COL = { down: "#dc2626", stale: "#475569", unknown: "#475569", warn: "#f59e0b" };
const SEV = ["#64748b", "#3b82f6", "#eab308", "#f97316", "#ef4444", "#b91c1c"];
const SEVN = ["Not classified", "Information", "Warning", "Average", "High", "Disaster"];
const scale = () => (L && L.settings && L.settings.scale && L.settings.scale.length) ? L.settings.scale : SCALE_DEFAULT;
function colorFor(p) { if (p == null) return "#64748b"; let c = scale()[0][1]; for (const [t, col] of scale()) if (p >= t) c = col; return c; }
function textOn(hex) { const n = parseInt(hex.slice(1), 16); return (0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) > 150 ? "#0b0f14" : "#f1f5f9"; }
const trunc = (s, n) => s.length > n ? s.slice(0, n - 1) + "…" : s;
const newId = p => p + Math.random().toString(36).slice(2, 8);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

// ---------- state
let L = null, canEdit = false, editing = false, tabId = null, view = null, status = null;
let sel = null, dragging = false, suppressClick = false, showIf = true;
try { showIf = localStorage.getItem("showIf") !== "0"; } catch (e) {}
const play = { id: null, idx: 0, running: false, remaining: 0, total: 0 };
const curTab = () => L && L.tabs.find(t => t.id === tabId);
const curPlaylist = () => L && L.playlists.find(p => p.id === play.id);

// ---------- display settings of this browser: the time range of the traffic graph and the data refresh interval (header). Remembered here; the URL can carry them
// (#range=1440&refresh=10) for a wall display. Until somebody picks, the server's defaults (config.json: graph_minutes, poll_seconds) apply.
const RANGES = [[5, "5m"], [10, "10m"], [30, "30m"], [60, "1h"], [180, "3h"], [360, "6h"], [720, "12h"], [1440, "24h"], [10080, "7d"], [20160, "14d"], [43200, "30d"]];      // minutes
const REFRESHES = [[5, "5s"], [10, "10s"], [30, "30s"], [60, "1m"], [300, "5m"], [900, "15m"]];      // seconds
const DISP = { range: 60, refresh: 30, defRange: 60, defRefresh: 30, saved: {} };
const CID = newId("c");      // this page, as far as the server is concerned: it polls Zabbix as often as the fastest open page wants data
const inList = (list, v) => list.some(x => x[0] === v);
try { const s = JSON.parse(localStorage.getItem("channelview-disp") || "{}"); if (inList(RANGES, s.range)) DISP.saved.range = s.range; if (inList(REFRESHES, s.refresh)) DISP.saved.refresh = s.refresh; } catch (e) {}
const saveDisp = () => { try { localStorage.setItem("channelview-disp", JSON.stringify(DISP.saved)); } catch (e) {} };
function initDisp(defaults, hash) {      // after the layout is loaded: server defaults < what this browser saved < the URL
  const d = defaults || {};
  DISP.defRange = inList(RANGES, d.graph_minutes) ? d.graph_minutes : 60;
  DISP.defRefresh = inList(REFRESHES, d.refresh_seconds) ? d.refresh_seconds : 30;
  for (const [k, key, list] of [["range", "range", RANGES], ["refresh", "refresh", REFRESHES]]) {
    const v = parseInt(hash.get(key), 10);
    if (inList(list, v)) { DISP.saved[k] = v; saveDisp(); }
    DISP[k] = DISP.saved[k] !== undefined ? DISP.saved[k] : DISP[k === "range" ? "defRange" : "defRefresh"];
  }
  $("#range-select").replaceChildren(...RANGES.map(([v, t]) => el("option", { value: v }, t))); $("#range-select").value = DISP.range;
  $("#refresh-select").replaceChildren(...REFRESHES.map(([v, t]) => el("option", { value: v }, t))); $("#refresh-select").value = DISP.refresh;
}
let refreshT = null;
function scheduleRefresh() { clearTimeout(refreshT); refreshT = setTimeout(async () => { try { await loadView(); } finally { scheduleRefresh(); } }, DISP.refresh * 1000); }
$("#range-select").addEventListener("change", e => { DISP.range = DISP.saved.range = parseInt(e.target.value, 10); saveDisp(); graphCache.clear(); setHash(); });
$("#refresh-select").addEventListener("change", e => { DISP.refresh = DISP.saved.refresh = parseInt(e.target.value, 10); saveDisp(); setHash(); scheduleRefresh(); loadView(); });

// ---------- tooltip
const tip = $("#tip");
let tipKey = "", tipPt = { clientX: 0, clientY: 0 };      // tipKey: what the open tooltip is about, so a graph that arrives late is only drawn if it still belongs there
function tipShow(e, lines, key = "") { tipKey = key; tip.replaceChildren(...lines.map((l, i) => el("div", { class: i === 0 ? "tip-h" : "" }, l))); tip.hidden = false; tipMove(e); }
function tipMove(e) { tipPt = { clientX: e.clientX, clientY: e.clientY }; const pad = 14; let x = e.clientX + pad, y = e.clientY + pad; const r = tip.getBoundingClientRect(); if (x + r.width > innerWidth - 8) x = e.clientX - r.width - pad; if (y + r.height > innerHeight - 8) y = e.clientY - r.height - pad; tip.style.left = Math.max(4, x) + "px"; tip.style.top = Math.max(4, y) + "px"; }
const tipHide = () => { tip.hidden = true; tipKey = ""; };

// ---------- map drawing
const nodeLabel = (n, v) => n.label || (v && v.name) || n.hostname || n.host;
// Device look: shape (square = the name inside a box, circle / triangle = a symbol with the name underneath) and size.
const NODE_SHAPES = ["square", "circle", "triangle"], NODE_SIZES = { small: 0.8, medium: 1, large: 1.35, xlarge: 1.8 };
const nodeShape = n => NODE_SHAPES.includes(n.shape) ? n.shape : "square";
const nodeSize = n => Object.prototype.hasOwnProperty.call(NODE_SIZES, n.size) ? n.size : "medium";
// Notifications: the round problem count on a device. On unless the device says `notify: false` (the editor switches it per device or for the whole map).
const nodeNotify = n => n.notify !== false;
function setNodeNotify(n, on) { if (on) delete n.notify; else n.notify = false; }      // "on" is the default, so it is not stored
function nodeGeo(n, v) {
  const shape = nodeShape(n), k = NODE_SIZES[nodeSize(n)], label = trunc(nodeLabel(n, v), 30), g = { x: n.x, y: n.y, shape, k, label };
  if (shape === "square") return Object.assign(g, { w: clamp(label.length * 8 + 46, 124, 280) * k, h: 38 * k, fs: 13 * k });
  const fs = clamp(12 * Math.sqrt(k), 10, 18);
  Object.assign(g, { fs, lw: label.length * fs * 0.62 + 10, lh: fs + 6 });      // lw / lh: room taken by the name under the symbol
  if (shape === "circle") return Object.assign(g, { r: 23 * k, w: 46 * k, h: 46 * k, ly: 23 * k + 3 });
  return Object.assign(g, { tw: 54 * k, th: 47 * k, w: 54 * k, h: 47 * k, ly: 23.5 * k + 3 });      // ly: top of the name, measured from the symbol's centre
}
function inShape(g, px, py) {      // is the point (relative to the device centre) on the device? the name under a symbol counts, so a link never runs through it
  if (g.shape === "square") return Math.abs(px) <= g.w / 2 && Math.abs(py) <= g.h / 2;
  if (Math.abs(px) <= g.lw / 2 && py >= g.ly - 3 && py <= g.ly + g.lh) return true;
  if (g.shape === "circle") return px * px + py * py <= g.r * g.r;
  const t = (py + g.th / 2) / g.th;      // triangle, apex up
  return t >= 0 && t <= 1 && Math.abs(px) <= t * g.tw / 2;
}
function edgeDist(g, ux, uy) {      // distance from the centre to where a link should start, going in direction (ux, uy)
  if (g.shape === "square") return Math.min((g.w / 2) / Math.max(Math.abs(ux), 1e-9), (g.h / 2) / Math.max(Math.abs(uy), 1e-9)) + 3;
  let t = 0; while (t < 500 && inShape(g, ux * t, uy * t)) t += 1;
  return t + 3;
}
function offsetLine(pts, d) {      // the polyline moved sideways by d (left of the direction of travel); bends get mitred joins, very sharp ones a bevel
  const dir = (a, b) => { const dx = b.x - a.x, dy = b.y - a.y, l = Math.hypot(dx, dy) || 1; return [dx / l, dy / l]; };
  const out = [], n = pts.length;
  for (let i = 0; i < n; i++) {
    if (i === 0 || i === n - 1) { const [ux, uy] = i === 0 ? dir(pts[0], pts[1]) : dir(pts[n - 2], pts[n - 1]); out.push({ x: pts[i].x - uy * d, y: pts[i].y + ux * d }); continue; }
    const [u1x, u1y] = dir(pts[i - 1], pts[i]), [u2x, u2y] = dir(pts[i], pts[i + 1]), n1x = -u1y, n1y = u1x, n2x = -u2y, n2y = u2x, k = 1 + n1x * n2x + n1y * n2y;
    if (k < 0.2) { out.push({ x: pts[i].x + n1x * d, y: pts[i].y + n1y * d }, { x: pts[i].x + n2x * d, y: pts[i].y + n2y * d }); }
    else out.push({ x: pts[i].x + (n1x + n2x) * d / k, y: pts[i].y + (n1y + n2y) * d / k });
  }
  return out;
}
function arrowPoly(path, shaft, head, headLen) {      // an arrow along a path (tail -> tip): a shaft that follows the bends, then the head on the last stretch
  const n = path.length, q = path[n - 1], p = path[n - 2];
  const dx = q.x - p.x, dy = q.y - p.y, len = Math.hypot(dx, dy) || 1, ux = dx / len, uy = dy / len, nx = -uy, ny = ux;
  const hl = Math.min(headLen, len * 0.6), sx = q.x - ux * hl, sy = q.y - uy * hl;
  const core = path.slice(0, n - 1).concat([{ x: sx, y: sy }]);
  const left = offsetLine(core, shaft / 2), right = offsetLine(core, -shaft / 2).reverse();
  const pts = left.map(a => [a.x, a.y]).concat([[sx + nx * head / 2, sy + ny * head / 2], [q.x, q.y], [sx - nx * head / 2, sy - ny * head / 2]], right.map(a => [a.x, a.y]));
  return pts.map(a => a[0].toFixed(1) + "," + a[1].toFixed(1)).join(" ");
}
function pill(g, x, y, text, fill, color, extra = {}) {
  const w = text.length * 6.6 + 14;
  g.append(svg("g", { transform: `translate(${x.toFixed(1)},${y.toFixed(1)})`, "pointer-events": "none" },
    svg("rect", { x: -w / 2, y: -9, width: w, height: 18, rx: 9, fill, stroke: "#05080c", "stroke-width": 1, opacity: (extra.opacity || 1) * (editing && canEdit ? 0.8 : 1) }),      // a little see-through while editing, so the dots that sit under a label show
    svg("text", { "text-anchor": "middle", y: 4, "font-size": 11, "font-weight": 700, fill: color, text })));
}
function linkTip(l, v, A, B) {
  const lines = [l.name || (A.label + " ↔ " + B.label), A.label + " " + l.a.if + "  ↔  " + B.label + " " + l.b.if];
  if (v) {
    lines.push("Status: " + v.status);
    lines.push(A.label + " → " + B.label + ": " + fmtBps(v.ab_bps) + " (" + fmtPct(v.ab_pct) + ")");
    lines.push(B.label + " → " + A.label + ": " + fmtBps(v.ba_bps) + " (" + fmtPct(v.ba_pct) + ")");
    lines.push("Capacity: " + (v.speed ? fmtBps(v.speed) + (v.speed_src === "override" ? " (override)" : "") : "unknown"));
    if (v.clock) lines.push("Updated " + ago(v.clock));
  }
  return lines;
}
// ---------- the in / out traffic graph in a link's hover card. The server reads the history (Zabbix history.get) and sends a thinned series per direction.
const GCOL = { ab: "#4aa3ff", ba: "#c084fc" };      // blue = first device → second, violet = the way back; neither is a status or usage colour, so a graph is never mistaken for a warning
const graphCache = new Map();      // "tab|link|range" -> { t, data, err, p }: hovering a link again within the refresh interval (the map is redrawn every refresh) does not ask the server again
function graphLoad(tid, lid) {
  const mins = DISP.range, key = tid + "|" + lid + "|" + mins, hit = graphCache.get(key);
  if (hit && (hit.p || (hit.t && Date.now() - hit.t < clamp(DISP.refresh * 1000, 15000, 300000)))) return hit;
  const ent = { t: 0, data: hit ? hit.data : null, err: "", p: null };      // an older picture stays on screen while the new one is on its way
  ent.p = api("/api/linkgraph?tab=" + encodeURIComponent(tid) + "&link=" + encodeURIComponent(lid) + "&minutes=" + mins)
    .then(d => { ent.data = d; ent.err = ""; }, e => { ent.err = e.message || "failed"; })
    .finally(() => { ent.t = Date.now(); ent.p = null; });
  graphCache.delete(key); graphCache.set(key, ent);
  if (graphCache.size > 200) graphCache.delete(graphCache.keys().next().value);
  return ent;
}
const niceMax = x => { if (!(x > 0)) return 1; const p = 10 ** Math.floor(Math.log10(x)), f = x / p; return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 4 ? 4 : f <= 5 ? 5 : f <= 8 ? 8 : 10) * p; };
const axisBps = v => v <= 0 ? "0" : v >= 1e9 ? +(v / 1e9).toFixed(2) + "G" : v >= 1e6 ? +(v / 1e6).toFixed(1) + "M" : v >= 1e3 ? +(v / 1e3).toFixed(1) + "k" : String(Math.round(v));
const clockOf = (ts, span) => {      // time of day on a short graph, weekday + time on a day-long one, date + time beyond three days
  const d = new Date(ts * 1000), t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return span > 3 * 86400 ? d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + t : span > 20 * 3600 ? d.toLocaleDateString([], { weekday: "short" }) + " " + t : t;
};
const spanText = min => min < 120 ? min + " min" : min < 2880 ? +(min / 60).toFixed(1) + " h" : +(min / 1440).toFixed(1) + " d";
function graphView(g, A, B, v) {
  const W = 340, H = 104, ML = 36, MR = 6, MT = 6, MB = 16, PW = W - ML - MR, PH = H - MT - MB, from = g.from, span = Math.max(1, g.to - g.from);
  const S = [["ab", A.label + " → " + B.label], ["ba", B.label + " → " + A.label]].map(([k, name]) => ({ k, name, pts: Array.isArray(g[k]) ? g[k] : [] }));
  const all = S.flatMap(s => s.pts.map(p => p[1]));
  if (!all.length) return el("div", { class: "muted" }, "No traffic history for this link in the last " + spanText(g.minutes) + ".");
  const ymax = niceMax(Math.max(...all) * 1.05), X = t => ML + clamp((t - from) / span, 0, 1) * PW, Y = b => MT + PH - clamp(b / ymax, 0, 1) * PH;
  const kids = [];
  for (const f of [0, 0.5, 1]) {
    const y = MT + PH - f * PH;
    kids.push(svg("line", { x1: ML, x2: W - MR, y1: y, y2: y, stroke: f ? "#243040" : "#3a4a5e", "stroke-width": 1 }), svg("text", { x: ML - 4, y: y + 3, "text-anchor": "end", "font-size": 9, fill: "#8b98a8", text: axisBps(ymax * f) }));
    kids.push(svg("text", { x: ML + f * PW, y: H - 3, "text-anchor": f === 0 ? "start" : f === 1 ? "end" : "middle", "font-size": 9, fill: "#8b98a8", text: clockOf(from + span * f, span) }));
  }
  if (v && v.speed && v.speed <= ymax) kids.push(svg("line", { x1: ML, x2: W - MR, y1: Y(v.speed), y2: Y(v.speed), stroke: "#94a3b8", "stroke-width": 1, "stroke-dasharray": "4 3" }));      // the link's capacity, when the graph reaches it
  const deltas = S.flatMap(s => s.pts.slice(1).map((p, i) => p[0] - s.pts[i][0])).sort((a, b) => a - b), gap = Math.max(90, 3 * (deltas.length ? deltas[deltas.length >> 1] : 60));      // a hole in the samples breaks the line instead of being drawn across
  for (const s of S) {
    const segs = []; let prev = null;
    for (const [t, b] of s.pts) { if (prev == null || t - prev > gap) segs.push([]); segs[segs.length - 1].push([X(t), Y(b)]); prev = t; }
    for (const seg of segs) {
      if (seg.length === 1) { kids.push(svg("circle", { cx: seg[0][0].toFixed(1), cy: seg[0][1].toFixed(1), r: 1.6, fill: GCOL[s.k] })); continue; }
      const d = seg.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" "), base = MT + PH;
      kids.push(svg("path", { d: d + " L" + seg[seg.length - 1][0].toFixed(1) + " " + base + " L" + seg[0][0].toFixed(1) + " " + base + " Z", fill: GCOL[s.k], opacity: 0.16 }));
      kids.push(svg("path", { d, fill: "none", stroke: GCOL[s.k], "stroke-width": 1.5, "stroke-linejoin": "round" }));
    }
  }
  const legend = S.map(s => {
    const vs = s.pts.map(p => p[1]);
    return el("div", { class: "tg-row" }, el("span", { class: "tg-sw", style: "background:" + GCOL[s.k] }), el("b", {}, trunc(s.name, 42)),
      el("div", { class: "tg-st muted" }, vs.length ? "now " + fmtBps(vs[vs.length - 1]) + " · avg " + fmtBps(vs.reduce((a, b) => a + b, 0) / vs.length) + " · max " + fmtBps(Math.max(...vs)) : "no data"));
  });
  return el("div", {}, el("div", { class: "tg-h" }, el("span", {}, "Traffic, last " + spanText(g.minutes)), el("span", {}, g.minutes > 1440 ? "bit/s · hourly averages" : "bit/s")),
    svg("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img", "aria-label": "In and out traffic of the link" }, ...kids), ...legend);
}
function linkGraphBox(l, A, B, v) {      // the graph block under the link's text lines: shows what it has at once, and fills in when the server answers
  const tid = tabId, key = tid + "|" + l.id, box = el("div", { class: "tgraph loading" }, el("span", { class: "muted" }, "Loading graph…")), ent = graphLoad(tid, l.id);
  tip.append(box);
  const paint = () => {
    if (tipKey !== key || !box.isConnected) return;
    const wait = !ent.data && (ent.p || !ent.err);
    box.className = "tgraph" + (wait ? " loading" : "");
    box.replaceChildren(ent.data ? graphView(ent.data, A, B, v) : el("span", { class: "muted" }, wait ? "Loading graph…" : "Graph unavailable: " + ent.err));
    tipMove(tipPt);
  };
  if (ent.data || ent.err) paint();
  if (ent.p) ent.p.then(paint);
}
// ---------- bent links: a link can carry bend points (`via`), so its line can be routed around other devices
const viaOf = l => (Array.isArray(l.via) ? l.via : []);
// Interface tags (the interface names written beside the two ends of a link): shown unless the link says `if_show: false`. `if_pos` ("above" /
// "below") fixes the side of the line they sit on (on a vertical stretch "above" is the left side); a link without it keeps the automatic side.
const linkShowIf = l => l.if_show !== false;
const linkIfPos = l => l.if_pos === "above" || l.if_pos === "below" ? l.if_pos : null;
const tagSide = new Map();      // link id -> the side its tags were last drawn on, so the menus can show where they are right now
const effIfPos = l => linkIfPos(l) || tagSide.get(l.id) || "above";
function setLinkTags(l, key, val) {
  if (key === "if_show") { if (val) delete l.if_show; else l.if_show = false; }
  else if (val === "above" || val === "below") l.if_pos = val; else delete l.if_pos;
}
const sideSign = (q, pos) => { const s = (q.ux > 1e-6 || (Math.abs(q.ux) <= 1e-6 && q.uy < 0)) ? 1 : -1; return pos === "below" ? s : -s; };      // which way along the stretch's normal (-uy, ux) "above" / "below" lies
function pathLen(pts) { let s = 0; for (let i = 1; i < pts.length; i++) s += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y); return s; }
function pathAt(pts, s) {      // the point s px along the path, plus the stretch it lies on: index i, direction (ux, uy), normal (nx, ny), distance d from the stretch's start and its length
  let acc = 0;
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1], b = pts[i], len = Math.hypot(b.x - a.x, b.y - a.y);
    if (acc + len >= s || i === pts.length - 1) {
      const t = len ? clamp((s - acc) / len, 0, 1) : 0, ux = len ? (b.x - a.x) / len : 1, uy = len ? (b.y - a.y) / len : 0;
      return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t, i: i - 1, ux, uy, nx: -uy, ny: ux, d: t * len, len };
    }
    acc += len;
  }
}
// The line of a link, from the edge of device A through its bend points to the edge of device B. Straight links keep the sideways offset
// that separates several links between the same two devices; a bent link is placed by hand, so it has none.
function linkPath(l, geo, off) {
  const A = geo.get(l.a.node), B = geo.get(l.b.node);
  if (!A || !B) return null;
  const via = viaOf(l);
  if (!via.length) {
    const dx = B.x - A.x, dy = B.y - A.y, len = Math.hypot(dx, dy);
    if (len < 30) return null;
    const ux = dx / len, uy = dy / len, nx = -uy, ny = ux, o = (l.a.node < l.b.node ? off : -off);
    const sA = edgeDist(A, ux, uy), sB = edgeDist(B, -ux, -uy);
    const p0 = { x: A.x + ux * sA + nx * o, y: A.y + uy * sA + ny * o }, p1 = { x: B.x - ux * sB + nx * o, y: B.y - uy * sB + ny * o };
    return Math.hypot(p1.x - p0.x, p1.y - p0.y) < 40 ? null : [p0, p1];
  }
  const dir = (from, to) => { const dx = to.x - from.x, dy = to.y - from.y, d = Math.hypot(dx, dy); return d < 1 ? [1, 0] : [dx / d, dy / d]; };
  const [ax, ay] = dir(A, via[0]), [bx, by] = dir(B, via[via.length - 1]), sA = edgeDist(A, ax, ay), sB = edgeDist(B, bx, by);
  const pts = [{ x: A.x + ax * sA, y: A.y + ay * sA }, ...via.map(v => ({ x: v.x, y: v.y })), { x: B.x + bx * sB, y: B.y + by * sB }], path = [pts[0]];
  for (let i = 1; i < pts.length; i++) {      // a bend sitting on top of its neighbour (or on a device edge) adds nothing
    const last = path[path.length - 1];
    if (Math.hypot(pts[i].x - last.x, pts[i].y - last.y) >= 4) path.push(pts[i]);
    else if (i === pts.length - 1 && path.length > 1) path[path.length - 1] = pts[i];
  }
  return path.length < 2 || pathLen(path) < 40 ? null : path;
}
function drawLink(gl, gt, gh, gd, m, l, v, geo, off) {
  const A = geo.get(l.a.node), B = geo.get(l.b.node), P = linkPath(l, geo, off);
  if (!P) return;
  const bent = P.length > 2, total = pathLen(P);
  // where the two directions meet: the middle of the line, nudged off a bend so each arrow head sits on a straight stretch
  let sm = total / 2;
  { const q = pathAt(P, sm), lo = Math.min(22, q.len * 0.5); if (q.d < lo) sm += lo - q.d; else if (q.len - q.d < lo) sm -= lo - (q.len - q.d); }
  const M = pathAt(P, sm), gap = 3;
  const mA = { x: M.x - M.ux * gap, y: M.y - M.uy * gap }, mB = { x: M.x + M.ux * gap, y: M.y + M.uy * gap };
  const halfA = [...P.slice(0, M.i + 1), mA], halfB = [...P.slice(M.i + 1).reverse(), mB];      // each arrow runs from its device to the middle
  const st = v ? v.status : "unknown";
  let cA, cB, dash = null, op = 1;
  if (st === "down") { cA = cB = COL.down; dash = "7 5"; op = 0.8; }
  else if (st === "stale" || st === "unknown") { cA = cB = COL.stale; dash = "7 5"; op = 0.7; }
  else { cA = colorFor(v.ab_pct); cB = colorFor(v.ba_pct); if (st === "warn") dash = "4 3"; }
  const selected = sel && sel.type === "link" && sel.id === l.id, pstr = P.map(p => p.x.toFixed(1) + "," + p.y.toFixed(1)).join(" ");
  if (selected) gl.append(svg("polyline", { points: pstr, fill: "none", stroke: "#fff", "stroke-width": 22, opacity: 0.13, "stroke-linecap": "round", "stroke-linejoin": "round" }));
  const stroke = st === "warn" ? COL.warn : "#05080c";
  gl.append(svg("polygon", { points: arrowPoly(halfA, 9, 18, 16), fill: cA, stroke, "stroke-width": st === "warn" ? 2 : 1, "stroke-dasharray": dash, opacity: op, "stroke-linejoin": "round" }));
  gl.append(svg("polygon", { points: arrowPoly(halfB, 9, 18, 16), fill: cB, stroke, "stroke-width": st === "warn" ? 2 : 1, "stroke-dasharray": dash, opacity: op, "stroke-linejoin": "round" }));
  const at = s => pathAt(P, s);
  const longestMid = pts => { let best = 0, bl = -1; for (let i = 1; i < pts.length; i++) { const d = Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y); if (d > bl) { bl = d; best = i; } } const a = pts[best - 1], b = pts[best]; return { x: a.x + (b.x - a.x) * 0.54, y: a.y + (b.y - a.y) * 0.54 }; };      // a label goes on the longest straight stretch, not on a bend
  if (st === "down") pill(gt, M.x, M.y, "DOWN", COL.down, "#fff");
  else if (st === "stale" || st === "unknown") pill(gt, M.x, M.y, st === "stale" ? "no data" : "unknown", COL.stale, "#e2e8f0");
  else {
    const lab = (bps, pct) => pct == null ? fmtBps(bps) : fmtPct(pct) + " · " + fmtBps(bps);
    const a = bent ? longestMid(halfA) : at(total * 0.27), b = bent ? longestMid(halfB) : at(total * 0.73);
    pill(gt, a.x, a.y, lab(v.ab_bps, v.ab_pct), cA, textOn(cA));
    pill(gt, b.x, b.y, lab(v.ba_bps, v.ba_pct), cB, textOn(cB));
  }
  // small text beside the line: anchored away from it so it never sits on top of the arrow
  const hitsName = (g, x0, x1, y0, y1) => !!(g && g.lw && x1 >= g.x - g.lw / 2 - 2 && x0 <= g.x + g.lw / 2 + 2 && y1 >= g.y + g.ly - 3 && y0 <= g.y + g.ly + g.lh + 2);      // does the box overlap the name written under a circle / triangle?
  const sideText = (q, dist, s, size, fill, near, side, attrs) => {      // q: a point on the line with its stretch's normal. side: +1 / -1 along that normal. near: the device this text sits next to - the text hops to the other side of the line if it would land on that device's name
    const w = s.length * size * 0.58, place = sg => {
      const cx = q.nx * sg, cy = q.ny * sg, anchor = cx > 0.4 ? "start" : cx < -0.4 ? "end" : "middle";
      const x = q.x + cx * dist, y = q.y + cy * dist + (Math.abs(cy) > 0.6 ? (cy > 0 ? 8 : -2) : 3), x0 = anchor === "start" ? x : anchor === "end" ? x - w : x - w / 2;
      return { x, y, anchor, sg, clash: hitsName(near, x0, x0 + w, y - size, y + 3) };
    };
    let p = place(side || 1); if (p.clash) { const q2 = place(-(side || 1)); if (!q2.clash) p = q2; }
    gt.append(svg("text", Object.assign({ x: p.x, y: p.y, "text-anchor": p.anchor, "font-size": size, fill, stroke: "#0b1118", "stroke-width": 3, "stroke-linejoin": "round", "paint-order": "stroke", "pointer-events": "none", text: s }, attrs)));
    return p.sg;      // the side it ended up on
  };
  const tagsOn = showIf && linkShowIf(l), tagPos = linkIfPos(l);
  if (tagsOn) {
    const first = Math.hypot(P[1].x - P[0].x, P[1].y - P[0].y), lastLen = Math.hypot(P[P.length - 1].x - P[P.length - 2].x, P[P.length - 1].y - P[P.length - 2].y);
    const qa = at(Math.min(total * 0.075, first * 0.45)), qb = at(total - Math.min(total * 0.075, lastLen * 0.45));
    const sgA = sideText(qa, 15, l.a.if, 10, "#9fb0c3", A, tagPos ? sideSign(qa, tagPos) : 1, { class: "iftag", "data-lid": l.id, "data-end": "a" });
    sideText(qb, 15, l.b.if, 10, "#9fb0c3", B, tagPos ? sideSign(qb, tagPos) : 1, { class: "iftag", "data-lid": l.id, "data-end": "b" });
    tagSide.set(l.id, sgA === sideSign(qa, "above") ? "above" : "below");
  }
  if (l.name && st !== "down" && st !== "stale" && st !== "unknown") sideText(M, 18, trunc(l.name, 28), 10, "#6b7a8c", null, tagsOn && tagPos ? sideSign(M, tagPos === "below" ? "above" : "below") : 1, { class: "linkname", "data-lid": l.id });      // with a chosen tag side the link name goes on the other side
  gl.append(svg("polyline", {
    points: pstr, fill: "none", stroke: "transparent", "stroke-width": 24, "stroke-linecap": "round", "stroke-linejoin": "round", style: "cursor:pointer", "data-lid": l.id,
    onmouseenter: e => { if (!ctxEl) { tipShow(e, linkTip(l, v, A, B), tabId + "|" + l.id); linkGraphBox(l, A, B, v); } }, onmousemove: tipMove, onmouseleave: tipHide,
    onclick: e => { if (editing) { e.stopPropagation(); select({ type: "link", id: l.id }); } },
    oncontextmenu: e => ctxOpen(e, { type: "link", id: l.id }),
  }));
  if (editing && canEdit) bendHandles(selected ? gh : gd, m, l, P, selected);      // the selected link's dots go on top of everything; the other links' small rings sit under the usage labels (which let clicks through), so they never hide the numbers
}
// Every link has one bend point, in the middle of it. A link nobody has bent is straight and follows its devices around; the first time its point is
// dragged it is fixed where it was dropped (`via`, one point) and stays put when a device moves. Straighten puts it back in the middle.
const BENDS = 1;
const snapPt = (p, m) => ({ x: clamp(Math.round(p.x / 10) * 10, 0, m.width), y: clamp(Math.round(p.y / 10) * 10, 0, m.height) });      // bends snap to the same 10 px grid as devices
const evenPoints = P => Array.from({ length: BENDS }, (_, i) => ({ x: P[0].x + (P[1].x - P[0].x) * (i + 1) / (BENDS + 1), y: P[0].y + (P[1].y - P[0].y) * (i + 1) / (BENDS + 1) }));      // P = [start, end] of a straight link
function straighten(m, l) { delete l.via; scheduleSave(); select({ type: "link", id: l.id }); }
function fitVia(m, l) {      // a link saved by an earlier version with several bend points keeps the one that bends it most (the point farthest from the straight line between its devices; among nearly equal ones, the middle one)
  const A = m.nodes.find(n => n.id === l.a.node), B = m.nodes.find(n => n.id === l.b.node), via = viaOf(l);
  if (!A || !B || via.length <= BENDS) return;
  const dx = B.x - A.x, dy = B.y - A.y, len = Math.hypot(dx, dy) || 1, off = v => Math.abs((v.x - A.x) * dy - (v.y - A.y) * dx) / len, mid = (via.length - 1) / 2;
  const far = Math.max(...via.map(off)); let best = -1;
  via.forEach((v, i) => { if (off(v) >= far * 0.9 - 0.5 && (best < 0 || Math.abs(i - mid) < Math.abs(best - mid))) best = i; });
  l.via = [{ x: via[best].x, y: via[best].y }];
}
function startBendDrag(e, m, l, i, base) {      // base: where the point is right now (the stored bend, or the middle spot of a straight link)
  e.preventDefault(); e.stopPropagation(); tipHide();
  const p0 = toSvg(e), off = { x: p0.x - base[i].x, y: p0.y - base[i].y };
  let moved = false;
  const mv = ev => {
    const p = toSvg(ev);
    if (!moved && Math.hypot(p.x - p0.x, p.y - p0.y) <= 3) return;      // still only a click
    moved = true; dragging = true; tipHide();
    if (!viaOf(l).length) l.via = base.map(q => ({ x: Math.round(q.x), y: Math.round(q.y) }));      // the first move fixes it where it is
    l.via[i] = snapPt({ x: p.x - off.x, y: p.y - off.y }, m);
    renderMain();
  };
  const up = () => {
    removeEventListener("pointermove", mv); removeEventListener("pointerup", up); dragging = false;
    suppressClick = true; setTimeout(() => { suppressClick = false; }, 80);      // the click that follows must not deselect
    if (moved) scheduleSave();
    select({ type: "link", id: l.id });      // a plain click on a point just selects its link
  };
  addEventListener("pointermove", mv); addEventListener("pointerup", up);
}
function bendHandles(gh, m, l, P, selected) {      // the dot of a link, in edit mode: a big blue one on the selected link, a small ring on the others. Drag it to bend the link, right-click for the menu.
  const via = viaOf(l), manual = via.length > 0, pts = manual ? via : P.length === 2 ? evenPoints(P) : null;
  if (!pts) return;
  const base = pts.map(q => ({ x: q.x, y: q.y }));
  pts.forEach((v, i) => gh.append(svg("circle", { class: "bend" + (selected ? "" : " idle"), cx: v.x.toFixed(1), cy: v.y.toFixed(1), r: selected ? 7.5 : 4, "data-link": l.id, "data-i": i,
    onpointerdown: e => { if (e.button === 0) startBendDrag(e, m, l, i, base); },
    oncontextmenu: e => ctxOpen(e, manual ? { type: "via", id: l.id, index: i } : { type: "link", id: l.id }) })));
}
// Weathermap device frame + dot: green = up, red = down. A host Zabbix does not report on (disabled / no data) stays neutral grey rather than pretending to be up.
const nodeStatusColor = v => !v ? "#475569" : v.status === "down" ? "#ef4444" : v.status === "up" ? "#22c55e" : "#64748b";
function drawNode(layer, n, v, g) {
  const col = nodeStatusColor(v), selected = sel && sel.type === "node" && sel.id === n.id, box = g.shape === "square";
  const frame = { fill: "#141d2a", stroke: selected ? "#fff" : col, "stroke-width": selected ? 3 : 2.5, "stroke-linejoin": "round" };
  const grp = svg("g", { class: "node", "data-shape": g.shape, "data-id": n.id, transform: box ? `translate(${g.x - g.w / 2},${g.y - g.h / 2})` : `translate(${g.x},${g.y})` });
  let bx, by;      // centre of the problem badge
  if (box) {
    grp.append(svg("rect", Object.assign({ width: g.w, height: g.h, rx: 9 * g.k }, frame)));
    grp.append(svg("circle", { cx: 16 * g.k, cy: g.h / 2, r: 6 * g.k, fill: col }));
    grp.append(svg("text", { x: 30 * g.k, y: g.h / 2 + g.fs * 0.385, "font-size": g.fs, "font-weight": 700, fill: "#e6edf3", text: g.label }));
    bx = g.w - 4; by = 4;
  } else {
    let cy = 0;
    if (g.shape === "circle") { grp.append(svg("circle", Object.assign({ cx: 0, cy: 0, r: g.r }, frame))); bx = g.r * 0.72; by = -g.r * 0.72; }
    else { grp.append(svg("polygon", Object.assign({ points: `0,${-g.th / 2} ${g.tw / 2},${g.th / 2} ${-g.tw / 2},${g.th / 2}` }, frame))); cy = g.th / 6; bx = g.tw * 0.16 + 6; by = -g.th * 0.18; }
    grp.append(svg("circle", { cx: 0, cy, r: Math.max(4, 6 * g.k), fill: col }));
    grp.append(svg("text", { x: 0, y: g.ly + g.fs * 0.85, "text-anchor": "middle", "font-size": g.fs, "font-weight": 700, fill: "#e6edf3", stroke: "#0b1118", "stroke-width": 3, "stroke-linejoin": "round", "paint-order": "stroke", text: g.label }));
  }
  const notify = nodeNotify(n);
  if (!notify && editing && canEdit) {      // edit mode only: a dashed ring with a slash marks a device whose notifications are off (view and kiosk show nothing there)
    grp.append(svg("circle", { class: "muted-mark", cx: bx, cy: by, r: 8, fill: "#0b1118", stroke: "#94a3b8", "stroke-width": 1.5, "stroke-dasharray": "3 2" }),
      svg("line", { class: "muted-mark", x1: bx - 5, y1: by + 5, x2: bx + 5, y2: by - 5, stroke: "#94a3b8", "stroke-width": 1.5 }));
  }
  if (notify && v && v.problems > 0) {
    grp.append(svg("circle", { class: "pbadge", cx: bx, cy: by, r: 9, fill: SEV[v.severity] || SEV[2], stroke: "#05080c" }));
    grp.append(svg("text", { class: "pbadge", x: bx, y: by + 4, "text-anchor": "middle", "font-size": 11, "font-weight": 700, fill: "#fff", text: v.problems > 9 ? "9+" : v.problems }));
  }
  grp.addEventListener("mouseenter", e => { if (dragging || ctxEl) return; const lines = [g.label, "Status: " + (v ? v.status : "unknown")]; if (!notify) lines.push("Notifications are off for this device"); for (const p of (v && v.top) || []) lines.push("• [" + SEVN[p.severity] + "] " + p.name); tipShow(e, lines); });
  grp.addEventListener("mousemove", tipMove);
  grp.addEventListener("mouseleave", tipHide);
  grp.addEventListener("pointerdown", e => { if (editing && e.button === 0) startDrag(e, n); });
  grp.addEventListener("contextmenu", e => ctxOpen(e, { type: "node", id: n.id }));
  layer.append(grp);
}
// The usage meter sits bottom-left unless the map says otherwise: `legend: { x, y, dir }` - x / y are 0..100 (% of the free space in the map area, so the same spot
// on any screen size and in kiosk), dir "v" lays it out vertically. In edit mode it carries a bar to drag it by, a switch for horizontal / vertical and a reset.
const LEG_M = 12;      // px kept free around it
const legPct = (v, span) => span > 0 ? Math.floor(clamp(v / span * 100, 0, 100) * 10 + 0.5) / 10 : 0;      // same rounding as the layout validation (0.1)
function setLegend(m, patch) {      // keeps the stored object minimal: nothing stored = bottom-left, horizontal
  const lg = Object.assign({}, m.legend, patch);
  if (lg.dir !== "v") delete lg.dir;
  if (typeof lg.x !== "number" || typeof lg.y !== "number") { delete lg.x; delete lg.y; }
  if (Object.keys(lg).length) m.legend = lg; else delete m.legend;
}
function placeLegend(host, m) {
  const lg = host.querySelector(".legend"), c = m && m.legend;
  if (!lg) return;
  if (!c || typeof c.x !== "number" || typeof c.y !== "number") { lg.style.left = lg.style.top = lg.style.bottom = ""; return; }      // not moved: the stylesheet's bottom-left
  lg.style.left = (LEG_M + Math.max(0, host.clientWidth - lg.offsetWidth - 2 * LEG_M) * c.x / 100).toFixed(1) + "px";
  lg.style.top = (LEG_M + Math.max(0, host.clientHeight - lg.offsetHeight - 2 * LEG_M) * c.y / 100).toFixed(1) + "px";
  lg.style.bottom = "auto";
}
function startLegendDrag(e, host, m) {
  if (e.button !== 0 || e.target.closest("button, label, input")) return;
  e.preventDefault(); e.stopPropagation(); tipHide();
  const r = host.querySelector(".legend").getBoundingClientRect(), dx = e.clientX - r.left, dy = e.clientY - r.top;
  let moved = false;
  const mv = ev => {
    const lg = host.querySelector(".legend"), hr = host.getBoundingClientRect(), W = host.clientWidth - lg.offsetWidth - 2 * LEG_M, H = host.clientHeight - lg.offsetHeight - 2 * LEG_M;      // (the live element: a redraw may have replaced it)
    setLegend(m, { x: legPct(ev.clientX - dx - hr.left - LEG_M, W), y: legPct(ev.clientY - dy - hr.top - LEG_M, H) });
    placeLegend(host, m); moved = true; dragging = true;
  };
  const up = () => { removeEventListener("pointermove", mv); removeEventListener("pointerup", up); dragging = false; if (moved) { scheduleSave(); renderDrawer(); } };
  addEventListener("pointermove", mv); addEventListener("pointerup", up);
}
addEventListener("resize", () => { const h = $("#main"), t = curTab(); if (h && t && t.type === "map" && L && L.maps[t.map]) placeLegend(h, L.maps[t.map]); });
function legend(m) {
  const s = scale(), vert = !!(m && m.legend && m.legend.dir === "v"), ed = editing && canEdit && !!m;
  const tg = el("input", { type: "checkbox" }); tg.checked = showIf;
  tg.addEventListener("change", () => { showIf = tg.checked; try { localStorage.setItem("showIf", showIf ? "1" : "0"); } catch (e) {} renderMain(); });
  return el("div", { class: "legend" + (vert ? " vert" : "") + (ed ? " editable" : ""), onpointerdown: ed ? e => startLegendDrag(e, $("#main"), m) : null },
    ed ? el("div", { class: "lg-bar" }, el("span", { class: "grip", title: "Drag to move the usage meter" }, "⠿ Usage meter"),
      el("button", { type: "button", class: "lg-flip", title: vert ? "Lay the meter out horizontally" : "Lay the meter out vertically", "aria-label": vert ? "Horizontal" : "Vertical", onclick: () => { setLegend(m, { dir: vert ? "h" : "v" }); changed(true); } }, vert ? "⇄" : "⇅"),
      el("button", { type: "button", class: "lg-reset", title: "Back to the bottom-left corner, horizontal", "aria-label": "Reset the usage meter", onclick: () => { delete m.legend; changed(true); } }, "↺")) : null,
    el("div", { class: "muted" }, "Link usage per direction – the label nearest a device is traffic leaving it"),
    el("div", { class: "bars" }, s.map(([t, c], i) => el("div", { class: "lg", style: `background:${c};color:${textOn(c)}` }, (s[i + 1] ? t + "–" + s[i + 1][0] : t + "+") + "%")),
      el("div", { class: "lg", style: `background:${COL.down};color:#fff` }, "down"), el("div", { class: "lg", style: `background:${COL.stale};color:#fff` }, "no data")),
    el("label", { title: "Hides / shows the interface names on every link on this screen. Which links carry them, and on which side of the line, is set per link in edit mode." }, tg, " show interface names"));
}
function drawMap(host, tab) {
  const m = L.maps[tab.map];
  if (!m) { host.replaceChildren(el("div", { class: "empty" }, "Map missing")); return; }
  const vn = new Map(((view && view.kind === "map" && view.nodes) || []).map(n => [n.id, n]));
  const vl = new Map(((view && view.kind === "map" && view.links) || []).map(l => [l.id, l]));
  const root = svg("svg", { class: "map", viewBox: `0 0 ${m.width} ${m.height}`, preserveAspectRatio: "xMidYMid meet" });
  if (editing) {
    root.append(svg("defs", {}, svg("pattern", { id: "grid", width: 20, height: 20, patternUnits: "userSpaceOnUse" }, svg("path", { d: "M20 0H0V20", fill: "none", stroke: "#1a2433", "stroke-width": 1 }))));
    root.append(svg("rect", { width: m.width, height: m.height, fill: "url(#grid)" }));
  }
  root.addEventListener("click", () => { if (editing && sel && !suppressClick) select(null); });
  const geo = new Map(m.nodes.map(n => [n.id, nodeGeo(n, vn.get(n.id))]));
  const pairs = new Map(), bentLinks = [];
  for (const l of m.links) { if (viaOf(l).length) { bentLinks.push(l); continue; } const k = [l.a.node, l.b.node].sort().join("|"); if (!pairs.has(k)) pairs.set(k, []); pairs.get(k).push(l); }      // a bent link is placed by hand: it does not take part in the side-by-side spreading
  const gl = svg("g"), gt = svg("g"), gn = svg("g"), gh = svg("g"), gd = svg("g");
  for (const arr of pairs.values()) arr.forEach((l, i) => drawLink(gl, gt, gh, gd, m, l, vl.get(l.id), geo, (i - (arr.length - 1) / 2) * 24));
  for (const l of bentLinks) drawLink(gl, gt, gh, gd, m, l, vl.get(l.id), geo, 0);
  for (const n of m.nodes) drawNode(gn, n, vn.get(n.id), geo.get(n.id));
  root.append(gl, gn, gd, gt, gh);
  host.replaceChildren(root, legend(m), el("div", { class: "stamp" }, view && view.ts ? "data " + (status && status.last_ok ? ago(status.last_ok) : "") : ""));
  placeLegend(host, m);
  if (!m.nodes.length) host.append(el("div", { class: "empty", style: "position:absolute;inset:0;pointer-events:none" }, canEdit ? "This map is empty – click Edit, then add devices from Zabbix and join them with links." : "This map is empty."));
}
function toSvg(e) { const s = $("#main svg.map"); const pt = s.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY; const m = s.getScreenCTM(); return m ? pt.matrixTransform(m.inverse()) : { x: 0, y: 0 }; }
function startDrag(e, n) {
  e.preventDefault(); e.stopPropagation();
  const m = L.maps[curTab().map], p0 = toSvg(e), off = { x: p0.x - n.x, y: p0.y - n.y };
  let moved = false;
  const mv = ev => { const p = toSvg(ev); dragging = true; moved = true; tipHide(); n.x = clamp(Math.round((p.x - off.x) / 10) * 10, 0, m.width); n.y = clamp(Math.round((p.y - off.y) / 10) * 10, 0, m.height); renderMain(); };
  const up = () => {
    removeEventListener("pointermove", mv); removeEventListener("pointerup", up); dragging = false;
    suppressClick = true; setTimeout(() => { suppressClick = false; }, 80);  // the click that follows a drag must not deselect
    if (moved) scheduleSave();
    select({ type: "node", id: n.id });
  };
  addEventListener("pointermove", mv); addEventListener("pointerup", up);
}

// ---------- devices tab (cards, list or heatmap)
const DEV_KEYS = ["name", "ip", "status", "proxy", "model", "os", "problems", "ifs", "busy"], DEV_GROUPS = ["none", "status", "proxy", "problems", "ip", "name", "os", "model"];
const PER_CHOICES = [10, 25, 50, 100, 200, 500];      // devices per page
const HEAT_KEYS = ["status", "busy", "sev"];      // what the heatmap tiles are coloured by (HEAT_METRICS below)
const DEVUI = { q: "", only: false, key: "name", dir: 1, group: "none", per: 50, heat: "status", pages: {}, open: new Set(), closed: new Set() };      // filter / sort / group / page size / expanded rows, shared by all device tabs; pages = current page of each tab
try {      // sort, group and "only issues" are remembered in this browser
  const s = JSON.parse(localStorage.getItem("channelview-devui") || "{}");
  if (DEV_KEYS.includes(s.key)) DEVUI.key = s.key;
  if (s.dir === 1 || s.dir === -1) DEVUI.dir = s.dir;
  if (DEV_GROUPS.includes(s.group)) DEVUI.group = s.group;
  if (PER_CHOICES.includes(s.per)) DEVUI.per = s.per;
  if (HEAT_KEYS.includes(s.heat)) DEVUI.heat = s.heat;
  DEVUI.only = s.only === true;
} catch (e) {}
const saveDevUI = () => { try { localStorage.setItem("channelview-devui", JSON.stringify({ key: DEVUI.key, dir: DEVUI.dir, group: DEVUI.group, only: DEVUI.only, per: DEVUI.per, heat: DEVUI.heat })); } catch (e) {} };
const DEV_MODES = ["list", "cards", "heatmap"];
const resetPages = () => { DEVUI.pages = {}; };      // a new filter / sort / grouping starts again on page 1
function devMode(tab) {
  try { const v = localStorage.getItem("channelview-devview:" + tab.id); if (DEV_MODES.includes(v)) return v; } catch (e) {}     // a viewer's own choice wins
  return DEV_MODES.includes(tab.style) ? tab.style : "cards";
}
function setDevMode(tab, v) { try { localStorage.setItem("channelview-devview:" + tab.id, v); } catch (e) {} renderMain(); }
const ST_RANK = { down: 0, unknown: 1, disabled: 2, up: 3 };
const ipSortKey = s => /^\d+\.\d+\.\d+\.\d+$/.test(s || "") ? "0" + String(s.split(".").reduce((a, p) => a * 256 + (+p), 0)).padStart(12, "0") : "1" + (s || "~").toLowerCase();
const subnet24 = ip => /^\d+\.\d+\.\d+\.\d+$/.test(ip || "") ? ip.split(".").slice(0, 3).join(".") + ".0/24" : "";
const DCOLS = [
  { k: "name", t: "Device", v: c => c.name.toLowerCase() },
  { k: "ip", t: "IP address", v: c => ipSortKey(c.ip) },
  { k: "status", t: "Status", v: c => ST_RANK[c.status] == null ? 1 : ST_RANK[c.status] },
  { k: "proxy", t: "Proxy", v: c => c.proxy ? c.proxy.toLowerCase() : "~" },
  { k: "model", t: "Model", v: c => c.model ? c.model.toLowerCase() : "~" },
  { k: "os", t: "OS", v: c => c.os ? c.os.toLowerCase() : "~" },
  { k: "problems", t: "Problems", desc: true, v: c => (c.problems || 0) * 10 + (c.severity || 0) },
  { k: "ifs", t: "Interfaces", desc: true, v: c => c.if_down * 1000 + c.if_total },
  { k: "busy", t: "Busiest interface", desc: true, v: c => (c.ifaces[0] && c.ifaces[0].pct != null) ? c.ifaces[0].pct : -1 },
];
// "Group by" choices: of = which group a device falls in, label = group heading, ord = how groups are ordered, desc = natural direction (most first)
const GROUPS = {
  status:   { t: "Status",              of: c => c.status || "unknown", label: g => g, ord: g => ST_RANK[g] == null ? 1 : ST_RANK[g], desc: false },
  proxy:    { t: "Proxy",               of: c => c.proxy || "", label: g => g || "No proxy (direct)", ord: g => g ? g.toLowerCase() : "~", desc: false },
  problems: { t: "Most problems",       of: c => c.problems || 0, label: g => g ? g + (g === 1 ? " problem" : " problems") : "No problems", ord: g => g, desc: true },
  ip:       { t: "IP address (subnet)", of: c => subnet24(c.ip), label: g => g || "Hostname / no IPv4 address", ord: g => g ? ipSortKey(g.split("/")[0]) : "2", desc: false },
  name:     { t: "Name (first letter)", of: c => ((c.name || "#")[0] || "#").toUpperCase(), label: g => g, ord: g => g, desc: false },
  os:       { t: "Operating system",    of: c => c.os || "", label: g => g || "Unknown OS (not in the host inventory)", ord: g => g ? g.toLowerCase() : "~", desc: false },
  model:    { t: "Device model",        of: c => c.model || "", label: g => g || "Unknown model (not in the host inventory)", ord: g => g ? g.toLowerCase() : "~", desc: false },
};
const naturalDir = k => GROUPS[k] && GROUPS[k].desc ? -1 : 1;
function devSort(list) {
  const col = DCOLS.find(c => c.k === DEVUI.key) || DCOLS[0], d = DEVUI.dir;
  return list.slice().sort((a, b) => { const x = col.v(a), y = col.v(b); return (x < y ? -d : x > y ? d : 0) || (a.name.toLowerCase() < b.name.toLowerCase() ? -1 : 1); });
}
function devGroups(list) {      // -> [{ id, g, label, items }]; id "" = no grouping
  const G = GROUPS[DEVUI.group];
  if (!G) return [{ id: "", items: devSort(list) }];
  const m = new Map();
  for (const c of list) { const g = G.of(c); if (!m.has(g)) m.set(g, []); m.get(g).push(c); }
  const d = DEVUI.key === DEVUI.group ? DEVUI.dir : naturalDir(DEVUI.group);      // the order button flips the groups when the list is sorted by the grouping column
  return [...m.entries()].map(([g, items]) => ({ id: DEVUI.group + ":" + g, g, label: G.label(g), items: devSort(items), o: G.ord(g) }))
    .sort((a, b) => a.o < b.o ? -d : a.o > b.o ? d : 0);
}
// Devices tab (cards / list) traffic light: red = down, yellow = Zabbix reports problems on it, green = up and clean, grey = not monitored / no data
const DEV_RED = "#ef4444", DEV_YEL = "#eab308", DEV_GRN = "#22c55e", DEV_GREY = "#64748b";
const devColor = c => c.status === "down" ? DEV_RED : c.status === "disabled" ? DEV_GREY : c.problems > 0 ? DEV_YEL : c.status === "up" ? DEV_GRN : DEV_GREY;
const devState = c => c.status === "down" ? "Down" : c.status === "disabled" ? "Not monitored" : c.problems > 0 ? "Up, with problems" : c.status === "up" ? "Up, no problems" : "No data";
const probEl = p => el("div", { class: "prob", style: "border-left-color:" + DEV_YEL }, el("span", { class: "sevn" }, SEVN[p.severity] || "Problem"), " ", p.name);      // severity stays readable as text; the colour is just "has a problem"
const stateColor = s => s === "up" ? "var(--ok)" : s === "down" ? "var(--bad)" : "var(--muted)";
function ifLine(i) {
  return el("div", { class: "if", title: i.alias || "" }, el("span", { class: "grow" }, i.name),
    el("div", { class: "bar" }, el("i", { style: `width:${clamp(i.pct || 0, 0, 100)}%;background:${colorFor(i.pct)}` })),
    el("span", {}, fmtPct(i.pct) + " · ↓" + fmtBps(i.in_bps).replace(" ", "") + " ↑" + fmtBps(i.out_bps).replace(" ", "")));
}
function cardBody(c) {      // what a card shows under its name; the heatmap shows the same in its detail dialog
  return [
    el("div", { class: "meta" }, el("span", {}, c.status), c.ip ? el("span", {}, c.ip) : null, c.model ? el("span", { title: "Device model" }, c.model) : null, c.os ? el("span", { title: "Operating system" }, c.os) : null, c.proxy ? el("span", {}, "via " + c.proxy) : null, c.problems ? el("span", { style: "color:" + DEV_YEL }, c.problems + " problem" + (c.problems > 1 ? "s" : "")) : null, c.if_total ? el("span", {}, c.if_total + " interfaces" + (c.if_down ? " (" + c.if_down + " down)" : "")) : null),
    c.top.map(p => probEl(p)),
    c.ifaces.map(ifLine)];
}
function cardEl(c) {
  const col = devColor(c);
  return el("div", { class: "card", style: "border-left-color:" + col },
    el("h3", {}, el("span", { class: "dot", style: "background:" + col, title: devState(c) }), c.name), cardBody(c));
}

// Heatmap: one tile per device, coloured by what the viewer picks (status / busiest interface / worst problem), the value written on the tile
// as well, so the colour is never the only signal. Hover shows the details, click opens them in a dialog.
const busyPct = c => c.ifaces && c.ifaces[0] && c.ifaces[0].pct != null ? c.ifaces[0].pct : null;
const SEV_NC = "#94a3b8";      // a problem of severity "Not classified": lighter than the grey of "no data"
const HEAT_METRICS = {
  status: { t: "Status", color: devColor,
    text: c => c.status === "down" ? "Down" : c.status === "disabled" ? "Not monitored" : c.problems > 0 ? c.problems + (c.problems === 1 ? " problem" : " problems") : c.status === "up" ? "Up" : "No data",
    legend: () => [[DEV_GRN, "Up, no problems"], [DEV_YEL, "Up, with problems"], [DEV_RED, "Down"], [DEV_GREY, "Not monitored / no data"]] },
  busy: { t: "Busiest interface", color: c => colorFor(busyPct(c)), text: c => busyPct(c) == null ? "–" : fmtPct(busyPct(c)),
    legend: () => { const sc = scale(); return [...sc.map(([t, col], i) => [col, sc.length === 1 ? "any usage" : i === 0 ? "< " + sc[1][0] + "%" : i === sc.length - 1 ? t + "%+" : t + "–" + sc[i + 1][0] + "%"]), [DEV_GREY, "No data"]]; } },
  sev: { t: "Worst problem", color: c => c.problems > 0 ? (SEV[c.severity] && c.severity > 0 ? SEV[c.severity] : SEV_NC) : c.status === "up" ? DEV_GRN : DEV_GREY,
    text: c => c.problems > 0 ? (SEVN[c.severity] || "Problem") + (c.problems > 1 ? " ×" + c.problems : "") : c.status === "up" ? "No problems" : c.status === "disabled" ? "Not monitored" : "No data",
    legend: () => [[DEV_GRN, "No problems"], [SEV_NC, SEVN[0]], ...[1, 2, 3, 4, 5].map(i => [SEV[i], SEVN[i]]), [DEV_GREY, "No data"]] },
};
function devTipLines(c) {
  const ids = [c.ip, c.model, c.os, c.proxy ? "via " + c.proxy : ""].filter(Boolean).join(" · ");
  return [c.name, devState(c), ids, ...c.top.map(p => (SEVN[p.severity] || "Problem") + ": " + p.name),
    ...c.ifaces.map(i => i.name + "  " + fmtPct(i.pct) + " · ↓" + fmtBps(i.in_bps).replace(" ", "") + " ↑" + fmtBps(i.out_bps).replace(" ", "")), "Click for details"].filter(Boolean);
}
function heatTile(c, M) {
  const col = M.color(c), txt = M.text(c);
  const t = el("div", { class: "hm", role: "button", tabindex: "0", "aria-label": c.name + ": " + txt, style: "background:" + col + ";color:" + textOn(col) }, el("b", {}, c.name), el("span", {}, txt));
  const open = () => { tipHide(); openModal(c.name, el("div", { class: "card", style: "border-left-color:" + devColor(c) }, cardBody(c)), [btn("Close", closeModal, "primary")]); };
  t.addEventListener("mouseenter", e => tipShow(e, devTipLines(c)));
  t.addEventListener("mousemove", tipMove);
  t.addEventListener("mouseleave", tipHide);
  t.addEventListener("click", open);
  t.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); open(); } });
  return t;
}
function devHeat(groups) {
  const M = HEAT_METRICS[DEVUI.heat] || HEAT_METRICS.status;
  const legend = el("div", { class: "hmlegend", id: "heat-legend" }, el("span", { class: "muted" }, "Colour: " + M.t),
    ...M.legend().map(([col, label]) => el("span", { class: "lg" }, el("i", { style: "background:" + col }), label)));
  return el("div", {}, legend, groups.map(g => [g.id ? gHead(g) : null, g.id && DEVUI.closed.has(g.id) ? null : el("div", { class: "heat" }, g.items.map(c => heatTile(c, M)))]));
}
function gHead(g, span) {      // clickable group heading (table row, or a block above the cards)
  const shut = DEVUI.closed.has(g.id), col = DEVUI.group === "status" ? stateColor(g.g) : null;
  const n = g.n || g.items.length;
  const inner = [el("span", { class: "twisty" }, shut ? "▸" : "▾"), col ? el("span", { class: "dot", style: "background:" + col }) : null, el("b", {}, g.label),
    el("span", { class: "muted" }, " · " + n + (n === 1 ? " device" : " devices") + (!shut && g.items.length < n ? " (" + g.items.length + " on this page)" : "") + (g.cont ? " · continued" : ""))];
  const toggle = () => { if (shut) DEVUI.closed.delete(g.id); else DEVUI.closed.add(g.id); renderMain(); };
  return span ? el("tr", { class: "ghead", title: "Click to collapse / expand", onclick: toggle }, el("td", { colspan: String(span) }, inner))
    : el("div", { class: "ghead", title: "Click to collapse / expand", onclick: toggle }, inner);
}
function devRows(c) {
  const col = devColor(c), open = DEVUI.open.has(c.id), b = c.ifaces[0], top = c.top[0], rows = [];
  rows.push(el("tr", { class: "drow", title: "Click for details", onclick: () => { if (open) DEVUI.open.delete(c.id); else DEVUI.open.add(c.id); renderMain(); } },
    el("td", { class: "nm", style: "border-left:4px solid " + col }, el("span", { class: "twisty" }, open ? "▾" : "▸"), el("span", { class: "dot", style: "background:" + col, title: devState(c) }), c.name),
    el("td", { class: "mono" }, c.ip || "–"),
    el("td", {}, el("span", { class: "st", style: "color:" + stateColor(c.status) + ";border-color:" + stateColor(c.status) }, c.status)),
    el("td", {}, c.proxy || el("span", { class: "muted" }, "–")),
    el("td", { class: "cl", title: c.model || "" }, c.model || el("span", { class: "muted" }, "–")),
    el("td", { class: "cl", title: c.os || "" }, c.os || el("span", { class: "muted" }, "–")),
    el("td", {}, c.problems ? el("span", { class: "pcount", style: "background:" + DEV_YEL }, c.problems) : el("span", { class: "muted" }, "–"),
      top ? el("span", { class: "ptxt", title: top.name }, top.name) : null),
    el("td", {}, c.if_total ? [c.if_total + "", c.if_down ? el("span", { style: "color:var(--bad)" }, " (" + c.if_down + " down)") : null] : el("span", { class: "muted" }, "–")),
    el("td", {}, b ? [el("span", { class: "ifn" }, b.name), el("span", { class: "pbar" }, el("i", { style: `width:${clamp(b.pct || 0, 0, 100)}%;background:${colorFor(b.pct)}` })),
      el("span", { class: "pv" }, fmtPct(b.pct) + " · ↓" + fmtBps(b.in_bps).replace(" ", "") + " ↑" + fmtBps(b.out_bps).replace(" ", ""))] : el("span", { class: "muted" }, "–"))));
  if (open) rows.push(el("tr", { class: "det" }, el("td", { colspan: String(DCOLS.length) },
    c.top.length ? c.top.map(p => probEl(p)) : el("div", { class: "muted" }, "No active problems."),
    c.ifaces.length ? el("div", { class: "dif" }, c.ifaces.map(ifLine), el("div", { class: "hint" }, "Busiest " + c.ifaces.length + " of " + c.if_total + " interfaces" + (canEdit ? " (change the count in Edit › This tab)." : "."))) : el("div", { class: "muted" }, "No interface data."))));
  return rows;
}
function devTable(groups) {
  const head = el("tr", {}, ...DCOLS.map(c => el("th", { title: "Sort by " + c.t.toLowerCase(), onclick: () => {
    if (DEVUI.key === c.k) DEVUI.dir = -DEVUI.dir; else { DEVUI.key = c.k; DEVUI.dir = c.desc ? -1 : 1; }
    resetPages(); saveDevUI(); renderMain(); } }, c.t, DEVUI.key === c.k ? el("span", { class: "ar" }, DEVUI.dir > 0 ? "▲" : "▼") : null)));
  const rows = [];
  for (const g of groups) {
    if (g.id) rows.push(gHead(g, DCOLS.length));
    if (g.id && DEVUI.closed.has(g.id)) continue;
    for (const c of g.items) rows.push(...devRows(c));
  }
  return el("div", { class: "dwrap" }, el("table", { class: "dt" }, el("thead", {}, head), el("tbody", {}, rows)));
}
function devCards(groups) {
  return el("div", {}, groups.map(g => [g.id ? gHead(g) : null, g.id && DEVUI.closed.has(g.id) ? null : el("div", { class: "cards" }, g.items.map(cardEl))]));
}
// Paging: the filtered, sorted and grouped list is cut into pages of DEVUI.per devices. Devices of a collapsed group take no room (so
// collapsing groups gives an overview); a group that runs over a page boundary continues under the same heading on the next page.
function devPage(groups, key) {
  const per = DEVUI.per, weight = g => g.id && DEVUI.closed.has(g.id) ? 0 : g.items.length;
  const total = groups.reduce((n, g) => n + weight(g), 0), pages = Math.max(1, Math.ceil(total / per)), page = clamp(DEVUI.pages[key] || 1, 1, pages);
  DEVUI.pages[key] = page;
  const from = (page - 1) * per, to = Math.min(total, from + per), out = [];
  let pos = 0;
  for (const g of groups) {
    const w = weight(g), a = Math.max(from, pos), b = Math.min(to, pos + w);
    if (w === 0) { if ((pos >= from && pos < to) || (pos >= total && page === pages)) out.push(Object.assign({}, g, { n: g.items.length })); }
    else if (b > a) out.push(Object.assign({}, g, { items: g.items.slice(a - pos, b - pos), n: g.items.length, cont: a > pos }));
    pos += w;
  }
  return { groups: out, total, pages, page, from, to };
}
function setPage(tab, n) { DEVUI.pages[tab.id] = n; renderMain(); const m = $("#main"); if (m) m.scrollTop = 0; }
function pagerEl(pg, tab) {
  if (pg.pages < 2) return null;
  const go = n => () => setPage(tab, n), nums = [...new Set([1, pg.pages, pg.page - 2, pg.page - 1, pg.page, pg.page + 1, pg.page + 2].filter(n => n >= 1 && n <= pg.pages))].sort((a, b) => a - b);
  const kids = [el("button", { type: "button", class: "pg pg-prev", disabled: pg.page <= 1, onclick: go(pg.page - 1) }, "‹ Prev")];
  let last = 0;
  for (const n of nums) {
    if (n - last > 1) kids.push(el("span", { class: "gap" }, "…"));
    kids.push(el("button", { type: "button", class: "pg pg-n" + (n === pg.page ? " on" : ""), "aria-current": n === pg.page ? "page" : false, onclick: go(n) }, String(n)));
    last = n;
  }
  kids.push(el("button", { type: "button", class: "pg pg-next", disabled: pg.page >= pg.pages, onclick: go(pg.page + 1) }, "Next ›"), el("span", { class: "pinfo" }, "Page " + pg.page + " of " + pg.pages));
  return el("div", { class: "pager", role: "navigation", "aria-label": "Pages" }, kids);
}
function drawDevices(host, tab) {
  const all = (view && view.kind === "devices" && view.cards) || [];
  if (!all.length) { host.replaceChildren(el("div", { class: "empty" }, view ? "No devices yet." + (canEdit ? " Click Edit to add hosts or host groups to this tab." : "") : "Loading…")); return; }
  const q = DEVUI.q.trim().toLowerCase(), mode = devMode(tab);
  const issue = c => c.status !== "up" || c.problems > 0 || c.if_down > 0;
  const list = all.filter(c => (!DEVUI.only || issue(c)) && (!q || [c.name, c.ip, c.proxy, c.status, ...c.top.map(p => p.name)].join("\n").toLowerCase().includes(q)));
  const ae = document.activeElement, keep = ae && ae.id === "dev-q" ? [ae.selectionStart, ae.selectionEnd] : null;     // the page re-renders on every poll: keep typing intact
  const qi = el("input", { id: "dev-q", type: "search", placeholder: "Filter by name, IP, proxy, problem…", spellcheck: "false", autocomplete: "off" });
  qi.value = DEVUI.q; qi.addEventListener("input", () => { DEVUI.q = qi.value; resetPages(); renderMain(); });
  const onlyCb = el("input", { type: "checkbox", style: "width:auto" }); onlyCb.checked = DEVUI.only;
  onlyCb.addEventListener("change", () => { DEVUI.only = onlyCb.checked; resetPages(); saveDevUI(); renderMain(); });
  const gsel = el("select", { id: "dev-group", title: "Group the devices" }, el("option", { value: "none" }, "None"), ...Object.entries(GROUPS).map(([k, v]) => el("option", { value: k }, v.t)));
  gsel.value = DEVUI.group;
  gsel.addEventListener("change", () => {
    DEVUI.group = gsel.value; DEVUI.closed.clear(); resetPages();
    if (GROUPS[DEVUI.group]) { DEVUI.key = DEVUI.group; DEVUI.dir = naturalDir(DEVUI.group); }      // grouping by X also orders by X, most-first for "Most problems"
    saveDevUI(); renderMain(); });
  const dirBtn = el("button", { id: "dev-dir", type: "button", title: "Reverse the order", onclick: () => { DEVUI.dir = -DEVUI.dir; resetPages(); saveDevUI(); renderMain(); } }, DEVUI.dir > 0 ? "▲ Ascending" : "▼ Descending");
  const pg = devPage(devGroups(list), tab.id);
  const perSel = el("select", { id: "dev-per", title: "Devices per page" }, ...PER_CHOICES.map(n => el("option", { value: String(n) }, String(n))));
  perSel.value = String(DEVUI.per);
  perSel.addEventListener("change", () => { DEVUI.per = +perSel.value; resetPages(); saveDevUI(); renderMain(); });
  const nDown = all.filter(c => c.status === "down").length, nProb = all.filter(c => c.problems > 0).length, whole = list.length === all.length;
  const sum = (pg.pages > 1 ? "Showing " + (pg.from + 1) + "–" + pg.to + " of " + pg.total + (pg.total !== list.length ? " in open groups" : whole ? " devices" : " matching") + (whole ? "" : " · " + all.length + " devices")
    : whole ? all.length + " devices" : list.length + " of " + all.length + " devices") + (nDown ? " · " + nDown + " down" : "") + (nProb ? " · " + nProb + " with problems" : "");
  const seg = el("span", { class: "seg" }, el("button", { class: mode === "list" ? "on" : "", type: "button", onclick: () => setDevMode(tab, "list") }, "List"),
    el("button", { class: mode === "cards" ? "on" : "", type: "button", onclick: () => setDevMode(tab, "cards") }, "Cards"),
    el("button", { class: mode === "heatmap" ? "on" : "", type: "button", id: "dev-mode-heatmap", onclick: () => setDevMode(tab, "heatmap") }, "Heatmap"));
  let heatSel = null, sortSel = null;
  if (mode === "heatmap") {      // the heatmap's own choices: what the colour means, and (with the order button) which devices come first
    heatSel = el("select", { id: "dev-heat", title: "What the tiles are coloured by" }, ...HEAT_KEYS.map(k => el("option", { value: k }, HEAT_METRICS[k].t)));
    heatSel.value = DEVUI.heat;
    heatSel.addEventListener("change", () => { DEVUI.heat = heatSel.value; saveDevUI(); renderMain(); });
    sortSel = el("select", { id: "dev-sort", title: "Order the devices by" }, ...DCOLS.map(c => el("option", { value: c.k }, c.t)));
    sortSel.value = DEVUI.key;
    sortSel.addEventListener("change", () => { const c = DCOLS.find(x => x.k === sortSel.value); DEVUI.key = c.k; DEVUI.dir = c.desc ? -1 : 1; resetPages(); saveDevUI(); renderMain(); });
  }
  const nav = pg.pages > 1 ? el("span", { class: "seg" }, el("button", { id: "dev-prev", type: "button", title: "Previous page", disabled: pg.page <= 1, onclick: () => setPage(tab, pg.page - 1) }, "‹"),
    el("button", { id: "dev-next", type: "button", title: "Next page", disabled: pg.page >= pg.pages, onclick: () => setPage(tab, pg.page + 1) }, "›")) : null;
  const bar = el("div", { class: "devbar" }, qi, el("label", { class: "inl2" }, onlyCb, " Only with issues"),
    heatSel ? el("label", { class: "inl2" }, el("span", { class: "muted" }, "Colour by"), heatSel) : null,
    el("label", { class: "inl2" }, el("span", { class: "muted" }, "Group by"), gsel),
    sortSel ? el("label", { class: "inl2" }, el("span", { class: "muted" }, "Sort by"), sortSel) : null, dirBtn,
    el("label", { class: "inl2" }, el("span", { class: "muted" }, "Per page"), perSel), el("span", { class: "muted grow" }, sum), nav, seg);
  const note = view.truncated ? el("div", { class: "box warnb dnote", id: "dev-cut" }, "Only the first " + view.max_hosts + " monitored hosts are loaded, but Zabbix has more. Raise max_hosts (config.json) to list them all.") : null;
  const body = !list.length ? el("div", { class: "empty" }, "No devices match the filter.") : mode === "list" ? devTable(pg.groups) : mode === "heatmap" ? devHeat(pg.groups) : devCards(pg.groups);
  host.replaceChildren(...[bar, note, body, list.length ? pagerEl(pg, tab) : null].filter(Boolean));      // (replaceChildren would print a null as the text "null")
  if (keep) { qi.focus(); try { qi.setSelectionRange(keep[0], keep[1]); } catch (e) {} }
}

// ---------- refresh / reconnect
let refreshing = false;
async function refreshNow() {      // new Zabbix session + fresh data right now (otherwise the page follows the 30 s poll)
  if (refreshing) return;
  refreshing = true;
  const b = $("#btn-refresh"), c = $("#chip");
  b.classList.add("spin"); b.disabled = true; c.textContent = "refreshing…"; c.className = "chip";
  try { await api("/api/refresh", { body: {} }); } catch (e) { /* the status chip reports the problem below */ }
  try { await loadView(); } finally { refreshing = false; b.classList.remove("spin"); b.disabled = false; }
}
$("#btn-refresh").addEventListener("click", refreshNow);

// ---------- render
function renderStatus() {
  const c = $("#chip");
  if (!status) { c.textContent = "connecting…"; c.className = "chip"; return; }
  const bad = !status.ok;
  $("#btn-refresh").classList.toggle("alert", bad);
  c.className = "chip " + (bad ? "bad" : "ok");
  const name = status.conn && !DEMO ? status.conn + " · " : "Zabbix ";
  c.textContent = bad ? "⚠ " + (status.error || "error") : name + (status.version || "") + " · " + status.hosts + " hosts · " + ago(status.last_ok);
  c.title = (bad ? (status.last_ok ? "Showing data from " + ago(status.last_ok) + ". " : "No data yet. ") : "") + "Click to manage connections (C)";
}
function renderHeader() {
  $("#tabs").replaceChildren(...L.tabs.map(t => el("button", { class: "tab" + (t.id === tabId ? " on" : ""), onclick: () => selectTab(t.id, true) }, t.name)));
  $("#pl").hidden = !L.playlists.length;
  const s = $("#pl-select");
  s.replaceChildren(el("option", { value: "" }, "Playlist…"), ...L.playlists.map(p => el("option", { value: p.id }, p.name)));
  s.value = play.id || "";
  $("#btn-edit").hidden = !canEdit; $("#btn-edit").classList.toggle("on", editing);
  $("#viewonly").hidden = canEdit; $("#demo").hidden = !DEMO;
  $("#range-wrap").hidden = !(curTab() && curTab().type === "map");      // the graph range only matters on a weathermap
  renderPlay(); renderCta();
}
function renderMain() {
  const keep = (!tip.hidden && !dragging && !ctxEl) ? tipPt : null;      // the map is redrawn on every poll: a card that is open comes back under the same pointer instead of vanishing
  tipHide();
  const main = $("#main"), tab = curTab();
  if (!tab) { main.replaceChildren(el("div", { class: "empty" }, "No tabs yet." + (canEdit ? " Click Edit to create one." : ""))); return; }
  if (tab.type === "map") drawMap(main, tab); else drawDevices(main, tab);
  if (keep) {
    const at = document.elementFromPoint(keep.clientX, keep.clientY), t = at && at.closest ? at.closest("g.node, polyline[data-lid], .hm") : null;
    if (t) t.dispatchEvent(new MouseEvent("mouseenter", { clientX: keep.clientX, clientY: keep.clientY }));
  }
}
function render() { renderHeader(); renderStatus(); renderMain(); renderDrawer(); }
function renderPlay() {
  const p = curPlaylist();
  $("#pl-play").textContent = play.running ? "⏸" : "▶";
  $("#pl-fill").style.width = (play.running && play.total) ? (100 * (1 - play.remaining / play.total)).toFixed(0) + "%" : "0";
  const t = curTab();
  $("#hud-text").textContent = (t ? t.name : "") + (play.running && p ? "  ·  " + p.name + "  ·  " + play.remaining + "s" : "");
}

// ---------- navigation, playlists, kiosk
function setHash() { const h = new URLSearchParams(); if (tabId) h.set("tab", tabId); if (play.running && play.id) h.set("play", play.id); if (document.body.classList.contains("kiosk")) h.set("kiosk", "1"); if (DISP.range !== DISP.defRange) h.set("range", DISP.range); if (DISP.refresh !== DISP.defRefresh) h.set("refresh", DISP.refresh); history.replaceState(null, "", "#" + h.toString()); }
function selectTab(id, manual) {
  if (manual && play.running) { play.running = false; }
  tabId = id; view = null; sel = null; setHash(); render(); loadView();
}
function playStart(id) {
  const p = L.playlists.find(x => x.id === id);
  if (!p || !p.items.length) return;
  play.id = id; play.idx = -1; play.running = true; playNext(1);
}
function playNext(dir) {
  const p = curPlaylist(); if (!p || !p.items.length) return;
  const n = p.items.length;
  let i = play.idx;
  for (let k = 0; k < n; k++) { i = (i + dir + n) % n; if (L.tabs.some(t => t.id === p.items[i].tab)) break; }
  if (!p.loop && ((dir > 0 && i <= play.idx && play.idx >= 0) || false)) { play.running = false; renderPlay(); return; }
  play.idx = i; play.total = play.remaining = p.items[i].seconds;
  tabId = p.items[i].tab; view = null; sel = null; setHash(); render(); loadView();
}
function tick() {
  if (play.running) { play.remaining -= 1; if (play.remaining <= 0) playNext(1); }
  renderPlay();
}
$("#pl-select").addEventListener("change", e => { if (e.target.value) playStart(e.target.value); else { play.running = false; play.id = null; renderHeader(); setHash(); } });
$("#pl-play").addEventListener("click", () => { if (!play.id) { const f = L.playlists[0]; if (f) playStart(f.id); return; } play.running = !play.running; if (play.running && play.remaining <= 0) playNext(1); renderPlay(); setHash(); });
$("#pl-next").addEventListener("click", () => { if (!play.id && L.playlists[0]) { playStart(L.playlists[0].id); return; } playNext(1); });
$("#pl-prev").addEventListener("click", () => { if (!play.id && L.playlists[0]) { playStart(L.playlists[0].id); return; } playNext(-1); });
function setKiosk(on) { document.body.classList.toggle("kiosk", on); if (on) { editing = false; document.body.classList.remove("editing"); } setHash(); render(); }
$("#btn-kiosk").addEventListener("click", () => setKiosk(true));
$("#hud-back").addEventListener("click", () => setKiosk(false));      // the wall-display way out for screens without a keyboard (Esc does the same)
$("#btn-full").addEventListener("click", () => { if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen().catch(() => {}); });
$("#btn-edit").addEventListener("click", () => { editing = !editing; document.body.classList.toggle("editing", editing); sel = null; render(); });
addEventListener("keydown", e => {
  if (!$("#conn").hidden || !$("#mdl").hidden || ctxEl || /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName) || e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === "k") setKiosk(!document.body.classList.contains("kiosk"));
  else if (k === "f") $("#btn-full").click();
  else if (k === "r") refreshNow();
  else if (k === "c" && !document.body.classList.contains("kiosk")) openConnections();
  else if (k === "e" && canEdit && !document.body.classList.contains("kiosk")) $("#btn-edit").click();
  else if (k === "escape" && document.body.classList.contains("kiosk")) setKiosk(false);
  else if (e.key === " " && L.playlists.length) { e.preventDefault(); $("#pl-play").click(); }
  else if (e.key === "ArrowRight" && play.id) playNext(1);
  else if (e.key === "ArrowLeft" && play.id) playNext(-1);
});
let idleT = null;
addEventListener("mousemove", () => { document.body.classList.remove("idle"); clearTimeout(idleT); idleT = setTimeout(() => document.body.classList.add("idle"), 3000); });

// ---------- data
async function loadView() {
  if (dragging) return;
  const tab = curTab();
  if (!tab) { renderStatus(); return; }
  if (dirty) await doSave();   // the server must know about a brand-new tab before we ask it for that tab's data
  try { const v = await api("/api/view?tab=" + encodeURIComponent(tab.id) + "&every=" + DISP.refresh + "&cid=" + CID); if (tab.id !== tabId || dragging) return; view = v; status = v.status; }
  catch (e) { status = { ok: false, error: e.message, last_ok: 0 }; }
  renderStatus(); renderMain();
}

// ---------- editor
let saveT = null, saveMsg = "", dirty = false;
function setSaved(m, bad) { saveMsg = m; const s = $("#saved"); if (s) { s.textContent = m; s.className = bad ? "err" : "muted"; } }
function scheduleSave() { dirty = true; setSaved("Unsaved changes…"); clearTimeout(saveT); saveT = setTimeout(doSave, 700); }
async function doSave() {
  clearTimeout(saveT);
  try { setSaved("Saving…"); await api("/api/layout", { body: { layout: L } }); dirty = false; setSaved("Saved"); setTimeout(loadView, 2500); }
  catch (e) { setSaved("Save failed: " + e.message, true); }
}
function changed(structural) { scheduleSave(); if (structural) render(); else { renderHeader(); renderMain(); } }
function select(s) { sel = s; renderMain(); renderDrawer(); }
const section = (title, ...kids) => el("details", { class: "sec", open: "" }, el("summary", {}, title), el("div", {}, kids));
const fld = (label, input) => el("label", { class: "fld" }, el("span", {}, label), input);
function inp(value, onchange, attrs = {}) { const i = el("input", attrs); i.value = value == null ? "" : value; i.addEventListener("change", () => onchange(i.value)); return i; }
const btn = (text, onclick, cls = "") => el("button", { class: cls, onclick, type: "button" }, text);

const ifCache = new Map();
function getIfaces(host) {
  if (!ifCache.has(host)) ifCache.set(host, api("/api/interfaces?host=" + encodeURIComponent(host)).then(r => r.interfaces).catch(e => { ifCache.delete(host); throw e; }));
  return ifCache.get(host);
}
function ifaceControl(host, value, onchange) {
  const wrap = el("span", { class: "ifc" }), s = el("select", {}, el("option", { value: "" }, "loading…"));
  s.addEventListener("change", () => onchange(s.value));
  wrap.append(s);
  getIfaces(host).then(list => {
    s.replaceChildren(el("option", { value: "" }, "— interface —"), ...list.map(i => el("option", { value: i.name }, i.name + (i.alias ? " — " + i.alias : ""))));
    if (value && !list.some(i => i.name === value)) s.append(el("option", { value }, value + " (not found)"));
    s.value = value || "";
  }).catch(err => { const t = el("input", { placeholder: "interface name, e.g. Gi0/0/1" }); t.value = value || ""; t.addEventListener("change", () => onchange(t.value)); wrap.replaceChildren(t, el("small", { class: "err" }, err.message)); });
  return wrap;
}
function searchBox(kind, placeholder, onpick) {
  const i = el("input", { placeholder }), res = el("div", { class: "results" });
  let t = null;
  i.addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(async () => {
      const q = i.value.trim();
      if (q.length < 2) { res.replaceChildren(); return; }
      try { const r = await api("/api/" + kind + "?q=" + encodeURIComponent(q)); res.replaceChildren(...(r.items.length ? r.items.map(it => el("div", { class: "hit", onclick: () => { onpick(it); i.value = ""; res.replaceChildren(); } }, it.name)) : [el("div", { class: "muted pad" }, "No matches")])); }
      catch (e) { res.replaceChildren(el("div", { class: "err pad" }, e.message)); }
    }, 300);
  });
  return el("div", { class: "search" }, i, res);
}

function secTabs() {
  const rows = L.tabs.map((t, i) => el("div", { class: "row" + (t.id === tabId ? " cur" : "") },
    el("span", { class: "grow link", onclick: () => selectTab(t.id, true) }, t.name, " ", el("small", { class: "muted" }, t.type)),
    btn("↑", () => moveTab(i, -1), "x"), btn("↓", () => moveTab(i, 1), "x"), btn("✕", () => delTab(i), "x")));
  const name = el("input", { placeholder: "New tab name" }), type = el("select", {}, el("option", { value: "map" }, "Weathermap"), el("option", { value: "devices" }, "Devices"));
  return section("Tabs", rows, el("div", { class: "row" }, name, type, btn("Add", () => addTab(name.value, type.value), "primary")));
}
function addTab(name, type) {
  const id = newId("t");
  if (type === "map") { const mid = newId("m"); L.maps[mid] = { name: name || "Map", width: 1600, height: 900, nodes: [], links: [] }; L.tabs.push({ id, name: name || "Map", type: "map", map: mid }); }
  else L.tabs.push({ id, name: name || "Devices", type: "devices", hosts: [], hostnames: {}, groups: [], ifaces: 4, style: "list" });
  changed(false); selectTab(id, true);
}
function moveTab(i, d) { const j = i + d; if (j < 0 || j >= L.tabs.length) return; [L.tabs[i], L.tabs[j]] = [L.tabs[j], L.tabs[i]]; changed(true); }
function delTab(i) {
  const t = L.tabs[i]; if (!confirm("Delete tab \"" + t.name + "\"?")) return;
  L.tabs.splice(i, 1);
  if (t.type === "map" && !L.tabs.some(x => x.map === t.map)) delete L.maps[t.map];
  for (const p of L.playlists) p.items = p.items.filter(it => it.tab !== t.id);
  if (tabId === t.id) { tabId = L.tabs[0] ? L.tabs[0].id : null; view = null; loadView(); }
  changed(true);
}
function secTab(tab) {
  const kids = [fld("Tab name", inp(tab.name, v => { tab.name = v.slice(0, 40) || "Tab"; changed(false); }))];
  if (tab.type === "map") {
    const m = L.maps[tab.map];
    kids.push(el("div", { class: "row" }, fld("Canvas width", inp(m.width, v => { m.width = clamp(parseInt(v) || 1600, 400, 8000); changed(false); }, { type: "number" })), fld("height", inp(m.height, v => { m.height = clamp(parseInt(v) || 900, 300, 8000); changed(false); }, { type: "number" }))));
  } else {
    kids.push(el("div", { class: "hint" }, "Shows every host below, all hosts in the listed host groups, and (optionally) every monitored host."));
    const sty = el("select", {}, el("option", { value: "list" }, "List (table)"), el("option", { value: "cards" }, "Cards"), el("option", { value: "heatmap" }, "Heatmap (tiles)")); sty.value = DEV_MODES.includes(tab.style) ? tab.style : "cards";
    sty.addEventListener("change", () => { tab.style = sty.value; try { localStorage.removeItem("channelview-devview:" + tab.id); } catch (e) {} changed(false); });
    kids.push(fld("Show devices as", sty));
    const allCb = el("input", { type: "checkbox", style: "flex:none;width:auto" }); allCb.checked = !!tab.all;
    allCb.addEventListener("change", () => { tab.all = allCb.checked; changed(true); setTimeout(loadView, 2500); });
    kids.push(el("label", { class: "row" }, allCb, el("span", {}, "Show all monitored hosts")));
    if (tab.all) kids.push(el("div", { class: "hint" }, "Lists every enabled host this API user may read - no limit. Long lists are paged: use “Per page” above the list. Hosts disabled in Zabbix are not monitored, so they are not listed."));
    kids.push(searchBox("hosts", "Add host (type 2+ letters)…", it => { if (!tab.hosts.includes(it.id)) { tab.hosts.push(it.id); tab.hostnames[it.id] = it.name; changed(true); setTimeout(loadView, 2500); } }));
    for (const h of tab.hosts) kids.push(el("div", { class: "row" }, el("span", { class: "grow" }, tab.hostnames[h] || h), btn("✕", () => { tab.hosts = tab.hosts.filter(x => x !== h); delete tab.hostnames[h]; changed(true); }, "x")));
    kids.push(searchBox("groups", "Add host group…", it => { if (!tab.groups.includes(it.id)) { tab.groups.push(it.id); changed(true); setTimeout(loadView, 2500); } }));
    for (const g of tab.groups) kids.push(el("div", { class: "row" }, el("span", { class: "grow" }, "group: " + g), btn("✕", () => { tab.groups = tab.groups.filter(x => x !== g); changed(true); }, "x")));
    kids.push(fld("Interfaces shown per device (busiest first)", inp(tab.ifaces, v => { tab.ifaces = clamp(parseInt(v) || 0, 0, 12); changed(false); setTimeout(loadView, 300); }, { type: "number" })));
  }
  return section("This tab", kids);
}
const lf = { a: "", ifA: "", b: "", ifB: "", name: "", mbps: "" };
function secMap(tab) {
  const m = L.maps[tab.map], nodeById = id => m.nodes.find(n => n.id === id);
  const label = n => n.label || n.hostname || n.host;
  const add = searchBox("hosts", "Add device from Zabbix (type 2+ letters)…", it => {
    const k = m.nodes.length; m.nodes.push({ id: newId("n"), host: it.id, hostname: it.name, label: "", x: 200 + (k % 6) * 220, y: 140 + Math.floor(k / 6) * 140 });
    changed(true); setTimeout(loadView, 2500);
  });
  const nodeSel = (key, ifKey) => { const s = el("select", {}, el("option", { value: "" }, "— device —"), ...m.nodes.map(n => el("option", { value: n.id }, label(n)))); s.value = lf[key]; s.addEventListener("change", () => { lf[key] = s.value; lf[ifKey] = ""; renderDrawer(); }); return s; };
  const nA = nodeById(lf.a), nB = nodeById(lf.b);
  const form = el("div", {},
    fld("Device A", nodeSel("a", "ifA")), nA ? fld("Interface on A", ifaceControl(nA.host, lf.ifA, v => { lf.ifA = v; })) : null,
    fld("Device B", nodeSel("b", "ifB")), nB ? fld("Interface on B", ifaceControl(nB.host, lf.ifB, v => { lf.ifB = v; })) : null,
    fld("Link name (optional)", inp(lf.name, v => { lf.name = v; })),
    fld("Capacity override in Mbps (optional – for rate-limited circuits)", inp(lf.mbps, v => { lf.mbps = v; }, { type: "number" })),
    btn("Add link", () => {
      if (!lf.a || !lf.b || lf.a === lf.b || !lf.ifA || !lf.ifB) { alert("Pick two different devices and an interface on each."); return; }
      m.links.push({ id: newId("L"), name: lf.name, a: { node: lf.a, if: lf.ifA }, b: { node: lf.b, if: lf.ifB }, bandwidth_bps: lf.mbps ? parseFloat(lf.mbps) * 1e6 : null });
      Object.assign(lf, { ifA: "", ifB: "", name: "", mbps: "" }); changed(true);
    }, "primary"));
  const bulk = (text, fn) => btn(text, () => { for (const l of m.links) fn(l); changed(true); }, "");
  const secs = [section("Add device", add, el("div", { class: "hint" }, "Click a device or link on the map to edit it. Drag devices to move them (snaps to a 10px grid); every link has one dot in the middle (click a link to see it big) - drag it to bend the link.")), section("Add link", form)];
  if (m.links.length) secs.push(section("Interface tags on all links", el("div", { class: "row wrap" },
    bulk("Show all", l => setLinkTags(l, "if_show", true)), bulk("Hide all", l => setLinkTags(l, "if_show", false)),
    bulk("All above", l => setLinkTags(l, "if_pos", "above")), bulk("All below", l => setLinkTags(l, "if_pos", "below"))),
    el("div", { class: "hint" }, "The interface names beside each end of a link. For one link only: right-click it, or select it.")));
  if (m.nodes.length) secs.push(section("Notifications on all devices", el("div", { class: "row wrap" },
    btn("All on", () => { for (const n of m.nodes) setNodeNotify(n, true); changed(true); }, ""), btn("All off", () => { for (const n of m.nodes) setNodeNotify(n, false); changed(true); }, "")),
    el("div", { class: "hint" }, "Notifications are the round problem count on a device. Off hides it (a dashed ring marks such devices while you edit; the map itself shows nothing). For one device only: right-click it, or select it.")));
  const vert = !!(m.legend && m.legend.dir === "v"), lgb = (text, on, fn) => el("button", { type: "button", class: on ? "on" : "", "aria-pressed": String(on), onclick: fn }, text);
  secs.push(section("Usage meter", el("div", { class: "row wrap" },
    lgb("Horizontal", !vert, () => { setLegend(m, { dir: "h" }); changed(true); }), lgb("Vertical", vert, () => { setLegend(m, { dir: "v" }); changed(true); }),
    btn("Reset", () => { delete m.legend; changed(true); }, "")),
    el("div", { class: "hint" }, "The colour key for link usage. Drag it by its bar on the map to move it (it keeps its place on any screen size and in kiosk); the buttons on its bar do the same as these. Reset = bottom-left, horizontal.")));
  if (sel) {
    if (sel.type === "node" && nodeById(sel.id)) {
      const n = nodeById(sel.id);
      secs.push(section("Selected device", el("div", { class: "hint" }, n.hostname + " (host " + n.host + ")"), fld("Label (blank = Zabbix name)", inp(n.label, v => { n.label = v.slice(0, 60); changed(false); })),
        notifyField(n),
        btn("Remove device and its links", () => askDeleteNode(m, n))));
    } else if (sel.type === "link") {
      const l = m.links.find(x => x.id === sel.id);
      if (l) { const A = nodeById(l.a.node), B = nodeById(l.b.node);
        secs.push(section("Selected link", fld("Link name", inp(l.name, v => { l.name = v.slice(0, 80); changed(false); })),
          fld("Interface on " + label(A), ifaceControl(A.host, l.a.if, v => { l.a.if = v; changed(false); setTimeout(loadView, 2500); })),
          fld("Interface on " + label(B), ifaceControl(B.host, l.b.if, v => { l.b.if = v; changed(false); setTimeout(loadView, 2500); })),
          fld("Capacity override (Mbps, blank = use Zabbix speed)", inp(l.bandwidth_bps ? l.bandwidth_bps / 1e6 : "", v => { l.bandwidth_bps = v ? parseFloat(v) * 1e6 : null; changed(false); setTimeout(loadView, 300); }, { type: "number" })),
          fld("Bend points", el("div", { class: "row" }, el("span", { class: "muted", style: "flex:1" }, viaOf(l).length ? "1 point, placed by hand" : "1 point, in the middle"), viaOf(l).length ? btn("Straighten", () => straighten(m, l)) : null)),
          el("div", { class: "hint" }, "Every link has one bend point, in the middle of the link. Drag the dot to route the link around other devices; Straighten puts it back in the middle."),
          tagFields(l),
          btn("Delete link", () => askDeleteLink(m, l)))); }
    }
  }
  return secs;
}
function notifyField(n) {      // "Notifications" control of one device: the problem count on the map, on or off
  const on = el("input", { type: "checkbox", style: "width:auto;flex:none" }); on.checked = nodeNotify(n);
  on.addEventListener("change", () => { setNodeNotify(n, on.checked); changed(false); });
  return fld("Notifications (problem count on the device)", el("div", { class: "row" }, el("label", { style: "display:inline-flex;align-items:center;gap:6px;flex:none;white-space:nowrap;cursor:pointer" }, on, " show")));
}
function tagFields(l) {      // "Interface tags" controls of one link: shown or not, and on which side of the line
  const on = el("input", { type: "checkbox", style: "width:auto;flex:none" }), pos = el("select", {}, el("option", { value: "above" }, "Above the line"), el("option", { value: "below" }, "Below the line"));
  on.checked = linkShowIf(l); pos.value = effIfPos(l); pos.disabled = !on.checked;
  on.addEventListener("change", () => { setLinkTags(l, "if_show", on.checked); changed(true); });
  pos.addEventListener("change", () => { setLinkTags(l, "if_pos", pos.value); changed(false); });
  return fld("Interface tags (names beside each end)", el("div", { class: "row" }, el("label", { style: "display:inline-flex;align-items:center;gap:6px;flex:none;white-space:nowrap;cursor:pointer" }, on, " show"), pos));
}
function secPlaylists() {
  const tabOpts = () => L.tabs.map(t => el("option", { value: t.id }, t.name));
  const blocks = L.playlists.map(p => {
    const items = p.items.map((it, i) => { const s = el("select", {}, tabOpts()); s.value = it.tab; s.addEventListener("change", () => { it.tab = s.value; changed(false); });
      return el("div", { class: "row" }, s, inp(it.seconds, v => { it.seconds = clamp(parseInt(v) || 20, 3, 3600); changed(false); }, { type: "number", style: "max-width:70px", title: "seconds" }), btn("✕", () => { p.items.splice(i, 1); changed(true); }, "x")); });
    const lp = el("input", { type: "checkbox", style: "flex:none" }); lp.checked = p.loop; lp.addEventListener("change", () => { p.loop = lp.checked; changed(false); });
    return el("div", { style: "border:1px solid var(--line);border-radius:8px;padding:8px;margin:8px 0" },
      el("div", { class: "row" }, inp(p.name, v => { p.name = v.slice(0, 40) || "Playlist"; changed(true); }), btn("Play", () => playStart(p.id)), btn("✕", () => { if (confirm("Delete playlist \"" + p.name + "\"?")) { L.playlists = L.playlists.filter(x => x !== p); if (play.id === p.id) { play.id = null; play.running = false; } changed(true); } }, "x")),
      items, el("div", { class: "row" }, btn("+ tab", () => { if (L.tabs.length) { p.items.push({ tab: L.tabs[0].id, seconds: 20 }); changed(true); } }), el("label", { class: "muted", style: "flex:1" }, lp, " loop")));
  });
  return section("Playlists", blocks, btn("New playlist", () => { L.playlists.push({ id: newId("p"), name: "Playlist " + (L.playlists.length + 1), loop: true, items: L.tabs.map(t => ({ tab: t.id, seconds: 20 })) }); changed(true); }, "primary"),
    el("div", { class: "hint" }, "Playlists rotate through tabs for wall displays. Open the page with #play=<id>&kiosk=1 to start one automatically (add &per=200 to list more devices per page); press K for kiosk, Esc to leave."));
}
function renderDrawer() {
  const d = $("#drawer");
  if (!editing || !L) { d.hidden = true; return; }
  d.hidden = false;
  const tab = curTab();
  const parts = [el("div", { class: "drawer-h" }, el("b", {}, "Editor"), btn("Connections…", () => openConnections()), el("span", { id: "saved", class: "muted" }, saveMsg)), secTabs()];
  if (tab) parts.push(secTab(tab));
  if (tab && tab.type === "map") parts.push(...secMap(tab));
  parts.push(secPlaylists());
  d.replaceChildren(...parts);
}

// ---------- map: right-click menu + confirmation windows
let ctxEl = null, ctxHold = 0;      // ctxHold: re-drawing the map right after a Shape / Size click can fire a scroll event - that must not close the menu
function endCtx() { if (ctxEl) { ctxEl.remove(); ctxEl = null; } }
function mapItem(target) {
  const tab = curTab(), m = tab && tab.type === "map" ? L.maps[tab.map] : null;
  if (!m) return null;
  const nodeById = id => m.nodes.find(n => n.id === id);
  if (target.type === "node") { const n = nodeById(target.id); return n ? { m, n } : null; }
  const l = m.links.find(x => x.id === target.id);
  return l && nodeById(l.a.node) && nodeById(l.b.node) ? { m, l, A: nodeById(l.a.node), B: nodeById(l.b.node), i: target.index } : null;
}
const nodeName = n => n.label || n.hostname || n.host;
function ctxOpen(e, target) {
  if (!canEdit || !editing || document.body.classList.contains("kiosk")) return;      // only in edit mode - otherwise the browser's own menu stays
  const it = mapItem(target);
  if (!it) return;
  e.preventDefault(); e.stopPropagation(); tipHide(); endCtx();
  select(target.type === "via" ? { type: "link", id: target.id } : target);      // the right-clicked item becomes the selected one, so it stays highlighted while its menu / window is open
  const title = target.type === "node" ? nodeName(it.n) : (it.l.name || nodeName(it.A) + " ↔ " + nodeName(it.B));
  const item = (icon, text, fn, cls) => el("button", { type: "button", role: "menuitem", class: cls || "", onclick: ev => { ev.stopPropagation(); endCtx(); fn(); } }, el("span", { class: "ci" }, icon), text);
  // Shape / Size apply at once (no confirmation - nothing is lost) and the menu stays open so several can be tried; the pressed button shows the current choice.
  const cur = { shape: () => nodeShape(it.n), size: () => nodeSize(it.n), notify: () => nodeNotify(it.n) ? "on" : "off", if_show: () => linkShowIf(it.l) ? "show" : "hide", if_pos: () => effIfPos(it.l) };
  const styleRow = (label, key, choices) => el("div", { class: "ctx-row" }, el("span", { class: "ctx-l" }, label),
    el("div", { class: "opts", role: "group", "aria-label": label }, choices.map(([val, txt, name]) => el("button", {
      type: "button", class: "opt", title: name, "aria-label": label + ": " + name, "data-key": key, "data-val": val, "aria-pressed": String(cur[key]() === val),
      onclick: ev => {
        ev.stopPropagation(); ctxHold = performance.now() + 500;
        if (key === "if_show") setLinkTags(it.l, key, val === "show"); else if (key === "if_pos") setLinkTags(it.l, key, val);
        else if (key === "notify") setNodeNotify(it.n, val === "on");
        else if (val === (key === "shape" ? "square" : "medium")) delete it.n[key]; else it.n[key] = val;      // defaults are not stored
        changed(true);
        for (const b of ctxEl.querySelectorAll(".opt")) b.setAttribute("aria-pressed", String(cur[b.dataset.key]() === b.dataset.val));
      },
    }, txt))));
  const isNode = target.type === "node", isVia = target.type === "via", sep = () => el("div", { class: "ctx-sep" });
  const manual = !isNode && viaOf(it.l).length > 0, undo = () => straighten(it.m, it.l);
  ctxEl = el("div", { class: "ctx", role: "menu", "aria-label": "Actions" },
    el("div", { class: "ctx-h" }, el("b", {}, title), isNode ? "Device" : isVia ? "Bend point" : "Link"),
    isVia ? item("╱", "Straighten link (bend point back to the middle)", undo)
      : item("✎", "Modify…", () => isNode ? modifyNode(it.m, it.n) : modifyLink(it.m, it.l)),
    isNode ? [sep(), styleRow("Shape", "shape", [["square", "■", "Square"], ["circle", "●", "Circle"], ["triangle", "▲", "Triangle"]]),
      styleRow("Size", "size", [["small", "S", "Small"], ["medium", "M", "Medium"], ["large", "L", "Large"], ["xlarge", "XL", "Extra large"]]),
      styleRow("Notifications", "notify", [["on", "On", "Show the problem count on this device"], ["off", "Off", "Hide the problem count on this device"]]), sep()] : null,
    !isNode && !isVia ? [manual ? item("╱", "Straighten link (bend point back to the middle)", undo) : null, manual ? sep() : null,
      styleRow("Interface tags", "if_show", [["show", "Show", "Show the interface names on this link"], ["hide", "Hide", "Hide the interface names on this link"]]),
      styleRow("Tag side", "if_pos", [["above", "Above", "Interface names above the line"], ["below", "Below", "Interface names below the line"]]), sep()] : null,
    isVia ? null : item("🗑", "Delete…", () => isNode ? askDeleteNode(it.m, it.n) : askDeleteLink(it.m, it.l), "danger"));
  document.body.append(ctxEl);
  const r = ctxEl.getBoundingClientRect();
  let left = e.clientX;
  const nodeEl = isNode ? document.querySelector('#main svg.map g.node[data-id="' + target.id + '"]') : null;
  if (nodeEl) {      // beside the device, not on top of it, so a Shape / Size change can be watched while the menu is open
    const nb = nodeEl.getBoundingClientRect(), roomR = innerWidth - nb.right - 14 >= r.width, roomL = nb.left - 14 >= r.width;
    if (e.clientX >= (nb.left + nb.right) / 2 ? roomR : !roomL && roomR) left = nb.right + 8; else if (roomL) left = nb.left - r.width - 8;
  }
  ctxEl.style.left = Math.max(4, Math.min(left, innerWidth - r.width - 6)) + "px";
  ctxEl.style.top = Math.max(4, Math.min(e.clientY, innerHeight - r.height - 6)) + "px";
  const first = ctxEl.querySelector("button"); if (first) first.focus();
}
document.addEventListener("pointerdown", e => { if (ctxEl && !ctxEl.contains(e.target)) endCtx(); }, true);
document.addEventListener("scroll", () => { if (performance.now() > ctxHold) endCtx(); }, true);
addEventListener("resize", () => endCtx());
addEventListener("blur", () => endCtx());
addEventListener("keydown", e => {
  if (e.key === "Escape") { if (!$("#mdl").hidden) closeModal(); else if (ctxEl) endCtx(); return; }
  if (ctxEl && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
    e.preventDefault(); const bs = [...ctxEl.querySelectorAll("button")], i = bs.indexOf(document.activeElement);
    bs[(i + (e.key === "ArrowDown" ? 1 : -1) + bs.length) % bs.length].focus();
  }
});

// A small confirmation window used by Modify and Delete (and by the editor's own delete buttons).
function closeModal() { const m = $("#mdl"); m.hidden = true; m.replaceChildren(); }
function openModal(title, kids, actions) {
  const m = $("#mdl"), box = el("div", { class: "mbox", role: "dialog", "aria-modal": "true", "aria-label": title },
    el("div", { class: "mh" }, el("b", {}, title), btn("✕", closeModal, "x")), el("div", { class: "mb" }, kids), el("div", { class: "mf" }, actions));
  m.replaceChildren(box); m.hidden = false;
  m.onmousedown = e => { if (e.target === m) closeModal(); };
  box.addEventListener("keydown", e => { if (e.key === "Enter" && e.target.tagName === "INPUT") { e.preventDefault(); const p = box.querySelector(".mf .primary, .mf .dangerbtn"); if (p) p.click(); } });
  const f = box.querySelector(".mb input, .mb select, .mb button"), p = box.querySelector(".mf .primary, .mf .dangerbtn");
  (f || box.querySelector(".mf button") || box).focus();
  return box;
}
const mErr = box => { let e = box.querySelector(".merr"); if (!e) { e = el("div", { class: "box badb merr" }); box.querySelector(".mb").append(e); } return e; };

function askDeleteNode(m, n) {
  const links = m.links.filter(l => l.a.node === n.id || l.b.node === n.id), by = id => m.nodes.find(x => x.id === id);
  openModal("Delete device?", [
    el("p", {}, "Remove ", el("b", {}, nodeName(n)), " from this map?"),
    links.length ? el("div", { class: "box warnb" }, "The " + links.length + " link" + (links.length > 1 ? "s" : "") + " attached to it will be deleted as well:",
      el("ul", {}, links.map(l => { const o = by(l.a.node === n.id ? l.b.node : l.a.node); return el("li", {}, (l.name ? l.name + " – " : "") + nodeName(n) + " " + (l.a.node === n.id ? l.a.if : l.b.if) + " ↔ " + (o ? nodeName(o) : "?") + " " + (l.a.node === n.id ? l.b.if : l.a.if)); }))) : null,
    el("p", { class: "muted" }, "This only changes the map. The host itself stays in Zabbix.")],
    [btn("Cancel", closeModal), btn("Delete", () => {
      m.nodes = m.nodes.filter(x => x !== n); m.links = m.links.filter(l => l.a.node !== n.id && l.b.node !== n.id);
      sel = null; closeModal(); changed(true); setTimeout(loadView, 300);
    }, "dangerbtn")]);
}
function askDeleteLink(m, l) {
  const A = m.nodes.find(x => x.id === l.a.node), B = m.nodes.find(x => x.id === l.b.node);
  openModal("Delete link?", [
    el("p", {}, "Delete the link ", el("b", {}, l.name || "(unnamed)"), "?"),
    el("div", { class: "box infob" }, (A ? nodeName(A) : "?") + "  " + l.a.if, el("br"), (B ? nodeName(B) : "?") + "  " + l.b.if),
    el("p", { class: "muted" }, "The two devices stay on the map.")],
    [btn("Cancel", closeModal), btn("Delete", () => { m.links = m.links.filter(x => x !== l); sel = null; closeModal(); changed(true); }, "dangerbtn")]);
}
function modifyNode(m, n) {
  const label = el("input", { placeholder: "blank = " + (n.hostname || n.host), spellcheck: "false", maxlength: "60" }); label.value = n.label || "";
  const x = el("input", { type: "number", step: "10" }), y = el("input", { type: "number", step: "10" }); x.value = n.x; y.value = n.y;
  const nt = el("input", { type: "checkbox", style: "width:auto;flex:none" }); nt.checked = nodeNotify(n);
  const box = openModal("Modify device", [
    el("div", { class: "hint" }, "Zabbix host: " + (n.hostname || "?") + " (id " + n.host + ")"),
    fld("Label shown on the map", label),
    el("div", { class: "row" }, fld("Position X", x), fld("Position Y", y)),
    el("div", { class: "hint" }, "Positions snap to a 10 px grid. You can also drag the device on the map while editing."),
    el("label", { class: "row", style: "cursor:pointer" }, nt, el("span", {}, "Notifications: show the problem count on this device"))],
    [btn("Cancel", closeModal), btn("Save changes", () => {
      const nx = parseFloat(x.value), ny = parseFloat(y.value);
      if (!isFinite(nx) || !isFinite(ny)) { mErr(box).textContent = "Position must be numbers."; return; }
      n.label = label.value.trim().slice(0, 60); n.x = clamp(Math.round(nx / 10) * 10, 0, m.width); n.y = clamp(Math.round(ny / 10) * 10, 0, m.height); setNodeNotify(n, nt.checked);
      closeModal(); changed(true);
    }, "primary")]);
}
function modifyLink(m, l) {
  const A = m.nodes.find(x => x.id === l.a.node), B = m.nodes.find(x => x.id === l.b.node), d = { ifA: l.a.if, ifB: l.b.if };
  const name = el("input", { spellcheck: "false", maxlength: "80" }); name.value = l.name || "";
  const mbps = el("input", { type: "number", min: "0", step: "any", placeholder: "blank = use the speed Zabbix reports" }); mbps.value = l.bandwidth_bps ? l.bandwidth_bps / 1e6 : "";
  const tOn = el("input", { type: "checkbox", style: "width:auto;flex:none" }), tPos = el("select", {}, el("option", { value: "above" }, "Above the line"), el("option", { value: "below" }, "Below the line"));
  let tMoved = false;      // the side is only fixed if the select was touched, otherwise the link keeps its automatic side
  tOn.checked = linkShowIf(l); tPos.value = effIfPos(l); tPos.disabled = !tOn.checked; tOn.addEventListener("change", () => { tPos.disabled = !tOn.checked; }); tPos.addEventListener("change", () => { tMoved = true; });
  const box = openModal("Modify link", [
    fld("Link name (optional)", name),
    fld("Interface on " + nodeName(A), ifaceControl(A.host, d.ifA, v => { d.ifA = v; })),
    fld("Interface on " + nodeName(B), ifaceControl(B.host, d.ifB, v => { d.ifB = v; })),
    fld("Capacity override in Mbps (optional – for rate-limited circuits)", mbps),
    fld("Interface tags (names beside each end)", el("div", { class: "row" }, el("label", { style: "display:inline-flex;align-items:center;gap:6px;flex:none;white-space:nowrap;cursor:pointer" }, tOn, " show"), tPos))],
    [btn("Cancel", closeModal), btn("Save changes", () => {
      const cap = mbps.value.trim() === "" ? null : parseFloat(mbps.value);
      if (!d.ifA || !d.ifB) { mErr(box).textContent = "Pick an interface on both devices."; return; }
      if (cap !== null && !(cap > 0)) { mErr(box).textContent = "Capacity must be a positive number of Mbps, or blank."; return; }
      l.name = name.value.trim().slice(0, 80); l.a.if = d.ifA; l.b.if = d.ifB; l.bandwidth_bps = cap === null ? null : cap * 1e6;
      setLinkTags(l, "if_show", tOn.checked); if (tMoved) setLinkTags(l, "if_pos", tPos.value);
      closeModal(); changed(true); setTimeout(loadView, 300);
    }, "primary")]);
}

// ---------- connections menu
// Works against whichever backend serves the page: the Python server, or the in-browser one in the standalone HTML file.
const CONN = { data: null, sel: null, draft: null, result: null, busy: false, mode: "server", msg: "" };
const NEW_ID = "__new__";
const cta = { dismissed: false };
try { cta.dismissed = sessionStorage.getItem("ctaDismissed") === "1"; } catch (e) {}

function renderCta() {
  const c = $("#cta");
  const show = DEMO && !cta.dismissed && $("#conn").hidden && !document.body.classList.contains("kiosk");
  c.hidden = !show;
  if (!show) return;
  c.replaceChildren(el("span", {}, "You are looking at demo data."), btn("Connect to Zabbix", () => openConnections(), "primary"),
    btn("✕", () => { cta.dismissed = true; try { sessionStorage.setItem("ctaDismissed", "1"); } catch (e) {} renderCta(); }, "x"));
}
const dlgShell = body => el("div", { class: "dlg", role: "dialog", "aria-label": "Connections to Zabbix" }, el("div", { class: "dlg-h" }, el("b", {}, "Connections to Zabbix"), btn("✕", closeConnections, "x")), body);
function closeConnections() { $("#conn").hidden = true; renderCta(); }
async function openConnections(msg) {
  CONN.msg = msg || ""; CONN.result = null;
  const box = $("#conn"); box.hidden = false; renderCta();
  box.replaceChildren(dlgShell(el("div", { class: "empty" }, "Loading…")));
  if (!canEdit) { box.replaceChildren(dlgShell(el("div", { class: "box warnb", style: "margin:16px" }, "Connections can only be changed from the machine running the dashboard, or by opening the page with ?token=… (see the start-up message)."))); return; }
  try { CONN.data = await api("/api/connections"); }
  catch (e) { box.replaceChildren(dlgShell(el("div", { class: "box badb", style: "margin:16px" }, e.message))); return; }
  CONN.mode = CONN.data.mode;
  const a = CONN.data.active, known = a === "demo" || CONN.data.profiles.some(p => p.id === a);
  pickConn(known && a ? a : (CONN.data.profiles.length ? CONN.data.profiles[0].id : NEW_ID));
}
function pickConn(id) {
  CONN.sel = id; CONN.result = null;
  if (id === NEW_ID) CONN.draft = { id: null, name: "", url: "", auth: "token", token: "", username: "", password: "", verify_tls: true, remember: true, match: {}, has_secret: false };
  else if (id !== "demo") { const p = CONN.data.profiles.find(x => x.id === id); CONN.draft = Object.assign({ token: "", password: "", remember: true }, JSON.parse(JSON.stringify(p))); }
  renderConn();
}
const pitem = (id, name, sub, inUse) => el("button", { class: "pitem" + (CONN.sel === id ? " on" : ""), type: "button", onclick: () => pickConn(id) },
  inUse ? el("span", { class: "use" }, "● in use") : null, el("b", {}, name), el("small", {}, sub));
function renderConn() {
  const d = CONN.data;
  const list = el("div", { class: "plist" }, pitem("demo", "Demo data", "Simulated Cisco network", d.active === "demo"),
    ...d.profiles.map(p => pitem(p.id, p.name, p.url, d.active === p.id)),
    btn("+ New connection", () => pickConn(NEW_ID), CONN.sel === NEW_ID ? "primary" : ""));
  $("#conn").replaceChildren(dlgShell(el("div", { class: "dlg-b" }, list, el("div", { class: "pform" }, CONN.sel === "demo" ? demoPane() : formPane()))));
  setResult(CONN.result);
}
const msgBox = () => CONN.msg ? el("div", { class: "box infob" }, CONN.msg) : null;
function demoPane() {
  const using = CONN.data.active === "demo";
  return el("div", {}, msgBox(), el("h3", {}, "Demo data"),
    el("p", { class: "muted" }, "A simulated Cisco network - 8 core / WAN devices with 10 links, plus 120 access switches on the “All devices” tab to try paging - so you can look around without a Zabbix server."),
    el("div", { class: "acts" }, using ? el("span", { class: "chip ok" }, "Currently in use") : btn("Use demo data", () => activateConn("demo"), "primary"),
      btn("Reset sample layout", resetDemo)), el("div", { id: "conn-result" }));
}
function formPane() {
  const dr = CONN.draft, ro = !!dr.readonly, standalone = CONN.mode === "standalone", isNew = CONN.sel === NEW_ID;
  const text = (k, attrs = {}) => { const i = el("input", Object.assign({ spellcheck: "false", autocomplete: "off" }, attrs)); i.value = dr[k] || ""; i.disabled = ro; i.addEventListener("input", () => { dr[k] = i.value; }); return i; };
  const secret = (k, ph) => {
    const i = el("input", { type: "password", autocomplete: "new-password", spellcheck: "false", placeholder: dr.has_secret ? "•••••••• saved – leave blank to keep it" : ph });
    i.value = dr[k] || ""; i.disabled = ro; i.addEventListener("input", () => { dr[k] = i.value; });
    const show = btn("show", () => { i.type = i.type === "password" ? "text" : "password"; show.textContent = i.type === "password" ? "show" : "hide"; });
    return el("div", { class: "row" }, i, show);
  };
  const radio = (val, label) => { const r = el("input", { type: "radio", name: "cauth" }); r.checked = dr.auth === val; r.disabled = ro; r.addEventListener("change", () => { dr.auth = val; CONN.result = null; renderConn(); }); return el("label", {}, r, " " + label); };
  const check = (k, label, note) => { const c = el("input", { type: "checkbox" }); c.checked = !!dr[k]; c.disabled = ro; c.addEventListener("change", () => { dr[k] = c.checked; }); return el("label", { class: "inl" }, c, el("span", {}, label, note ? el("small", { class: "muted" }, " " + note) : null)); };
  const defs = CONN.data.match_defaults || {};
  const labels = { in_key: "Inbound traffic item key starts with", out_key: "Outbound traffic item key starts with", status_key: "Operational status item key starts with", speed_key: "Speed item key starts with",
    tag_name: "Item tag that holds the interface name", alias_tag: "Item tag that holds the interface description", name_regex: "Fallback: regex on the item name (needs a named group ifname)" };
  const adv = el("details", { class: "sec", style: "border:1px solid var(--line);border-radius:8px;margin:12px 0" }, el("summary", {}, "Advanced: how interface items are recognised"),
    el("div", {}, el("div", { class: "hint" }, "The defaults fit the stock Cisco / SNMP templates. Change them only if your templates use other item keys."),
      ...Object.keys(labels).map(k => { const i = el("input", { placeholder: defs[k] || "", spellcheck: "false" }); i.value = (dr.match && dr.match[k]) || ""; i.disabled = ro; i.addEventListener("input", () => { dr.match = dr.match || {}; if (i.value.trim()) dr.match[k] = i.value; else delete dr.match[k]; }); return fld(labels[k], i); })));
  return el("div", {}, msgBox(), el("h3", {}, isNew ? "New connection" : dr.name),
    ro ? el("div", { class: "box infob" }, "This connection comes from config.json / environment variables, so it is read-only here. You can still test and use it.") : null,
    fld("Name", text("name", { placeholder: "e.g. Production Zabbix" })),
    fld("Zabbix address / API URL", text("url", { placeholder: "https://zabbix.example.com/api_jsonrpc.php" })),
    el("div", { class: "hint" }, "The server address alone is enough, /api_jsonrpc.php is added for you. Zabbix 7.0."),
    el("div", { class: "fld" }, el("span", {}, "Authentication"), el("div", { class: "radios" }, radio("token", "API token (recommended)"), radio("password", "Username & password"))),
    dr.auth === "token" ? fld("API token (Zabbix: Users > API tokens)", secret("token", "paste the API token")) : el("div", {}, fld("Username", text("username")), fld("Password", secret("password", ""))),
    standalone ? check("remember", "Remember the secret in this browser", "(kept unencrypted in this browser's local storage; use a read-only token)") : check("verify_tls", "Verify the HTTPS certificate", "(untick only for a self-signed certificate)"),
    adv,
    el("div", { class: "acts" }, btn("Test connection", runTest), ro ? btn("Use this connection", () => activateConn(dr.id), "primary") : btn("Save & use", () => saveConn(true), "primary"),
      ro ? null : btn("Save", () => saveConn(false)), (!ro && !isNew) ? btn("Delete", deleteConn, "x") : null),
    el("div", { id: "conn-result" }),
    el("div", { class: "box infob" }, standalone
      ? [el("b", {}, "Browser mode. "), "This page calls your Zabbix straight from the browser, so Zabbix's web server must allow it (CORS), or this file must be served from the Zabbix server itself. The Test button tells you if that is the problem and how to fix it."]
      : [el("b", {}, "Server mode. "), "The dashboard server calls Zabbix for you, so there are no browser (CORS) limits. The secret stays on the server and is never sent to the browser."]));
}
const payload = () => { const d = CONN.draft; return { id: d.id, name: d.name, url: d.url, auth: d.auth, token: d.auth === "token" ? d.token : "", username: d.auth === "password" ? d.username : "", password: d.auth === "password" ? d.password : "", verify_tls: d.verify_tls, remember: d.remember, match: d.match || {} }; };
const NGINX = "location = /api_jsonrpc.php {\n    add_header Access-Control-Allow-Origin  \"*\" always;\n    add_header Access-Control-Allow-Headers \"Content-Type, Authorization\" always;\n    add_header Access-Control-Allow-Methods \"POST, OPTIONS\" always;\n    if ($request_method = OPTIONS) { return 204; }\n    # keep the fastcgi_pass / include lines you already have for PHP here\n}";
const APACHE = "<Location \"/api_jsonrpc.php\">\n    Header always set Access-Control-Allow-Origin \"*\"\n    Header always set Access-Control-Allow-Headers \"Content-Type, Authorization\"\n    Header always set Access-Control-Allow-Methods \"POST, OPTIONS\"\n</Location>\nRewriteEngine On\nRewriteCond %{REQUEST_METHOD} OPTIONS\nRewriteRule ^/api_jsonrpc\\.php$ - [R=204,L]";
function corsHelp() {
  return el("div", { class: "box infob" }, el("b", {}, "How to let this page call Zabbix"),
    el("div", {}, "A browser only lets a page talk to another server if that server agrees (CORS). Pick one:"),
    el("ol", {},
      el("li", {}, el("b", {}, "Easiest: serve this file from the Zabbix web server."), " Copy it into the Zabbix frontend folder (often /usr/share/zabbix/), open https://YOUR-ZABBIX/<file name> and use api_jsonrpc.php as the address. Same server, so no CORS."),
      el("li", {}, el("b", {}, "Or let Zabbix's web server answer browser calls."), " Add CORS headers for api_jsonrpc.php (adapt to your setup, reload the web server, test again):", el("pre", {}, NGINX), el("pre", {}, APACHE),
        el("small", {}, "Access-Control-Allow-Origin: * lets any web page try the API (it still needs a valid token). Use a read-only token.")),
      el("li", {}, el("b", {}, "Or run the Python version"), " (channelview.py). It calls Zabbix itself, so there is no CORS and the token never reaches the browser.")));
}
function setResult(r) {
  const out = $("#conn-result"); if (!out) return;
  if (!r) { out.replaceChildren(); return; }
  if (r.pending) { out.replaceChildren(el("div", { class: "box infob" }, r.text || "Testing… contacting Zabbix")); return; }
  if (r.saved) { out.replaceChildren(el("div", { class: "box okb" }, "Saved.")); return; }
  if (r.ok) {
    const kids = [el("b", {}, "✓ Connected to Zabbix " + r.version),
      el("div", {}, r.hosts + " monitored host" + (r.hosts === 1 ? "" : "s") + " · " + (r.proxies == null ? "proxies: n/a" : r.proxies + (r.proxies === 1 ? " proxy" : " proxies")) + " · " + r.interface_items + " interface items found")];
    if (r.sample && r.sample.length) kids.push(el("div", { class: "samples" }, ...r.sample.map(h => el("span", {}, el("i", { class: "dot", style: "width:8px;height:8px;margin-right:6px;background:" + (h.status === "up" ? "var(--ok)" : h.status === "down" ? "var(--bad)" : "var(--muted)") }), h.name)), r.hosts > r.sample.length ? el("span", { class: "muted" }, "+" + (r.hosts - r.sample.length) + " more") : null));
    out.replaceChildren(el("div", { class: "box okb" }, kids));
    if (!r.interface_items) out.append(el("div", { class: "box warnb" }, "No interface items matched (net.if.in[ / net.if.out[ …). Hosts will show up, but links will have no traffic data. Open “Advanced” above and set the item key prefixes your templates use."));
    return;
  }
  out.replaceChildren(el("div", { class: "box badb" }, el("b", {}, "✗ " + (r.error || "Connection failed")), r.hint ? el("div", { style: "margin-top:4px" }, r.hint) : null));
  if (r.kind === "cors") out.append(corsHelp());
}
async function runTest() {
  if (CONN.busy) return;
  CONN.busy = true; setResult({ pending: true });
  try { CONN.result = await api("/api/connections/test", { body: { profile: payload() } }); }
  catch (e) { CONN.result = { ok: false, error: e.message, kind: "api" }; }
  CONN.busy = false; setResult(CONN.result);
  return CONN.result;
}
function goLive() { try { history.replaceState(null, "", location.pathname + location.search); } catch (e) {} location.reload(); }
async function saveConn(use) {
  if (use) { const r = await runTest(); if (!r || !r.ok) return; }     // "Save & use" only switches once the test passed
  try {
    const r = await api("/api/connections/save", { body: { profile: payload(), activate: use } });
    if (use) { goLive(); return; }
    CONN.data = r; pickConn(r.id); CONN.result = { saved: true }; setResult(CONN.result);
  } catch (e) { CONN.result = { ok: false, error: e.message, kind: "api" }; setResult(CONN.result); }
}
async function activateConn(id) {
  try { await api("/api/connections/activate", { body: { id } }); goLive(); }
  catch (e) { CONN.result = { ok: false, error: e.message, kind: "api" }; setResult(CONN.result); }
}
async function deleteConn() {
  if (!confirm("Delete the connection \"" + (CONN.draft.name || "") + "\"? Its saved layout stays on disk but is no longer used.")) return;
  const wasActive = CONN.data.active === CONN.draft.id;
  try { const r = await api("/api/connections/delete", { body: { id: CONN.draft.id } }); if (wasActive) { goLive(); return; } CONN.data = r; pickConn(r.profiles.length ? r.profiles[0].id : "demo"); }
  catch (e) { CONN.result = { ok: false, error: e.message, kind: "api" }; setResult(CONN.result); }
}
async function resetDemo() {
  if (!confirm("Discard your changes to the demo layout and restore the sample?")) return;
  try { await api("/api/connections/reset-demo", { body: {} }); if (CONN.data.active === "demo") goLive(); else { CONN.result = { saved: true }; setResult(CONN.result); } }
  catch (e) { CONN.result = { ok: false, error: e.message, kind: "api" }; setResult(CONN.result); }
}
$("#chip").addEventListener("click", () => openConnections());
$("#chip").addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openConnections(); } });
$("#conn").addEventListener("mousedown", e => { if (e.target === e.currentTarget) closeConnections(); });
addEventListener("keydown", e => { if (e.key === "Escape" && !$("#conn").hidden) closeConnections(); });

// ---------- boot
async function boot() {
  let r;
  try {
    r = await api("/api/layout");
    L = r.layout; canEdit = r.can_edit; DEMO = !!r.demo; CONN.mode = r.mode || "server";
    for (const m of Object.values(L.maps)) for (const l of m.links) fitVia(m, l);      // links bent by earlier versions with several bend points: keep the one that matters
  } catch (e) { $("#main").replaceChildren(el("div", { class: "empty err" }, "Cannot load the dashboard: " + e.message)); return; }
  const h = new URLSearchParams(location.hash.slice(1));
  initDisp(r.defaults, h);
  tabId = (h.get("tab") && L.tabs.some(t => t.id === h.get("tab"))) ? h.get("tab") : (L.tabs[0] && L.tabs[0].id);
  if (h.get("kiosk")) document.body.classList.add("kiosk");
  const per = parseInt(h.get("per"), 10);
  if (PER_CHOICES.includes(per)) { DEVUI.per = per; saveDevUI(); }      // e.g. a wall display: ...#kiosk=1&per=200
  if (h.get("play") && L.playlists.some(p => p.id === h.get("play"))) playStart(h.get("play"));
  render(); loadView();
  scheduleRefresh(); setInterval(tick, 1000);
  if (r.unconfigured && canEdit && !h.get("kiosk")) openConnections("Add your Zabbix server to see your own devices.");
}
boot();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- HTTPS

class TlsContext:
    """The server's HTTPS settings (--tls-cert / --tls-key). A renewed certificate (certbot, channelview-cert) is picked up WITHOUT a restart:
    at most every TLS_RELOAD_CHECK_SECONDS a new connection compares the two files' modification times, and a changed pair is loaded into a
    new context that later connections use. If the new files do not load (half written, key from another certificate) the old context
    stays in use and the problem is printed once; the next change to the files is tried again."""

    def __init__(self, cert, key=None):
        self.cert, self.key = Path(cert), (Path(key) if key else None)
        self.lock = threading.Lock()
        self.ctx = self._load()                  # raises if the files are unusable: the server then does not start
        self.stamp = self._stamp()
        self.failed = None
        self.checked = time.monotonic()

    def _stamp(self):
        out = []
        for f in [self.cert] + ([self.key] if self.key else []):
            st = f.stat()
            out.append((st.st_mtime_ns, st.st_size))
        return tuple(out)

    def _load(self):
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(self.cert), str(self.key) if self.key else None)
        return ctx

    def current(self):
        with self.lock:
            now = time.monotonic()
            if now - self.checked >= TLS_RELOAD_CHECK_SECONDS:
                self.checked = now
                try:
                    stamp = self._stamp()
                    if stamp != self.stamp and stamp != self.failed:
                        try:
                            self.ctx, self.stamp, self.failed = self._load(), stamp, None
                            print("TLS certificate reloaded.", flush=True)
                        except (ssl.SSLError, OSError, ValueError) as e:
                            self.failed = stamp
                            print(f"! The new TLS certificate files are not usable yet ({e}); the old certificate stays in use.", flush=True)
                except OSError:
                    pass                          # a file is being replaced right now: look again later
            return self.ctx


class DashServer(ThreadingHTTPServer):
    tls = None                                    # a TlsContext when this server speaks HTTPS

    def handle_error(self, request, client_address):
        # A browser that does not trust the certificate, a port scanner, a dropped connection: not worth a traceback in the log.
        if self.tls is not None and isinstance(sys.exc_info()[1], (ssl.SSLError, ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


# --------------------------------------------------------------------------- HTTP server

class Handler(BaseHTTPRequestHandler):
    server_version = "ChannelView"
    app: App = None
    edit_token = ""
    allowed_hosts = None  # None = do not check (only enforced when bound to loopback)
    tls_timeout = 15      # seconds for the TLS handshake of a new connection (HTTPS only)
    _plain_http = False

    def setup(self):
        tls = self.server.tls
        if tls is not None:
            raw = self.request
            raw.settimeout(self.tls_timeout)
            first = raw.recv(1, socket.MSG_PEEK)
            if not first:
                raise ConnectionAbortedError("closed before sending anything")
            if first != b"\x16":                  # a TLS connection starts with 0x16; this is somebody typing http:// -> send them to https://
                self._plain_http = True
            else:
                self.request = tls.current().wrap_socket(raw, server_side=True)      # the handshake, here in the connection's own thread
                self.request.settimeout(60)
        super().setup()

    def handle(self):
        if self._plain_http:
            return self._redirect_to_https()
        super().handle()

    def finish(self):
        try:
            super().finish()
        finally:
            if self.server.tls is not None:
                self.request.close()              # the wrapped socket; the original one the server holds is already detached

    def _redirect_to_https(self):
        line = self.rfile.readline(4096).decode("latin-1").split()
        target = line[1] if len(line) >= 2 and re.fullmatch(r"/[\x21-\x7e]*", line[1]) else "/"
        host = ""
        for _ in range(100):
            h = self.rfile.readline(4096).decode("latin-1")
            if h in ("", "\r\n", "\n"):
                break
            if h.lower().startswith("host:"):
                host = h[5:].strip()
        if not re.fullmatch(r"[A-Za-z0-9.-]+(:\d{1,5})?|\[[0-9A-Fa-f:.]+\](:\d{1,5})?", host):
            body = b"This port speaks HTTPS. Use https:// instead of http://.\n"
            head = b"HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain\r\n"
        else:
            body = f"This port speaks HTTPS: https://{host}{target}\n".encode("ascii")
            head = f"HTTP/1.1 307 Temporary Redirect\r\nLocation: https://{host}{target}\r\nContent-Type: text/plain\r\n".encode("ascii")   # 307, not 301: browsers keep a 301 forever
        self.wfile.write(head + b"Content-Length: " + str(len(body)).encode() + b"\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n" + body)

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        if len(body) > 2048 and "gzip" in self.headers.get("Accept-Encoding", "").lower():      # a few hundred devices is a big JSON every 5 s: shrink it ~10x
            body = gzip.compress(body, 4)
            gz = True
        else:
            gz = False
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "connect-src 'self'; img-src data:; frame-ancestors 'none'; base-uri 'none'; "
                             "form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _host_ok(self):
        return self.allowed_hosts is None or self.headers.get("Host", "") in self.allowed_hosts

    def _edit_ok(self, token=None):
        got = (token if token is not None else self.headers.get("X-Edit-Token", "")).encode("utf-8", "replace")
        return bool(got) and hmac.compare_digest(got, self.edit_token.encode("utf-8"))

    def _is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "bad host header"}, 403)
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        path, app = url.path, self.app
        try:
            if path == "/":
                token = self.edit_token if (self._is_local() or self._edit_ok(q.get("token", ""))) else ""
                html = PAGE.replace("__EDIT_TOKEN__", token)
                css = app.custom_css()
                if css:                                           # inline, so the page's security policy needs no exception
                    html = html.replace("</head>", '<style id="custom-css">\n' + css + "\n</style>\n</head>", 1)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/layout":
                return self._json({"layout": app.store.get(), "can_edit": self._edit_ok(), "demo": app.is_demo,
                                   "mode": "server", "conn": {"active": app.active, "name": app.conn_name},
                                   "unconfigured": app.source is None and not app.is_demo,
                                   "defaults": {"graph_minutes": app.cfg["graph_minutes"],
                                                "refresh_seconds": snap_to(app.cfg["poll_seconds"], REFRESH_CHOICES)}})
            if path == "/api/linkgraph":      # read-only like /api/view, and only for a link that exists in the layout
                tab = next((t for t in app.store.get()["tabs"] if t["id"] == q.get("tab")), None)
                if not tab:
                    return self._json({"error": "no such tab"}, 404)
                g = app.link_graph(tab, q.get("link", ""), _int(q.get("minutes"), None))
                return self._json(g) if g is not None else self._json({"error": "no such link"}, 404)
            if path == "/api/connections":
                if not self._edit_ok():
                    return self._json({"error": "editing is locked - open the page with ?token=..."}, 403)
                return self._json(app.connections_info())
            if path == "/api/view":
                tab = next((t for t in app.store.get()["tabs"] if t["id"] == q.get("tab")), None)
                if not tab:
                    return self._json({"error": "no such tab"}, 404)
                app.note_client(q.get("cid", ""), _int(q.get("every"), None))      # the page says how often it wants data: Zabbix is polled that often
                return self._json(app.view(tab))
            if path in ("/api/hosts", "/api/groups", "/api/interfaces"):
                if not self._edit_ok():
                    return self._json({"error": "editing is locked - open the page with ?token=..."}, 403)
                if app.source is None:
                    return self._json({"error": app.status["error"]}, 502)
                if path == "/api/hosts":
                    return self._json({"items": app.source.search_hosts(q.get("q", "")[:60])})
                if path == "/api/groups":
                    return self._json({"items": app.source.search_groups(q.get("q", "")[:60])})
                host = q.get("host", "")
                if not HOSTID_RX.match(host):
                    return self._json({"error": "bad host id"}, 400)
                return self._json({"interfaces": app.source.interfaces_of(host)})
        except ZabbixError as e:
            return self._json({"error": str(e)}, 502)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._host_ok():
            return self._json({"error": "bad host header"}, 403)
        if urllib.parse.urlparse(self.path).path == "/api/refresh":     # harmless and throttled, so view-only displays may use it too
            st = self.app.refresh()
            return self._json({"ok": bool(st.get("ok")), "status": st})
        if not self._edit_ok():
            return self._json({"error": "editing is locked - open the page with ?token=..."}, 403)
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n < 0 or n > MAX_BODY:
                return self._json({"error": "body too large"}, 413)
            data = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json({"error": "bad request"}, 400)
        path, app = urllib.parse.urlparse(self.path).path, self.app
        if not isinstance(data, dict):
            return self._json({"error": "bad request"}, 400)
        if path == "/api/layout":
            try:
                app.store.put(data.get("layout"))
            except (ValueError, AttributeError, TypeError, KeyError) as e:
                return self._json({"error": f"invalid layout: {e}"}, 400)
            app.wake.set()
            return self._json({"ok": True})
        if path.startswith("/api/connections/"):
            try:
                return self._connections_post(path[len("/api/connections/"):], data)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        self._json({"error": "not found"}, 404)

    def _connections_post(self, action, data):
        app, conns = self.app, self.app.conns
        if action == "test":
            try:
                prof = conns.candidate(data.get("profile") or {})
            except ValueError as e:      # e.g. a URL that is not http(s):// - report it like any other failed test
                return self._json({"ok": False, "error": str(e), "kind": "url", "hint": HINTS["url"]})
            return self._json(test_connection(app.cfg, prof))
        if action == "save":
            p = conns.upsert(data.get("profile") or {})
            if data.get("activate"):
                app.activate(p["id"])
            return self._json({"ok": True, "id": p["id"], **app.connections_info()})
        if action == "activate":
            cid = str(data.get("id") or "")
            if cid != "demo" and not conns.get(cid):
                return self._json({"error": "unknown connection"}, 404)
            app.activate(cid)
            return self._json({"ok": True, **app.connections_info()})
        if action == "delete":
            cid = str(data.get("id") or "")
            conns.delete(cid)
            if app.active == cid:
                app.activate("demo")
            return self._json({"ok": True, **app.connections_info()})
        if action == "reset-demo":
            Store(app.layout_path("demo"), demo_layout).put(demo_layout())
            if app.active == "demo":
                app.activate("demo", persist=False)
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser(description="ChannelView - weathermap dashboard for Zabbix")
    ap.add_argument("--demo", action="store_true", help="simulated Cisco devices, no Zabbix needed")
    ap.add_argument("--init", action="store_true", help="write a config.json template and exit")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (use 0.0.0.0 for a LAN wall display)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--data-dir", default=os.environ.get("CHANNELVIEW_DATA", str(Path.home() / ".channelview")))
    ap.add_argument("--config", default="", help="path to config.json (default: <data-dir>/config.json)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--token-file", default="", help="keep the edit token in this file (made owner-only at the first start) so it survives restarts - "
                                                     "what the Debian service does; default: a new random token at every start")
    ap.add_argument("--tls-cert", default=os.environ.get("CHANNELVIEW_TLS_CERT", ""), metavar="FILE",
                    help="serve HTTPS with this certificate (PEM, with the intermediate certificates after it; also: CHANNELVIEW_TLS_CERT). "
                         "A renewed file is used without a restart. Without it the server speaks plain HTTP.")
    ap.add_argument("--tls-key", default=os.environ.get("CHANNELVIEW_TLS_KEY", ""), metavar="FILE",
                    help="the private key of --tls-cert (PEM; also: CHANNELVIEW_TLS_KEY); not needed if the key is in the certificate file")
    ap.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    args = ap.parse_args()
    if args.tls_key and not args.tls_cert:
        sys.exit("--tls-key needs --tls-cert as well.")
    tls = None
    if args.tls_cert:
        try:
            tls = TlsContext(Path(args.tls_cert).expanduser(), Path(args.tls_key).expanduser() if args.tls_key else None)
        except (ssl.SSLError, OSError, ValueError) as e:
            sys.exit(f"Cannot use the TLS certificate {args.tls_cert}: {e}")

    data = Path(args.data_dir).expanduser()
    data.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(data, 0o700)
    except OSError:
        pass
    cfg_path = Path(args.config).expanduser() if args.config else data / "config.json"
    if args.init:
        if cfg_path.exists():
            sys.exit(f"{cfg_path} already exists - not overwriting.")
        cfg_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        try:
            os.chmod(cfg_path, 0o600)
        except OSError:
            pass
        print(f"Wrote {cfg_path}\nSet zabbix.url and zabbix.token (Users > API tokens in Zabbix), then run again.\n"
              "(You can also skip this file and add the connection in the page: Connections menu.)")
        return
    try:
        cfg = load_config(cfg_path)
    except (ValueError, OSError) as e:
        sys.exit(f"Cannot read {cfg_path}: {e}")

    conns = Connections(data / "connections.json", cfg)
    if args.demo:
        start, persist = "demo", False                   # --demo: simulated data for this run only
    elif conns.active == "demo" or conns.get(conns.active):
        start, persist = conns.active, True
    else:                                                # first run: config.json / env connection if there is one
        start, persist = ("default" if conns.get("default") else ""), True
    app = App(cfg, conns, data, start, persist)

    server, port = None, args.port
    for p in range(args.port, args.port + 20):
        try:
            server, port = DashServer((args.host, p), Handler), p
            break
        except OSError:
            continue
    if server is None:
        sys.exit(f"No free port in {args.port}-{args.port + 19}.")
    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    server.tls = tls
    scheme = "https" if tls else "http"
    Handler.app = app
    Handler.edit_token = load_edit_token(Path(args.token_file).expanduser()) if args.token_file else secrets.token_urlsafe(24)
    Handler.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"} if loopback else None
    threading.Thread(target=app.loop, daemon=True).start()

    url = f"{scheme}://127.0.0.1:{port}/"
    print(f"{APP_NAME} {VERSION} running at {url}" + ("   [DEMO DATA]" if app.is_demo else ""))
    print(f"Data folder: {data}")
    if app.source is None:
        print("! " + app.status["error"])
    if not loopback:
        print(f"Listening on {args.host}:{port}. Viewing needs no login; to edit from another machine open")
        if args.token_file:      # a stored token stays out of the log: the log may be read by more people than the file
            print(f"  {scheme}://<this-host>:{port}/?token=<the token in {args.token_file}>")
        else:
            print(f"  {scheme}://<this-host>:{port}/?token={Handler.edit_token}")
    if not tls and not loopback:
        print("  (plain HTTP: the token and everything else cross the network readable - --tls-cert / --tls-key switch on HTTPS)")
    if not args.no_browser and loopback:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
