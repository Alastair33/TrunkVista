"""A tiny stand-in for the GitHub releases API, for testing channelview-update without the internet.

In a test:        gh = MockGitHub("owner/name", token=None); base = gh.start(); gh.add_release("v1.0.1", {"channelview_1.0.1_all.deb": b"..."}); ...; gh.stop()
On the command line (used by tests/e2e_debian.sh):
    python3 tests/mock_github.py --repo owner/name --port 8765 --release v1.0.1:dist/channelview_1.0.1_all.deb,dist/SHA256SUMS
It answers the three calls the updater makes (releases/latest, releases?per_page, releases/tags/<tag>), serves the files through a redirect to a
second host name (like github.com -> objects.githubusercontent.com), renames asset files the way GitHub does (a ~ becomes a .), and remembers
which requests carried an Authorization header."""
import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class MockGitHub:
    def __init__(self, repo="owner/name", token=None, port=0):
        self.repo, self.token, self.port_wanted = repo, token, port
        self.releases = []          # oldest first, like a repository's history
        self.seen = []              # (kind, path, Authorization header or None)
        self.rate_limited = False
        self.server = None

    # ---- content
    @staticmethod
    def github_name(name):
        """GitHub renames special characters in the file names of release assets ('~', spaces, ':' ... become '.')."""
        return re.sub(r"[^A-Za-z0-9_.+-]", ".", name).strip(".")

    def add_release(self, tag, assets, prerelease=False, draft=False, body="Release notes."):
        self.releases.append({"tag": tag, "assets": {self.github_name(n): b for n, b in assets.items()}, "prerelease": prerelease, "draft": draft, "body": body, "n": len(self.releases) + 1})

    def _asset_id(self, rel, name):
        return rel["n"] * 100 + sorted(rel["assets"]).index(name)

    def _json_release(self, rel):
        base = self.base
        return {"tag_name": rel["tag"], "name": rel["tag"], "body": rel["body"], "draft": rel["draft"], "prerelease": rel["prerelease"],
                "assets": [{"name": n, "size": len(b), "id": self._asset_id(rel, n),
                            "url": f"{base}/repos/{self.repo}/releases/assets/{self._asset_id(rel, n)}",
                            "browser_download_url": f"{base}/download/{rel['tag']}/{n}"} for n, b in rel["assets"].items()]}

    def _find_asset(self, aid):
        for rel in self.releases:
            for n, b in rel["assets"].items():
                if self._asset_id(rel, n) == aid:
                    return b
        return None

    # ---- server
    def start(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="application/json", headers=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self):
                return outer.token is None or self.headers.get("Authorization") == "Bearer " + outer.token

            def do_GET(self):
                path = self.path
                outer.seen.append(("GET", path, self.headers.get("Authorization")))
                if outer.rate_limited and path.startswith("/repos/"):
                    return self._send(403, b'{"message":"API rate limit exceeded"}', headers={"X-RateLimit-Remaining": "0"})
                prefix = f"/repos/{outer.repo}/"
                if path.startswith("/repos/"):
                    if not path.startswith(prefix) or not self._authorized():
                        return self._send(404, b'{"message":"Not Found"}')
                    rest = path[len(prefix):]
                    live = [r for r in outer.releases if not r["draft"]]
                    if rest == "releases/latest":
                        stable = [r for r in live if not r["prerelease"]]
                        return self._send(200, json.dumps(outer._json_release(stable[-1])).encode()) if stable else self._send(404, b'{"message":"Not Found"}')
                    if rest.startswith("releases?"):
                        return self._send(200, json.dumps([outer._json_release(r) for r in reversed(live)]).encode())
                    m = re.match(r"^releases/tags/(.+)$", rest)
                    if m:
                        hit = [r for r in live if r["tag"] == m.group(1)]
                        return self._send(200, json.dumps(outer._json_release(hit[0])).encode()) if hit else self._send(404, b'{"message":"Not Found"}')
                    m = re.match(r"^releases/assets/(\d+)$", rest)
                    if m:
                        if self.headers.get("Accept") != "application/octet-stream":
                            return self._send(200, b"{}")
                        return self._send(302, headers={"Location": f"http://localhost:{outer.port}/cdn/{m.group(1)}"})
                    return self._send(404, b'{"message":"Not Found"}')
                m = re.match(r"^/download/([^/]+)/([^/]+)$", path)
                if m:                                                  # the public download address redirects to a CDN host name
                    for rel in outer.releases:
                        if rel["tag"] == m.group(1) and m.group(2) in rel["assets"]:
                            return self._send(302, headers={"Location": f"http://localhost:{outer.port}/cdn/{outer._asset_id(rel, m.group(2))}"})
                    return self._send(404)
                m = re.match(r"^/cdn/(\d+)$", path)
                if m:
                    data = outer._find_asset(int(m.group(1)))
                    outer.seen.append(("CDN", path, self.headers.get("Authorization")))
                    return self._send(200, data, "application/octet-stream") if data is not None else self._send(404)
                return self._send(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port_wanted), H)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.base

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="owner/name")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--token", default=None, help="require this token (like a private repository)")
    ap.add_argument("--release", action="append", default=[], metavar="TAG:FILE,FILE", help="a release and the files it carries (repeatable, oldest first)")
    ap.add_argument("--prerelease", action="append", default=[], metavar="TAG", help="tags that are marked as pre-releases")
    a = ap.parse_args()
    gh = MockGitHub(a.repo, a.token, a.port)
    for spec in a.release:
        tag, files = spec.split(":", 1)
        gh.add_release(tag, {Path(f).name: Path(f).read_bytes() for f in files.split(",")}, prerelease=tag in a.prerelease, body=f"Notes for {tag}.\nSecond line.")
    print(gh.start(), flush=True)
    while True:
        time.sleep(3600)
