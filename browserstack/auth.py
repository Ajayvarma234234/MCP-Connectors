"""Shared per-request credential + HTTP helpers for all connectors.

Design rules (these are what make a connector usable by *any* user):
  * Credentials are read from the incoming MCP HTTP request headers on EVERY
    tool call. Nothing is read from env vars, files, or module globals.
  * No HTTP client or token is cached between calls, so one caller's
    credentials can never be reused for another caller.
  * Tokens are never written to logs or included in error messages.
"""
from __future__ import annotations

from typing import Any

import httpx
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers

TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def get_credential(*header_names: str, strip_bearer: bool = True, required: bool = True) -> str | None:
    """Return the caller's credential from the first matching request header.

    include_all=True is essential: FastMCP strips `authorization` and `cookie`
    from get_http_headers() by default.
    """
    headers = get_http_headers(include_all=True)
    for name in header_names:
        value = headers.get(name.lower())
        if not value:
            continue
        value = value.strip()
        if strip_bearer and value.lower().startswith("bearer "):
            value = value[7:].strip()
        if value:
            return value
    if required:
        wanted = " or ".join(header_names)
        raise ToolError(f"Missing credentials: send the '{wanted}' header with your MCP request.")
    return None


def get_optional_header(name: str) -> str | None:
    value = get_http_headers(include_all=True).get(name.lower())
    return value.strip() if value and value.strip() else None


async def call_api(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    json: Any = None,
    data: dict[str, Any] | None = None,
) -> Any:
    """One-shot upstream request. A fresh client per call keeps callers isolated."""
    params = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.request(method, url, headers=headers, params=params, json=json, data=data)
    except httpx.HTTPError as exc:
        raise ToolError(f"Upstream request failed: {type(exc).__name__}") from exc

    if resp.status_code >= 400:
        raise ToolError(f"Upstream API returned {resp.status_code}: {resp.text[:500]}")
    if not resp.content:
        return {"status": resp.status_code}
    try:
        return resp.json()
    except ValueError:
        return {"status": resp.status_code, "body": resp.text[:2000]}
