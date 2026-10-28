"""Try the draw.io connector end to end, exactly as an MCP client would use it over HTTP.

    python try_drawio.py

What it does:
  1. starts the connector on a free local port (and stops it when done)
  2. connects over HTTP, lists the tools, and asks for a small example diagram
  3. saves the diagram as  demo.drawio  and the link as  demo-link.html  (both in this folder)
  4. opens the diagram in your default browser (draw.io editor)

Options:
  --no-open     do not open the browser (just write the files)
  --url URL     test an already-running connector instead, e.g. http://localhost:8000/mcp
"""
import argparse
import asyncio
import json
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from html import escape
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

HERE = Path(__file__).resolve().parent

EXAMPLE = {
    "title": "Demo flow",
    "direction": "TB",
    "nodes": [
        {"id": "user", "label": "User", "shape": "actor"},
        {"id": "web", "label": "Web App", "shape": "rounded", "fill": "#dae8fc", "stroke": "#6c8ebf"},
        {"id": "api", "label": "API", "shape": "rounded", "fill": "#d5e8d4", "stroke": "#82b366"},
        {"id": "db", "label": "Database", "shape": "cylinder"},
    ],
    "edges": [
        {"source": "user", "target": "web", "label": "uses"},
        {"source": "web", "target": "api", "label": "HTTPS"},
        {"source": "api", "target": "db", "label": "SQL"},
    ],
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(base: str, timeout: float = 30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(f"{base}/health", timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    return False


async def run(url: str, open_browser: bool) -> int:
    async with Client(StreamableHttpTransport(url)) as c:
        print("Connected. Tools:", ", ".join(t.name for t in await c.list_tools()))
        result = json.loads((await c.call_tool("create_diagram", EXAMPLE)).content[0].text)

    link = result["editor_url"]
    (HERE / "demo.drawio").write_text(result["drawio_file"], encoding="utf-8")
    (HERE / "demo-link.html").write_text(
        f'<!doctype html><meta charset="utf-8"><title>Open diagram</title>'
        f'<p>Click to open the diagram in draw.io: <a href="{escape(link, quote=True)}">open diagram</a></p>',
        encoding="utf-8")
    print(f"\nLink length: {len(link)} characters")
    print(f"Saved: {HERE / 'demo.drawio'}")
    print(f"Saved: {HERE / 'demo-link.html'}")

    if open_browser:
        print("\nOpening the diagram in your browser...")
        webbrowser.open(link)
    print("\nYou should see 4 shapes (User, Web App, API, Database) joined by arrows labelled uses / HTTPS / SQL.")
    print("If the browser shows nothing useful, open  demo-link.html  by double-clicking it, or in draw.io use")
    print("File > Open from > Device and choose  demo.drawio.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-open", action="store_true", help="do not open the browser")
    ap.add_argument("--url", help="use an already-running connector, e.g. http://localhost:8000/mcp")
    args = ap.parse_args()

    proc = None
    url = args.url
    try:
        if not url:
            port = free_port()
            proc = subprocess.Popen([sys.executable, "server.py"], cwd=HERE,
                                    env={**__import__("os").environ, "PORT": str(port), "HOST": "127.0.0.1"},
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not wait_health(f"http://127.0.0.1:{port}"):
                print("The connector did not start. Run  python server.py  to see the error.")
                return 1
            url = f"http://127.0.0.1:{port}/mcp"
        return asyncio.run(run(url, not args.no_open))
    except Exception as exc:
        print(f"\nFAILED: {exc}")
        return 1
    finally:
        if proc:
            proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
