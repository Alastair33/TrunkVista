[← README](../README.md)

# Servers (Zabbix agent)

Besides the interfaces of your Cisco devices, TrunkVista reads **server data** from Zabbix: a host that Zabbix monitors with its **agent** (passive or active) shows its **CPU, memory, swap, load average, filesystems and uptime** on its card, in the list, in the heatmap and in the tooltip of its node on a weathermap. Nothing has to be set up: the same API token is used, and the data is found by the item keys of Zabbix's own templates. Servers also take part in weathermaps - their network cards are interfaces like those of a switch, so a link between a core switch port and `ens192` of a server is drawn like any other.

| Shown | Where it comes from (item key) |
|---|---|
| **CPU** % | `system.cpu.util` (Linux and Windows by Zabbix agent); `system.cpu.util[...]` (the SNMP templates, so **Cisco devices show their CPU, too** - the busiest CPU counts); older templates: `100 - system.cpu.util[,idle]` |
| **Memory** % | `vm.memory.utilization` (Linux); `vm.memory.size[pused]` (Windows); `vm.memory.util[...]` (SNMP); else from `vm.memory.size[pavailable]` or `[available]` and `[total]` |
| **Swap** % | `system.swap.size[,pused]` or `[,pfree]`; `system.swap.pfree` (Windows) |
| **Load** | `system.cpu.load[all,avg1]` (the 1 minute average), and as a share of `system.cpu.num` CPUs |
| **Disks** | `vfs.fs.dependent.size[<filesystem>,pused]` (and `vfs.fs.size[<filesystem>,pused]`) - the fullest six; the fullest is the one in the list and the heatmap |
| **Uptime** | `system.uptime`, `system.boottime`, or `system.net.uptime[...]` / `system.hw.uptime[...]` (SNMP) |
| **Network cards** | `net.if.in["ens192"]` and `net.if.out["ens192"]` as for any interface (the error and drop counters of the agent templates are not traffic and are left out); on **Linux** the state and the speed of a card come from `vfs.file.contents["/sys/class/net/<card>/operstate"]` and `.../speed`, which `Linux by Zabbix agent` collects |

- **Bars are coloured with the colour scale of the links** (the legend of the weathermap), so 90 % of a disk looks like 90 % of a link. In the **list**, the columns *Type*, *CPU*, *Memory* and *Fullest disk* appear when there is something to show in them; in the **heatmap** *Colour* offers CPU, Memory and Fullest disk; **Group by › Type** separates servers from network devices.
- **Server or network device?** A host that Zabbix reaches with an **agent interface** (and not with SNMP) is a *server*; so is a host that reports a load average, swap or disks. Everything else is a *network device*.
- **Hosts on active checks** (no passive agent): Zabbix does not show their availability, so a server whose values are fresh counts as *up*. If Zabbix says the agent interface is unreachable, the host stays *down*.
- **Old values are not shown.** A value that has not been updated for `stale_seconds` (default 300; filesystems: at least an hour) is left out - a dead agent does not keep showing its last CPU.
- **It costs one more request per batch of hosts** (a light one: no names, no tags) and is read together with the rest at every refresh. If that request fails, the network view keeps working and the header chip shows a **⚠** with the reason when you point at it.
- **What it does not do:** keys of your own templates (the keys above are the ones of Zabbix's stock templates), the disks of SNMP hosts (`vfs.fs.pused[...]`), and per-process or per-service data. A host is a "server" by its interface type, so a Linux host monitored by SNMP only is a network device with the data the SNMP template gives it.
- The **Test** button of the Connections window (and `trunkvista.py connection test`) says how many server items it found. **`--demo`** and the standalone `trunkvista_demo.html` have six servers (Linux and Windows), a *Datacenter* weathermap and a *Servers* tab to try it with.
