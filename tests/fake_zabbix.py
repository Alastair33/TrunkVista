"""A tiny stand-in for a Zabbix 7.0 server's JSON-RPC API, for testing the connection commands without a Zabbix.

In a test:        z = FakeZabbix(token="secret-token"); url = z.start(); ...; z.stop()
On the command line (used by tests/e2e_debian.sh and tests/test_setup.sh):
    python3 tests/fake_zabbix.py --port 8766 --token secret-token [--user Admin --password zabbix]
It answers what the Test button of the page asks: apiinfo.version (no login needed), user.login, and host.get / proxy.get / item.get
(which need the right token, as a Bearer header). Every request is remembered in .seen as (method, Authorization header or None)."""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeZabbix:
    def __init__(self, token="secret-token", user="Admin", password="zabbix", port=0, version="7.0.8", interface_items=12, tls=None):
        self.token, self.user, self.password, self.port_wanted = token, user, password, port
        self.tls = tls          # (certificate file, key file): answer over HTTPS
        self.version, self.interface_items = version, interface_items
        self.seen = []
        self.server = None
        self.thread = None

    def start(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0") or 0)) or b"{}")
                method, params = body.get("method"), body.get("params") or {}
                auth = self.headers.get("Authorization")
                outer.seen.append((method, auth))
                out = {"jsonrpc": "2.0", "id": body.get("id")}
                if self.path.split("?")[0] != "/api_jsonrpc.php":
                    self.send_response(404)
                    self.end_headers()
                    return
                if method == "apiinfo.version":
                    out["result"] = outer.version
                elif method == "user.login":
                    if params.get("username") == outer.user and params.get("password") == outer.password:
                        out["result"] = outer.token
                    else:
                        out["error"] = {"code": -32500, "message": "Application error.", "data": "Incorrect user name or password or account is temporarily blocked."}
                elif auth != "Bearer " + outer.token:
                    out["error"] = {"code": -32602, "message": "Invalid params.", "data": "Session terminated, re-login, please."}
                elif method == "host.get" and params.get("countOutput"):
                    out["result"] = "3"
                elif method == "proxy.get":
                    out["result"] = "2"
                elif method == "item.get" and params.get("countOutput"):
                    out["result"] = str(outer.interface_items)
                elif method == "host.get":
                    out["result"] = [{"hostid": str(i), "name": f"core-{i}", "status": "0", "proxyid": "0",
                                      "interfaces": [{"type": "2", "available": "1", "main": "1"}]} for i in (1, 2, 3)]
                else:
                    out["result"] = []
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port_wanted), H)
        if self.tls:
            import ssl
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*self.tls)
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"{'https' if self.tls else 'http'}://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--token", default="secret-token")
    ap.add_argument("--user", default="Admin")
    ap.add_argument("--password", default="zabbix")
    a = ap.parse_args()
    z = FakeZabbix(a.token, a.user, a.password, a.port)
    print(z.start(), flush=True)
    z.thread.join()
