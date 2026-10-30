"""BrowserStack MCP connector (FastMCP, Streamable HTTP, per-request credentials).

How it works
  The server holds NO credentials and NO state. Each caller sends their own BrowserStack username and access key
  in request headers:

      X-BrowserStack-Username:    <username>
      X-BrowserStack-Access-Key:  <access key>

  On every tool call the server calls the BrowserStack REST API as that caller (HTTP Basic auth, fresh client,
  nothing cached), so one deployed connector serves any number of users and accounts.

Products covered
  Automate      (web Selenium / Playwright / Cypress sessions)   https://api.browserstack.com
  App Automate  (mobile app sessions on real devices)            https://api-cloud.browserstack.com

Safety
  * The BrowserStack hosts are fixed. Callers cannot point the server at another address (no SSRF).
  * Build / session ids are validated before being placed in a URL (no path tricks).
  * Credentials never appear in errors or logs.
  * Read-only except set_session_status (marks a session passed/failed). No delete tools are provided.

Operator / test settings (environment variables, set by whoever deploys the connector)
  BROWSERSTACK_AUTOMATE_BASE   override the Automate base URL     (used by the automated tests)
  BROWSERSTACK_APP_BASE        override the App Automate base URL  (used by the automated tests)
"""
import base64
import os
import re
from typing import Any, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from auth import get_credential

USER_HEADER = "x-browserstack-username"
KEY_HEADER = "x-browserstack-access-key"
MAX_ITEMS = 200
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
TIMEOUT = httpx.Timeout(30.0, connect=10.0)

READ_ONLY = {"readOnlyHint": True, "openWorldHint": True}

mcp = FastMCP(
    name="BrowserStack MCP Server",
    instructions=(
        "Inspect BrowserStack test runs as the caller: Automate (web) and App Automate (mobile). Check plan and "
        "parallel-session usage, find supported browsers/devices, list builds and sessions, read a session's "
        "details and logs, and mark a session passed or failed. Send your BrowserStack username and access key in "
        "the 'X-BrowserStack-Username' and 'X-BrowserStack-Access-Key' headers. A good flow is: *_list_builds -> "
        "*_list_sessions (needs a build id) -> *_get_session / *_get_session_log (needs a session id). "
        "Tools starting with automate_ are for web tests, app_automate_ for mobile app tests."
    ),
)


# ------------------------------------------------------------------ plumbing
def _base(product: str) -> str:
    if product == "automate":
        return os.getenv("BROWSERSTACK_AUTOMATE_BASE", "https://api.browserstack.com").rstrip("/")
    return os.getenv("BROWSERSTACK_APP_BASE", "https://api-cloud.browserstack.com").rstrip("/")


def _auth_header() -> dict[str, str]:
    user = get_credential(USER_HEADER, strip_bearer=False)
    key = get_credential(KEY_HEADER, strip_bearer=False)
    token = base64.b64encode(f"{user}:{key}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}


def _ident(value: str, what: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ToolError(f"Invalid {what}: use the id exactly as returned by the list tools (letters, digits, - and _).")
    return value


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


async def _request(product: str, method: str, path: str, *, params: dict | None = None, body: Any = None):
    headers = _auth_header()  # raises 'Missing credentials' before any network call
    url = _base(product) + path
    params = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, max_redirects=3) as client:
            resp = await client.request(method, url, headers=headers, params=params, json=body)
    except httpx.HTTPError as exc:
        # Never include str(exc): keep error text free of anything caller-specific.
        raise ToolError(f"Could not reach BrowserStack ({type(exc).__name__}). Try again in a moment.")
    if resp.status_code in (401, 403):
        raise ToolError("BrowserStack rejected the username/access key (HTTP "
                        f"{resp.status_code}). Check both headers, and that this account has the product enabled.")
    if resp.status_code == 404:
        raise ToolError("BrowserStack returned 404: that id or product was not found for this account.")
    if resp.status_code == 429:
        raise ToolError("BrowserStack rate limit reached (about 1600 requests per 5 minutes). Wait and retry.")
    if resp.status_code >= 400:
        raise ToolError(f"BrowserStack returned HTTP {resp.status_code}: {resp.text[:300]}")
    return resp


async def _json(product: str, method: str, path: str, **kw) -> Any:
    resp = await _request(product, method, path, **kw)
    try:
        return resp.json()
    except ValueError:
        raise ToolError("BrowserStack returned a response that was not JSON.")


def _unwrap(item: Any) -> Any:
    """BrowserStack wraps list items like {'automation_build': {...}}; return the inner object."""
    if isinstance(item, dict) and len(item) == 1:
        inner = next(iter(item.values()))
        if isinstance(inner, dict):
            return inner
    return item


def _pick(obj: dict, keys: tuple[str, ...]) -> dict:
    return {k: obj[k] for k in keys if k in obj and obj[k] is not None}


BUILD_KEYS = ("hashed_id", "name", "status", "duration", "created_at", "project_name", "build_tag", "framework")
SESSION_KEYS = ("hashed_id", "name", "status", "reason", "build_name", "project_name", "browser", "browser_version",
                "os", "os_version", "device", "duration", "created_at", "start_time")
SESSION_DETAIL_EXTRA = ("browser_url", "public_url", "video_url", "logs", "browser_console_logs_url",
                        "har_logs_url", "appium_logs_url", "device_logs_url", "build_hashed_id", "app_details")


def _clip_text(text: str, n: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= n else (text[:n], True)


# ------------------------------------------------------------------ shared tool bodies
async def _plan(product: str) -> dict:
    prefix = "automate" if product == "automate" else "app-automate"
    return await _json(product, "GET", f"/{prefix}/plan.json")


async def _list_builds(product: str, status, limit, offset) -> dict:
    prefix = "automate" if product == "automate" else "app-automate"
    limit = _clamp(limit, 1, 100)
    offset = max(0, int(offset))
    data = await _json(product, "GET", f"/{prefix}/builds.json",
                       params={"status": status, "limit": limit, "offset": offset})
    builds = [_pick(_unwrap(b), BUILD_KEYS) for b in (data if isinstance(data, list) else [])]
    return {"count": len(builds), "limit": limit, "offset": offset, "builds": builds}


async def _list_sessions(product: str, build_id, status, limit, offset) -> dict:
    prefix = "automate" if product == "automate" else "app-automate"
    build_id = _ident(build_id, "build_id")
    limit = _clamp(limit, 1, 100)
    offset = max(0, int(offset))
    data = await _json(product, "GET", f"/{prefix}/builds/{build_id}/sessions.json",
                       params={"status": status, "limit": limit, "offset": offset})
    sessions = [_pick(_unwrap(s), SESSION_KEYS) for s in (data if isinstance(data, list) else [])]
    return {"build_id": build_id, "count": len(sessions), "limit": limit, "offset": offset, "sessions": sessions}


async def _get_session(product: str, session_id) -> dict:
    prefix = "automate" if product == "automate" else "app-automate"
    session_id = _ident(session_id, "session_id")
    data = await _json(product, "GET", f"/{prefix}/sessions/{session_id}.json")
    return _pick(_unwrap(data) if isinstance(data, dict) else {}, SESSION_KEYS + SESSION_DETAIL_EXTRA)


async def _set_status(product: str, session_id, status, reason) -> dict:
    prefix = "automate" if product == "automate" else "app-automate"
    session_id = _ident(session_id, "session_id")
    if status not in ("passed", "failed"):
        raise ToolError("status must be 'passed' or 'failed'.")
    body: dict[str, str] = {"status": status}
    if reason:
        body["reason"] = str(reason)[:255]
    data = await _json(product, "PUT", f"/{prefix}/sessions/{session_id}.json", body=body)
    return _pick(_unwrap(data) if isinstance(data, dict) else {}, SESSION_KEYS)


async def _get_log(product: str, session_id, kind, kinds: dict[str, str], max_chars) -> dict:
    prefix = "automate" if product == "automate" else "app-automate"
    session_id = _ident(session_id, "session_id")
    if kind not in kinds:
        raise ToolError(f"kind must be one of: {', '.join(kinds)}.")
    max_chars = _clamp(max_chars, 500, 20000)
    resp = await _request(product, "GET", f"/{prefix}/sessions/{session_id}/{kinds[kind]}")
    text, truncated = _clip_text(resp.text, max_chars)
    return {"session_id": session_id, "kind": kind, "truncated": truncated, "total_chars": len(resp.text),
            "log": text}


def _filter_list(rows: list, filters: dict[str, str | None], keep: tuple[str, ...]) -> dict:
    wanted = {k: v.lower() for k, v in filters.items() if v}
    out = []
    for r in rows:
        r = _unwrap(r)
        if not isinstance(r, dict):
            continue
        if all(w in str(r.get(k, "")).lower() for k, w in wanted.items()):
            out.append(_pick(r, keep))
    return {"matched": len(out), "returned": min(len(out), MAX_ITEMS), "truncated": len(out) > MAX_ITEMS,
            "items": out[:MAX_ITEMS]}


Status = Literal["running", "done", "failed", "timeout", "queued"]
Verdict = Literal["passed", "failed"]


# ------------------------------------------------------------------ Automate (web)
@mcp.tool(annotations=READ_ONLY)
async def automate_plan() -> dict:
    """Automate plan details: plan name, parallel sessions allowed / running, and queue size. Good first call to
    confirm the credentials work."""
    return await _plan("automate")


@mcp.tool(annotations=READ_ONLY)
async def automate_browsers(os: str | None = None, browser: str | None = None, device: str | None = None) -> dict:
    """List the OS / browser / real-device combinations Automate supports. The full list is huge, so filter with
    case-insensitive substrings, e.g. os='Windows', browser='chrome', device='iPhone'. At most 200 rows are returned."""
    rows = await _json("automate", "GET", "/automate/browsers.json")
    return _filter_list(rows if isinstance(rows, list) else [],
                        {"os": os, "browser": browser, "device": device},
                        ("os", "os_version", "browser", "browser_version", "device", "real_mobile"))


@mcp.tool(annotations=READ_ONLY)
async def automate_list_builds(status: Status | None = None, limit: int = 10, offset: int = 0) -> dict:
    """Recent Automate builds (newest first) with id (hashed_id), name, status and duration. limit is 1-100."""
    return await _list_builds("automate", status, limit, offset)


@mcp.tool(annotations=READ_ONLY)
async def automate_list_sessions(build_id: str, status: Status | None = None, limit: int = 20,
                                 offset: int = 0) -> dict:
    """Sessions (individual test runs) inside one Automate build. build_id is a hashed_id from automate_list_builds."""
    return await _list_sessions("automate", build_id, status, limit, offset)


@mcp.tool(annotations=READ_ONLY)
async def automate_get_session(session_id: str) -> dict:
    """Details of one Automate session: browser/OS, status, reason, duration, plus links to the dashboard page,
    video and log files. session_id is a hashed_id from automate_list_sessions."""
    return await _get_session("automate", session_id)


@mcp.tool(annotations=READ_ONLY)
async def automate_get_session_log(session_id: str, kind: Literal["text", "network", "console"] = "text",
                                   max_chars: int = 6000) -> dict:
    """Read a session's log: 'text' (Selenium/Playwright command log), 'network' (HAR) or 'console' (browser
    console). Output is cut at max_chars (500-20000); check 'truncated'."""
    return await _get_log("automate", session_id, kind,
                          {"text": "logs", "network": "networklogs", "console": "consolelogs"}, max_chars)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                       "openWorldHint": True})
async def automate_set_session_status(session_id: str, status: Verdict, reason: str | None = None) -> dict:
    """Mark an Automate session 'passed' or 'failed' on the BrowserStack dashboard, with an optional reason
    (max 255 chars). This changes data in the caller's account, so only call it when asked."""
    return await _set_status("automate", session_id, status, reason)


# ------------------------------------------------------------------ App Automate (mobile)
@mcp.tool(annotations=READ_ONLY)
async def app_automate_plan() -> dict:
    """App Automate plan details: parallel tests allowed / running / queued. Errors if the account has no App
    Automate access."""
    return await _plan("app")


@mcp.tool(annotations=READ_ONLY)
async def app_automate_devices(os: str | None = None, device: str | None = None) -> dict:
    """List real devices available in App Automate. Filter with case-insensitive substrings, e.g. os='android',
    device='Galaxy S23'. At most 200 rows are returned."""
    rows = await _json("app", "GET", "/app-automate/devices.json")
    return _filter_list(rows if isinstance(rows, list) else [], {"os": os, "device": device},
                        ("device", "os", "os_version", "realMobile"))


@mcp.tool(annotations=READ_ONLY)
async def app_automate_list_builds(status: Status | None = None, limit: int = 10, offset: int = 0) -> dict:
    """Recent App Automate builds (newest first). limit is 1-100."""
    return await _list_builds("app", status, limit, offset)


@mcp.tool(annotations=READ_ONLY)
async def app_automate_list_sessions(build_id: str, status: Status | None = None, limit: int = 20,
                                     offset: int = 0) -> dict:
    """Sessions inside one App Automate build. build_id is a hashed_id from app_automate_list_builds."""
    return await _list_sessions("app", build_id, status, limit, offset)


@mcp.tool(annotations=READ_ONLY)
async def app_automate_get_session(session_id: str) -> dict:
    """Details of one App Automate session: device/OS, status, reason, duration and links to video and logs."""
    return await _get_session("app", session_id)


@mcp.tool(annotations=READ_ONLY)
async def app_automate_get_session_log(session_id: str,
                                       kind: Literal["text", "network", "device", "appium"] = "text",
                                       max_chars: int = 6000) -> dict:
    """Read an App Automate session log: 'text', 'network', 'device' or 'appium'. Cut at max_chars (500-20000)."""
    return await _get_log("app", session_id, kind,
                          {"text": "logs", "network": "networklogs", "device": "devicelogs", "appium": "appiumlogs"},
                          max_chars)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                       "openWorldHint": True})
async def app_automate_set_session_status(session_id: str, status: Verdict, reason: str | None = None) -> dict:
    """Mark an App Automate session 'passed' or 'failed', with an optional reason (max 255 chars). Changes data
    in the caller's account, so only call it when asked."""
    return await _set_status("app", session_id, status, reason)


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok", "server": "browserstack"})


if __name__ == "__main__":
    mcp.run(transport="http", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")),
            stateless_http=True)
