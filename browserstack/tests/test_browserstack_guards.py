"""BrowserStack connector: everything that does not need a real BrowserStack account.

A fake BrowserStack API runs locally and the connector is pointed at it. Covers: missing credentials, id/path
validation (rejected before any upstream call), correct HTTP Basic auth per caller, two users interleaved under
concurrency never mixed up, upstream 401 handled without leaking the key, unreachable upstream handled without
leaking the key, limit clamping, status validation, the PUT body for set_session_status, and result filtering/caps.
"""
import asyncio
import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

ROOT = Path(__file__).resolve().parent.parent
SERVER_PORT, DEAD_PORT = 9501, 9502
FAKE_AUTOMATE, FAKE_APP = 9511, 9512
SECRET = "SECRETKEY123"

log: list[dict] = []   # every request the fake BrowserStack received
failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def fake_browserstack(port, product):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, payload, raw=None):
            body = raw if raw is not None else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json" if raw is None else "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _user(self):
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                return None, None
            user, _, key = base64.b64decode(auth[6:]).decode().partition(":")
            return user, key

        def _handle(self, method):
            parts = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            user, key = self._user()
            log.append({"product": product, "method": method, "path": parts.path, "query": parse_qs(parts.query),
                        "user": user, "key": key, "body": body})
            if user == "baduser":
                return self._send(401, {"message": "Unauthorized"})
            p = parts.path
            if p.endswith("/plan.json"):
                return self._send(200, {"automate_plan": f"plan-for-{user}", "parallel_sessions_max_allowed": 5})
            if p.endswith("/browsers.json"):
                rows = [{"os": "Windows" if i % 2 else "OS X", "os_version": "11", "browser": "chrome" if i % 3 else "firefox",
                         "device": None, "browser_version": str(i), "real_mobile": None} for i in range(600)]
                return self._send(200, rows)
            if p.endswith("/devices.json"):
                return self._send(200, [{"device": "Galaxy S23", "os": "android", "os_version": "13.0", "realMobile": True},
                                        {"device": "iPhone 15", "os": "ios", "os_version": "17", "realMobile": True}])
            if p.endswith("/builds.json"):
                return self._send(200, [{"automation_build": {"hashed_id": "b1", "name": "nightly", "status": "done",
                                                              "duration": 12, "secret_internal": "x"}}])
            if p.endswith("/sessions.json") and "/builds/" in p:
                return self._send(200, [{"automation_session": {"hashed_id": "s1", "name": "login", "status": "failed",
                                                               "browser": "chrome", "os": "Windows"}}])
            if p.endswith("/logs"):
                return self._send(200, None, raw=("line\n" * 5000).encode())
            if "/sessions/" in p and p.endswith(".json"):
                if method == "PUT":
                    return self._send(200, {"automation_session": {"hashed_id": "s1", "status": body.get("status")}})
                return self._send(200, {"automation_session": {"hashed_id": "s1", "status": "failed",
                                                              "video_url": "https://v", "internal_note": "x"}})
            return self._send(404, {"message": "nope"})

        def do_GET(self):
            self._handle("GET")

        def do_PUT(self):
            self._handle("PUT")

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
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


def client(port, user="alice", key="alicekey", extra=None):
    headers = {}
    if user is not None:
        headers["X-BrowserStack-Username"] = user
    if key is not None:
        headers["X-BrowserStack-Access-Key"] = key
    headers.update(extra or {})
    return Client(StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp", headers=headers))


async def call(port, tool, args=None, **kw):
    """Returns (data, error_text)."""
    try:
        async with client(port, **kw) as c:
            res = await c.call_tool(tool, args or {})
        return res.data, None
    except Exception as exc:
        return None, str(exc)


async def main():
    p = SERVER_PORT

    # --- credentials
    _, e = await call(p, "automate_plan", user=None)
    check(e and "Missing credentials" in e, f"missing username: {e}")
    _, e = await call(p, "automate_plan", key=None)
    check(e and "Missing credentials" in e, f"missing access key: {e}")

    # --- happy path + correct Basic auth reaches upstream
    log.clear()
    d, e = await call(p, "automate_plan", user="alice", key="alicekey")
    check(e is None and d and d.get("automate_plan") == "plan-for-alice", f"plan: {d} {e}")
    check(log and log[-1]["user"] == "alice" and log[-1]["key"] == "alicekey", f"basic auth: {log[-1:]}")

    # --- app automate routes to the App Automate host and path
    log.clear()
    d, e = await call(p, "app_automate_plan")
    check(e is None and log and log[-1]["product"] == "app" and log[-1]["path"] == "/app-automate/plan.json",
          f"app plan routing: {log[-1:]} {e}")

    # --- id validation happens before any upstream call
    for bad in ("../../etc/passwd", "a/b", "abc?x=1", "a b", "", "x" * 200, "%2e%2e"):
        log.clear()
        _, e = await call(p, "automate_get_session", {"session_id": bad})
        check(e and "Invalid session_id" in e and not log, f"bad session id {bad!r} should be refused: {e} {log}")
    log.clear()
    _, e = await call(p, "automate_list_sessions", {"build_id": "../x"})
    check(e and "Invalid build_id" in e and not log, f"bad build id: {e}")

    # --- limit clamping and offset floor
    log.clear()
    await call(p, "automate_list_builds", {"limit": 100000, "offset": -5})
    q = log[-1]["query"] if log else {}
    check(q.get("limit") == ["100"] and q.get("offset") == ["0"], f"clamp: {q}")

    # --- result shaping: only known keys survive, list items unwrapped
    d, e = await call(p, "automate_list_builds")
    b = (d or {}).get("builds", [{}])[0] if d else {}
    check(b.get("hashed_id") == "b1" and "secret_internal" not in b, f"build shaping: {d} {e}")
    d, e = await call(p, "automate_get_session", {"session_id": "s1"})
    check(d and d.get("video_url") == "https://v" and "internal_note" not in d, f"session shaping: {d} {e}")

    # --- browsers: filtered and capped
    d, e = await call(p, "automate_browsers", {"os": "windows", "browser": "chrome"})
    check(d and d["matched"] > 0 and all(i["os"] == "Windows" and i["browser"] == "chrome" for i in d["items"]),
          f"browser filter: {e}")
    d, e = await call(p, "automate_browsers")
    check(d and d["returned"] == 200 and d["truncated"] is True, f"browser cap: {d and (d['returned'], d['truncated'])} {e}")

    # --- logs: truncated
    d, e = await call(p, "automate_get_session_log", {"session_id": "s1", "max_chars": 700})
    check(d and d["truncated"] is True and len(d["log"]) == 700 and d["total_chars"] == 25000, f"log truncation: {e}")
    _, e = await call(p, "automate_get_session_log", {"session_id": "s1", "kind": "../x"})
    check(e is not None, "log kind must be validated")

    # --- set_session_status: validation + exact PUT body
    log.clear()
    _, e = await call(p, "automate_set_session_status", {"session_id": "s1", "status": "deleted"})
    check(e is not None and not [r for r in log if r["method"] == "PUT"], f"bad status must not reach upstream: {e}")
    d, e = await call(p, "automate_set_session_status", {"session_id": "s1", "status": "passed", "reason": "ok " * 200})
    puts = [r for r in log if r["method"] == "PUT"]
    check(e is None and puts and puts[-1]["body"]["status"] == "passed" and len(puts[-1]["body"]["reason"]) == 255,
          f"PUT body: {puts[-1:] if puts else None} {e}")

    # --- upstream 401: friendly error, key never echoed
    _, e = await call(p, "automate_plan", user="baduser", key=SECRET)
    check(e and "rejected" in e and SECRET not in e, f"401 handling / key leak: {e}")

    # --- two users interleaved: every caller gets THEIR OWN account's answer
    async def who(i):
        u = "alice" if i % 2 == 0 else "bob"
        d, e = await call(p, "automate_plan", user=u, key=f"{u}key")
        return u, (d or {}).get("automate_plan"), e
    results = await asyncio.gather(*[who(i) for i in range(30)])
    check(all(got == f"plan-for-{u}" for u, got, _ in results),
          f"user mix-up: {[(u, g, e) for u, g, e in results if g != f'plan-for-{u}'][:3]}")

    # --- unreachable upstream: clear error, key never echoed
    _, e = await call(DEAD_PORT, "automate_plan", user="alice", key=SECRET)
    check(e and "Could not reach BrowserStack" in e and SECRET not in e, f"unreachable upstream: {e}")


if __name__ == "__main__":
    fake_browserstack(FAKE_AUTOMATE, "automate")
    fake_browserstack(FAKE_APP, "app")
    procs = []
    try:
        for port, auto, app in ((SERVER_PORT, FAKE_AUTOMATE, FAKE_APP), (DEAD_PORT, 1, 1)):
            env = {**os.environ, "PORT": str(port), "HOST": "127.0.0.1",
                   "BROWSERSTACK_AUTOMATE_BASE": f"http://127.0.0.1:{auto}",
                   "BROWSERSTACK_APP_BASE": f"http://127.0.0.1:{app}"}
            procs.append(subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=env,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for port in (SERVER_PORT, DEAD_PORT):
            assert wait_http(f"http://127.0.0.1:{port}/health"), f"server on {port} did not start"
        asyncio.run(main())
    finally:
        for pr in procs:
            pr.terminate()
    print("browserstack (guards): " + ("PASS" if not failures else "FAIL\n  " + "\n  ".join(failures)))
    sys.exit(1 if failures else 0)
