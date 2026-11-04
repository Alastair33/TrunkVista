# Changelog

All notable changes. The newest release is at the top; the text of a release's section is what GitHub shows as its release notes.

## [Unreleased]

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
