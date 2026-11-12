# Administration guide

[← README](../README.md)

Install details, network access and HTTPS, accounts, updates, where files live and troubleshooting. The short version is in the [README](../README.md).

## Install on Debian / Ubuntu

Supported: Debian 11+, Ubuntu 22.04+ (anything with Python 3.9+ and systemd).

### The four lines

```
wget https://github.com/Alastair33/TrunkVista/releases/latest/download/trunkvista-latest.tar.gz
tar xzf trunkvista-latest.tar.gz
cd trunkvista
sudo ./install.sh
```

What `install.sh` does, in this order: checks the package next to it against the checksum that came with it (and stops if they differ), then installs it with `apt-get`, which asks for **the administrator's password** - nothing else. Empty = a random one is made and kept in `/var/lib/trunkvista/initial-admin-password`; so is it without a terminal (a script, Ansible), where nothing is asked at all. The administrator is called `admin`; add people with [`trunkvista-user`](#accounts-and-sign-in) afterwards, or preseed another name (see there). The installation does not need the internet, apart from what apt fetches for the package's own dependencies (Python 3).

The dashboard then runs on **this computer only** (`http://127.0.0.1:8088/`). Open it, sign in, and **enter your Zabbix server in the page**: its address (`https://zabbix.example.com`) and an API token (Zabbix: *Users → API tokens → Create API token*; a read-only user that can see your hosts is enough) → **Test** → **Save & use**. Neither is asked by the installation, and neither is typed into a file. Reaching the dashboard from other PCs is one more command: [`sudo trunkvista-setup --network`](#trunkvista-setup-network-access-and-updates).

`install.sh` is a short script: read it before you run it as root (`less install.sh`). The checksum in the archive catches a damaged or half-copied package; to be sure the archive itself is the release's, compare it with `SHA256SUMS` from the same release (`sha256sum -c --ignore-missing SHA256SUMS`, see [Download](../README.md#download)). While the repository is **private** `wget` cannot reach it without signing in: download with the GitHub tool as shown in [Download](../README.md#download), then continue at `tar xzf`.

### One command, no archive

```
curl -fsSL https://github.com/Alastair33/TrunkVista/releases/latest/download/install.sh | sudo sh
```

The same script on its own: it downloads `trunkvista-latest.deb` and `SHA256SUMS` from the **latest release** (never from the branch), checks the package and installs it - then the same password question, the same page. `sudo sh install.sh --version 1.3.0` takes a release other than the newest (this way only: an unpacked archive installs the package it holds). Piped from `curl`, the script still asks its one question on your terminal.

### Step by step

The same, by hand - for a machine where you want to see each step, or a private repository:

1. Download `trunkvista-latest.deb` and `SHA256SUMS` from the **[latest release](https://github.com/Alastair33/TrunkVista/releases/latest)** and check them (see [Download](../README.md#download)).
2. Install it:

   ```
   sudo apt install ./trunkvista-latest.deb
   ```

   This creates the `trunkvista` system user, installs the systemd service `trunkvista`, enables it and starts it. **It asks for the password of the first administrator** (at least 10 characters; common passwords, patterns like `1234567890` and the user name with digits are refused). Leave it empty and one is made for you - see [Accounts and sign-in](#accounts-and-sign-in).
3. Open `http://127.0.0.1:8088` on that machine, **sign in**, and add your Zabbix in the window that opens (as above).

### trunkvista-setup, network access and updates

After the installation the dashboard listens on this computer only and the nightly update check is off. `sudo trunkvista-setup` asks two questions about that and does the rest. Every question has its answer in `[brackets]`; Enter takes it, and **running it again is safe** - it changes only what you answer. (The Zabbix server is not among them: it is entered in the page.)

| Question | What it does |
|---|---|
| **Who will open the dashboard?** | *This computer only* (the default) or *other computers*: the dashboard then listens on all interfaces (`TRUNKVISTA_HOST=0.0.0.0`) and **HTTPS is switched on** - a self-signed certificate for this machine's names and addresses is made and entered into `/etc/default/trunkvista`. Logged in over SSH, the default is the second answer: that is a server. It also prints the addresses to open and, if `ufw` or `firewalld` is active, the one command that lets the port through. |
| **Install new releases automatically?** | Switches the nightly `trunkvista-update.timer` on. Default no: it installs software unattended, so you decide. |

Afterwards it restarts the service, waits until it answers, and prints where to open it. It edits only the lines of `/etc/default/trunkvista` that it must (comments, port and update settings stay); your own certificate (`TRUNKVISTA_TLS_CERT` already set) is never replaced.

Without questions - for a script or a fleet - give the answers as options; anything you do not mention stays as it is:

```
sudo trunkvista-setup --network --auto-update
sudo trunkvista-setup --local --yes
```

| Option | |
|---|---|
| `--network`, `--local` | other PCs may open it (listens everywhere, HTTPS on) / this computer only. `--network` needs an account to exist |
| `--no-https` | with `--network`: no HTTPS, because a reverse proxy in front does it ([see below](#https-do-this-as-soon-as-other-pcs-open-it)) |
| `--auto-update`, `--no-auto-update` | the nightly update check on / off |
| `--yes`, `--no-restart` | never ask / do not restart the service |

**Zabbix from a script.** A fleet that must come up connected can give the connection as options as well (it is tried first and saved only if Zabbix accepts it; on failure nothing else is changed either): `--zabbix-url URL` (`/api_jsonrpc.php` is added), the API token from `--token-file FILE` or `--token-stdin` (never an option on the command line, where other users could read it), `--zabbix-user NAME` (log in with this user and a password in the file / standard input instead of a token), `--zabbix-name TEXT` and `--no-verify-tls` (accept a self-signed HTTPS certificate on the Zabbix side):

```
echo "$ZABBIX_API_TOKEN" | sudo trunkvista-setup --zabbix-url https://zabbix.example.com --token-stdin --yes
```

The same is available on its own, which is the way for a run without the package and for a script that has no sudo: `python3 trunkvista.py connection add https://zabbix.example.com` (`list` and `test` show and try what is saved; a running dashboard reads the connections when it starts).

### Without the package (any system with Python 3.9+)

One file, standard library only - no installation:

```
curl -fLO https://github.com/Alastair33/TrunkVista/releases/latest/download/trunkvista.py
python3 trunkvista.py user add NAME --role admin          # the first account; asks for the password twice
python3 trunkvista.py connection add https://zabbix.example.com       # asks for the API token (typing is hidden), tries it, saves it
python3 trunkvista.py                                      # http://127.0.0.1:8088 - for other PCs: --host 0.0.0.0 --tls-cert ... --tls-key ... (see HTTPS)
```

The data (accounts, connections, layouts) lives in `~/.trunkvista`; `--data-dir` changes that. It is not started again after a reboot - for that, use the Debian package, or write a service for your system.

Check on it:

```
systemctl status trunkvista
journalctl -u trunkvista -f
```

### Accounts and sign-in

**Everybody signs in** - viewing needs an account as well as editing. There are two roles:

| Role | May |
|---|---|
| **admin** | everything: edit tabs, devices and links, the Zabbix connections, and the accounts of the others |
| **viewer** | look: tabs, playlists, kiosk mode, heatmap, graphs, and *Refresh now*. Nothing else - not even the list of Zabbix connections |

**At the installation** `apt install` asks for the administrator's password (the account is called `admin`; preseed `trunkvista/admin_user`, below, to choose another name - or add a second administrator with `trunkvista-user add NAME --role admin` and delete `admin`). If you leave the password empty - or the installation runs without a terminal, as an unattended upgrade does - a random one is made: it is shown at the end of an installation from a terminal and kept in `/var/lib/trunkvista/initial-admin-password` (readable by root only). That account **has to choose its own password at the first sign-in**; the file is deleted then. `apt` keeps a copy of the installation screen in `/var/log/apt/term.log`, which is why the account must change the password before anything else.

To answer the questions in advance (an automated installation):

```
echo 'trunkvista trunkvista/admin_user string admin
trunkvista trunkvista/admin_password password CHOOSE-A-LONG-PASSWORD
trunkvista trunkvista/admin_password_again password CHOOSE-A-LONG-PASSWORD' | sudo debconf-set-selections
sudo DEBIAN_FRONTEND=noninteractive apt install ./trunkvista-latest.deb
```

The password is not kept in debconf's database after the installation, but a preseed file you wrote still holds it - delete it afterwards.

**Upgrading from 1.2.x or older:** those versions had no sign-in (viewing was open, editing used the edit token). The upgrade creates the administrator as above, removes the old `edit_token` and from then on asks everybody to sign in. A `trunkvista-update` that runs from the timer cannot ask: it makes the random-password account, so read `/var/lib/trunkvista/initial-admin-password` (or run `sudo trunkvista-user passwd admin`) after such an update.

**Managing accounts** - as an administrator, in the page: the button with your name in the header opens *Account* (change your own password, sign out) and, for administrators, *Users…* (add, change role, automatic sign-out, disable, set a password, sign out everywhere, delete). Or on the server, which also works when nobody can sign in:

```
sudo trunkvista-user list
sudo trunkvista-user add anna --role viewer                       # asks for the password twice
sudo trunkvista-user add wall1 --idle never --password-stdin      # a wall display; the password comes from standard input
sudo trunkvista-user add bob --role admin --random-password       # prints the password once
sudo trunkvista-user passwd anna --must-change                    # new password; anna has to change it at the next sign-in
sudo trunkvista-user set anna --idle 120                          # sign out after 2 hours without use
sudo trunkvista-user set anna --disable                           # or --enable, --role admin
sudo trunkvista-user signout anna
sudo trunkvista-user delete anna
```

A running server sees these changes at once. **Locked out?** Every administrator forgot the password: `sudo trunkvista-user passwd NAME --must-change`.

**Automatic sign-out (per account).** Idle time counts from what a person does on the page (a click, a key, a touch) - the page refreshing itself does not count. The defaults are 60 minutes for an administrator and 8 hours for a viewer; `--idle never` (or *Never* in the Users dialog) is for a **wall display** that has to stay on. The sign-in itself is remembered by the browser and by the server (it survives a restart and an update), so a wall display needs to be signed in once. Give such an account the `viewer` role: a display that is never signed out should not be able to change anything.

**Good to know**

- Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes (600 000 rounds) in `users.json`; sessions as hashes of a random key in `sessions.json`; both owner-only. A new password, disabling an account or *sign out everywhere* ends all sessions of that account.
- Wrong passwords are slowed down: 10 failures from one address in 5 minutes block further tries from it for a while (HTTP 429); 20 failures for one account in 15 minutes block it for every address that has not signed in to that account before, so strangers cannot lock the owner out. Behind a reverse proxy all visitors share the proxy's address, so the limit for the address applies to all of them together; the limit per account protects them in any case.
- The last enabled administrator cannot be deleted, demoted or disabled, and nobody can demote, disable or delete **their own** account.
- Every sign-in, failed attempt and account change is in the log (`journalctl -u trunkvista | grep accounts`) - never a password or a session key.
- The session cookie is `HttpOnly` and `SameSite=Strict`, and marked `Secure` when the dashboard serves HTTPS itself. Changes are only accepted with a request token that belongs to the session, so another website cannot make your browser change anything.
- The Zabbix API token never reaches the browser, whoever is signed in.
- Do not run the program with `--no-login` on a network: it ignores the accounts and falls back to the old behaviour (everyone can view, editing needs the edit token). Without any account the program refuses to listen on other addresses than the local computer.
- Running it by hand (without the package): `python3 trunkvista.py user add NAME --role admin` creates the first account in the data folder (`~/.trunkvista` by default); `--require-login` makes a server ask for a sign-in even before the first account exists.

### Making it reachable from other PCs / wall displays

The easy way: **`sudo trunkvista-setup --network`** (or answer *other computers* when `sudo trunkvista-setup` asks). It does what is described here and in the next section: listens on all interfaces, makes a certificate, switches HTTPS on, restarts, and prints the addresses. Take `--local` to go back.

By hand: by default it listens on `127.0.0.1` only. Edit `/etc/default/trunkvista`:

```
TRUNKVISTA_HOST=0.0.0.0
```

then `sudo systemctl restart trunkvista` (open the port in your firewall if you run one).

- **Everybody has to sign in** (see above): create the accounts for the people and wall displays first, then change the setting. Without HTTPS the passwords cross the network readable - switch it on (next section) before other PCs open the page.
- A port below 1024 needs `sudo systemctl edit trunkvista` → `[Service]` / `AmbientCapabilities=CAP_NET_BIND_SERVICE`.

### HTTPS (do this as soon as other PCs open it)

Without HTTPS the passwords, the session cookie and everything else cross the network readable. The dashboard serves HTTPS **itself** – no Apache or Nginx needed. `sudo trunkvista-setup --network` switches it on in one step (with the self-signed certificate below); this section is what it does, and how to use a certificate of your own.

```
sudo trunkvista-cert          # self-signed certificate for this machine -> /etc/trunkvista/tls/
```

Add the two lines it prints to `/etc/default/trunkvista` (a fresh install has them there, commented out; after an upgrade from 1.0.0 you add them), then restart:

```
TRUNKVISTA_TLS_CERT=/etc/trunkvista/tls/server.crt
TRUNKVISTA_TLS_KEY=/etc/trunkvista/tls/server.key
```
```
sudo systemctl restart trunkvista      # now:  https://<server>:8088/
```

- Typing `http://` on that port redirects to `https://`. TLS 1.2 or newer only.
- **Browser warning:** a self-signed certificate is not trusted until you import `server.crt` as a trusted certificate on each PC / kiosk that opens the page. With a certificate from your own CA or Let's Encrypt there is no warning.
- **Your own certificate:** point the two settings at your files. The certificate file holds the full chain (certificate first); the key must be readable by the group `trunkvista` (`chgrp trunkvista key.pem && chmod 640 key.pem`). The key may also sit in the certificate file – then leave `TRUNKVISTA_TLS_KEY` out.
- **Renewal needs no restart.** The files are looked at again when they change (checked on new connections, about every 10 s). `sudo trunkvista-cert --force` or certbot is enough. If the new files do not load (the key does not match, a file is half written) the old certificate stays in use and the log says so.
- A wrong certificate path or a key that does not fit stops the service at start with the reason in `journalctl -u trunkvista`.
- Let's Encrypt, as an example of a deploy hook (`/etc/letsencrypt/renewal-hooks/deploy/trunkvista`, adjust the paths):

  ```
  #!/bin/sh
  install -D -m 0644 "$RENEWED_LINEAGE/fullchain.pem" /etc/trunkvista/tls/server.crt
  install -D -m 0640 -g trunkvista "$RENEWED_LINEAGE/privkey.pem" /etc/trunkvista/tls/server.key
  ```
- **Reverse proxy instead?** Not needed. If you want one anyway (one address for several tools): the proxy must speak HTTPS to the browsers, and you add `--secure-cookie` to `TRUNKVISTA_ARGS` in `/etc/default/trunkvista` so that the sign-in cookie is marked `Secure` although the proxy talks plain HTTP to the dashboard. With a certificate that browsers trust (the proxy's, or your own on the dashboard) you may also add `--hsts`: browsers then never go back to `http://`. Leave it off for a self-signed certificate, where it also removes the "continue anyway" button. While the dashboard listens on `127.0.0.1` only, it accepts requests whose `Host` header is `127.0.0.1:<port>` / `localhost:<port>` (protection against DNS rebinding), so the proxy has to send that (nginx: `proxy_set_header Host 127.0.0.1:8088;`), or the dashboard has to listen on a non-loopback address that only the proxy can reach. Accounts are always required, so a proxy on the same machine does not open anything up.

## Updating from GitHub

```
sudo trunkvista-update              # shows what is new, asks, installs, restarts the service
sudo trunkvista-update --check      # only look; exit status 10 = an update is available (handy for monitoring)
sudo trunkvista-update --yes        # no question
sudo trunkvista-update --version 1.0.0 --force     # go back to a given release
sudo trunkvista-update --channel prerelease        # release candidates too (or TRUNKVISTA_UPDATE_CHANNEL=prerelease in /etc/default/trunkvista)
```

It reads `TRUNKVISTA_UPDATE_REPO` from `/etc/default/trunkvista` (the release workflow fills in your repository), downloads the `.deb` and `SHA256SUMS` of the **newest full release** (the one GitHub marks *Latest*; pre-releases are skipped unless you choose that channel), checks them, and installs with `apt`. It never installs from the branch, and it does not go back to an older release unless you ask with `--version ... --force`. Your settings in `/etc/default/trunkvista`, your connections, layouts and accounts are kept.

**Automatic updates** are off. To check every night (about 03:30, random delay up to an hour) and install:

```
sudo systemctl enable --now trunkvista-update.timer
```

Look at it with `systemctl list-timers trunkvista-update.timer` and `journalctl -u trunkvista-update`.

**Private repository:** put a GitHub token (fine-grained, read-only access to *Contents* of this repository) in a file, `chmod 600` it, and set `TRUNKVISTA_UPDATE_TOKEN_FILE=/path/to/file` in `/etc/default/trunkvista`. The token is only ever sent to the GitHub API.

### What protects an update

- Every release carries `SHA256SUMS`; the update refuses a `.deb` that does not match it, and a release without `SHA256SUMS`.
- It checks that the file really is `trunkvista` for `all` architectures with the announced version before installing.
- **Optional signature.** If `/etc/trunkvista/release-key.gpg` exists, `SHA256SUMS` must carry a valid GPG signature (`SHA256SUMS.sig`) from that key, otherwise nothing is installed. Set it up once, see [Signing releases](maintainer.md#signing-releases-optional-recommended).
- Without a signature the trust is "whoever can publish a release in the repository". With one it is "whoever holds the signing key". Either way you are installing code that runs as a service on your server: keep the GitHub account protected (2FA) and the repository private if the dashboard shows anything you would rather not publish.

## Upgrading from ChannelView

TrunkVista is the new name of ChannelView (same program, same data). On a Debian / Ubuntu machine that runs the old package:

```
# download trunkvista-latest.deb from the latest release (see Download), then:
sudo apt install ./trunkvista-latest.deb
```

The package of the new name **replaces** `channelview` and takes everything over: layouts, Zabbix connections, accounts, `custom.css`, the HTTPS certificate and key, the settings in `/etc/default` (their names change from `CHANNELVIEW_*` to `TRUNKVISTA_*`) and the nightly update check if you had switched it on. The installation prints what it moved. Everybody has to sign in once more. If you had no accounts yet (ChannelView 1.2.1 or older), it asks for the first administrator as in a fresh installation.

- **This step is manual, once:** `channelview-update` only looks for packages called `channelview`, so it cannot find the new release. After the first `apt install` above, `sudo trunkvista-update` works as usual.
- **Tidy up:** `sudo apt purge channelview` removes the leftover package record; the moved data is not touched.
- **Not moved:** a systemd drop-in in `/etc/systemd/system/channelview.service.d/` (copy it to `trunkvista.service.d/`), and anything of your own that calls `channelview-user`, `channelview-update` or `channelview-cert`, the old service name or the old paths.
- **Without the package** (running `python3 trunkvista.py`): `CHANNELVIEW_*` variables are still read, `~/.channelview` is renamed to `~/.trunkvista` the first time, and the browser's remembered choices (theme, view options) are carried over.
- **GitHub:** when the repository is renamed, GitHub forwards the old addresses, so existing links and `TRUNKVISTA_UPDATE_REPO` settings keep working.

## Where things are

| What | Where |
|---|---|
| Program | `/usr/lib/trunkvista/trunkvista.py` (command `trunkvista`) |
| Network / update setup, updater, certificate helper, account manager | `/usr/bin/trunkvista-setup`, `/usr/bin/trunkvista-update`, `/usr/bin/trunkvista-cert`, `/usr/bin/trunkvista-user` |
| Service, update service and timer | `/lib/systemd/system/trunkvista*.service`, `trunkvista-update.timer` |
| Settings (listen address, port, HTTPS files, update repository / channel) | `/etc/default/trunkvista` – kept on upgrades; `trunkvista-setup` edits it for you |
| Data: connections (Zabbix API token, owner-only), layouts, accounts (password hashes, owner-only), sessions | `/var/lib/trunkvista/` (`connections.json`, `dashboard*.json`, `users.json`, `sessions.json`) |
| The random first password, until it has been changed (root only) | `/var/lib/trunkvista/initial-admin-password` |
| Your style rules (optional) | `/var/lib/trunkvista/custom.css` |
| HTTPS certificate and key (optional) | `/etc/trunkvista/tls/` (`server.crt`, `server.key`) |
| Signing key for updates (optional) | `/etc/trunkvista/release-key.gpg` |

The service runs as its own user `trunkvista`, sandboxed by systemd (no access to home directories, read-only system, only its data folder is writable).

**Backup:** copy `/var/lib/trunkvista` (it holds the API token and the password hashes – treat the copy as a secret).

**Remove:** `sudo apt remove trunkvista` removes the program, keeps settings and data. `sudo apt purge trunkvista` also removes `/etc/default/trunkvista`. Data (including the accounts), the user and `/etc/trunkvista` (certificate, keys) are **kept** even on purge, so a reinstall picks up where you left off; to delete them for good: `sudo rm -rf /var/lib/trunkvista /etc/trunkvista && sudo deluser --system trunkvista`.

## Troubleshooting

| Symptom | Look at |
|---|---|
| Page does not open | `systemctl status trunkvista`, `journalctl -u trunkvista -n 50`; port taken? change `TRUNKVISTA_PORT` |
| Service stops right after enabling HTTPS | `journalctl -u trunkvista -n 20` – "Cannot use the TLS certificate": wrong path, key does not fit, or the key is not readable by the group `trunkvista` |
| Browser: "not secure" / certificate warning | the certificate is self-signed: import `server.crt` on that PC, or use a certificate from your CA / Let's Encrypt; the name in the address must be one of the names `trunkvista-cert` printed |
| Cannot sign in: "Wrong user name or password" | the same message is shown for an unknown name, a wrong password and a disabled account; `sudo trunkvista-user list` shows the accounts, `sudo trunkvista-user passwd NAME` sets a new password |
| "Too many failed attempts" | wait the number of seconds it says (the log tells who and from where: `journalctl -u trunkvista \| grep accounts`) |
| Signed out again and again | the account's automatic sign-out is too short for the use: `sudo trunkvista-user set NAME --idle never` (wall display) or a longer time |
| Page says "No account exists yet" | the installation could not make one: `sudo trunkvista-user add NAME --role admin` |
| "This needs an administrator account" | you are signed in as a viewer |
| A server shows no CPU / memory / disks | the Test button (Connections) says how many server items it found - 0 means no host has items with the keys in [Servers](servers.md); open the host in Zabbix > Latest data and look for `system.cpu.util`, `vm.memory.util...`, `vfs.fs.dependent.size[...,pused]`; items older than 5 minutes (`stale_seconds`) are left out, so also check that the agent is alive |
| Header chip shows ⚠ next to the host count | the request for the server data failed (the network view is not affected); point at the chip for the reason - often the API user's role may not read those items |
| Page shows no data / "unconfigured" | Connections menu (click the status chip): URL, API token, **Test** (the page opens it by itself for an administrator while nothing is connected) |
| `trunkvista-setup`: "does not answer on port ..." | the service did not start with the new settings: `journalctl -u trunkvista -n 30` (a bad certificate path, a port that is taken); undo with `sudo trunkvista-setup --local` |
| `wget` / `install.sh`: "could not download", 404 | no internet access from this machine (a proxy?), or a private repository: download with `gh` (see [Download](../README.md#download)); with no access to GitHub at all, fetch `trunkvista-latest.tar.gz` elsewhere and copy it over |
| `install.sh`: "more than one package next to install.sh" | the folder holds two `trunkvista_*_all.deb`; keep the one you want installed |
| Updater: "no stable release" | the repository has no non-pre-release release yet, or `TRUNKVISTA_UPDATE_REPO` is wrong; private repository → token file |
| Updater: "releases must be signed" | `/etc/trunkvista/release-key.gpg` exists but the release is not signed – sign it, or remove the key file |
| Updater: "API rate limit is used up" | GitHub allows 60 unauthenticated API calls per hour per address; use a token file |
