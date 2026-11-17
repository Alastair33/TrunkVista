# Changelog

All notable changes. The newest release is at the top; the text of a release's section is what GitHub shows as its release notes.

## [Unreleased]

## [1.4.0] - 2026-11-17

### Privacy
- Device names read from Zabbix are no longer saved in the layout file: the program stores only the host ids you picked and shows names live. Layouts from older versions are cleaned on the first start (and the `.bak` that still held the names is removed). `docs/administration.md` now lists what is and is not stored.

### Editor
- In **Add link**, the device picked as A or as B is lit on the map with a soft glow, so it is clear which device the link will start and end at.

### Demo
- The standalone demo opens on the list of all devices.

### Licence
- TrunkVista is now released under the **MIT License** (`LICENSE`; the Debian package carries it in its `copyright` file).

### Sign-in page intro
- The sign-in page opens with a short intro: the logo and name fade in at the centre, then slide to the left while the sign-in box fades in (about 2.5 seconds). A key press or click skips it, so does a repeat visit in the same browser tab and the system setting "reduce motion"; add `?intro` to the address to see it again. It is CSS only and needs nothing from outside.

### Fixed after a review of the whole program
- `trunkvista-cert` (and so `trunkvista-setup --network`) failed on machines whose full host name is longer than 64 characters, which a certificate's common name cannot hold. The full name stays in the certificate's address list, which is what browsers check.
- The data folder is `0700` everywhere. The server always set it to 700 when it started, while the package and the service file said 750.
- A server item without a host in the Zabbix reply is skipped instead of stopping the server metrics.
- `packaging/whats_new.py` and the logo tool no longer use syntax that fails on Python 3.9 to 3.11; the CI lint now checks against Python 3.9 so that this cannot come back.

**Server data from the Zabbix agent.** TrunkVista reads **CPU, memory, swap, load average, filesystems and uptime** of servers through the API, next to the interface data of the Cisco devices. Nothing to set up - the same API token is used and the items are found by the keys of Zabbix's stock templates (*Linux by Zabbix agent*, *Windows by Zabbix agent*, and the SNMP templates). [Details in the README](README.md#servers-zabbix-agent)

- **Everywhere a device is shown:** cards get CPU / Memory / Swap / Disk bars (and the uptime), the list gets the columns *Type*, *CPU*, *Memory* and *Fullest disk* (only when the list has such data), the heatmap can be coloured by **CPU, Memory or Fullest disk**, **Group by › Type** separates servers from network devices, and the tooltips of a device and of a node on a weathermap show the numbers. The bars use the colour scale of the links.
- **Cisco devices show their CPU and memory, too,** when their SNMP template has them (`system.cpu.util[...]`, `vm.memory.util[...]`, uptime).
- **Servers on weathermaps:** the network cards of a Linux server are interfaces like those of a switch; their **state and speed** come from `/sys/class/net/<card>/operstate` and `.../speed` (items of *Linux by Zabbix agent*). The agent templates' error and drop counters, which share the `net.if.in[` prefix, are not taken for traffic.
- **A server is a host that Zabbix reaches with its agent** (or that reports load, swap or disks); a server on active checks counts as *up* while its values are fresh. Values older than `stale_seconds` are left out, so a dead agent does not keep showing its last CPU.
- **Light and safe:** one more `item.get` per batch of hosts (no names, no tags). If it fails, the network view keeps working and the header chip shows a ⚠ with the reason. The **Test** button and `trunkvista.py connection test` say how many server items were found.
- **Demo:** `--demo` and `trunkvista_demo.html` have six servers (Linux and Windows), a *Datacenter* weathermap and a *Servers* tab; the standalone page reads Zabbix with the same rules as the server (tests compare the two).

**Shorter README.** The README is a one-screen start page now (what it is, the four install lines, the download files, a few everyday commands): the long parts moved unchanged into `docs/` - an *administration guide* (install details, `trunkvista-setup`, accounts, HTTPS, updates, troubleshooting), *Servers*, *Changing the design* and a *maintainer guide*. Only two screenshots are kept (the weathermap and the heatmap); the other ten were removed. The release notes are no longer repeated in the README: they are in this file and on the Releases page.

**Security fixes after a penetration test.**
- *A saved Zabbix API token stays with its server.* Leaving the token (or password) field empty keeps the saved one only while the connection still points at the same server (scheme, host and port). Before, **Test** or **Save** with another address used the saved token there - whoever held an administrator's session could read the token back that way. With another address the page now asks for the token again.
- *Connections that never finish cannot shut everybody out.* The server takes at most 512 connections at a time and 64 from one address (more get `503`), and a request that has begun must have its headers complete within 15 seconds and its body within 30, however slowly it drips in. Before, about a thousand half-open connections used up the service's file handles and nobody else got in. The service's file limit is 4096 now.
- *Strangers cannot lock the owner out.* After 20 wrong passwords for one account in 15 minutes, further tries for it are refused - except from addresses that signed in to it before, so someone guessing from elsewhere can no longer shut the real user out. The limit of 10 failures per address in 5 minutes is unchanged.
- *A bad interface-name regex cannot freeze the server.* The `name_regex` of a connection is refused (when saved, and when a configuration is loaded) if it repeats a group that repeats again or has alternatives, such as `(a+)+` or `(a|a)*`; a safe form like `(?:/\d+)*` is fine. Item names are cut to 256 characters before matching.
- *Weak passwords are refused,* not just short ones: runs such as `1234567890` or `qwertyuiop`, fewer than 5 different characters, common words with digits or symbols added (`Password123!`, `P@ssw0rd2024`), and the user name with digits added. The minimum of 10 characters stays.
- *Quieter headers.* The `Server` header says `TrunkVista` and no longer the Python version. `--hsts` (or `TRUNKVISTA_HSTS=1`) adds `Strict-Transport-Security` on HTTPS; it is off by default because with a self-signed certificate it also removes the browser's "continue anyway" button.
- *A port that was asked for is the port.* With `--port N` (the Debian service always passes it) the server now stops with a clear message when N is taken, instead of quietly listening on N+1 where nothing expects it. Without `--port` it still looks for a free one.
- The answer of a Zabbix server is read up to 64 MB; a wrong address that streams for ever is refused instead of filling the memory. The check for a bad `name_regex` also catches bounded repeats such as `(a+){1,30}`.
- Zabbix API calls no longer follow redirects (the token would have travelled along); the message names the address to enter instead - typically `https://` instead of `http://`.

**Fixed.** A malformed layout (a number where an object belongs, thousands of nested brackets) is answered with "bad request" instead of a server error and a traceback in the log; dropped connections no longer write tracebacks to the log; `stale_seconds` and `min_severity` given as text in `config.json` work.

## [1.3.0] - 2026-11-08

**New name: TrunkVista.** The program, called ChannelView until now, is **TrunkVista**. It has a new logo, and everything that carried the name changed with it:

- **Package and files:** `trunkvista` (`trunkvista_<version>_all.deb`, `trunkvista-latest.deb`), `trunkvista.py`, `trunkvista_demo.html`; commands `trunkvista-user`, `trunkvista-update`, `trunkvista-cert`; service `trunkvista` and `trunkvista-update.timer`; folders `/var/lib/trunkvista`, `/etc/trunkvista`, `/etc/default/trunkvista`, `~/.trunkvista`; settings `TRUNKVISTA_*`; the session cookie is `tv_session`.
- **The Debian package of the new name replaces the old one** (`Conflicts` / `Replaces: channelview`). When it is installed over a ChannelView installation, it moves the data (layouts, the saved Zabbix connections, accounts, `custom.css`), the HTTPS certificate and key, the update signing key and the settings in `/etc/default` (renamed to `TRUNKVISTA_*`) to the new places, removes the old service user and says what it did (the nightly update check is off afterwards: `sudo trunkvista-setup --auto-update` switches it on again). Everybody signs in again once (the cookie has a new name). Afterwards `sudo apt purge channelview` removes the leftover package record; it does not touch the moved data.
- **Without the package:** `CHANNELVIEW_*` environment variables are still read (`TRUNKVISTA_*` wins), `~/.channelview` is renamed to `~/.trunkvista` the first time (when the new folder does not exist and no `--data-dir` is given), and the browser's remembered choices (colour theme, view options, the standalone demo's layouts) are moved to the new keys.
- **A new logo** for the new name: a blue *trunk* that forks at a router node into four *links* in the usage colours, each ending in a node. It is in the page header, on the sign-in page (both follow the accent colour) and as the tab icon; `docs/logo` holds the logo set (SVG and PNG for dark and light backgrounds, one-colour versions, app icon, favicon, GitHub social preview) with the scripts that draw it, and `tools/embed_logo.py` puts it into the program. The screenshots in the README show it.

**Easier installation.** From nothing to a dashboard that is running, in four lines and one answer - the administrator's password; the Zabbix server and its API token are entered in the web page only:

```
wget https://github.com/<owner>/<repository>/releases/latest/download/trunkvista-latest.tar.gz
tar xzf trunkvista-latest.tar.gz
cd trunkvista
sudo ./install.sh
```

- **`trunkvista-latest.tar.gz`** - a new file in every release (listed in `SHA256SUMS`): a folder `trunkvista/` with `install.sh`, the package and its checksum. It works on a machine without internet access (copy the archive there); `install.sh` checks the package against the checksum and installs it with apt. The archive is made by the new `packaging/build_bundle.sh`, the same way every time.
- **`install.sh`** also stays a file of its own, for `curl -fsSL https://github.com/<owner>/<repository>/releases/latest/download/install.sh | sudo sh`: it downloads the newest release (never the branch), checks the package against the release's `SHA256SUMS` (a mismatch stops it) and installs it; `--version X.Y.Z` takes another release. Even piped from curl it asks its one question on your terminal.
- **Only the administrator's password is asked** (by apt, as before; empty = a random one, kept for root). The administrator is called `admin` unless `trunkvista/admin_user` is preseeded - the name is no longer asked. Nothing is asked about Zabbix: an administrator who opens the page is taken to the Connections window by itself, where the address and the API token are entered and tried before they are saved.
- **`trunkvista-setup`** - a new command for what the page cannot do, up to two questions with their answers in `[brackets]`: **who will open the dashboard** (this computer, or other PCs: the dashboard then listens on the network and **HTTPS is switched on** with a certificate it makes, the addresses to open are printed, and so is the command for an active `ufw` / `firewalld`; over SSH the default is the network) and **nightly updates**. It restarts the service and waits until it answers. Safe to run again; it edits only the lines of `/etc/default/trunkvista` it must and never replaces a certificate of your own. `--network`, `--local`, `--auto-update`, `--yes`, ... do the same without questions; for scripted installations `--zabbix-url` with `--token-stdin` / `--token-file` also saves a Zabbix connection (tried first).
- **`trunkvista.py connection add | list | test`** - the Zabbix connections from the command line, also without the Debian package. The API token is read hidden (or from standard input / a file, never from the command line) and is tried before it is saved.
- The README starts with a **quick start**, the install steps are in three flavours (four lines / one command / step by step), and a section describes running it **without the package** on any system with Python 3.9+.

**Upgrading from ChannelView (1.2.1 or older):** `channelview-update` cannot cross the rename (it looks for packages called `channelview`), so install the new package **once by hand**: download `trunkvista-latest.deb` from the latest release and run `sudo apt install ./trunkvista-latest.deb`. From then on `sudo trunkvista-update` works as before. Not moved by the installation: a systemd drop-in in `/etc/systemd/system/channelview.service.d/` (copy it to `trunkvista.service.d/`), scripts or monitoring that call the old command names, and a reverse proxy or firewall rule that names the old service. Past releases keep their old name in this file.

**Sign-in.** TrunkVista now asks everybody to sign in, and the Debian installation creates the first account. **This changes how an existing installation behaves** - see *Upgrading* below.

- **Accounts with two roles.** *Administrators* edit (tabs, devices, links), manage the Zabbix connections and the other accounts; *viewers* can only look and use *Refresh now*. Viewing needs an account too: without a session the server answers with the sign-in page and nothing else (no layout, no device names), and `401` to every API call.
- **The installation asks for the first administrator's password** (at least 10 characters; the account is called `admin`) with a debconf question; an empty password - or an installation without a terminal, such as an unattended update - makes a random one. It is kept in `/var/lib/trunkvista/initial-admin-password` (root only), shown when installing from a terminal, and the account must choose its own password at the first sign-in (which deletes the file). The answers can be preseeded with `debconf-set-selections`; the password is not left in debconf's database.
- **Automatic sign-out per account.** Administrators set it for each account (1 minute up to a year, or *never* for a wall display); defaults are 60 minutes for administrators and 8 hours for viewers. Only what a person does counts as activity, not the page refreshing itself. Sessions are kept by the server, so they survive a restart and an update - a wall display is signed in once.
- **Account management** in the page (the button with your name: *Account*, *Users…*: add, role, automatic sign-out, disable, set a password, sign out everywhere, delete) and on the server: **`trunkvista-user`** (`list`, `add`, `passwd`, `set`, `delete`, `signout`; also `python3 trunkvista.py user ...` without the package). A running server sees the changes at once. The last enabled administrator cannot be removed, and nobody can demote, disable or delete their own account.
- **Hardening.** Passwords are salted PBKDF2-HMAC-SHA256 (600 000 rounds); only a hash of each session key is stored; the cookie is `HttpOnly`, `SameSite=Strict` and `Secure` over HTTPS (or with `--secure-cookie` behind an HTTPS proxy); changes need a token that belongs to the session (the old shared edit token is gone); sign-in is rate-limited per address and per account, an unknown user costs as much time as a wrong password, and unknown name / wrong password / disabled account look the same. Sign-ins, failures and account changes are logged without any secret. Concurrent hashing is limited so a flood of sign-ins cannot starve the dashboard.
- **Safer defaults.** The service starts with `--require-login`: with no account at all nobody can get in (instead of everybody). The program refuses to listen on other addresses than the local computer while no account exists (`--no-login` keeps the old behaviour, not recommended). `custom.css` also applies to the sign-in page.

**Upgrading from 1.2.x:** the upgrade creates the administrator (it asks; see above), removes the old `edit_token` and requires a sign-in from then on. If `trunkvista-update` runs the upgrade unattended, read `/var/lib/trunkvista/initial-admin-password` afterwards (or `sudo trunkvista-user passwd admin`). A reverse proxy in front of the dashboard needs `--secure-cookie` in `TRUNKVISTA_ARGS`; see the README.

## [1.2.1] - 2026-11-05

People who download ChannelView are now sent to the **latest release**, never to the branch.

- **Fixed file names in every release.** Besides `channelview_<version>_all.deb`, each release carries `channelview-latest.deb` (the same package),
  `channelview.py` (the single program file, for systems without apt), `channelview_demo.html` and `SHA256SUMS`, which lists all of them. GitHub's
  `releases/latest/download/<name>` address therefore always serves the newest full release (pre-releases are never "latest"). `channelview-update`
  is unaffected: it still looks for `channelview_<version>_all.deb` only.
- **README.** A new *Download* section (and a note under the logo) says to take ChannelView from the latest release and not with *Code → Download ZIP*
  or a clone of `main`, which can hold unreleased changes; it lists the files and their permanent addresses, how to check the checksums, and how
  to download from a private repository with `gh`. The Debian install steps no longer name a version, *Updating* says that only the newest full
  release is installed, and *Development* says that a clone is for working on the program.
- The release notes on GitHub end with the same advice. A test keeps the README and the release workflow in agreement about the file names.

## [1.2.0] - 2026-11-05

A **light mode** and a **heatmap view** for the device tabs, easier-to-read labels, and the fixes from a review of the whole program.

- **Light mode.** A button in the header (or the **T** key) switches the colour theme: *Dark* (the default, as before), *Light*, or *Auto*, which
  follows the system setting and changes with it. The choice is remembered in the browser; a wall display can be set from its address
  (`#theme=light`, `#theme=dark` or `#theme=auto`, e.g. `...#kiosk=1&theme=light`). The map, the traffic graph, the heatmap, hover cards, menus and
  dialogs all follow the theme. `custom.css` keeps working in both themes: a `:root { ... }` rule applies to both, `:root[data-theme=light] { ... }`
  or `:root[data-theme=dark] { ... }` to one of them. The map's frame, label and graph colours are variables now (`--nodefill`, `--halo`,
  `--iftag`, `--gab`, `--gba`, ...), so they can be restyled there as well.
- **Heatmap view for device tabs**, next to List and Cards: one small tile per device, coloured by *Status*, *Busiest interface* (the usage
  scale of the weathermaps) or *Worst problem* (Zabbix severity), with the value written on the tile and a legend above. *Sort by* and the
  order button put the hottest devices first; hover a tile for details, click it (or press Enter) to open them. Filter, grouping, paging and
  kiosk mode work as in the other views. A device tab can open as a heatmap by default (Edit > This tab > Style); the viewer's choice of view
  and colour is remembered in the browser.
- Text on a coloured fill (the usage labels on the weathermap, heatmap tiles, the usage scale) is now dark or light, whichever reads better;
  white text on the mid greens and oranges was hard to read (about 2:1).
- The name of the selected connection in the Connections window was almost invisible (dark text on a dark highlight); fixed.
- The toolbar summary ("Showing 1-50 of 128 ...") wraps to its own line instead of being cut off when the toolbar is full.

**Fixes from a review of the whole program**

- **The edit token is no longer given to a request that comes through a reverse proxy.** A proxy on the same machine made every visitor look local, so
  anyone who could reach the proxy could edit. A request that carries a proxy header (`X-Forwarded-For`, `X-Forwarded-Host`, `X-Real-IP`, `Forwarded`,
  `Via`) gets a read-only page now. Not every proxy sends such a header, so the README still says: keep the dashboard on the loopback address
  behind a proxy and protect the proxy itself.
- The page can be opened through the IPv6 loopback address (`http://[::1]:8088`); the check of the `Host` header refused it.
- An address that answers with something other than a Zabbix JSON-RPC reply (a web page, other JSON) is reported as a wrong URL instead of an
  internal error.
- `connections.json` (it holds the API tokens) is created readable by its owner only from the first byte; there was a moment during the write
  when it carried the default permissions.
- A connection that sends nothing is dropped after 60 s, so idle sockets cannot pile up.
- A drag that the browser cancels (a touch that turns into a page scroll) no longer leaves the page "dragging", which had stopped the data from
  refreshing; in edit mode a finger drags a device or a bend point instead of scrolling the page.
- Playlists: one whose tabs have all been deleted does not start or loop on nothing, and a playlist without *loop* stops after its last tab.

**Project**

- The README has a **Release notes** section: every release gets a "What's new in X.Y.Z" segment (the latest three are shown, older ones are in
  this file). `packaging/release.sh` writes it from the notes of the release, so it never differs from what GitHub shows; pre-releases are skipped.
- The README screenshots were retaken (they still showed the old diamond logo) and two light-mode screenshots were added.
- CI also runs ruff (pyflakes rules), a JavaScript syntax check of both page scripts, and a test that keeps the dark and light palettes
  defining the same variables.

## [1.1.1] - 2026-11-05

- **New logo.** A C made of four link segments in the usage colours (green, lime, amber, orange) around a blue node. It is now in the page
  header, replacing the plain diamond, and is the browser tab icon (a drawing tuned to stay readable at 16 px). The header mark's centre node
  follows `--accent`, so a `custom.css` accent colour restyles it.
- The logo files are in the repository under `docs/logo`: SVG and PNG logos for dark and light backgrounds, one-colour marks, app icon,
  favicon, a 1280 x 640 social preview image, the colour and usage rules, and the script that draws them.
- The README now opens with the logo and shows screenshots of the interface (demo data) in `docs/screenshots`.

## [1.1.0] - 2026-11-04

- **Renamed from "Zabbix Dashboard" to ChannelView** (before the first public release): program `channelview.py`, package, commands
  (`channelview-update`, `channelview-cert`), service, folders (`/var/lib/channelview`, `/etc/channelview`) and settings (`CHANNELVIEW_*`)
  all use the lowercase name that Debian requires; the page, README and release titles say ChannelView. The settings the page keeps in
  the browser (graph range, refresh interval, list view) and the standalone demo's saved connections are stored under new
  `channelview-*` names, so a browser that used the old name starts with the defaults.
- **HTTPS built in** - no Apache or Nginx needed: `--tls-cert` / `--tls-key` (or `CHANNELVIEW_TLS_CERT` / `CHANNELVIEW_TLS_KEY` in `/etc/default/channelview`).
  TLS 1.2 or newer; a renewed certificate file is used without a restart; `http://` typed on the HTTPS port is redirected to `https://`;
  an unusable certificate stops the start with a clear message.
- `channelview-cert`: makes a self-signed certificate (names and addresses of the machine) and prints the two settings to use.
- `custom.css` in the data folder (`/var/lib/channelview/custom.css`): your own style rules, added after the built-in ones and read at every
  page load. Updates never touch it.
- `packaging/release.sh patch | minor | major`: next version, `[Unreleased]` notes become the release section, tests, commit, tag
  (`--push` publishes). Pre-releases (`X.Y.Z-rc.1`) keep the notes for the final release.

## [1.0.0] - 2026-11-04

First packaged release.

- Weathermap tabs: devices joined by interface links, coloured by status and utilisation, labelled with % and bit/s per direction;
  one bend point per link, interface names above / below the line, per-device notification switch, movable usage meter.
- Hover a link for an in / out traffic graph; time range 5 min to 30 days (hourly Zabbix trends beyond 24 h) and data refresh interval
  5 s to 15 min are picked in the header.
- Device tabs (cards and sortable list, grouped by status, proxy, OS, model, ...), playlists and kiosk mode.
- Built-in editor, demo mode (`--demo`), connection menu for Zabbix 7.0 (API token), standalone demo page.
- Debian / Ubuntu package (`channelview_1.0.0_all.deb`): systemd service running as its own user, data in `/var/lib/channelview`,
  settings in `/etc/default/channelview`, edit token that survives restarts (`--token-file`).
- `channelview-update`: installs newer releases from GitHub (SHA-256 check, optional GPG signature check, optional nightly timer).
