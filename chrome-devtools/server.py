"""Chrome DevTools MCP connector (FastMCP, Streamable HTTP, per-request browser endpoint).

How it works
  The server holds NO browser and NO state. Each caller sends the address of THEIR OWN Chrome/Chromium
  (started with --remote-debugging-port, or a hosted CDP provider) in a request header:

      X-Chrome-CDP-Endpoint: http://<host>:9222          (or ws://... / wss://... incl. provider tokens)

  On every tool call the server connects to that endpoint over the Chrome DevTools Protocol, does the work,
  and disconnects. Page identity between calls is the CDP target id (see list_pages).

Operator hardening (environment variables, set by whoever deploys the connector)
  CDP_ALLOWED_HOSTS   comma-separated hostnames / '*.suffix' patterns callers may point at (unset = any host)
  CDP_BLOCK_PRIVATE   'true' to also refuse loopback/private addresses (recommended for multi-tenant hosting)
  Link-local / cloud-metadata addresses (169.254.0.0/16, fe80::/10) are ALWAYS refused.
"""
import asyncio
import fnmatch
import ipaddress
import os
import socket
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import Image
from playwright.async_api import Browser, Page, async_playwright

from auth import get_credential

ALLOWED_SCHEMES = {"http", "https", "ws", "wss"}
NAV_SCHEMES = {"http", "https"}
ENDPOINT_HEADER = "x-chrome-cdp-endpoint"
MAX_ITEMS = 100

mcp = FastMCP(
    name="Chrome DevTools MCP Server",
    instructions=(
        "Inspect and debug web pages in the caller's own Chrome via DevTools Protocol: list/open/close tabs, "
        "navigate with console + network capture, screenshots, DOM/accessibility snapshots, run JavaScript, "
        "click/fill, and read performance metrics. Send your browser's CDP address in the "
        "'X-Chrome-CDP-Endpoint' header. Start with list_pages."
    ),
)


# ------------------------------------------------------------------ endpoint validation
def _blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    if ip.is_link_local or ip.is_unspecified or ip.is_multicast:
        return True
    if os.getenv("CDP_BLOCK_PRIVATE", "").lower() == "true" and (ip.is_private or ip.is_loopback):
        return True
    return False


async def _validate_endpoint(raw: str) -> str:
    parts = urlsplit(raw)
    if parts.scheme not in ALLOWED_SCHEMES or not parts.hostname:
        raise ToolError("X-Chrome-CDP-Endpoint must be an http(s):// or ws(s):// URL with a host.")
    host = parts.hostname.lower()

    allowed = [h.strip().lower() for h in os.getenv("CDP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if allowed and not any(fnmatch.fnmatch(host, pat) for pat in allowed):
        raise ToolError("The endpoint host is not on this connector's allowlist.")  # never echo the URL (may hold a token)

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, parts.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ToolError("The endpoint host could not be resolved.")
    for info in infos:
        if _blocked_ip(ipaddress.ip_address(info[4][0])):
            raise ToolError("The endpoint resolves to an address this connector refuses to connect to.")
    return raw


@asynccontextmanager
async def _browser():
    raw = get_credential(ENDPOINT_HEADER, strip_bearer=False)
    endpoint = await _validate_endpoint(raw)
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(endpoint, timeout=15_000)
        except Exception as exc:
            # Do not include str(exc): it can contain the endpoint (and any token in it).
            raise ToolError(f"Could not connect to Chrome at the supplied endpoint ({type(exc).__name__}). "
                            "Is it running with --remote-debugging-port and reachable from this server?")
        try:
            yield browser
        finally:
            await browser.close()  # for CDP-attached browsers this disconnects; it does not quit the caller's Chrome


# ------------------------------------------------------------------ page helpers
def _pages(browser: Browser) -> list[Page]:
    return [p for c in browser.contexts for p in c.pages]


async def _target_id(page: Page) -> str:
    session = await page.context.new_cdp_session(page)
    try:
        info = await session.send("Target.getTargetInfo")
        return info["targetInfo"]["targetId"]
    finally:
        await session.detach()


async def _page(browser: Browser, target_id: str | None) -> Page:
    pages = _pages(browser)
    if target_id:
        for p in pages:
            if await _target_id(p) == target_id:
                return p
        raise ToolError("No open page has that target_id. Call list_pages for current ids.")
    for p in pages:  # default: first real tab, skipping extension/devtools pages
        if not p.url.startswith(("chrome-extension://", "devtools://")):
            return p
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    return await ctx.new_page()


def _check_nav_url(url: str) -> str:
    if urlsplit(url).scheme not in NAV_SCHEMES:
        raise ToolError("Only http:// and https:// URLs can be opened.")
    return url


def _clip(text: str | None, n: int = 500) -> str | None:
    if text is None:
        return None
    return text if len(text) <= n else text[:n] + "…"


class _Capture:
    """Collects console output, uncaught errors and network activity while it is attached."""

    def __init__(self, page: Page):
        self.page = page
        self.console: list[dict] = []
        self.errors: list[str] = []
        self.failed: list[dict] = []
        self.http_errors: list[dict] = []
        self.by_type: dict[str, int] = {}
        self.total = 0

    def _on_console(self, msg):
        if len(self.console) < MAX_ITEMS:
            loc = msg.location or {}
            self.console.append({"type": msg.type, "text": _clip(msg.text),
                                 "source": _clip(f"{loc.get('url', '')}:{loc.get('lineNumber', '')}", 200)})

    def _on_pageerror(self, err):
        if len(self.errors) < MAX_ITEMS:
            self.errors.append(_clip(getattr(err, "message", str(err))))

    def _on_failed(self, req):
        if len(self.failed) < MAX_ITEMS:
            self.failed.append({"url": _clip(req.url, 300), "type": req.resource_type, "reason": req.failure})

    def _on_response(self, resp):
        self.total += 1
        rtype = resp.request.resource_type
        self.by_type[rtype] = self.by_type.get(rtype, 0) + 1
        if resp.status >= 400 and len(self.http_errors) < MAX_ITEMS:
            self.http_errors.append({"url": _clip(resp.url, 300), "status": resp.status, "type": rtype})

    def __enter__(self):
        self.page.on("console", self._on_console)
        self.page.on("pageerror", self._on_pageerror)
        self.page.on("requestfailed", self._on_failed)
        self.page.on("response", self._on_response)
        return self

    def __exit__(self, *exc):
        for ev, fn in (("console", self._on_console), ("pageerror", self._on_pageerror),
                       ("requestfailed", self._on_failed), ("response", self._on_response)):
            self.page.remove_listener(ev, fn)

    def report(self) -> dict[str, Any]:
        return {"console": self.console, "uncaught_errors": self.errors,
                "network": {"requests": self.total, "by_type": self.by_type,
                            "http_errors": self.http_errors, "failed": self.failed}}


# ------------------------------------------------------------------ tools
@mcp.tool
async def list_pages() -> list:
    """List open tabs with their target_id, URL and title. Use target_id in the other tools."""
    async with _browser() as browser:
        return [{"target_id": await _target_id(p), "url": p.url, "title": await p.title()} for p in _pages(browser)]


@mcp.tool
async def new_page(url: str | None = None) -> dict:
    """Open a new tab, optionally at an http(s) URL."""
    if url:
        _check_nav_url(url)
    async with _browser() as browser:
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await ctx.new_page()
        if url:
            await page.goto(url, wait_until="load", timeout=30_000)
        return {"target_id": await _target_id(page), "url": page.url, "title": await page.title()}


@mcp.tool
async def close_page(target_id: str) -> dict:
    """Close a tab."""
    async with _browser() as browser:
        page = await _page(browser, target_id)
        await page.close()
        return {"closed": target_id}


@mcp.tool
async def navigate(url: str, target_id: str | None = None, wait_until: str = "load", timeout_ms: int = 30000,
                   settle_ms: int = 500) -> dict:
    """Go to an http(s) URL and report what happened during the load: final URL/title, console messages,
    uncaught JS errors, failed requests, HTTP 4xx/5xx responses and request counts by type.
    wait_until: load | domcontentloaded | networkidle | commit."""
    _check_nav_url(url)
    if wait_until not in ("load", "domcontentloaded", "networkidle", "commit"):
        raise ToolError("wait_until must be load, domcontentloaded, networkidle or commit.")
    async with _browser() as browser:
        page = await _page(browser, target_id)
        with _Capture(page) as cap:
            resp = await page.goto(url, wait_until=wait_until, timeout=min(max(timeout_ms, 1000), 60_000))
            await page.wait_for_timeout(min(max(settle_ms, 0), 5000))
            return {"target_id": await _target_id(page), "url": page.url, "title": await page.title(),
                    "status": resp.status if resp else None, **cap.report()}


@mcp.tool
async def capture_activity(target_id: str | None = None, reload: bool = True, duration_ms: int = 3000) -> dict:
    """Record console output and network activity on a tab. With reload=true the page is reloaded first, so
    the capture includes everything the page does on load; otherwise it listens for duration_ms (max 15000)."""
    async with _browser() as browser:
        page = await _page(browser, target_id)
        with _Capture(page) as cap:
            if reload:
                await page.reload(wait_until="load", timeout=30_000)
            await page.wait_for_timeout(min(max(duration_ms, 0), 15_000))
            return {"url": page.url, "title": await page.title(), **cap.report()}


@mcp.tool
async def screenshot(target_id: str | None = None, full_page: bool = False, selector: str | None = None,
                     image_format: str = "png") -> Image:
    """Take a screenshot of a tab, a CSS selector, or the full scrollable page. image_format: png | jpeg."""
    if image_format not in ("png", "jpeg"):
        raise ToolError("image_format must be png or jpeg.")
    async with _browser() as browser:
        page = await _page(browser, target_id)
        if selector:
            data = await page.locator(selector).first.screenshot(type=image_format, timeout=15_000)
        else:
            data = await page.screenshot(type=image_format, full_page=full_page, timeout=15_000)
        return Image(data=data, format=image_format)


@mcp.tool
async def evaluate_script(expression: str, target_id: str | None = None) -> Any:
    """Run JavaScript in the page and return the JSON-serializable result.
    Accepts an expression ('document.title') or a function ('() => document.links.length'); async is fine."""
    async with _browser() as browser:
        page = await _page(browser, target_id)
        try:
            return await page.evaluate(expression)
        except Exception as exc:
            raise ToolError(f"Script failed: {_clip(str(exc), 400)}")


@mcp.tool
async def get_page_content(target_id: str | None = None, mode: str = "text", max_chars: int = 20000) -> dict:
    """Read the page. mode: text (visible text) | html (full DOM) | accessibility (ARIA snapshot, compact and
    good for finding clickable things)."""
    async with _browser() as browser:
        page = await _page(browser, target_id)
        if mode == "text":
            body = await page.inner_text("body")
        elif mode == "html":
            body = await page.content()
        elif mode == "accessibility":
            body = await page.locator("body").aria_snapshot()
        else:
            raise ToolError("mode must be text, html or accessibility.")
        cap = min(max(max_chars, 500), 200_000)
        return {"url": page.url, "title": await page.title(), "mode": mode, "truncated": len(body) > cap,
                "content": body[:cap]}


@mcp.tool
async def click(selector: str, target_id: str | None = None) -> dict:
    """Click the first element matching a CSS/text selector (e.g. 'button:has-text("Save")')."""
    async with _browser() as browser:
        page = await _page(browser, target_id)
        await page.locator(selector).first.click(timeout=10_000)
        return {"clicked": selector, "url": page.url}


@mcp.tool
async def fill(selector: str, value: str, target_id: str | None = None, press_enter: bool = False) -> dict:
    """Type text into an input matching the selector, optionally pressing Enter."""
    async with _browser() as browser:
        page = await _page(browser, target_id)
        loc = page.locator(selector).first
        await loc.fill(value, timeout=10_000)
        if press_enter:
            await loc.press("Enter")
        return {"filled": selector, "url": page.url}


_PERF_JS = """
async () => {
  const nav = performance.getEntriesByType('navigation')[0];
  const paints = Object.fromEntries(performance.getEntriesByType('paint').map(p => [p.name, Math.round(p.startTime)]));
  const observe = (type) => new Promise(res => {
    try {
      const out = [];
      new PerformanceObserver(l => out.push(...l.getEntries())).observe({type, buffered: true});
      setTimeout(() => res(out), 150);
    } catch (e) { res([]); }
  });
  const lcpEntries = await observe('largest-contentful-paint');
  const shifts = await observe('layout-shift');
  const lcp = lcpEntries.length ? lcpEntries[lcpEntries.length - 1] : null;
  return {
    navigation: nav ? {
      ttfb_ms: Math.round(nav.responseStart), dom_content_loaded_ms: Math.round(nav.domContentLoadedEventEnd),
      load_ms: Math.round(nav.loadEventEnd), transfer_bytes: nav.transferSize, type: nav.type } : null,
    paint_ms: paints,
    largest_contentful_paint: lcp ? {start_ms: Math.round(lcp.startTime), size: lcp.size, url: lcp.url || null} : null,
    cumulative_layout_shift: Math.round(shifts.filter(s => !s.hadRecentInput).reduce((a, s) => a + s.value, 0) * 1000) / 1000,
    resources: performance.getEntriesByType('resource').length,
  };
}
"""


@mcp.tool
async def performance_metrics(target_id: str | None = None) -> dict:
    """Page performance: navigation timing, paint timing, LCP, CLS, resource count, plus raw CDP metrics
    (JS heap, DOM node count, layout/recalc counts)."""
    async with _browser() as browser:
        page = await _page(browser, target_id)
        session = await page.context.new_cdp_session(page)
        try:
            await session.send("Performance.enable")
            raw = await session.send("Performance.getMetrics")
        finally:
            await session.detach()
        keep = {"JSHeapUsedSize", "JSHeapTotalSize", "Nodes", "Documents", "Frames", "JSEventListeners",
                "LayoutCount", "RecalcStyleCount", "LayoutDuration", "ScriptDuration", "TaskDuration"}
        cdp = {m["name"]: m["value"] for m in raw["metrics"] if m["name"] in keep}
        web = await page.evaluate(_PERF_JS)
        return {"url": page.url, "web_vitals": web, "cdp_metrics": cdp}


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok", "server": "chrome-devtools"})


if __name__ == "__main__":
    mcp.run(transport="http", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")),
            stateless_http=True)
