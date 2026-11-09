# Maintainer guide

[← README](../README.md)

## Setting up GitHub and the first release (maintainer)

One-time setup:

```
cd TrunkVista
git init -b main
git add -A && git commit -m "Initial import"
git remote add origin git@github.com:<you>/<repository>.git
git push -u origin main
```

The first release (the notes are already written under `## [1.1.0]`; later ones: *Patching and releasing* below):

```
packaging/release.sh --push 1.1.0
```

What happens after the push: GitHub Actions (`.github/workflows/release.yml`) runs the tests, builds the `.deb`, **installs / upgrades / updates / removes / purges it on a clean runner**, and publishes the release with `trunkvista_1.1.0_all.deb`, the same package again as `trunkvista-latest.deb`, `trunkvista.py`, `trunkvista_demo.html` and `SHA256SUMS` (and `SHA256SUMS.sig`). The fixed names are what makes `releases/latest/download/<name>` always serve the newest release (see [Download](../README.md#download)); `trunkvista-update` ignores `trunkvista-latest.deb` because it looks for `trunkvista_<version>_all.deb`. If anything fails nothing is published – fix it, delete the tag (`git push --delete origin v1.1.0; git tag -d v1.1.0`) and release again. Servers get it with `sudo trunkvista-update`.

A tag with a dash (`v1.2.0-rc.1`) is a **pre-release**: only servers on the `prerelease` channel see it. Inside the package its version is `1.2.0~rc.1`, which sorts before `1.2.0`. The file is called `trunkvista_1.2.0-rc.1_all.deb` because GitHub renames a `~` in file names.

The workflow refuses a tag that does not match `VERSION`.

`.github/workflows/ci.yml` runs the tests, shellcheck, a demo-page check and the install test on every push and pull request.

### Signing releases (optional, recommended)

```
gpg --quick-generate-key "TrunkVista releases" ed25519 sign never      # choose a passphrase
gpg --armor --export-secret-keys "TrunkVista releases"                 # -> GitHub: Settings > Secrets and variables > Actions > RELEASE_GPG_PRIVATE_KEY
                                                                             #    (the passphrase goes into the secret RELEASE_GPG_PASSPHRASE)
gpg --export "TrunkVista releases" > release-key.gpg                   # the public key, for the servers:
sudo apt install gpgv
sudo install -D -m 0644 release-key.gpg /etc/trunkvista/release-key.gpg
```

From the next release on the workflow signs `SHA256SUMS`. From the moment the key file exists on a server, that server installs **only** signed releases – do the first signed release before you copy the key to the servers.

## Patching and releasing (maintainer)

`VERSION` in `trunkvista.py` is the only place a version lives; `packaging/release.sh` sets it.

| Command | Use it for | Example |
|---|---|---|
| `packaging/release.sh patch --push` | a fix or a small design change | 1.1.0 → 1.1.1 |
| `packaging/release.sh minor --push` | a new feature | 1.1.0 → 1.2.0 |
| `packaging/release.sh major --push` | something that changes how it is used | 1.1.0 → 2.0.0 |
| `packaging/release.sh 1.1.1-rc.1 --push` | a pre-release for trying it first | – |

It needs notes under `## [Unreleased]` in `CHANGELOG.md` (they become the release notes on GitHub; if the README has a `<!-- whats-new:start -->` / `<!-- whats-new:end -->` block, `packaging/whats_new.py` fills it with the latest versions from the CHANGELOG - this README has none), runs the unit tests (a failure puts everything back), commits, tags, and with `--push` pushes – GitHub Actions does the rest. Without `--push` it stops before publishing and tells you how to undo.

**Try a patch on one server first:** release `1.1.1-rc.1`, then on a test server `sudo trunkvista-update --channel prerelease`. If it is good, release the final version with `packaging/release.sh 1.1.1 --push` (the notes are still under `[Unreleased]`); servers on the normal channel only ever see that one.

**Back out a bad release:** on each server `sudo trunkvista-update --version 1.1.0 --force`, then fix and release again. Do not delete a release servers have installed.

## Development

Clone the repository only to work on the program. To *use* TrunkVista take the [latest release](../README.md#download): the branch you clone can be ahead of it.

```
python3 trunkvista.py --demo                       # run from a checkout
python3 -m unittest discover -s tests -v              # unit tests: app, server data, accounts and sign-in, HTTPS (needs openssl), updater against a fake GitHub, release script (needs git); a few compare the standalone page's JavaScript with the server (need node)
python3 tools/build_demo.py trunkvista_demo.html   # rebuild the standalone demo page after changing the dashboard page
packaging/build_deb.sh                                # -> dist/trunkvista_<version>_all.deb + SHA256SUMS (needs dpkg-deb)
sudo tests/e2e_debian.sh                              # really installs / upgrades / purges the package - only on a throw-away machine
```

`tests/e2e_debian.sh` also tests the move from the old name: it builds `channelview` packages from the git history (the last release, and the commit before the rename, `589d2cb`), so run it in a clone with the full history (`fetch-depth: 0` in the workflows), or give it a package of the old name with `E2E_LEGACY_DEB=path/channelview_X_all.deb`; without either that part is skipped.
