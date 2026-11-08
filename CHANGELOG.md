# Changelog

All notable changes. The newest release is at the top; the text of a release's section is what GitHub shows as its release notes.

## [Unreleased]

**Sign-in.** ChannelView now asks everybody to sign in, and the Debian installation creates the first account. **This changes how an existing installation behaves** - see *Upgrading* below.

- **Accounts with two roles.** *Administrators* edit (tabs, devices, links), manage the Zabbix connections and the other accounts; *viewers* can only look and use *Refresh now*. Viewing needs an account too: without a session the server answers with the sign-in page and nothing else (no layout, no device names), and `401` to every API call.
- **The installation asks for the first administrator** (user name and password, at least 10 characters) with a debconf question; an empty password - or an installation without a terminal, such as an unattended update - makes a random one. It is kept in `/var/lib/channelview/initial-admin-password` (root only), shown when installing from a terminal, and the account must choose its own password at the first sign-in (which deletes the file). The answers can be preseeded with `debconf-set-selections`; the password is not left in debconf's database.
- **Automatic sign-out per account.** Administrators set it for each account (1 minute up to a year, or *never* for a wall display); defaults are 60 minutes for administrators and 8 hours for viewers. Only what a person does counts as activity, not the page refreshing itself. Sessions are kept by the server, so they survive a restart and an update - a wall display is signed in once.
- **Account management** in the page (the button with your name: *Account*, *Users…*: add, role, automatic sign-out, disable, set a password, sign out everywhere, delete) and on the server: **`channelview-user`** (`list`, `add`, `passwd`, `set`, `delete`, `signout`; also `python3 channelview.py user ...` without the package). A running server sees the changes at once. The last enabled administrator cannot be removed, and nobody can demote, disable or delete their own account.
- **Hardening.** Passwords are salted PBKDF2-HMAC-SHA256 (600 000 rounds); only a hash of each session key is stored; the cookie is `HttpOnly`, `SameSite=Strict` and `Secure` over HTTPS (or with `--secure-cookie` behind an HTTPS proxy); changes need a token that belongs to the session (the old shared edit token is gone); sign-in is rate-limited per address and per account, an unknown user costs as much time as a wrong password, and unknown name / wrong password / disabled account look the same. Sign-ins, failures and account changes are logged without any secret. Concurrent hashing is limited so a flood of sign-ins cannot starve the dashboard.
- **Safer defaults.** The service starts with `--require-login`: with no account at all nobody can get in (instead of everybody). The program refuses to listen on other addresses than the local computer while no account exists (`--no-login` keeps the old behaviour, not recommended). `custom.css` also applies to the sign-in page.

**Upgrading from 1.2.x:** the upgrade creates the administrator (it asks; see above), removes the old `edit_token` and requires a sign-in from then on. If `channelview-update` runs the upgrade unattended, read `/var/lib/channelview/initial-admin-password` afterwards (or `sudo channelview-user passwd admin`). A reverse proxy in front of the dashboard needs `--secure-cookie` in `CHANNELVIEW_ARGS`; see the README.

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
