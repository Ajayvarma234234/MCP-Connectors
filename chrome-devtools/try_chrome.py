"""Try the Chrome DevTools connector end to end with ONE command.

    python try_chrome.py

What it does (everything is local; nothing leaves your machine except the optional --site visit):
  1. finds Chrome or Edge, starts it hidden (headless) with a remote-debugging port and a THROWAWAY profile
     (your own Chrome, tabs and logins are not touched)
  2. starts the connector on a free local port
  3. connects to the connector over HTTP, sending the browser's address in the X-Chrome-CDP-Endpoint header
  4. runs a tour: open page, capture console / JS errors / failed requests, screenshot, accessibility snapshot,
     run JavaScript, click and type, performance metrics
  5. saves  demo-screenshot.png  in this folder, then stops everything and deletes the temporary profile

Options:
  --visible        show the browser window while it runs
  --site URL       also visit a real https:// site and report on it (e.g. --site https://example.com)
  --browser PATH   use this browser executable instead of auto-detecting
  --url URL        use an already-running connector (needs --cdp for the browser address)
  --cdp ENDPOINT   use an already-running Chrome, e.g. http://127.0.0.1:9222 (it will not be started or closed)
"""
import argparse
import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

HERE = Path(__file__).resolve().parent

FIXTURE = b"""<!doctype html><html><head><title>Connector Demo Page</title></head><body>
<h1 id="h">Hello from the demo page</h1>
<label>Name <input id="name" aria-label="Name"></label>
<button id="b" onclick="document.getElementById('h').textContent='Button was clicked'">Click me</button>
<script>
  console.log('demo console message');
  fetch('/missing.json');                              // will 404 on purpose
  setTimeout(() => { throw new Error('demo uncaught error') }, 50);
</script></body></html>"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(url: str, timeout: float = 30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.4)
    return False


def find_browser(explicit: str | None) -> str | None:
    if explicit:
        return explicit if Path(explicit).exists() else None
    env = os.environ
    candidates = [
        Path(env.get("ProgramFiles", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(env.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(env.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(env.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(env.get("ProgramFiles", "C:/Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ]
    for c in candidates:
        if str(c) and c.exists():
            return str(c)
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def serve_fixture() -> tuple[HTTPServer, int]:
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
    port = free_port()
    srv = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def step(n: int, text: str):
    print(f"\n[{n}] {text}")


def jd(result):
    return json.loads(result.content[0].text)


async def tour(connector_url: str, cdp: str, page_url: str, site: str | None) -> int:
    problems: list[str] = []

    def expect(cond: bool, msg: str):
        if not cond:
            problems.append(msg)
            print(f"    PROBLEM: {msg}")

    async with Client(StreamableHttpTransport(connector_url, headers={"X-Chrome-CDP-Endpoint": cdp})) as c:
        print("Connected to the connector. Tools:", ", ".join(t.name for t in await c.list_tools()))

        step(1, "Open a new tab and load the demo page, recording everything that happens")
        tab = jd(await c.call_tool("new_page", {}))
        tid = tab["target_id"]
        nav = jd(await c.call_tool("navigate", {"url": page_url, "target_id": tid}))
        print(f"    title: {nav['title']!r}   HTTP status: {nav['status']}")
        print(f"    console messages: {[m['text'] for m in nav['console']]}")
        print(f"    uncaught JS errors: {nav['uncaught_errors']}")
        print(f"    failed/404 requests: {[(e['status'], e['url'].rsplit('/', 1)[-1]) for e in nav['network']['http_errors']]}")
        expect(nav["title"] == "Connector Demo Page", "wrong page title")
        expect(any("demo console message" in m["text"] for m in nav["console"]), "console message not captured")
        expect(any("demo uncaught error" in e for e in nav["uncaught_errors"]), "JS error not captured")
        expect(any(e["status"] == 404 for e in nav["network"]["http_errors"]), "404 request not captured")

        step(2, "Read the page: visible text and accessibility snapshot")
        text = jd(await c.call_tool("get_page_content", {"target_id": tid, "mode": "text"}))["content"]
        aria = jd(await c.call_tool("get_page_content", {"target_id": tid, "mode": "accessibility"}))["content"]
        print(f"    text: {text.strip()[:80]!r}")
        print("    accessibility snapshot:\n      " + aria.strip().replace("\n", "\n      ")[:300])
        expect("Hello from the demo page" in text, "page text not read")

        step(3, "Type into the input, click the button, and read the result with JavaScript")
        await c.call_tool("fill", {"selector": "#name", "value": "Aman", "target_id": tid})
        await c.call_tool("click", {"selector": "#b", "target_id": tid})
        heading = (await c.call_tool("evaluate_script", {"expression": "document.getElementById('h').textContent",
                                                         "target_id": tid})).content[0].text
        typed = (await c.call_tool("evaluate_script", {"expression": "document.getElementById('name').value",
                                                       "target_id": tid})).content[0].text
        print(f"    heading is now: {heading!r}   input value: {typed!r}")
        expect("Button was clicked" in heading, "click did not change the page")
        expect("Aman" in typed, "typing did not reach the input")

        step(4, "Take a screenshot")
        shot = await c.call_tool("screenshot", {"target_id": tid})
        raw = base64.b64decode(shot.content[0].data)
        expect(raw[:4] == b"\x89PNG", "screenshot is not a PNG")
        (HERE / "demo-screenshot.png").write_bytes(raw)
        print(f"    saved {HERE / 'demo-screenshot.png'} ({len(raw) // 1024} KB)")

        step(5, "Performance metrics")
        perf = jd(await c.call_tool("performance_metrics", {"target_id": tid}))
        nav_t = perf["web_vitals"]["navigation"] or {}
        print(f"    load: {nav_t.get('load_ms')} ms   DOM nodes: {perf['cdp_metrics'].get('Nodes')}   "
              f"JS heap: {int(perf['cdp_metrics'].get('JSHeapUsedSize', 0)) // 1024} KB")
        expect(perf["cdp_metrics"].get("Nodes", 0) > 0, "no performance metrics returned")

        if site:
            step(6, f"Visit a real site: {site}")
            real = jd(await c.call_tool("navigate", {"url": site, "target_id": tid, "wait_until": "load"}))
            print(f"    title: {real['title']!r}   status: {real['status']}   requests: {real['network']['requests']}   "
                  f"errors: {len(real['network']['http_errors'])} HTTP, {len(real['uncaught_errors'])} JS")
            expect(real["status"] is not None and real["status"] < 400, f"site answered with {real['status']}")

        step(7, "Close the tab")
        await c.call_tool("close_page", {"target_id": tid})
        left = [p["target_id"] for p in jd(await c.call_tool("list_pages", {}))]
        expect(tid not in left, "tab was not closed")

    print()
    if problems:
        print(f"FAILED: {len(problems)} problem(s) above.")
        return 1
    print("OK: the Chrome DevTools connector works with a real browser.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--visible", action="store_true")
    ap.add_argument("--site")
    ap.add_argument("--browser")
    ap.add_argument("--url")
    ap.add_argument("--cdp")
    ap.add_argument("--no-sandbox", action="store_true", help="Linux containers running as root only")
    args = ap.parse_args()

    if args.site and not args.site.startswith(("http://", "https://")):
        print("--site must start with http:// or https://")
        return 2

    chrome = profile = connector = fixture = None
    try:
        fixture, fport = serve_fixture()
        page_url = f"http://127.0.0.1:{fport}/"

        cdp = args.cdp
        if not cdp:
            exe = find_browser(args.browser)
            if not exe:
                print("Could not find Chrome or Edge. Install one, or pass  --browser \"C:\\path\\to\\chrome.exe\"")
                return 1
            port = free_port()
            profile = tempfile.mkdtemp(prefix="cdp-demo-")
            cmd = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}", "--no-first-run",
                   "--no-default-browser-check", "about:blank"]
            if not args.visible:
                cmd.insert(1, "--headless=new")
            if args.no_sandbox:
                cmd.insert(1, "--no-sandbox")
            print(f"Starting browser: {exe}")
            chrome = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            cdp = f"http://127.0.0.1:{port}"
            if not wait_for(f"{cdp}/json/version", 30):
                print("\nThe browser started but its debugging port never answered.")
                print("Likely cause: your company blocks remote debugging in the browser (a policy called")
                print("RemoteDebuggingAllowed), or security software is blocking the local port.")
                print("Try  --browser \"<path to msedge.exe>\"  to use Edge instead, or ask IT.")
                return 1
            print(f"Browser is listening for DevTools at {cdp}")

        url = args.url
        if not url:
            cport = free_port()
            connector = subprocess.Popen([sys.executable, "server.py"], cwd=HERE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL,
                                         env={**os.environ, "PORT": str(cport), "HOST": "127.0.0.1"})
            if not wait_for(f"http://127.0.0.1:{cport}/health", 30):
                print("The connector did not start. Run  python server.py  to see the error.")
                return 1
            url = f"http://127.0.0.1:{cport}/mcp"
        return asyncio.run(tour(url, cdp, page_url, args.site))
    except Exception as exc:
        print(f"\nFAILED: {exc}")
        return 1
    finally:
        for p in (connector, chrome):
            if p:
                p.terminate()
        if chrome:
            try:
                chrome.wait(timeout=10)
            except Exception:
                chrome.kill()
        if fixture:
            fixture.shutdown()
        if profile:
            time.sleep(1)  # Windows keeps the profile folder locked for a moment
            shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
