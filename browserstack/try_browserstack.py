"""One command: start the connector and run a read-only tour against YOUR BrowserStack account.

    python try_browserstack.py

Credentials come from the environment (BROWSERSTACK_USERNAME, BROWSERSTACK_ACCESS_KEY) or, if not set, you are
asked for them (the access key is typed hidden). They are only sent to the local connector and to BrowserStack.

Options
    --url http://localhost:8000/mcp   test an already-running server instead of starting one
    --mark SESSION_ID passed|failed   ALSO change that session's status (the only write this script can do)
"""
import argparse
import asyncio
import getpass
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

ROOT = Path(__file__).resolve().parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: int = 30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def get_credentials() -> tuple[str, str]:
    user = os.getenv("BROWSERSTACK_USERNAME") or input("BrowserStack username: ").strip()
    key = os.getenv("BROWSERSTACK_ACCESS_KEY") or getpass.getpass("BrowserStack access key (hidden): ").strip()
    if not user or not key:
        sys.exit("Username and access key are both required (BrowserStack dashboard -> Account -> Summary).")
    return user, key


async def tour(url: str, user: str, key: str, mark: tuple[str, str] | None) -> int:
    headers = {"X-BrowserStack-Username": user, "X-BrowserStack-Access-Key": key}
    problems = 0
    async with Client(StreamableHttpTransport(url, headers=headers)) as c:
        tools = [t.name for t in await c.list_tools()]
        print(f"Connected to the connector. {len(tools)} tools: {', '.join(tools)}\n")

        async def call(name, args=None):
            return (await c.call_tool(name, args or {})).data

        # [1] credentials + Automate plan
        print("[1] Automate plan (also proves your username/access key work)")
        try:
            plan = await call("automate_plan")
            print(f"    plan: {plan.get('automate_plan')}   parallel allowed: {plan.get('parallel_sessions_max_allowed')}"
                  f"   running: {plan.get('parallel_sessions_running')}   queued: {plan.get('queued_sessions')}")
        except Exception as exc:
            print(f"    FAILED: {exc}")
            return 1

        # [2] browsers
        print("\n[2] Supported browsers (filter: Windows + chrome)")
        try:
            b = await call("automate_browsers", {"os": "Windows", "browser": "chrome"})
            sample = [f"{i['os']} {i['os_version']} / chrome {i.get('browser_version')}" for i in b["items"][:3]]
            print(f"    {b['matched']} combinations match. First few: {sample}")
        except Exception as exc:
            print(f"    FAILED: {exc}")
            problems += 1

        # [3] builds -> [4] sessions -> session -> log
        print("\n[3] Recent Automate builds")
        build_id = None
        try:
            builds = (await call("automate_list_builds", {"limit": 5}))["builds"]
            if not builds:
                print("    none yet. That is normal on a new trial: run `python sample_session.py` once, then re-run this.")
            for bd in builds:
                print(f"    {bd.get('hashed_id')}  {bd.get('name')!r}  status={bd.get('status')}  duration={bd.get('duration')}s")
            build_id = builds[0]["hashed_id"] if builds else None
        except Exception as exc:
            print(f"    FAILED: {exc}")
            problems += 1

        session_id = None
        if build_id:
            print("\n[4] Sessions in the newest build, one session's details, and the start of its log")
            try:
                sessions = (await call("automate_list_sessions", {"build_id": build_id, "limit": 5}))["sessions"]
                for s in sessions:
                    print(f"    {s.get('hashed_id')}  {s.get('name')!r}  {s.get('status')}  "
                          f"{s.get('browser')} on {s.get('os')} {s.get('os_version')}")
                if sessions:
                    session_id = sessions[0]["hashed_id"]
                    d = await call("automate_get_session", {"session_id": session_id})
                    print(f"    details: status={d.get('status')} reason={d.get('reason')!r} dashboard={d.get('public_url') or d.get('browser_url')}")
                    lg = await call("automate_get_session_log", {"session_id": session_id, "max_chars": 500})
                    first = [ln for ln in lg["log"].splitlines() if ln.strip()][:3]
                    print(f"    log ({lg['total_chars']} chars, first lines): {first}")
            except Exception as exc:
                print(f"    FAILED: {exc}")
                problems += 1
        else:
            print("\n[4] skipped (no build to look inside)")

        # [5] App Automate (needs its own plan; on a trial it may not be enabled)
        print("\n[5] App Automate plan and Android devices")
        try:
            ap = await call("app_automate_plan")
            print(f"    plan: {ap.get('automate_plan')}   parallel allowed: {ap.get('parallel_sessions_max_allowed')}")
            dv = await call("app_automate_devices", {"os": "android"})
            print(f"    {dv['matched']} Android devices. First few: "
                  f"{[i['device'] + ' ' + str(i['os_version']) for i in dv['items'][:3]]}")
        except Exception as exc:
            print(f"    skipped: App Automate is not available for this account ({str(exc)[:120]})")

        # [6] optional write
        if mark:
            print(f"\n[6] Mark session {mark[0]} as {mark[1]} (--mark)")
            try:
                r = await call("automate_set_session_status", {"session_id": mark[0], "status": mark[1],
                                                               "reason": "set by try_browserstack.py"})
                print(f"    now: {r.get('status')}")
            except Exception as exc:
                print(f"    FAILED: {exc}")
                problems += 1
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="URL of a running connector, e.g. http://localhost:8000/mcp")
    ap.add_argument("--mark", nargs=2, metavar=("SESSION_ID", "STATUS"), help="also set a session passed/failed")
    args = ap.parse_args()
    if args.mark and args.mark[1] not in ("passed", "failed"):
        sys.exit("--mark status must be 'passed' or 'failed'")

    user, key = get_credentials()
    proc = None
    url = args.url
    try:
        if not url:
            port = free_port()
            env = {**os.environ, "PORT": str(port), "HOST": "127.0.0.1"}
            proc = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not wait_http(f"http://127.0.0.1:{port}/health"):
                print("The connector did not start. Run `python server.py` to see the error.")
                return 1
            url = f"http://127.0.0.1:{port}/mcp"
        problems = asyncio.run(tour(url, user, key, tuple(args.mark) if args.mark else None))
    finally:
        if proc:
            proc.terminate()
    if problems:
        print(f"\nDONE WITH PROBLEMS: {problems} step(s) failed (see above).")
        return 1
    print("\nOK: the BrowserStack connector works with your account.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
