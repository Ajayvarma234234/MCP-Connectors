"""REAL-BROWSER test for the Chrome DevTools connector. Needs a Chrome you start yourself:

    chrome --headless=new --remote-debugging-port=9222 --user-data-dir=/tmp/cdp-test about:blank
    CHROME_CDP_ENDPOINT=http://127.0.0.1:9222 python tests/test_chrome_real.py

(On macOS the binary is /Applications/Google Chrome.app/Contents/MacOS/Google Chrome.)
The connector connects to that Chrome, loads a local fixture page and exercises every tool.
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
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

ROOT = Path(__file__).resolve().parent.parent
ENDPOINT = os.environ.get("CHROME_CDP_ENDPOINT")
PAGE_PORT, SRV_PORT = 9501, 9502
FIXTURE = b"""<!doctype html><html><head><title>Fixture Page</title></head><body>
<h1 id="h">Hello</h1><input id="name" aria-label="Name"><button id="b" onclick="document.getElementById('h').textContent='Clicked'">Go</button>
<script>console.log('fixture-log'); fetch('/missing.json'); setTimeout(() => { throw new Error('boom') }, 50)</script>
</body></html>"""
failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def serve_fixture():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(FIXTURE)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass
    threading.Thread(target=HTTPServer(("127.0.0.1", PAGE_PORT), H).serve_forever, daemon=True).start()


def wait_http(url, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    return False


async def main():
    url = f"http://127.0.0.1:{PAGE_PORT}/"
    async with Client(StreamableHttpTransport(f"http://127.0.0.1:{SRV_PORT}/mcp",
                                              headers={"X-Chrome-CDP-Endpoint": ENDPOINT})) as c:
        async def call(tool, args=None):
            return json.loads((await c.call_tool(tool, args or {})).content[0].text)

        page = await call("new_page", {})
        tid = page["target_id"]
        check(tid in [p["target_id"] for p in await call("list_pages")], "new_page not listed")

        nav = await call("navigate", {"url": url, "target_id": tid, "wait_until": "load"})
        check(nav["title"] == "Fixture Page" and nav["status"] == 200, f"navigate: {nav['title']} {nav['status']}")
        check(any("fixture-log" in m["text"] for m in nav["console"]), f"console not captured: {nav['console']}")
        check(any("boom" in e for e in nav["uncaught_errors"]), f"uncaught error not captured: {nav['uncaught_errors']}")
        check(any(e["status"] == 404 and "missing.json" in e["url"] for e in nav["network"]["http_errors"]),
              f"404 not captured: {nav['network']}")

        text = await call("get_page_content", {"target_id": tid, "mode": "text"})
        check("Hello" in text["content"], "text content")
        html = await call("get_page_content", {"target_id": tid, "mode": "html"})
        check("<h1" in html["content"], "html content")
        aria = await call("get_page_content", {"target_id": tid, "mode": "accessibility"})
        check("button" in aria["content"].lower(), f"accessibility snapshot: {aria['content'][:120]}")

        await call("fill", {"selector": "#name", "value": "Aman", "target_id": tid})
        r = await c.call_tool("evaluate_script", {"expression": "document.getElementById('name').value", "target_id": tid})
        check("Aman" in r.content[0].text, f"fill/evaluate: {r.content[0].text}")

        await call("click", {"selector": "#b", "target_id": tid})
        r = await c.call_tool("evaluate_script", {"expression": "() => document.getElementById('h').textContent",
                                                  "target_id": tid})
        check("Clicked" in r.content[0].text, f"click: {r.content[0].text}")

        shot = await c.call_tool("screenshot", {"target_id": tid})
        check(shot.content[0].type == "image" and base64.b64decode(shot.content[0].data)[:4] == b"\x89PNG",
              "screenshot is not a PNG")
        shot = await c.call_tool("screenshot", {"target_id": tid, "selector": "#h", "image_format": "jpeg"})
        check(shot.content[0].type == "image", "element screenshot")

        perf = await call("performance_metrics", {"target_id": tid})
        check(perf["web_vitals"]["navigation"] is not None and perf["cdp_metrics"].get("Nodes", 0) > 0,
              f"performance metrics: {perf}")

        act = await call("capture_activity", {"target_id": tid, "reload": True, "duration_ms": 500})
        check(any("fixture-log" in m["text"] for m in act["console"]), "capture_activity console")

        await call("close_page", {"target_id": tid})
        check(tid not in [p["target_id"] for p in await call("list_pages")], "close_page")


if __name__ == "__main__":
    if not ENDPOINT:
        print("SKIPPED: set CHROME_CDP_ENDPOINT to a Chrome started with --remote-debugging-port (see docstring).")
        sys.exit(0)
    serve_fixture()
    srv = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, stdout=subprocess.DEVNULL,
                           env={**os.environ, "PORT": str(SRV_PORT), "HOST": "127.0.0.1"})
    try:
        assert wait_http(f"http://127.0.0.1:{SRV_PORT}/health"), "connector did not start"
        asyncio.run(main())
    finally:
        srv.terminate()
    print("chrome-devtools (real browser): " + ("PASS" if not failures else "FAIL\n  " + "\n  ".join(failures)))
    sys.exit(1 if failures else 0)
