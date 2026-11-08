/* ===== Standalone backend ============================================================
   This replaces the Python server when the dashboard runs as a single HTML file.
   The page talks to /api/* with fetch(); here those calls are answered inside the browser:
     - "Demo data": a simulated Cisco network (8 hosts, 2 proxies, 10 links)
     - saved connections: the browser calls the Zabbix API directly (JSON-RPC over fetch).
       This needs Zabbix's web server to allow it (CORS), or this file served from Zabbix itself.
   Connections and layouts are kept in this browser's localStorage.
   ================================================================================== */
(() => {
  "use strict";
  const realFetch = window.fetch ? window.fetch.bind(window) : null;   // the Zabbix client must bypass the /api/* interception below
  const DOWN_STATES = new Set([2, 6, 7]);          // ifOperStatus: down, notPresent, lowerLayerDown
  const STALE_SECONDS = 300, MIN_SEVERITY = 2, POLL_MS = 30000, LIST_TTL_MS = 60000, RETRY_MS = 10000;
  const MAX_HOSTS = 5000;                        // safety cap for "Show all monitored hosts" (the page says so if it is ever reached)
  const HOST_BATCH = 100, ITEM_BATCH = 40;       // hosts per host.get / trigger.get request, per item.get request
  const ITEM_LIMIT = 50000, TRIG_LIMIT = 5000;   // a reply this big is treated as cut off: the batch is split and asked again
  const MAX_PARALLEL = 6;                        // requests to Zabbix in flight at once
  const GRAPH_DEFAULT = 60, GRAPH_POINTS = 200, GRAPH_STEP = 30, GRAPH_CACHE_MS = 15000, HIST_LIMIT = 50000, TREND_LIMIT = 50000;    // the link graph: default range (minutes), points drawn, demo sample spacing, cache time, history / trend rows asked for
  const GRAPH_RANGES = [5, 10, 30, 60, 180, 360, 720, 1440, 10080, 20160, 43200], TREND_FROM = 1440;      // the time ranges the header offers (minutes); beyond TREND_FROM minutes the graph is drawn from hourly trends
  const REFRESH_CHOICES = [5, 10, 30, 60, 300, 900];      // the data refresh intervals the header offers (seconds)
  let pollMs = POLL_MS;                          // how long a Zabbix poll stays fresh: the refresh interval the page picked
  const gcache = new Map();                      // link graphs asked for lately (see linkGraph)
  const ID_RX = /^[A-Za-z0-9_-]{1,40}$/;
  const MATCH_DEFAULTS = { in_key: "net.if.in[", out_key: "net.if.out[", status_key: "net.if.status[", speed_key: "net.if.speed[",
    tag_name: "interface", alias_tag: "description", name_regex: "^Interface (?P<ifname>[^(:]+?)(?:\\((?P<alias>.*?)\\))?: " };

  // ---------------------------------------------------------------- browser storage (falls back to memory)
  const MEM = {};
  const mkStore = area => ({
    get: k => { try { const v = window[area].getItem(k); return v === null ? (MEM[area + k] ?? null) : v; } catch (e) { return MEM[area + k] ?? null; } },
    set: (k, v) => { MEM[area + k] = v; try { window[area].setItem(k, v); } catch (e) { /* blocked: keep in memory */ } },
    del: k => { delete MEM[area + k]; try { window[area].removeItem(k); } catch (e) { /* ignore */ } },
  });
  const ls = mkStore("localStorage"), ss = mkStore("sessionStorage");

  // ================================================================ Zabbix client (browser -> Zabbix JSON-RPC)
  class ZbxError extends Error { constructor(msg, kind = "api", auth = false) { super(msg); this.kind = kind; this.auth = auth; } }
  const HINTS = {
    url: "Check the address. Enter the Zabbix web address or the API endpoint, e.g. https://zabbix.example.com/api_jsonrpc.php",
    network: "The browser could not connect. Check the host name or IP, the port, and your firewall or VPN. With an https:// address and a self-signed certificate, open the Zabbix address in a new tab once, accept the certificate warning, then test again.",
    cors: "Zabbix is reachable, but its web server does not allow this page to call it (CORS), so the browser blocked the request. Pick one of the options below.",
    mixed: "This page was opened over https:// but the Zabbix address is http://. Browsers block that. Use an https:// Zabbix address, or open this page from a file or an http:// address.",
    auth: "Zabbix rejected the credentials. For a token: Users > API tokens, and the user's role must allow API access. For a username/password: check the account is enabled and not temporarily blocked.",
    http: "The web server answered with an error. Make sure the address points at your Zabbix frontend (api_jsonrpc.php).",
    api: "Zabbix returned an API error - see the message above.",
  };
  function apiUrl(raw) {
    let u = String(raw || "").trim().replace(/\/+$/, "");
    if (!u) throw new ZbxError("Enter the Zabbix address", "url");
    if (!/^https?:\/\//i.test(u)) {
      // a relative address (api_jsonrpc.php) works when this file is served by the Zabbix web server itself
      if (/^https?:$/.test(location.protocol) && !/^[a-z][a-z0-9+.-]*:/i.test(u)) u = new URL(u, location.href).href.replace(/\/+$/, "");
      else throw new ZbxError("The address must start with http:// or https://", "url");
    }
    return /\.php$/i.test(u) ? u : u + "/api_jsonrpc.php";
  }
  async function diagnose(url, e) {
    if (e && e.name === "AbortError") return new ZbxError("Timed out waiting for Zabbix (" + url + ")", "network");
    if (location.protocol === "https:" && /^http:/i.test(url)) return new ZbxError("The browser blocked an http:// request from an https:// page", "mixed");
    // A no-cors request succeeds whenever the server is reachable, even if CORS forbids reading the answer:
    // reachable + the real request failed  =>  CORS.   unreachable  =>  network / certificate.
    try { await realFetch(url, { method: "GET", mode: "no-cors", credentials: "omit", cache: "no-store" }); }
    catch (e2) { return new ZbxError("Cannot reach Zabbix at " + url + (e && e.message ? " (" + e.message + ")" : ""), "network"); }
    return new ZbxError("The browser blocked the request to " + url + " (CORS)", "cors");
  }
  class ZbxClient {
    constructor(url, auth, secret, user, timeoutMs = 20000) {
      this.url = apiUrl(url); this.timeout = timeoutMs; this.id = 0; this.loginP = null; this.active = 0; this.waiters = [];
      this.token = auth === "token" ? secret : ""; this.user = auth === "password" ? (user || "") : ""; this.password = auth === "password" ? secret : "";
    }
    async post(method, params, auth = true) {
      const headers = { "Content-Type": "application/json-rpc" };
      if (auth && this.token) headers.Authorization = "Bearer " + this.token;
      if (this.active >= MAX_PARALLEL) await new Promise(r => this.waiters.push(r));      // a big inventory means many batched requests: keep a few in flight, not hundreds
      this.active++;
      let res;
      try {
        const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), this.timeout);
        try { res = await realFetch(this.url, { method: "POST", headers, body: JSON.stringify({ jsonrpc: "2.0", method, params, id: ++this.id }), signal: ctl.signal, credentials: "omit", cache: "no-store" }); }
        catch (e) { throw await diagnose(this.url, e); }
        finally { clearTimeout(timer); }
      } finally { this.active--; const w = this.waiters.shift(); if (w) w(); }
      if (!res.ok) throw new ZbxError("HTTP " + res.status + " from Zabbix (" + this.url + ")", res.status === 404 ? "url" : "http");
      let data;
      try { data = await res.json(); } catch (e) { throw new ZbxError("Zabbix did not return JSON - check the address", "url"); }
      if (data && data.error) {
        const er = data.error, text = ((er.message || "error") + ": " + (er.data || "")).replace(/^[: ]+|[: ]+$/g, ""), low = text.toLowerCase();
        const relogin = low.includes("re-login") || low.includes("not authori") || low.includes("session");
        const badLogin = method === "user.login" || low.includes("permission") || low.includes("incorrect user");
        throw new ZbxError(method + ": " + text, (relogin || badLogin) ? "auth" : "api", relogin);
      }
      return data ? data.result : undefined;
    }
    async login() {
      if (!this.loginP) this.loginP = this.post("user.login", { username: this.user, password: this.password }, false).then(t => { this.token = t; }).finally(() => { this.loginP = null; });
      return this.loginP;
    }
    async ready() { if (!this.token && this.user) await this.login(); }
    async call(method, params = {}) {
      await this.ready();
      try { return await this.post(method, params); }
      catch (e) { if (e.auth && this.user) { this.token = ""; await this.login(); return this.post(method, params); } throw e; }
    }
    version() { return this.post("apiinfo.version", {}, false); }    // must be called WITHOUT auth in Zabbix 7.0
  }

  // ================================================================ data sources (all async, same interface)
  const natCmp = (a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
  const makeNameRx = src => new RegExp("^(?:" + String(src).replace(/\(\?P</g, "(?<") + ")");   // python-style (?P<x>) -> JS (?<x>), anchored like re.match

  class LiveSource {
    constructor(profile, secret, timeoutMs) {
      this.m = Object.assign({}, MATCH_DEFAULTS);
      for (const [k, v] of Object.entries(profile.match || {})) if (k in MATCH_DEFAULTS && typeof v === "string" && v.trim()) this.m[k] = v;
      this.nameRx = makeNameRx(this.m.name_regex);
      this.zbx = new ZbxClient(profile.url, profile.auth, secret, profile.username, timeoutMs);
    }
    version() { return this.zbx.version(); }
    async searchHosts(q) {
      const rows = await this.zbx.call("host.get", { output: ["hostid", "name"], search: { name: q, host: q }, searchByAny: true, monitored_hosts: true, sortfield: "name", limit: 50 });
      return rows.map(r => ({ id: r.hostid, name: r.name }));
    }
    async searchGroups(q) {
      const rows = await this.zbx.call("hostgroup.get", { output: ["name"], search: { name: q }, sortfield: "name", limit: 50 });
      return rows.map(r => ({ id: r.name, name: r.name }));
    }
    async groupHostIds(name) {
      const groups = await this.zbx.call("hostgroup.get", { output: ["groupid"], filter: { name: [name] } });
      if (!groups.length) return [];
      const hosts = await this.zbx.call("host.get", { output: ["hostid"], monitored_hosts: true, groupids: groups.map(g => g.groupid) });
      return hosts.map(h => h.hostid);
    }
    async allHostIds(cap) {      // every monitored host (by name) -> { ids, cut }: cut = there are more than `cap` of them
      cap = Math.trunc(cap);
      const rows = await this.zbx.call("host.get", { output: ["hostid"], monitored_hosts: true, sortfield: "name", limit: cap + 1 }), ids = rows.map(r => r.hostid);
      return { ids: ids.slice(0, cap), cut: ids.length > cap };
    }
    async interfacesOf(hostid) {
      const by = this._interfaces(await this._items([hostid])), rows = by[hostid] || {};
      return Object.entries(rows).sort((a, b) => natCmp(a[0], b[0])).map(([name, r]) => ({ name, alias: r.alias, oper: r.oper, speed_bps: r.speed_bps }));
    }
    async _batched(method, ids, size, make, limit) {      // ask in batches of `size` hosts; a batch whose reply reaches `limit` may be cut off, so it is split and asked again
      const ask = async chunk => {
        const rows = await this.zbx.call(method, make(chunk));
        if (limit && rows.length >= limit && chunk.length > 1) { const h = chunk.length >> 1; return (await Promise.all([ask(chunk.slice(0, h)), ask(chunk.slice(h))])).flat(); }
        return rows;
      };
      const chunks = []; for (let i = 0; i < ids.length; i += size) chunks.push(ids.slice(i, i + size));
      return (await Promise.all(chunks.map(ask))).flat();
    }
    _items(hostids) {
      const m = this.m;
      return this._batched("item.get", hostids, ITEM_BATCH, ids => ({ output: ["itemid", "hostid", "name", "key_", "lastvalue", "lastclock", "units", "value_type"], hostids: ids, monitored: true,
        search: { key_: [m.in_key, m.out_key, m.status_key, m.speed_key] }, searchByAny: true, startSearch: true, selectTags: ["tag", "value"], limit: ITEM_LIMIT }), ITEM_LIMIT);
    }
    _kind(key) {
      const m = this.m;
      for (const [kind, pref] of [["in", m.in_key], ["out", m.out_key], ["status", m.status_key], ["speed", m.speed_key]]) if (key.startsWith(pref)) return kind;
      return null;
    }
    _ifname(it) {
      const tags = {}; for (const t of it.tags || []) tags[t.tag] = t.value;
      if (tags[this.m.tag_name]) return [tags[this.m.tag_name], tags[this.m.alias_tag] || ""];
      const mm = this.nameRx.exec(it.name || "");
      if (mm && mm.groups && mm.groups.ifname) return [mm.groups.ifname.trim(), mm.groups.alias || ""];
      const mi = /\.(\d+)\]$/.exec(it.key_);
      return [mi ? "idx:" + mi[1] : it.key_, ""];
    }
    static _value(it, kind) {
      if (it.lastvalue == null || String(it.lastvalue).trim() === "") return null;
      let v = Number(it.lastvalue);
      if (!Number.isFinite(v)) return null;
      const units = String(it.units || "").trim();
      if ((kind === "in" || kind === "out") && (units === "Bps" || units === "B/s")) v *= 8;          // bytes/s -> bits/s (case matters: "bps" is already bits)
      if (kind === "speed") v *= { kbps: 1e3, mbps: 1e6, gbps: 1e9 }[units.toLowerCase()] || 1;
      return v;
    }
    _interfaces(items) {
      const out = {};
      for (const it of items) {
        const kind = this._kind(it.key_);
        if (!kind) continue;
        const [name, alias] = this._ifname(it);
        const perHost = out[it.hostid] || (out[it.hostid] = Object.create(null));
        const rec = perHost[name] || (perHost[name] = { alias: "", oper: null, in_bps: null, out_bps: null, speed_bps: null, clock: 0, ref: {} });
        if (alias && !rec.alias) rec.alias = alias;
        const v = LiveSource._value(it, kind);
        if (kind === "status") rec.oper = v != null ? Math.trunc(v) : null; else rec[{ in: "in_bps", out: "out_bps", speed: "speed_bps" }[kind]] = v;
        if (kind === "in" || kind === "out") { const u = String(it.units || "").trim(); rec.ref[kind] = { id: String(it.itemid), vt: parseInt(it.value_type, 10) || 0, mul: (u === "Bps" || u === "B/s") ? 8 : 1 }; }     // where the in / out history of this interface lives (the link graph reads it with history.get)
        rec.clock = Math.max(rec.clock, parseInt(it.lastclock || 0, 10) || 0);
      }
      return out;
    }
    async series(refs, t0, t1, trends) {      // history of the items `refs` point at -> one [[clock, bits per second], ...] list per ref (empty when a ref is null or its item does not hold numbers); trends: hourly averages (trend.get), an item that keeps no trends falls back to its history
      const want = new Map();
      for (const r of refs) if (r && (r.vt === 0 || r.vt === 3)) want.set(r.id, r);      // 0 = float, 3 = unsigned integer: the two types a traffic item can have
      const got = new Map([...want.keys()].map(i => [i, []]));
      let missing = [...want.keys()];
      if (trends && want.size) {
        const rows = await this.zbx.call("trend.get", { output: ["itemid", "clock", "value_avg"], itemids: [...want.keys()], time_from: Math.trunc(t0), time_till: Math.trunc(t1), limit: TREND_LIMIT });
        for (const row of rows) {
          const r = want.get(row.itemid);
          if (!r || row.value_avg == null || String(row.value_avg).trim() === "") continue;
          const v = Number(row.value_avg) * r.mul, c = parseInt(row.clock, 10) + 1800;      // an hour's average belongs in the middle of it
          if (Number.isFinite(v) && Number.isFinite(c)) got.get(row.itemid).push([c, v]);
        }
        missing = missing.filter(i => !got.get(i).length);      // no trends kept for these: read their history instead
      }
      for (const vt of [0, 3]) {
        const ids = missing.filter(i => want.get(i).vt === vt);
        if (!ids.length) continue;
        const rows = await this.zbx.call("history.get", { output: ["itemid", "clock", "value"], history: vt, itemids: ids, time_from: Math.trunc(t0), time_till: Math.trunc(t1), sortfield: "clock", sortorder: "DESC", limit: HIST_LIMIT });
        for (const row of rows) {
          const r = want.get(row.itemid);
          if (!r || row.value == null || String(row.value).trim() === "") continue;
          const v = Number(row.value) * r.mul, c = parseInt(row.clock, 10);
          if (Number.isFinite(v) && Number.isFinite(c)) got.get(row.itemid).push([c, v]);
        }
      }
      return refs.map(r => (r && got.has(r.id)) ? got.get(r.id).sort((a, b) => a[0] - b[0] || a[1] - b[1]) : []);
    }
    static _hostState(h) {
      if (String(h.status) === "1") return "disabled";
      const ifs = h.interfaces || [], snmp = ifs.filter(i => String(i.type) === "2"), use = snmp.length ? snmp : ifs;
      const av = use.map(i => String(i.available));
      return av.includes("2") ? "down" : av.includes("1") ? "up" : "unknown";
    }
    static _hostIp(h) {     // address Zabbix polls the host on: main SNMP interface, else main / first
      const ifs = h.interfaces || [], s = String;
      const pick = [ifs.filter(i => s(i.type) === "2" && s(i.main) === "1"), ifs.filter(i => s(i.type) === "2"), ifs.filter(i => s(i.main) === "1"), ifs].find(a => a.length);
      if (!pick) return "";
      const i = pick[0], first = s(i.useip) === "0" ? i.dns : i.ip, second = s(i.useip) === "0" ? i.ip : i.dns;
      return String(first || second || "");
    }
    static _inventory(h) {     // [operating system, device model] from the host inventory; blank when inventory is off or the field is empty
      const inv = (h.inventory && typeof h.inventory === "object" && !Array.isArray(h.inventory)) ? h.inventory : {};      // Zabbix sends [] for a host without inventory
      const pick = (...keys) => { for (const k of keys) { const v = String(inv[k] == null ? "" : inv[k]).split(/\s+/).filter(Boolean).join(" "); if (v) return v.slice(0, 80); } return ""; };
      return [pick("os_short", "os", "os_full"), pick("model", "hardware")];
    }
    async snapshot(hostids, minSev) {
      const snap = { ts: Date.now() / 1000, hosts: {}, interfaces: {} };
      const ids = [...new Set(hostids)].sort();
      if (!ids.length) return snap;
      const z = this.zbx;
      await z.ready();     // log in once up front so the parallel calls below do not race each other
      const [hosts, proxyRows, trig, items] = await Promise.all([
        this._batched("host.get", ids, HOST_BATCH, c => ({ output: ["hostid", "name", "status", "proxyid"], hostids: c, selectInterfaces: ["type", "available", "main", "ip", "dns", "useip"], selectInventory: ["os", "os_short", "os_full", "model", "hardware"] })),
        z.call("proxy.get", { output: ["proxyid", "name"] }).catch(e => { if (e instanceof ZbxError) return []; throw e; }),
        this._batched("trigger.get", ids, HOST_BATCH, c => ({ output: ["triggerid", "description", "priority"], hostids: c, monitored: true, only_true: true, skipDependent: true, min_severity: Math.trunc(minSev),
          selectHosts: ["hostid"], expandDescription: true, sortfield: "priority", sortorder: "DESC", limit: TRIG_LIMIT }), TRIG_LIMIT),
        this._items(ids),
      ]);
      const proxies = {}; for (const p of proxyRows) proxies[p.proxyid] = p.name;
      const per = {}, seenT = new Set();
      for (const t of trig) {
        if (seenT.has(t.triggerid)) continue;      // a trigger that spans hosts of two batches comes back from both: count it once
        seenT.add(t.triggerid);
        for (const h of t.hosts || []) (per[h.hostid] || (per[h.hostid] = [])).push({ name: t.description, severity: parseInt(t.priority, 10) });
      }
      for (const h of hosts) {
        const probs = (per[h.hostid] || []).slice().sort((a, b) => b.severity - a.severity);
        snap.hosts[h.hostid] = { id: h.hostid, name: h.name, status: LiveSource._hostState(h), ip: LiveSource._hostIp(h), os: LiveSource._inventory(h)[0], model: LiveSource._inventory(h)[1], proxy: (h.proxyid != null && h.proxyid !== "0") ? (proxies[h.proxyid] || "") : "",
          severity: probs.length ? probs[0].severity : 0, problems: probs.length, top: probs.slice(0, 5) };
      }
      snap.interfaces = this._interfaces(items);
      return snap;
    }
    async probe() {
      const z = this.zbx, m = this.m;
      const version = await z.version();
      const hosts = parseInt(await z.call("host.get", { countOutput: true, monitored_hosts: true }), 10);
      let proxies = null;
      try { proxies = parseInt(await z.call("proxy.get", { countOutput: true }), 10); } catch (e) { if (!(e instanceof ZbxError)) throw e; }
      const items = parseInt(await z.call("item.get", { countOutput: true, monitored: true, search: { key_: [m.in_key, m.out_key] }, searchByAny: true, startSearch: true }), 10);
      const rows = await z.call("host.get", { output: ["hostid", "name", "status", "proxyid"], monitored_hosts: true, sortfield: "name", limit: 8, selectInterfaces: ["type", "available", "main"] });
      return { version, hosts, proxies, interface_items: items, sample: rows.map(r => ({ id: r.hostid, name: r.name, status: LiveSource._hostState(r) })) };
    }
  }

  // ---------------------------------------------------------------- simulated network (the "Demo data" connection)
  const DEMO_HOSTS = [   // id, name, proxy, group, role
    ["10001", "core-c9500-1", "Proxy-A", "Cisco core", "core"], ["10002", "core-c9500-2", "Proxy-A", "Cisco core", "core"],
    ["10003", "pe-c8300-1", "Proxy-A", "Cisco WAN routers", "router"], ["10004", "pe-c8300-2", "Proxy-B", "Cisco WAN routers", "router"],
    ["10005", "ce-c8200l-1", "Proxy-B", "Cisco edge", "edge"], ["10006", "ce-c8200l-2", "Proxy-B", "Cisco edge", "edge"],
    ["10007", "ce-c8200l-3", "Proxy-B", "Cisco edge", "edge"], ["10008", "dist-c9300-stack", "Proxy-A", "Cisco core", "core"],
  ];
  const DEMO_INFO = {   // host inventory the demo "reports": id -> [operating system, device model]; ce-c8200l-2 has none, to show the "unknown" group
    10001: ["Cisco IOS XE 17.9.4", "Catalyst C9500-24Y4C"], 10002: ["Cisco IOS XE 17.9.4", "Catalyst C9500-24Y4C"], 10003: ["Cisco IOS XE 17.9.4", "C8300-2N2S-4T2X"], 10004: ["Cisco IOS XE 17.6.5", "C8300-2N2S-4T2X"],
    10005: ["Cisco IOS XE 17.9.4", "C8200L-1N-4T"], 10006: ["", ""], 10007: ["Cisco IOS XE 17.6.5", "C8200L-1N-4T"], 10008: ["Cisco IOS XE 17.6.5", "Catalyst C9300-48P"],
  };
  const DEMO_IFACES = {
    core: [1, 2, 3, 4, 5, 6].map(i => ["Te1/0/" + i, 10e9]),
    router: [["Gi0/0/0", 1e9], ["Gi0/0/1", 1e9], ["Te0/0/2", 10e9], ["Te0/0/3", 10e9]],
    edge: [["Gi0/0/0", 1e9], ["Gi0/0/1", 1e9], ["Gi0/1/0", 1e9]],
  };
  const DEMO_LINKS = [   // id, hostA, ifA, hostB, ifB, name
    ["L1", "10001", "Te1/0/1", "10002", "Te1/0/1", "Core interconnect"], ["L2", "10001", "Te1/0/2", "10003", "Te0/0/2", "Core1 - PE1"],
    ["L3", "10002", "Te1/0/2", "10004", "Te0/0/2", "Core2 - PE2"], ["L4", "10003", "Te0/0/3", "10004", "Te0/0/3", "PE1 - PE2"],
    ["L5", "10003", "Gi0/0/0", "10005", "Gi0/0/0", "Branch 1"], ["L6", "10003", "Gi0/0/1", "10006", "Gi0/0/0", "Branch 2 (primary)"],
    ["L7", "10004", "Gi0/0/0", "10007", "Gi0/0/0", "Branch 3"], ["L8", "10004", "Gi0/0/1", "10006", "Gi0/0/1", "Branch 2 (backup)"],
    ["L9", "10001", "Te1/0/3", "10008", "Te1/0/1", "Core1 - Dist"], ["L10", "10002", "Te1/0/3", "10008", "Te1/0/2", "Core2 - Dist"],
  ];
  const DEMO_ACCESS = 120;     // extra access switches (acc-sw-001 ..) that only the "All devices" tab lists - so there is something to page through
  const accessInfo = i => i % 11 === 0 ? ["", ""] : [i % 3 ? "Cisco IOS XE 17.6.5" : "Cisco IOS 15.2(7)E4", i % 2 ? "C9200-48P" : "C2960X-48FPD-L"];     // [os, model]; every 11th has none
  const HOT = new Set(["L6"]);    // always busy: shows the red end of the colour scale
  const FLAP = new Set(["L7"]);   // goes down for 45 s every 3 minutes
  const HOSTS = {}, PEER = {};
  for (const [id, name, proxy, group, role] of DEMO_HOSTS) HOSTS[id] = { name, proxy, group, ifs: Object.fromEntries(DEMO_IFACES[role].map(([n, s]) => [n, { speed: s, alias: "" }])) };
  for (let i = 1; i <= DEMO_ACCESS; i++) HOSTS[String(10100 + i)] = { name: "acc-sw-" + String(i).padStart(3, "0"), proxy: i % 2 ? "Proxy-A" : "Proxy-B", group: "Cisco access",
    ifs: Object.fromEntries([1, 2, 3, 4].map(k => ["Gi1/0/" + k, { speed: 1e9, alias: "user port " + k }])) };
  for (const [lid, ha, ia, hb, ib] of DEMO_LINKS) {
    PEER[ha + "|" + ia] = [lid, "a"]; PEER[hb + "|" + ib] = [lid, "b"];
    HOSTS[ha].ifs[ia].alias = "to " + HOSTS[hb].name; HOSTS[hb].ifs[ib].alias = "to " + HOSTS[ha].name;
  }
  const CRC_TABLE = (() => { const t = new Uint32Array(256); for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; } return t; })();
  const crc32 = s => { let c = 0xFFFFFFFF; for (const b of new TextEncoder().encode(s)) c = CRC_TABLE[(c ^ b) & 255] ^ (c >>> 8); return (c ^ 0xFFFFFFFF) >>> 0; };
  function util(key, dir, t) {
    const h = crc32(key), phase = (h % 628) / 100, base = 0.10 + (h % 45) / 100;
    const wave = 0.5 + 0.5 * Math.sin(t / 80 + phase + (dir === "ba" ? 1.7 : 0));
    const noise = ((crc32(key + ":" + dir + ":" + Math.floor(t / 6)) % 1000) / 1000 - 0.5) * 0.05;
    const day = 0.85 + 0.15 * Math.sin((t - 14400) * (2 * Math.PI / 86400));      // busier in the afternoon: the daily shape that 7 / 14 / 30 day graphs show
    let u = base * (0.45 + 0.9 * wave) * day + noise;
    if (HOT.has(key)) u = 0.78 + 0.18 * wave;
    return Math.max(0, Math.min(0.99, u));
  }
  const flapping = (lid, t) => FLAP.has(lid) && Math.floor(t / 45) % 4 === 2;
  function ifAt(hid, name, t) {      // [bits/s in, bits/s out, ifOperStatus] of one simulated interface at time t
    const speed = HOSTS[hid].ifs[name].speed, acc = parseInt(hid, 10) > 10100 ? parseInt(hid, 10) - 10100 : 0, lk = PEER[hid + "|" + name];
    if (acc) {
      const port = parseInt(name.slice(-1), 10);
      if (acc % 17 === 0 || (acc + port) % 19 === 0) return [0, 0, 2];      // the switch itself is down / this port is down
      return [util(hid + "/" + name, "ba", t) * speed, util(hid + "/" + name, "ab", t) * speed, 1];
    }
    if (lk) {
      const [lid, side] = lk;
      if (flapping(lid, t)) return [0, 0, 2];
      const ab = util(lid, "ab", t) * speed, ba = util(lid, "ba", t) * speed;
      return side === "a" ? [ba, ab, 1] : [ab, ba, 1];
    }
    return [util(hid + "/" + name, "ba", t) * speed * 0.06, util(hid + "/" + name, "ab", t) * speed * 0.06, 1];
  }
  function demoSnapshot(hostids, minSev) {
    const t = Date.now() / 1000, snap = { ts: t, hosts: {}, interfaces: {} };
    for (const hid of [...new Set(hostids)].sort()) {
      const h = HOSTS[hid];
      if (!h) continue;
      let probs = [];
      if (hid === "10007" && flapping("L7", t)) probs.push({ name: "Interface Gi0/0/0(to pe-c8300-2): Link down", severity: 4 });
      if (hid === "10002" && Math.floor(t / 120) % 3 === 1) probs.push({ name: "High CPU utilization (over 90% for 5m)", severity: 3 });
      if (hid === "10005" && Math.floor(t / 200) % 2 === 0) probs.push({ name: "High memory utilization (>90% for 5m)", severity: 2 });
      const acc = parseInt(hid, 10) > 10100 ? parseInt(hid, 10) - 10100 : 0;      // access switch number (0 = one of the 8 core / WAN devices)
      let status = "up", ip = "10.0.0." + (parseInt(hid, 10) - 10000), [os_, model] = DEMO_INFO[hid] || ["", ""];
      if (acc) {
        status = acc % 17 === 0 ? "down" : "up"; ip = "10.1." + (Math.floor(acc / 40) + 1) + "." + (acc % 40 + 1); [os_, model] = accessInfo(acc);
        if (status === "down") probs.push({ name: "SNMP agent is not available for 5m", severity: 4 });
        if (status === "up" && acc % 9 === 0) probs.push({ name: "High memory utilization (>90% for 5m)", severity: 2 });
        if (status === "up" && acc % 23 === 0) probs.push({ name: "High CPU utilization (over 90% for 5m)", severity: 3 });
      }
      probs = probs.filter(p => p.severity >= minSev);
      snap.hosts[hid] = { id: hid, name: h.name, status, ip, os: os_, model, proxy: h.proxy, severity: Math.max(0, ...probs.map(p => p.severity)), problems: probs.length, top: probs };
      const ifs = {};
      for (const [name, meta] of Object.entries(h.ifs)) {
        const [inn, out, oper] = ifAt(hid, name, t);
        ifs[name] = { alias: meta.alias, oper, in_bps: inn, out_bps: out, speed_bps: meta.speed, clock: Math.floor(t),
          ref: { in: { hid, if: name, dir: "in" }, out: { hid, if: name, dir: "out" } } };
      }
      snap.interfaces[hid] = ifs;
    }
    return snap;
  }
  const demoSource = {
    version: async () => "7.0 (demo)",
    searchHosts: async q => Object.entries(HOSTS).sort((a, b) => (a[1].name < b[1].name ? -1 : 1)).filter(([, h]) => h.name.toLowerCase().includes(q.toLowerCase())).slice(0, 50).map(([id, h]) => ({ id, name: h.name })),
    searchGroups: async q => [...new Set(Object.values(HOSTS).map(h => h.group))].sort().filter(n => n.toLowerCase().includes(q.toLowerCase())).map(n => ({ id: n, name: n })),
    groupHostIds: async name => Object.entries(HOSTS).filter(([, h]) => h.group === name).map(([id]) => id),
    allHostIds: async cap => { const ids = Object.entries(HOSTS).sort((a, b) => (a[1].name < b[1].name ? -1 : 1)).map(([id]) => id); return { ids: ids.slice(0, Math.trunc(cap)), cut: ids.length > Math.trunc(cap) }; },
    interfacesOf: async hid => HOSTS[hid] ? Object.entries(HOSTS[hid].ifs).sort((a, b) => natCmp(a[0], b[0])).map(([name, m]) => ({ name, alias: m.alias, oper: 1, speed_bps: m.speed })) : [],
    snapshot: async (ids, minSev) => demoSnapshot(ids, minSev),
    series: async (refs, t0, t1, trends) => {      // the simulated history behind the link graph: the same curves the snapshots follow, one sample every GRAPH_STEP seconds; trends: one value per hour (the average of twelve samples, dated to the middle of the hour), like Zabbix trends
      const stamps = [];
      if (trends) { for (let h = (Math.floor(Math.trunc(t0) / 3600) + 1) * 3600; h < Math.trunc(t1) - 3599; h += 3600) stamps.push([h + 1800, Array.from({ length: 12 }, (_, k) => h + 150 + 300 * k)]); }
      else { for (let c = (Math.floor(Math.trunc(t0) / GRAPH_STEP) + 1) * GRAPH_STEP; c <= Math.trunc(t1); c += GRAPH_STEP) stamps.push([c, [c]]); }
      return refs.map(r => {
        if (!r) return [];
        const k = r.dir === "in" ? 0 : 1;
        return stamps.map(([c, xs]) => { let sum = 0; for (const x of xs) sum += ifAt(r.hid, r.if, x)[k]; return [c, sum / xs.length]; });
      });
    },
  };

  // ---------------------------------------------------------------- layouts
  function demoLayout() {
    const node = (id, host, x, y) => ({ id, host, hostname: HOSTS[host].name, label: "", x, y });
    const link = (lid, na, nb) => { const l = DEMO_LINKS.find(l => l[0] === lid); return { id: lid, name: l[5], a: { node: na, if: l[2] }, b: { node: nb, if: l[4] }, bandwidth_bps: null }; };
    const wan = { name: "Core & WAN", width: 1200, height: 600,
      nodes: [node("n-c1", "10001", 260, 130), node("n-c2", "10002", 940, 130), node("n-d1", "10008", 600, 290), node("n-p1", "10003", 260, 450), node("n-p2", "10004", 940, 430)],
      links: [link("L1", "n-c1", "n-c2"), link("L2", "n-c1", "n-p1"), link("L3", "n-c2", "n-p2"), link("L4", "n-p1", "n-p2"), link("L9", "n-c1", "n-d1"), link("L10", "n-c2", "n-d1")] };
    wan.links[3].via = [{ x: 600, y: 540 }];      // shows a bent link (its bend point moved by hand): L4 is routed underneath the others
    const br = { name: "Branches", width: 1200, height: 600,
      nodes: [node("n-p1", "10003", 260, 130), node("n-p2", "10004", 940, 130), node("n-e1", "10005", 150, 440), node("n-e2", "10006", 600, 440), node("n-e3", "10007", 1050, 440)],
      links: [link("L5", "n-p1", "n-e1"), link("L6", "n-p1", "n-e2"), link("L7", "n-p2", "n-e3"), link("L8", "n-p2", "n-e2")] };
    return {
      version: 1, maps: { "m-wan": wan, "m-br": br },
      tabs: [
        { id: "t-wan", name: "Core & WAN", type: "map", map: "m-wan" }, { id: "t-br", name: "Branches", type: "map", map: "m-br" },
        { id: "t-rt", name: "Routers", type: "devices", hosts: [], hostnames: {}, groups: ["Cisco WAN routers", "Cisco edge"], ifaces: 4 },
        { id: "t-core", name: "Core switches", type: "devices", hosts: ["10001", "10002", "10008"], hostnames: { "10001": "core-c9500-1", "10002": "core-c9500-2", "10008": "dist-c9300-stack" }, groups: [], ifaces: 4 },
        { id: "t-all", name: "All devices", type: "devices", hosts: [], hostnames: {}, groups: [], ifaces: 4, all: true, style: "list" },
      ],
      playlists: [{ id: "p-noc", name: "NOC wall", loop: true, items: [{ tab: "t-wan", seconds: 20 }, { tab: "t-br", seconds: 20 }, { tab: "t-rt", seconds: 15 }, { tab: "t-core", seconds: 15 }] }],
      settings: {},
    };
  }
  function liveStarterLayout() {   // first layout of a real connection: an "All devices" tab so hosts show up right away + an empty weathermap
    return { version: 1, maps: { m1: { name: "Network", width: 1600, height: 900, nodes: [], links: [] } },
      tabs: [{ id: "t-all", name: "All devices", type: "devices", hosts: [], hostnames: {}, groups: [], ifaces: 4, all: true, style: "list" }, { id: "t1", name: "Network", type: "map", map: "m1" }],
      playlists: [], settings: {} };
  }
  const MAX_VIA = 8;      // bend points per link
  function validateLayout(d) {   // same rules as the Python server
    const HID = /^[0-9]{1,12}$/;
    const fail = m => { throw new Error(m); };
    const isObj = v => v !== null && typeof v === "object" && !Array.isArray(v);
    const sid = (v, what) => (typeof v === "string" && ID_RX.test(v)) ? v : fail("bad " + what + " id: " + JSON.stringify(v));
    const txt = (v, n = 80) => String(v == null ? "" : v).trim().slice(0, n);
    const num = (v, lo, hi, def) => { if (v == null || v === "") return def; const f = Number(v); return Number.isFinite(f) ? Math.max(lo, Math.min(hi, f)) : def; };
    if (!isObj(d)) fail("layout must be an object");
    const out = { version: 1, maps: {}, tabs: [], playlists: [], settings: {} };
    const maps = d.maps || {};
    if (!isObj(maps) || Object.keys(maps).length > 200) fail("too many maps");
    for (const [mid, m] of Object.entries(maps)) {
      sid(mid, "map"); if (!isObj(m)) fail("bad map");
      const nodes = [], ids = new Set();
      for (const n of (m.nodes || []).slice(0, 500)) {
        const nid = sid(n.id, "node"), host = txt(n.host, 20);
        if (ids.has(nid) || !HID.test(host)) fail("bad node " + nid);
        ids.add(nid);
        const node = { id: nid, host, hostname: txt(n.hostname, 100), label: txt(n.label, 60), x: num(n.x, -5000, 20000, 0), y: num(n.y, -5000, 20000, 0) };
        if (n.shape === "circle" || n.shape === "triangle") node.shape = n.shape;      // default is square
        if (n.size === "small" || n.size === "large" || n.size === "xlarge") node.size = n.size;      // default is medium
        if (n.notify === false) node.notify = false;      // the problem count on the device is on unless this says false
        nodes.push(node);
      }
      const links = [], lids = new Set();
      for (const l of (m.links || []).slice(0, 2000)) {
        const lid = sid(l.id, "link"), a = l.a || {}, b = l.b || {};
        if (lids.has(lid) || !ids.has(a.node) || !ids.has(b.node)) fail("bad link " + lid);
        lids.add(lid);
        const link = { id: lid, name: txt(l.name), a: { node: a.node, if: txt(a.if, 100) }, b: { node: b.node, if: txt(b.if, 100) },
          bandwidth_bps: (l.bandwidth_bps == null || l.bandwidth_bps === "") ? null : num(l.bandwidth_bps, 1, 1e13, null) };
        const via = [];      // bend points: the line is routed through them, in order
        for (const p of (Array.isArray(l.via) ? l.via : []).slice(0, MAX_VIA)) {
          if (!isObj(p)) continue;
          const x = num(p.x, -5000, 20000, null), y = num(p.y, -5000, 20000, null);
          if (x !== null && y !== null) via.push({ x: Math.floor(x + 0.5), y: Math.floor(y + 0.5) });
        }
        if (via.length) link.via = via;
        if (l.if_show === false) link.if_show = false;      // interface tags are shown unless a link hides them; if_pos fixes their side (without it: automatic)
        if (l.if_pos === "above" || l.if_pos === "below") link.if_pos = l.if_pos;
        links.push(link);
      }
      out.maps[mid] = { name: txt(m.name, 60), width: Math.trunc(num(m.width, 400, 8000, 1600)), height: Math.trunc(num(m.height, 300, 8000, 900)), nodes, links };
      const lg = isObj(m.legend) ? m.legend : null, leg = {};      // the usage meter: vertical or horizontal (the default), and where it was dragged to (x, y: 0..100, both or neither)
      if (lg) {
        if (lg.dir === "v") leg.dir = "v";
        if (typeof lg.x === "number" && typeof lg.y === "number" && Number.isFinite(lg.x) && Number.isFinite(lg.y)) {
          leg.x = Math.floor(Math.max(0, Math.min(100, lg.x)) * 10 + 0.5) / 10; leg.y = Math.floor(Math.max(0, Math.min(100, lg.y)) * 10 + 0.5) / 10;
        }
      }
      if (Object.keys(leg).length) out.maps[mid].legend = leg;
    }
    const tabIds = new Set();
    for (const t of (d.tabs || []).slice(0, 200)) {
      const tid = sid(t.id, "tab");
      if (tabIds.has(tid) || (t.type !== "map" && t.type !== "devices")) fail("bad tab " + tid);
      tabIds.add(tid);
      const tab = { id: tid, name: txt(t.name, 40) || "Tab", type: t.type };
      if (t.type === "map") {
        if (!out.maps[t.map]) fail("tab " + tid + " points at a missing map");
        tab.map = t.map;
      } else {
        const hosts = (t.hosts || []).slice(0, 300).map(h => txt(h, 20));
        if (!hosts.every(h => HID.test(h))) fail("bad host id in devices tab");
        const names = t.hostnames || {};
        tab.hosts = hosts; tab.hostnames = Object.fromEntries(hosts.map(h => [h, txt(names[h], 100)]));
        tab.groups = (t.groups || []).slice(0, 30).map(g => txt(g, 100)); tab.ifaces = Math.trunc(num(t.ifaces, 0, 12, 4));
        tab.all = !!t.all; tab.style = t.style === "list" || t.style === "heatmap" ? t.style : "cards";      // (older layouts also stored a "limit" of 40 here - ignored now: "all" means all)
      }
      out.tabs.push(tab);
    }
    const plIds = new Set();
    for (const p of (d.playlists || []).slice(0, 50)) {
      const pid = sid(p.id, "playlist");
      if (plIds.has(pid)) fail("duplicate playlist id");
      plIds.add(pid);
      out.playlists.push({ id: pid, name: txt(p.name, 40) || "Playlist", loop: p.loop === undefined ? true : !!p.loop,
        items: (p.items || []).slice(0, 100).filter(i => tabIds.has(i.tab)).map(i => ({ tab: i.tab, seconds: Math.trunc(num(i.seconds, 3, 3600, 20)) })) });
    }
    const scale = (d.settings || {}).scale;
    if (Array.isArray(scale) && scale.length) {
      const clean = scale.slice(0, 12).filter(x => Array.isArray(x) && typeof x[1] === "string" && /^#[0-9a-fA-F]{6}$/.test(x[1])).map(([t, c]) => [num(t, 0, 1000, 0), c]).sort((a, b) => a[0] - b[0]);
      if (clean.length) out.settings.scale = clean;
    }
    return out;
  }
  const layoutKey = id => id === "demo" ? "trunkvista-demo-layout-v1" : "trunkvista-layout-v1:" + id;   // one layout per connection
  function loadLayout(id) {
    try { const s = ls.get(layoutKey(id)); if (s) return validateLayout(JSON.parse(s)); } catch (e) { /* corrupt or blocked: start from the sample */ }
    return validateLayout(id === "demo" ? demoLayout() : liveStarterLayout());
  }
  const saveLayout = l => { layout = l; ls.set(layoutKey(conns.active), JSON.stringify(l)); };

  // ---------------------------------------------------------------- saved connections
  const CONN_KEY = "trunkvista-connections-v1", SECRET_KEY = "trunkvista-secret:";
  let conns = { active: "demo", profiles: [] }, layout = null, source = null, sourceError = "", cache = null;
  const randId = () => "c-" + Array.from(crypto.getRandomValues(new Uint8Array(4)), b => b.toString(16).padStart(2, "0")).join("");
  const str = (v, n) => String(v == null ? "" : v).trim().slice(0, n);
  function cleanMatch(m) {
    const out = {};
    if (m && typeof m === "object") for (const k of Object.keys(MATCH_DEFAULTS)) if (typeof m[k] === "string" && m[k].trim()) out[k] = m[k].trim().slice(0, 300);
    if (out.name_regex) {
      let rx; try { rx = makeNameRx(out.name_regex); } catch (e) { throw new Error("name_regex is not a valid regular expression: " + e.message); }
      if (!/\(\?P?<ifname>/.test(out.name_regex)) throw new Error("name_regex needs a named group (?P<ifname>...)");
    }
    return out;
  }
  function cleanProfile(d, existing) {    // submitted fields -> a clean profile; blank secrets keep the stored ones
    if (!d || typeof d !== "object") throw new Error("bad connection");
    const url = str(d.url, 300);
    if (!/^https?:\/\/[^\s/]+/i.test(url) && !(/^https?:$/.test(location.protocol) && url && !/^[a-z][a-z0-9+.-]*:/i.test(url))) throw new Error("The address must start with http:// or https://");
    const auth = d.auth === "password" ? "password" : "token", same = !!existing && existing.auth === auth;
    let host = ""; try { host = new URL(apiUrl(url)).hostname; } catch (e) { /* name falls back below */ }
    const p = { id: existing ? existing.id : "", name: str(d.name, 60) || host || "Zabbix", url, auth, remember: d.remember !== false,
      username: auth === "password" ? str(d.username, 100) : "", token: "", password: "", match: cleanMatch(d.match) };
    if (auth === "token") p.token = str(d.token, 400) || (same ? existing.token || "" : "");
    else { if (!p.username) throw new Error("Enter the Zabbix username"); p.password = str(d.password, 400) || (same ? existing.password || "" : ""); }
    return p;
  }
  const publicProfile = p => ({ id: p.id, name: p.name, url: p.url, auth: p.auth, username: p.username || "", has_secret: !!(p.auth === "token" ? p.token : p.password),
    verify_tls: true, remember: p.remember !== false, match: Object.assign({}, p.match || {}), readonly: false });
  function persistConns() {
    const stripped = conns.profiles.map(p => p.remember === false ? Object.assign({}, p, { token: "", password: "" }) : p);   // not remembered: kept for this tab only
    ls.set(CONN_KEY, JSON.stringify({ active: conns.active, profiles: stripped }));
    for (const p of conns.profiles) if (p.remember === false) ss.set(SECRET_KEY + p.id, JSON.stringify({ token: p.token || "", password: p.password || "" }));
  }
  function loadConns() {
    try {
      const d = JSON.parse(ls.get(CONN_KEY) || "null");
      if (d && Array.isArray(d.profiles)) {
        conns.profiles = d.profiles.filter(p => p && ID_RX.test(p.id) && p.id !== "demo").map(p => {
          const q = Object.assign({ remember: true, token: "", password: "", username: "", match: {} }, p);
          if (q.remember === false) { try { const s = JSON.parse(ss.get(SECRET_KEY + q.id) || "{}"); q.token = s.token || ""; q.password = s.password || ""; } catch (e) { /* none */ } }
          return q;
        });
        conns.active = String(d.active || "demo");
      }
    } catch (e) { /* start clean */ }
    if (conns.active !== "demo" && !conns.profiles.some(p => p.id === conns.active)) conns.active = "demo";
  }
  const connName = () => conns.active === "demo" ? "Demo data" : ((conns.profiles.find(p => p.id === conns.active) || {}).name || "");
  const connInfo = () => ({ mode: "standalone", active: conns.active, demo_available: true, profiles: conns.profiles.map(publicProfile), match_defaults: MATCH_DEFAULTS });
  function resetCaches() { gcache.clear(); cache = { snap: null, key: "", t: 0, inflight: null, error: "", lastOk: 0, retryAt: 0, version: "", lists: {}, groupIds: {}, allIds: [], allCut: false }; }
  function activate(id) {
    conns.active = id; persistConns(); layout = loadLayout(id); resetCaches(); source = null; sourceError = "";
    if (id === "demo") { source = demoSource; return; }
    const p = conns.profiles.find(x => x.id === id), secret = p && (p.auth === "token" ? p.token : p.password);
    if (!p) sourceError = "No connection selected.";
    else if (!secret) sourceError = "The saved secret is not available in this browser session. Open Connections and enter it again.";
    else { try { source = new LiveSource(p, secret); } catch (e) { sourceError = e.message; } }
  }

  // ---------------------------------------------------------------- views (same logic as the Python server)
  const emptySnap = () => ({ ts: 0, hosts: {}, interfaces: {} });
  function needed() {
    const ids = new Set(), groups = new Set(); let wantAll = false;
    for (const m of Object.values(layout.maps)) for (const n of m.nodes) ids.add(n.host);
    for (const t of layout.tabs) if (t.type === "devices") { t.hosts.forEach(h => ids.add(h)); t.groups.forEach(g => groups.add(g)); if (t.all) wantAll = true; }
    return { ids, groups, wantAll };
  }
  const listCached = async (key, loader) => {
    const hit = cache.lists[key];
    if (hit && conns.active !== "demo" && Date.now() - hit.t < LIST_TTL_MS) return hit.v;
    const v = await loader(); cache.lists[key] = { t: Date.now(), v }; return v;
  };
  async function compute() {
    const demo = conns.active === "demo", src = source, c = cache;
    if (!src) return { snap: emptySnap(), groupIds: {}, allIds: [], allCut: false, error: sourceError, lastOk: 0 };
    if (!demo && c.error && Date.now() < c.retryAt) return { snap: c.snap || emptySnap(), groupIds: c.groupIds, allIds: c.allIds, allCut: c.allCut, error: c.error, lastOk: c.lastOk };   // back off after a failure
    const { ids, groups, wantAll } = needed();
    try {
      const groupIds = {};
      for (const g of groups) groupIds[g] = await listCached("g:" + g, () => src.groupHostIds(g));
      const all = wantAll ? await listCached("all", () => src.allHostIds(MAX_HOSTS)) : { ids: [], cut: false }, allIds = all.ids;
      const want = new Set([...ids, ...allIds, ...Object.values(groupIds).flat()]), key = [...want].sort().join(",");
      if (demo || !c.snap || c.key !== key || Date.now() - c.t >= pollMs) {
        const snap = await src.snapshot([...want], MIN_SEVERITY);
        if (!c.version) c.version = await src.version();
        if (c !== cache) return { snap: emptySnap(), groupIds: {}, allIds: [], allCut: false, error: "", lastOk: 0 };    // the connection was switched meanwhile
        c.snap = snap; c.key = key; c.t = Date.now(); c.lastOk = Date.now() / 1000; c.error = "";
      }
      c.groupIds = groupIds; c.allIds = allIds; c.allCut = all.cut;
      return { snap: c.snap, groupIds, allIds, allCut: all.cut, error: "", lastOk: c.lastOk };
    } catch (e) {
      c.error = e.message || String(e); c.retryAt = Date.now() + RETRY_MS;
      return { snap: c.snap || emptySnap(), groupIds: c.groupIds, allIds: c.allIds, allCut: c.allCut, error: c.error, lastOk: c.lastOk };
    }
  }
  let lastRefresh = 0;
  async function doRefresh() {     // Refresh button: new Zabbix session (re-login), caches and error back-off dropped, poll right now
    if (Date.now() - lastRefresh < 2000) return { ok: !cache.error };
    lastRefresh = Date.now();
    const old = cache;
    resetCaches();
    cache.snap = old.snap; cache.groupIds = old.groupIds; cache.allIds = old.allIds; cache.allCut = old.allCut; cache.lastOk = old.lastOk;     // keep showing the last good data if Zabbix is still failing
    if (conns.active !== "demo") {
      const p = conns.profiles.find(x => x.id === conns.active), secret = p && (p.auth === "token" ? p.token : p.password);
      if (p && secret) { try { source = new LiveSource(p, secret); sourceError = ""; } catch (e) { sourceError = e.message; } }
    }
    const d = await currentData();
    return { ok: !d.error, error: d.error };
  }
  async function currentData() {      // one fetch at a time: concurrent view requests share the cached result
    while (cache.inflight) { try { await cache.inflight; } catch (e) { /* ignore */ } }
    const p = compute(); cache.inflight = p;
    try { return await p; } finally { if (cache.inflight === p) cache.inflight = null; }
  }
  function resolveLink(l, nodes, snap, now) {
    const na = nodes[l.a.node], nb = nodes[l.b.node];
    const ia = na ? (snap.interfaces[na.host] || {})[l.a.if] : null, ib = nb ? (snap.interfaces[nb.host] || {})[l.b.if] : null;
    const g = (i, k) => i ? (i[k] == null ? null : i[k]) : null;
    const first = (...v) => { const x = v.find(y => y != null); return x === undefined ? null : x; };
    const ab = first(g(ia, "out_bps"), g(ib, "in_bps")), ba = first(g(ia, "in_bps"), g(ib, "out_bps"));
    const speeds = [g(ia, "speed_bps"), g(ib, "speed_bps")].filter(Boolean), override = l.bandwidth_bps || null;
    const speed = override || (speeds.length ? Math.min(...speeds) : null);
    const opers = [g(ia, "oper"), g(ib, "oper")].filter(o => o != null), clocks = [g(ia, "clock"), g(ib, "clock")].filter(Boolean);
    const newest = clocks.length ? Math.max(...clocks) : 0;
    let status;
    if (!ia && !ib) status = "unknown";
    else if (newest && now - newest > STALE_SECONDS) status = "stale";
    else if (opers.some(o => DOWN_STATES.has(o))) status = "down";
    else if (opers.length && opers.every(o => o === 1)) status = "up";
    else if (opers.length) status = "warn";
    else if (ab != null || ba != null) status = "up";
    else status = "unknown";
    const pct = v => (v != null && speed) ? v / speed * 100 : null;
    return { id: l.id, status, ab_bps: ab, ba_bps: ba, ab_pct: pct(ab), ba_pct: pct(ba), speed, speed_src: override ? "override" : (speed ? "zabbix" : ""), clock: newest || null };
  }
  function thin(points, t0, t1, n = GRAPH_POINTS) {      // at most about `n` points for a graph: when there are more, each time slice of the window becomes the average of its samples
    const half = x => Math.floor(x + 0.5);
    if (points.length <= n) return points.map(([c, v]) => [Math.trunc(c), half(v)]);
    const width = (t1 - t0) / n, acc = new Map();
    for (const [c, v] of points) {
      const k = Math.min(n - 1, Math.max(0, Math.floor((c - t0) / width))), a = acc.get(k) || (acc.set(k, [0, 0, 0]), acc.get(k));
      a[0] += c; a[1] += v; a[2] += 1;
    }
    return [...acc.entries()].sort((x, y) => x[0] - y[0]).map(([, a]) => [half(a[0] / a[2]), half(a[1] / a[2])]);
  }
  async function linkGraph(tab, lid, minutes) {      // in / out traffic of one link over the last `minutes` (same logic as the Python server's link_graph); null when the tab or link does not exist
    if (tab.type !== "map") return null;
    const m = layout.maps[tab.map], l = m.links.find(x => x.id === lid);
    if (!l) return null;
    const src = source;
    if (!src) throw new ZbxError(sourceError || "not connected");
    const d = await currentData(), ifs = d.snap.interfaces, nodes = Object.fromEntries(m.nodes.map(n => [n.id, n]));
    const end = side => { const n = nodes[l[side].node]; return n ? ((ifs[n.host] || {})[l[side].if] || null) : null; };
    const pick = (...cands) => {      // same preference as resolveLink: the first end that has a current value, else the first that has an item -> [ref, current value]
      const c = cands.filter(([i, k]) => i && i.ref && i.ref[k]), ok = c.find(([i, k]) => i[k + "_bps"] != null), x = ok || c[0];
      return x ? [x[0].ref[x[1]], x[0][x[1] + "_bps"] == null ? null : x[0][x[1] + "_bps"]] : [null, null];
    };
    const ia = end("a"), ib = end("b"), [refAb, nowAb] = pick([ia, "out"], [ib, "in"]), [refBa, nowBa] = pick([ia, "in"], [ib, "out"]), refs = [refAb, refBa];
    if (!GRAPH_RANGES.includes(minutes)) minutes = GRAPH_DEFAULT;
    const key = JSON.stringify([conns.active, refs, minutes]), now = Date.now(), hit = gcache.get(key);
    if (hit && hit.exp > now) return hit.v;
    const t1 = Math.floor(now / 1000), t0 = t1 - minutes * 60, trends = minutes > TREND_FROM;
    let [ab, ba] = await src.series(refs, t0, t1, trends);
    if (trends) {      // trends lag by up to an hour: close the gap to the present with the current value
      if (ab.length && nowAb != null) ab = ab.concat([[t1, nowAb]]);
      if (ba.length && nowBa != null) ba = ba.concat([[t1, nowBa]]);
    }
    const out = { from: t0, to: t1, minutes, ab: thin(ab, t0, t1), ba: thin(ba, t0, t1) };
    if (src === source) {
      for (const [k, e] of gcache) if (e.exp <= now) gcache.delete(k);
      if (gcache.size >= 256) [...gcache.entries()].sort((x, y) => x[1].exp - y[1].exp).slice(0, 64).forEach(([k]) => gcache.delete(k));
      gcache.set(key, { exp: now + GRAPH_CACHE_MS, v: out });
    }
    return out;
  }
  async function view(tab) {
    const now = Date.now() / 1000, d = await currentData(), snap = d.snap, demo = conns.active === "demo";
    const status = { ok: !d.error, error: d.error, last_ok: demo ? now : d.lastOk, last_try: now, seconds: 0, version: cache.version, hosts: Object.keys(snap.hosts).length, demo, conn: connName() };
    const out = { ts: now, status, kind: tab.type };
    if (tab.type === "map") {
      const m = layout.maps[tab.map], nodes = Object.fromEntries(m.nodes.map(n => [n.id, n]));
      out.nodes = m.nodes.map(n => { const h = snap.hosts[n.host] || {}; return { id: n.id, name: h.name || "", status: h.status || "unknown", severity: h.severity || 0, problems: h.problems || 0, proxy: h.proxy || "", top: h.top || [] }; });
      out.links = m.links.map(l => resolveLink(l, nodes, snap, now));
    } else {
      let list = [...tab.hosts]; for (const g of tab.groups) list = list.concat(d.groupIds[g] || []);
      if (tab.all) { list = list.concat(d.allIds); out.truncated = !!d.allCut; out.max_hosts = MAX_HOSTS; }      // truncated: more monitored hosts exist than MAX_HOSTS allows
      const seen = new Set(), cards = [];
      for (const hid of list) {
        if (seen.has(hid)) continue; seen.add(hid);
        const h = snap.hosts[hid];
        if (!h) { cards.push({ id: hid, name: tab.hostnames[hid] || hid, status: "unknown", ip: "", os: "", model: "", severity: 0, problems: 0, proxy: "", top: [], ifaces: [], if_total: 0, if_down: 0 }); continue; }
        const ifs = Object.entries(snap.interfaces[hid] || {}).map(([name, r]) => {
          const vals = [r.in_bps, r.out_bps].filter(v => v != null), peak = vals.length ? Math.max(...vals) : null;
          return { name, alias: r.alias, oper: r.oper, in_bps: r.in_bps, out_bps: r.out_bps, speed: r.speed_bps, pct: (peak != null && r.speed_bps) ? peak / r.speed_bps * 100 : null };
        });
        const up = ifs.filter(i => i.oper === 1 || i.oper == null).sort((a, b) => ((a.pct == null) - (b.pct == null)) || ((b.pct || 0) - (a.pct || 0)));
        cards.push({ id: hid, name: h.name, status: h.status, ip: h.ip || "", os: h.os || "", model: h.model || "", severity: h.severity, problems: h.problems, proxy: h.proxy, top: h.top.slice(0, 3),
          ifaces: up.slice(0, tab.ifaces), if_total: ifs.length, if_down: ifs.filter(i => DOWN_STATES.has(i.oper)).length });
      }
      cards.sort((a, b) => a.name.toLowerCase() < b.name.toLowerCase() ? -1 : 1);
      out.cards = cards;
    }
    return out;
  }

  // ---------------------------------------------------------------- the "server": fetch() interception
  const json = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });
  const failure = (kind, e) => ({ ok: false, error: e.message || String(e), kind, hint: HINTS[kind] || "" });
  async function testProfile(d) {
    let cand;
    try {
      const existing = d && d.id ? conns.profiles.find(p => p.id === d.id) : null;
      cand = cleanProfile(d, existing);
    } catch (e) { return failure("url", e); }
    const secret = cand.auth === "token" ? cand.token : cand.password;
    if (!secret) return failure("auth", new Error(cand.auth === "token" ? "Enter the API token" : "Enter the password"));
    try { return Object.assign({ ok: true }, await new LiveSource(cand, secret, 10000).probe()); }
    catch (e) { return e instanceof ZbxError ? failure(e.kind, e) : failure("api", e); }
  }
  async function connectionsPost(action, body) {
    if (action === "test") return json(await testProfile(body.profile || {}));
    if (action === "save") {
      const d = body.profile || {}, existing = d.id ? conns.profiles.find(p => p.id === d.id) : null;
      if (d.id && !existing) return json({ error: "that connection cannot be changed here (or does not exist)" }, 400);
      let p; try { p = cleanProfile(d, existing); } catch (e) { return json({ error: e.message }, 400); }
      p.id = existing ? existing.id : randId();
      conns.profiles = existing ? conns.profiles.map(x => x.id === p.id ? p : x) : conns.profiles.concat([p]);
      if (body.activate) activate(p.id); else { persistConns(); if (conns.active === p.id) activate(p.id); }
      return json(Object.assign({ ok: true, id: p.id }, connInfo()));
    }
    if (action === "activate") {
      const id = String(body.id || "");
      if (id !== "demo" && !conns.profiles.some(p => p.id === id)) return json({ error: "unknown connection" }, 404);
      activate(id); return json(Object.assign({ ok: true }, connInfo()));
    }
    if (action === "delete") {
      const id = String(body.id || "");
      if (!conns.profiles.some(p => p.id === id)) return json({ error: "that connection cannot be deleted here (or does not exist)" }, 400);
      conns.profiles = conns.profiles.filter(p => p.id !== id); ss.del(SECRET_KEY + id); ls.del(layoutKey(id));
      if (conns.active === id) activate("demo"); else persistConns();
      return json(Object.assign({ ok: true }, connInfo()));
    }
    if (action === "reset-demo") { ls.del(layoutKey("demo")); if (conns.active === "demo") activate("demo"); return json({ ok: true }); }
    return json({ error: "not found" }, 404);
  }
  window.fetch = async (input, opts = {}) => {
    const raw = typeof input === "string" ? input : (input && input.url) || "";
    if (!raw.includes("/api/")) return realFetch ? realFetch(input, opts) : Promise.reject(new TypeError("fetch unavailable"));
    const u = new URL(raw, "http://demo.invalid/"), q = u.searchParams, path = u.pathname.slice(u.pathname.indexOf("/api/"));
    const post = (opts.method || "GET").toUpperCase() === "POST";
    try {
      if (!post) {
        if (path === "/api/layout") return json({ layout, can_edit: true, demo: conns.active === "demo", mode: "standalone", conn: { active: conns.active, name: connName() }, unconfigured: false, defaults: { graph_minutes: GRAPH_DEFAULT, refresh_seconds: POLL_MS / 1000 } });
        if (path === "/api/connections") return json(connInfo());
        if (path === "/api/linkgraph") {
          const tab = layout.tabs.find(t => t.id === q.get("tab"));
          if (!tab) return json({ error: "no such tab" }, 404);
          const mm = parseInt(q.get("minutes"), 10), g = await linkGraph(tab, q.get("link") || "", Number.isFinite(mm) ? mm : null);
          return g ? json(g) : json({ error: "no such link" }, 404);
        }
        if (path === "/api/view") { const ev = parseInt(q.get("every"), 10); if (REFRESH_CHOICES.includes(ev)) pollMs = ev * 1000; const tab = layout.tabs.find(t => t.id === q.get("tab")); return tab ? json(await view(tab)) : json({ error: "no such tab" }, 404); }
        if (path === "/api/hosts" || path === "/api/groups" || path === "/api/interfaces") {
          if (!source) return json({ error: sourceError || "no connection" }, 502);
          if (path === "/api/hosts") return json({ items: await source.searchHosts((q.get("q") || "").slice(0, 60)) });
          if (path === "/api/groups") return json({ items: await source.searchGroups((q.get("q") || "").slice(0, 60)) });
          const h = q.get("host") || "";
          return /^[0-9]{1,12}$/.test(h) ? json({ interfaces: await source.interfacesOf(h) }) : json({ error: "bad host id" }, 400);
        }
      } else {
        let body; try { body = JSON.parse(opts.body || "{}"); } catch (e) { return json({ error: "bad request" }, 400); }
        if (path === "/api/layout") {
          try { saveLayout(validateLayout(body.layout)); } catch (e) { return json({ error: "invalid layout: " + e.message }, 400); }
          return json({ ok: true });
        }
        if (path === "/api/refresh") return json(await doRefresh());
        if (path.startsWith("/api/connections/")) return await connectionsPost(path.slice("/api/connections/".length), body);
      }
    } catch (e) { return json({ error: e instanceof ZbxError ? e.message : (e.name + ": " + e.message) }, e instanceof ZbxError ? 502 : 500); }
    return json({ error: "not found" }, 404);
  };

  loadConns();
  activate(conns.active);
  const badge = document.getElementById("demo");
  if (badge) badge.title = "Simulated Cisco network that runs entirely in this browser. Click the status chip at the top (or press C) to connect to your Zabbix.";
})();
