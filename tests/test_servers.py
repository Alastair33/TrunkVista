"""Server data: CPU, memory, swap, load, disks and uptime of hosts that Zabbix monitors with its agent (and what the SNMP templates of Cisco devices report
under the same keys), plus the state and speed of a Linux server's network cards. Checks the rules in trunkvista.py (metrics_of, ZabbixSource), the demo
servers through the real server, and that the standalone page (tools/demo_shim.js) applies the same rules as the Python server.
Run:  python3 -m unittest discover -s tests -v      (standard library only; the JavaScript check needs node and is skipped without it)"""
import json
import re
import shutil
import subprocess
import tempfile
import time
import unittest

from test_app import ROOT, Server, load_app_module

tv = load_app_module()
NOW = 1_800_000_000


def item(host, key, value, ago=10):
    return {"hostid": host, "key_": key, "lastvalue": str(value), "lastclock": str(NOW - ago)}


LINUX = [
    item("1", "system.cpu.util", 23.456), item("1", "system.cpu.util[,idle]", 76.5), item("1", "system.cpu.util[,user]", 10),
    item("1", "vm.memory.utilization", 61.2), item("1", "vm.memory.size[total]", 8e9), item("1", "vm.memory.size[available]", 3e9),
    item("1", "system.swap.size[,pfree]", 90), item("1", "system.cpu.load[all,avg1]", 2.0), item("1", "system.cpu.load[all,avg5]", 1.5),
    item("1", "system.cpu.num", 4), item("1", "system.uptime", 86400 * 3 + 5),
    item("1", "vfs.fs.dependent.size[/,pused]", 41.0), item("1", "vfs.fs.dependent.size[/var,pused]", 88.2), item("1", "vfs.fs.dependent.size[/var,total]", 9e9),
]
WINDOWS = [item("2", "system.cpu.util", 5), item("2", "vm.memory.size[pused]", 50), item("2", "system.swap.pfree", 99),
           item("2", "vfs.fs.dependent.size[C:,pused]", 70), item("2", "system.uptime", 1000)]
CISCO = [item("3", "system.cpu.util[cpmCPUTotal5minRev.1]", 12), item("3", "system.cpu.util[cpmCPUTotal5minRev.2]", 30),
         item("3", "vm.memory.util[memoryUsedPercentage.1]", 44), item("3", "system.net.uptime[sysUpTime.0]", 999)]


class MetricsOf(unittest.TestCase):
    def get(self, items, host, stale=300):
        return tv.metrics_of(items, NOW, stale).get(host)

    def test_linux_by_zabbix_agent(self):
        m = self.get(LINUX, "1")
        self.assertEqual((m["cpu"], m["mem"], m["swap"]), (23.5, 61.2, 10.0), "util (busy) is taken, not 100 - idle; swap comes as the free share")
        self.assertEqual((m["load"], m["cores"], m["load_pct"]), (2.0, 4, 50.0), "the load of the 1 minute average, and as a share of the CPUs")
        self.assertEqual(m["uptime"], 86400 * 3 + 5)
        self.assertEqual((m["disk"], m["disk_name"]), (88.2, "/var"), "the fullest filesystem")
        self.assertEqual(m["disks"], [{"name": "/var", "pct": 88.2}, {"name": "/", "pct": 41.0}], "only the pused items, fullest first")

    def test_windows_by_zabbix_agent(self):
        m = self.get(WINDOWS, "2")
        self.assertEqual((m["cpu"], m["mem"], m["swap"], m["disk"], m["disk_name"]), (5.0, 50.0, 1.0, 70.0, "C:"))
        self.assertNotIn("load", m, "Windows has no load average")

    def test_cisco_snmp_templates_give_cpu_memory_uptime(self):
        m = self.get(CISCO, "3")
        self.assertEqual(m, {"cpu": 30.0, "mem": 44.0, "uptime": 999, "clock": NOW - 10}, "the busiest of the CPUs; no disks, swap or load")

    def test_an_older_template_with_only_the_idle_time(self):
        self.assertEqual(self.get([item("5", "system.cpu.util[,idle]", 90)], "5")["cpu"], 10.0)
        self.assertEqual(self.get([item("5", "system.cpu.util[all,idle]", 25)], "5")["cpu"], 75.0)

    def test_memory_from_bytes_when_there_is_no_percentage_item(self):
        m = self.get([item("6", "vm.memory.size[total]", 8e9), item("6", "vm.memory.size[available]", 2e9)], "6")
        self.assertEqual(m["mem"], 75.0)
        m = self.get([item("6", "vm.memory.size[pavailable]", 20)], "6")
        self.assertEqual(m["mem"], 80.0)

    def test_uptime_from_the_boot_time(self):
        self.assertEqual(self.get([item("7", "system.boottime", NOW - 7200)], "7"), {"uptime": 7200})      # (static items carry no clock of their own)

    def test_old_values_are_left_out(self):
        """An agent that stopped long ago must not show its last CPU as if it were now."""
        old = [item("4", "system.cpu.util", 55, ago=4000), item("4", "vm.memory.utilization", 55, ago=4000), item("4", "system.cpu.num", 4, ago=4000)]
        self.assertEqual(self.get(old, "4"), {"cores": 4}, "what does not change is kept, what is measured is not")
        self.assertEqual(self.get(old, "4", stale=5000)["cpu"], 55.0)
        fs = [item("8", "vfs.fs.dependent.size[/,pused]", 50, ago=1800), item("8", "vfs.fs.dependent.size[/var,pused]", 60, ago=4000)]
        self.assertEqual(self.get(fs, "8")["disks"], [{"name": "/", "pct": 50.0}], "filesystems are polled slowly: an hour is still current")

    def test_values_that_are_no_numbers_are_ignored(self):
        junk = [item("9", "system.cpu.util", ""), item("9", "vm.memory.utilization", "n/a"), item("9", "system.swap.size[,pfree]", "nan"),
                item("9", "system.uptime", "inf"), {"hostid": "9", "key_": "system.cpu.load[all,avg1]", "lastvalue": None, "lastclock": "1"}]
        self.assertEqual(tv.metrics_of(junk, NOW, 300), {})

    def test_percentages_are_kept_between_0_and_100_with_one_decimal_halves_up(self):
        m = self.get([item("10", "system.cpu.util", 123), item("10", "vm.memory.utilization", -5), item("10", "system.swap.size[,pused]", 23.45)], "10")
        self.assertEqual((m["cpu"], m["mem"], m["swap"]), (100.0, 0.0, 23.5))

    def test_at_most_six_filesystems_and_other_keys_are_ignored(self):
        many = [item("11", f"vfs.fs.dependent.size[/d{i},pused]", i) for i in range(10)] + [item("11", "agent.ping", 1), item("11", "system.localtime", 5)]
        m = self.get(many, "11")
        self.assertEqual([d["name"] for d in m["disks"]], ["/d9", "/d8", "/d7", "/d6", "/d5", "/d4"])

    def test_the_page_gets_the_numbers_only(self):
        m = self.get(LINUX, "1")
        full, short = tv.public_metrics(m), tv.public_metrics(m, short=True)
        self.assertNotIn("clock", full)
        self.assertEqual(set(short), {"cpu", "mem", "disk", "disk_name"})
        self.assertIsNone(tv.public_metrics(None))
        self.assertIsNone(tv.public_metrics({"clock": 5}, short=True))


class Stub:
    """Answers the Zabbix calls of ZabbixSource: hosts, no proxies or triggers, and the items per host that a test gives."""

    def __init__(self, hosts, interface_items=(), metric_items=(), metric_error=None):
        self.hosts, self.iface_items, self.metric_items, self.metric_error = hosts, list(interface_items), list(metric_items), metric_error
        self.calls = []

    def call(self, method, params=None):
        params = params or {}
        self.calls.append((method, params))
        if method == "host.get" and params.get("countOutput"):
            return str(len(self.hosts))
        if method == "host.get":
            return self.hosts
        if method == "proxy.get" and params.get("countOutput"):
            return "2"
        if method in ("proxy.get", "trigger.get"):
            return []
        if method == "item.get":
            prefixes = params["search"]["key_"]
            if params.get("countOutput"):
                return str(len(self.metric_items) if "system.cpu.util" in prefixes else len(self.iface_items))
            if "system.cpu.util" in prefixes:
                if self.metric_error:
                    raise self.metric_error
                return self.metric_items
            return self.iface_items
        raise AssertionError("unexpected call " + method)

    def version(self):
        return "7.0.8"


def source(stub):
    src = tv.ZabbixSource.__new__(tv.ZabbixSource)      # (no real connection: the stub answers)
    src.cfg = {"stale_seconds": 300}
    src.m = tv.DEFAULT_CONFIG["match"]
    src.name_rx = re.compile(src.m["name_regex"])
    src.zbx = stub
    return src


def host(hid, types, available="1", status="0"):
    return {"hostid": hid, "name": "host-" + hid, "status": status, "proxyid": "0",
            "interfaces": [{"type": t, "available": available, "main": "1", "ip": "10.0.0." + hid, "dns": "", "useip": "1"} for t in types]}


class ServerNics(unittest.TestCase):
    """A Linux server has no net.if.status: Linux by Zabbix agent keeps the state and the speed of a card in /sys."""

    def setUp(self):
        self.src = source(Stub([]))

    def test_kind_of_the_sys_items(self):
        k = self.src._kind
        self.assertEqual(k('vfs.file.contents["/sys/class/net/ens192/operstate"]'), "status")
        self.assertEqual(k('vfs.file.contents["/sys/class/net/ens192/speed"]'), "speed")
        self.assertEqual(k("vfs.file.contents[/sys/class/net/eth0/speed]"), "speed")
        self.assertIsNone(k('vfs.file.contents["/sys/class/net/ens192/carrier"]'), "other files are no interface items")
        self.assertEqual(k('net.if.in["ens192"]'), "in")
        self.assertEqual(k('net.if.out["ens192",bytes]'), "out")
        self.assertIsNone(k('net.if.in["ens192",errors]'), "the agent's templates count errors and drops under the same prefix: that is not traffic")
        self.assertIsNone(k("net.if.out[ens192,dropped]"))
        self.assertEqual(k("net.if.in[ifHCInOctets.7]"), "in")
        self.assertEqual(k("net.if.status[ifOperStatus.7]"), "status", "the SNMP keys are unchanged")

    def test_values(self):
        v = tv.ZabbixSource._value
        sys_state, sys_speed = 'vfs.file.contents["/sys/class/net/eth0/operstate"]', 'vfs.file.contents["/sys/class/net/eth0/speed"]'
        self.assertEqual(v({"key_": sys_state, "lastvalue": "up"}, "status"), 1.0)
        self.assertEqual(v({"key_": sys_state, "lastvalue": "Down\n"}, "status"), 2.0)
        self.assertEqual(v({"key_": sys_state, "lastvalue": "lowerlayerdown"}, "status"), 7.0)
        self.assertIsNone(v({"key_": sys_state, "lastvalue": "banana"}, "status"))
        self.assertEqual(v({"key_": sys_speed, "lastvalue": "10000"}, "speed"), 10e9, "the kernel says Mbit/s")
        self.assertEqual(v({"key_": sys_speed, "lastvalue": "10000000000", "units": "bps"}, "speed"), 10e9, "a template that multiplied it already")
        self.assertIsNone(v({"key_": sys_speed, "lastvalue": "-1"}, "speed"), "the kernel does not know (a virtual card)")
        self.assertEqual(v({"key_": "net.if.status[ifOperStatus.7]", "lastvalue": "2"}, "status"), 2.0)
        self.assertEqual(v({"key_": "net.if.speed[ifHighSpeed.7]", "lastvalue": "1000", "units": "Mbps"}, "speed"), 1e9, "SNMP speed units are unchanged")

    def test_name_of_a_card_without_tags_and_with_another_item_name(self):
        n = self.src._ifname
        self.assertEqual(n({"name": "x", "key_": 'net.if.in["ens192"]', "tags": []}), ("ens192", ""))
        self.assertEqual(n({"name": "x", "key_": "net.if.out[eth0,bytes]", "tags": []}), ("eth0", ""))
        self.assertEqual(n({"name": "x", "key_": 'vfs.file.contents["/sys/class/net/ens192/operstate"]', "tags": []}), ("ens192", ""))
        self.assertEqual(n({"name": "x", "key_": "net.if.in[ifHCInOctets.12]", "tags": []}), ("idx:12", ""), "SNMP items keep their index name")
        self.assertEqual(n({"name": "Interface Gi0/1(uplink): Bits received", "key_": "net.if.in[ifHCInOctets.1]", "tags": []}), ("Gi0/1", "uplink"))


class ServerSnapshot(unittest.TestCase):
    def snap(self, hosts, metric_items, interface_items=(), metric_error=None):
        return source(Stub(hosts, interface_items, metric_items, metric_error)).snapshot([h["hostid"] for h in hosts], 2)

    def test_server_card_data_and_interfaces(self):
        now = int(time.time())
        tags = [{"tag": "interface", "value": "ens192"}]
        ifaces = [
            {"itemid": "1", "hostid": "1", "name": "Interface ens192: Bits received", "key_": 'net.if.in["ens192"]', "lastvalue": "2000000000", "lastclock": str(now), "units": "bps", "value_type": "0", "tags": tags},
            {"itemid": "2", "hostid": "1", "name": "Interface ens192: Bits sent", "key_": 'net.if.out["ens192"]', "lastvalue": "500000000", "lastclock": str(now), "units": "bps", "value_type": "0", "tags": tags},
            {"itemid": "3", "hostid": "1", "name": "Interface ens192: Operational status", "key_": 'vfs.file.contents["/sys/class/net/ens192/operstate"]', "lastvalue": "up", "lastclock": str(now), "units": "", "value_type": "4", "tags": tags},
            {"itemid": "4", "hostid": "1", "name": "Interface ens192: Speed", "key_": 'vfs.file.contents["/sys/class/net/ens192/speed"]', "lastvalue": "10000", "lastclock": str(now), "units": "bps", "value_type": "3", "tags": tags},
            # the same card's error counters come later in the answer and must not replace the traffic
            {"itemid": "5", "hostid": "1", "name": "Interface ens192: Inbound packets with errors", "key_": 'net.if.in["ens192",errors]', "lastvalue": "0", "lastclock": str(now), "units": "pps", "value_type": "0", "tags": tags},
            {"itemid": "6", "hostid": "1", "name": "Interface ens192: Outbound packets with errors", "key_": 'net.if.out["ens192",errors]', "lastvalue": "0", "lastclock": str(now), "units": "pps", "value_type": "0", "tags": tags},
        ]
        metrics = [dict(i, lastclock=str(now)) for i in LINUX]
        s = self.snap([host("1", "1")], metrics, ifaces)
        h = s["hosts"]["1"]
        self.assertEqual((h["kind"], h["status"]), ("server", "up"))
        self.assertEqual(s["metrics"]["1"]["cpu"], 23.5)
        nic = s["interfaces"]["1"]["ens192"]
        self.assertEqual((nic["oper"], nic["speed_bps"], nic["in_bps"], nic["out_bps"]), (1, 10e9, 2e9, 5e8), "state and speed from /sys, one record with the traffic")
        self.assertEqual(s["warn"], "")

    def test_kind_of_a_host(self):
        now = int(time.time())
        fresh = lambda items: [dict(i, lastclock=str(now)) for i in items]      # noqa: E731
        s = self.snap([host("1", "1"), host("3", "2"), host("12", "12"), host("13", "1")], fresh(LINUX + CISCO + [item("12", "system.cpu.load[all,avg1]", 1), item("12", "system.cpu.util", 1)]
                                                                                                 + [item("13", "system.cpu.util", 1)]))
        kinds = {i: h["kind"] for i, h in s["hosts"].items()}
        self.assertEqual(kinds, {"1": "server", "3": "network", "12": "server", "13": "server"},
                         "agent only = server; SNMP only = network (CPU and memory do not make a switch a server); a host with both is a server when it reports load")
        self.assertEqual(s["hosts"]["12"]["kind"], "server")

    def test_a_server_on_active_checks_is_up_when_its_values_are_fresh(self):
        now = int(time.time())
        fresh = [item("1", "system.cpu.util", 5, ago=0), item("2", "system.cpu.util", 5, ago=0), item("3", "system.cpu.util", 5, ago=0), item("5", "system.cpu.util", 5, ago=0)]
        stale = [item("4", "system.cpu.util", 5, ago=4000)]
        items = [dict(i, lastclock=str(now - int(NOW - int(i["lastclock"])))) for i in fresh + stale]      # (relative to the real time, the program asks time.time())
        hosts = [host("1", "1", available="0"),      # agent availability unknown: an active agent
                 host("2", "1", available="2"),      # Zabbix says: not reachable - that stays
                 host("3", "2", available="0"),      # a switch: unknown stays unknown, only servers get the fallback
                 host("4", "1", available="0"),      # active agent, but nothing new for a long time
                 host("5", "1", available="1", status="1")]      # not monitored
        s = self.snap(hosts, items)
        self.assertEqual({i: h["status"] for i, h in s["hosts"].items()}, {"1": "up", "2": "down", "3": "unknown", "4": "unknown", "5": "disabled"})

    def test_a_failed_request_for_the_metrics_does_not_take_the_network_view_down(self):
        s = self.snap([host("3", "2")], [], metric_error=tv.ZabbixError("item.get: Not enough memory", kind="api"))
        self.assertIn("3", s["hosts"], "the hosts are still there")
        self.assertEqual(s["metrics"], {})
        self.assertTrue(s["warn"].startswith("server metrics: item.get: Not enough memory"), s["warn"])

    def test_a_session_problem_is_not_hidden(self):
        with self.assertRaises(tv.ZabbixError):
            self.snap([host("3", "2")], [], metric_error=tv.ZabbixError("item.get: Session terminated, re-login, please.", auth=True, kind="auth"))

    def test_the_metrics_are_one_extra_light_request(self):
        stub = Stub([host("1", "1")], [], [])
        source(stub).snapshot(["1"], 2)
        item_calls = [p for m, p in stub.calls if m == "item.get"]
        self.assertEqual(len(item_calls), 2, "interface items, and the metric items - not one more per metric")
        metric_call = next(p for p in item_calls if "system.cpu.util" in p["search"]["key_"])
        self.assertTrue(metric_call["startSearch"] and metric_call["searchByAny"] and metric_call["monitored"])
        self.assertEqual(set(metric_call["output"]), {"hostid", "key_", "lastvalue", "lastclock"}, "no names, no tags")
        self.assertIn(tv.SYSFS_SEARCH, next(p for p in item_calls if "net.if.in[" in p["search"]["key_"])["search"]["key_"], "the cards' /sys items come with the interface items")

    def test_probe_counts_the_server_items(self):
        stub = Stub([host("1", "1")], [item("1", "net.if.in[x]", 1)] * 3, [item("1", "system.cpu.util", 1)] * 5)
        r = source(stub).probe()
        self.assertEqual((r["interface_items"], r["server_items"], r["proxies"]), (3, 5, 2))
        stub.call = lambda method, params=None, _c=stub.call: (_ for _ in ()).throw(tv.ZabbixError("denied")) if (
            method == "item.get" and "system.cpu.util" in (params or {}).get("search", {}).get("key_", [])) else _c(method, params)
        self.assertIsNone(source(stub).probe()["server_items"], "a role that may not count them: unknown, not a failed test")


class DemoServers(unittest.TestCase):
    """The demo has six servers (Linux and Windows), a Datacenter map and a Servers tab - through the real server."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = Server(cls.tmp.name, "--demo", "--data-dir", cls.tmp.name + "/d").__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.srv.__exit__()
        cls.tmp.cleanup()

    def view(self, tab):
        for _ in range(50):
            code, body = self.srv.get(f"/api/view?tab={tab}&every=30&cid=cunit")
            v = json.loads(body)
            if code == 200 and (v.get("cards") or v.get("nodes")) and v["status"]["ok"]:
                return v
            time.sleep(0.1)
        self.fail("no data for tab " + tab)

    def test_servers_tab(self):
        cards = {c["name"]: c for c in self.view("t-srv")["cards"]}
        self.assertEqual(sorted(cards), ["srv-app-01", "srv-backup-01", "srv-db-01", "srv-db-02", "srv-web-01", "srv-web-02"])
        db = cards["srv-db-01"]
        self.assertEqual(db["kind"], "server")
        self.assertEqual(set(db["metrics"]), {"cpu", "mem", "swap", "load", "cores", "load_pct", "disk", "disk_name", "disks", "uptime"})
        self.assertGreater(db["metrics"]["cpu"], 75, "the always busy server")
        self.assertNotIn("load", cards["srv-app-01"]["metrics"], "Windows has no load average")
        self.assertEqual(cards["srv-backup-01"]["metrics"]["disk_name"], "/backup")
        self.assertTrue(any("Free disk space" in p["name"] for p in cards["srv-backup-01"]["top"]), "the nearly full disk raises a problem, like Zabbix's own trigger")
        for c in cards.values():
            self.assertTrue(all(0 <= c["metrics"][k] <= 100 for k in ("cpu", "mem", "swap", "disk")), c["name"])
            self.assertTrue(any(i["oper"] == 1 and i["speed"] for i in c["ifaces"]), "a card of a server carries its network cards, too")

    def test_datacenter_map_nodes(self):
        v = self.view("t-dc")
        nodes = {n["name"]: n for n in v["nodes"]}
        self.assertEqual(nodes["srv-web-01"]["kind"], "server")
        self.assertEqual(set(nodes["srv-web-01"]["metrics"]), {"cpu", "mem", "disk", "disk_name"}, "the tooltip of a node gets the headline numbers only")
        core = nodes["core-c9500-1"]
        self.assertEqual((core["kind"], set(core["metrics"])), ("network", {"cpu", "mem"}), "a Cisco device reports CPU and memory, no disks")
        self.assertEqual(len(v["links"]), 7)
        self.assertTrue(all(l["status"] == "up" and l["ab_pct"] is not None for l in v["links"]), "server links are drawn like any other link")

    def test_all_devices_tab_has_both_kinds(self):
        code, body = self.srv.get("/api/view?tab=t-all&every=30&cid=cunit")
        cards = json.loads(body)["cards"]
        self.assertEqual({c["kind"] for c in cards}, {"server", "network"})
        down = [c for c in cards if c["status"] == "down"]
        self.assertTrue(down and all(c["metrics"] is None for c in down), "a switch that is down reports no numbers")

    def test_the_page_has_what_it_needs(self):
        html = self.srv.get("/")[1].decode()
        for needle in ('{ k: "cpu", t: "CPU"', '"Servers (Zabbix agent)"', 'HEAT_KEYS = ["status", "busy", "cpu", "mem", "disk", "sev"]', "function metricLines(", "nodeMetricLine(v.metrics)"):
            self.assertIn(needle, html)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class StandalonePageParity(unittest.TestCase):
    """tools/demo_shim.js is the browser's copy of the server's Zabbix reading: the same items must give the same numbers."""

    def test_metrics_of_gives_the_same_answer_in_javascript(self):
        shim = (ROOT / "tools" / "demo_shim.js").read_text(encoding="utf-8")
        nat = re.search(r"^\s*const natCmp = .*$", shim, re.M)
        start, end = shim.find("  const METRIC_SEARCH = ["), shim.find("  class LiveSource {")
        self.assertTrue(nat and 0 < start < end, "tools/demo_shim.js no longer has the metrics block this test cuts out")
        cases = {"linux": LINUX, "windows": WINDOWS, "cisco": CISCO,
                 "old": [item("5", "system.cpu.util[,idle]", 90), item("6", "vm.memory.size[total]", 8e9), item("6", "vm.memory.size[available]", 2e9)],
                 "stale": [item("4", "system.cpu.util", 55, ago=4000), item("4", "system.cpu.num", 4, ago=4000), item("8", "vfs.fs.dependent.size[/,pused]", 50, ago=1800),
                           item("8", "vfs.fs.dependent.size[/var,pused]", 60, ago=4000), item("7", "system.boottime", NOW - 7200)],
                 "odd": [item("10", "system.cpu.util", 123), item("10", "vm.memory.utilization", -5), item("10", "system.swap.size[,pused]", 23.45),
                         item("9", "system.cpu.util", ""), item("9", "vm.memory.utilization", "n/a"), item("11", "vfs.fs.dependent.size[/d1,pused]", 1.25),
                         item("11", "system.cpu.load[percpu,avg1]", 0.755), item("11", "system.cpu.load[all,avg1]", -1)],
                 "many": [item("11", f"vfs.fs.dependent.size[/d{i},pused]", i) for i in range(10)]}
        script = ("const vm = require('vm'); const ctx = vm.createContext({}); "
                  f"vm.runInContext({json.dumps(nat.group(0) + chr(10) + shim[start:end] + chr(10) + 'this.metricsOf = metricsOf; this.publicMetrics = publicMetrics;')}, ctx); "
                  "const cases = JSON.parse(require('fs').readFileSync(0, 'utf8')); const out = {}; "
                  f"for (const [k, items] of Object.entries(cases)) {{ const m = ctx.metricsOf(items, {NOW}, 300); const o = {{}}; for (const [h, v] of Object.entries(m)) o[h] = {{ full: ctx.publicMetrics(v), short: ctx.publicMetrics(v, true), clock: v.clock == null ? null : v.clock }}; out[k] = o; }} "
                  "console.log(JSON.stringify(out));")
        got = json.loads(subprocess.run(["node", "-e", script], input=json.dumps(cases), capture_output=True, text=True, check=True, timeout=60).stdout)
        for name, items in cases.items():
            py = {h: {"full": tv.public_metrics(m), "short": tv.public_metrics(m, short=True), "clock": m.get("clock")} for h, m in tv.metrics_of(items, NOW, 300).items()}
            self.assertEqual(got[name], json.loads(json.dumps(py)), "case " + name)

    def test_the_live_source_reads_hosts_the_same_way_in_javascript(self):
        """The whole snapshot - kind, status, metrics, interfaces - of the same stub answers from Zabbix, by the Python server and by the standalone page."""
        shim = (ROOT / "tools" / "demo_shim.js").read_text(encoding="utf-8")
        end = shim.rstrip().rfind("})();")
        self.assertGreater(end, 0)
        shim = shim[:end] + "globalThis.__tv = { LiveSource };\n" + shim[end:]
        now = int(time.time())
        rebase = lambda items: [dict(i, lastclock=str(now - (NOW - int(i["lastclock"])))) for i in items]      # noqa: E731
        tags = lambda n: [{"tag": "interface", "value": n}]      # noqa: E731
        nic = lambda hid, n, key, value, units="bps": {"itemid": str(abs(hash((hid, key)))), "hostid": hid, "name": f"Interface {n}: x", "key_": key, "lastvalue": str(value),
                                                         "lastclock": str(now), "units": units, "value_type": "0", "tags": tags(n)}      # noqa: E731
        ifaces = [nic("1", "ens192", 'net.if.in["ens192"]', 2e9), nic("1", "ens192", 'net.if.out["ens192"]', 5e8),
                  nic("1", "ens192", 'vfs.file.contents["/sys/class/net/ens192/operstate"]', "up", ""), nic("1", "ens192", 'vfs.file.contents["/sys/class/net/ens192/speed"]', 10000),
                  nic("1", "ens224", 'vfs.file.contents["/sys/class/net/ens224/operstate"]', "down", ""), nic("1", "ens224", 'vfs.file.contents["/sys/class/net/ens224/speed"]', -1),
                  nic("1", "ens192", 'net.if.in["ens192",errors]', 0, "pps"), nic("1", "ens192", 'net.if.out["ens192",dropped]', 0, "pps"),
                  nic("3", "Gi0/1", "net.if.in[ifHCInOctets.1]", 125e6, "Bps"), nic("3", "Gi0/1", "net.if.status[ifOperStatus.1]", 1, ""), nic("3", "Gi0/1", "net.if.speed[ifHighSpeed.1]", 1000, "Mbps")]
        metrics = rebase(LINUX + WINDOWS + CISCO + [item("4", "system.cpu.util", 5, ago=4000), item("4", "system.cpu.num", 2, ago=4000)])
        hosts = [host("1", "1"), host("2", "1", available="0"), host("3", "2"), host("4", "1", available="0"), host("12", "12")]
        py = source(Stub(hosts, ifaces, metrics)).snapshot([h["hostid"] for h in hosts], 2)
        script = ("const vm = require('vm'); const ctx = vm.createContext({ window: {}, location: { protocol: 'http:', href: 'http://x/' }, document: { getElementById: () => null }, "
                  "TextEncoder, URL, Response, AbortController, setTimeout, clearTimeout, console }); "
                  f"vm.runInContext({json.dumps(shim)}, ctx); const d = JSON.parse(require('fs').readFileSync(0, 'utf8')); "
                  "const src = new ctx.__tv.LiveSource({ url: 'http://x', auth: 'token', match: {} }, 'secret'); "
                  "src.zbx = { ready: async () => {}, call: async (m, p) => m === 'host.get' ? d.hosts : (m === 'proxy.get' || m === 'trigger.get') ? [] : "
                  "m === 'item.get' ? (p.search.key_.includes('system.cpu.util') ? d.metrics : d.ifaces) : null }; "
                  "src.snapshot(d.hosts.map(h => h.hostid), 2).then(s => console.log(JSON.stringify(s)));")
        out = subprocess.run(["node", "-e", script], input=json.dumps({"hosts": hosts, "ifaces": ifaces, "metrics": metrics}), capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        js = json.loads(out.stdout)

        def digest(snap):
            return {"hosts": {i: [h["kind"], h["status"]] for i, h in snap["hosts"].items()},
                    "metrics": {i: {k: v for k, v in m.items() if k != "clock"} for i, m in snap["metrics"].items()},
                    "ifaces": {i: {n: [r["oper"], r["speed_bps"], r["in_bps"], r["out_bps"]] for n, r in rows.items()} for i, rows in snap["interfaces"].items()},
                    "warn": snap["warn"]}
        self.assertEqual(digest(js), json.loads(json.dumps(digest(py))))
        self.assertEqual(py["hosts"]["2"]["status"], "up", "(the case is meaningful: the Windows server on active checks is up through its fresh values)")
        self.assertEqual(py["interfaces"]["1"]["ens224"]["oper"], 2)
        self.assertEqual(py["interfaces"]["3"]["Gi0/1"]["speed_bps"], 1e9)

    def test_the_demo_servers_report_the_same_numbers_in_javascript(self):
        """The standalone page simulates the same servers as `trunkvista.py --demo` (at a fixed boot time, so that the uptime can be compared, too)."""
        shim = (ROOT / "tools" / "demo_shim.js").read_text(encoding="utf-8")
        self.assertIn("const DEMO_STARTED = Date.now() / 1000;", shim)
        shim = shim.replace("const DEMO_STARTED = Date.now() / 1000;", "const DEMO_STARTED = 0;")
        end = shim.rstrip().rfind("})();")
        shim = shim[:end] + "globalThis.__tv = { demoMetrics };\n" + shim[end:]
        ids = ["10001", "10002", "10005", "10011", "10012", "10013", "10014", "10015", "10016", "10107"]      # Cisco core, edge, the six servers, an access switch
        times = [1_800_000_000.5, 1_800_000_321.25, 1_790_000_000.0]
        flags = [(False, False), (True, False), (False, True)]
        script = ("const vm = require('vm'); const ctx = vm.createContext({ window: {}, location: { protocol: 'http:', href: 'http://x/' }, document: { getElementById: () => null }, "
                  "TextEncoder, URL, Response, AbortController, setTimeout, clearTimeout, console }); "
                  f"vm.runInContext({json.dumps(shim)}, ctx); const out = []; "
                  f"for (const t of {json.dumps(times)}) for (const id of {json.dumps(ids)}) for (const [c, m] of {json.dumps(flags)}) out.push(ctx.__tv.demoMetrics(id, t, c, m)); "
                  "console.log(JSON.stringify(out));")
        js = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=60).stdout)
        demo = tv.DemoSource()
        demo.started = 0
        py = [demo._metrics_at(i, t, c, m) for t in times for i in ids for c, m in flags]
        self.assertEqual(js, json.loads(json.dumps(py)))


if __name__ == "__main__":
    unittest.main()
