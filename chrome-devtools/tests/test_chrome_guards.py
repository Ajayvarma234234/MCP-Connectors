"""Chrome DevTools connector: everything that does not need a live browser.

Covers: missing header, scheme/host validation, SSRF guards (link-local always refused, allowlist,
CDP_BLOCK_PRIVATE), endpoint secrets never appear in errors, navigate() URL restriction, and that two users'
X-Chrome-CDP-Endpoint headers are routed to THEIR OWN browser under concurrency (using two fake CDP hosts).
"""
import asyncio
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

ROOT = Path(__file__).resolve().parent.parent
PORTS = {"default": 9401, "allowlist": 9402, "blockprivate": 9403}
FAKE_A, FAKE_B = 9411, 9412
hits = {FAKE_A: 0, FAKE_B: 0}
failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def fake_cdp(port):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            hits[port] += 1
            body = b'{"Browser":"fake","webSocketDebuggerUrl":"ws://127.0.0.1:1/devtools/browser/x"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def wait_http(url, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def client(port, endpoint=None):
    headers = {"X-Chrome-CDP-Endpoint": endpoint} if endpoint else {}
    return Client(StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp", headers=headers))


async def err_of(port, endpoint, tool="list_pages", args=None):
    try:
        async with client(port, endpoint) as c:
            await c.call_tool(tool, args or {})
        return None
    except Exception as exc:
        return str(exc)


async def main():
    d, a, b = PORTS["default"], PORTS["allowlist"], PORTS["blockprivate"]

    e = await err_of(d, None)
    check(e and "Missing credentials" in e, f"missing header: {e}")

    e = await err_of(d, "ftp://127.0.0.1:9222")
    check(e and "http(s):// or ws(s)://" in e, f"bad scheme: {e}")

    e = await err_of(d, "http://169.254.169.254/latest/meta-data")
    check(e and "refuses" in e, f"metadata IP must be refused: {e}")

    e = await err_of(d, "http://[fe80::1]:9222")
    check(e and ("refuses" in e or "could not be resolved" in e), f"ipv6 link-local must not connect: {e}")

    e = await err_of(d, "http://nonexistent-host.invalid:9222")
    check(e and "could not be resolved" in e, f"unresolvable host: {e}")

    e = await err_of(d, "http://127.0.0.1:1/?token=SECRET123")
    check(e and "Could not connect" in e, f"unreachable endpoint: {e}")
    check(e and "SECRET123" not in e, f"ENDPOINT TOKEN LEAKED IN ERROR: {e}")

    e = await err_of(d, "http://127.0.0.1:1", "navigate", {"url": "file:///etc/passwd"})
    check(e and "Only http" in e, f"file:// must be refused before any connection: {e}")
    e = await err_of(d, "http://127.0.0.1:1", "new_page", {"url": "javascript:alert(1)"})
    check(e and "Only http" in e, f"javascript: must be refused: {e}")

    e = await err_of(a, "http://127.0.0.1:1/?token=SECRET123")
    check(e and "allowlist" in e and "SECRET123" not in e, f"allowlist: {e}")

    e = await err_of(b, "http://127.0.0.1:1/?token=SECRET123")
    check(e and "refuses" in e and "SECRET123" not in e, f"CDP_BLOCK_PRIVATE: {e}")

    # two users, two browsers, interleaved: each call must reach its caller's own endpoint
    before = dict(hits)
    await asyncio.gather(*[err_of(d, f"http://127.0.0.1:{FAKE_A if i % 2 == 0 else FAKE_B}") for i in range(20)])
    da, db = hits[FAKE_A] - before[FAKE_A], hits[FAKE_B] - before[FAKE_B]
    check(da > 0 and db > 0, f"routing: each fake browser should be contacted (A={da}, B={db})")
    check(da == db, f"routing: symmetric workload must hit both browsers equally (A={da}, B={db})")


if __name__ == "__main__":
    fake_cdp(FAKE_A)
    fake_cdp(FAKE_B)
    procs = []
    try:
        for name, port in PORTS.items():
            env = {**os.environ, "PORT": str(port), "HOST": "127.0.0.1"}
            if name == "allowlist":
                env["CDP_ALLOWED_HOSTS"] = "example.com,*.corp.example"
            if name == "blockprivate":
                env["CDP_BLOCK_PRIVATE"] = "true"
            procs.append(subprocess.Popen([sys.executable, "server.py"], cwd=ROOT,
                                          env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for port in PORTS.values():
            assert wait_http(f"http://127.0.0.1:{port}/health"), f"server on {port} did not start"
        asyncio.run(main())
    finally:
        for p in procs:
            p.terminate()
    print("chrome-devtools (guards): " + ("PASS" if not failures else "FAIL\n  " + "\n  ".join(failures)))
    sys.exit(1 if failures else 0)
