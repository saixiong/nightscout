"""web_search through the provider picked in Settings → Chat & search.

SearXNG: the user's own instance, `{url}/search?q=…&format=json` (no key). Brave Search API:
the key lives in the OS keychain under BRAVE_KEY_ID and only ever goes in the
X-Subscription-Token header; it never appears in results, errors or logs.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from .. import secrets
from .base import ToolContext, fail, http_client

BRAVE_KEY_ID = "brave-search"
BRAVE_API = "https://api.search.brave.com/res/v1/web/search"
RESULTS = 6
_TAGS = re.compile(r"<[^>]+>")


def _text(value: Any, limit: int) -> str:
    return " ".join(_TAGS.sub("", str(value or "")).split())[:limit]


def _result(title: Any, url: Any, snippet: Any, **extra: Any) -> dict[str, Any] | None:
    u = str(url or "")
    if not u.startswith(("http://", "https://")):
        return None
    return {"title": _text(title, 200), "url": u[:500], "snippet": _text(snippet, 400), **extra}


def _searxng(ctx: ToolContext, query: str) -> dict[str, Any]:
    base = (ctx.cfg.search.searxng_url or "").rstrip("/")
    if not base:
        return fail("SearXNG is picked but has no URL. Add it in Settings → Chat & search.")
    try:
        with http_client(ctx, timeout=15.0) as client:
            resp = client.get(f"{base}/search", params={"q": query, "format": "json"})
    except httpx.TimeoutException:
        return fail(f"SearXNG at {base} timed out.")
    except httpx.HTTPError as e:
        return fail(f"Couldn't reach SearXNG at {base} ({type(e).__name__}). Is it running?")
    if resp.status_code == 403:
        return fail(
            f"SearXNG at {base} refused JSON output (HTTP 403). Enable it in the instance's "
            "settings.yml: under `search:`, add `json` to `formats:` (next to `html`), then "
            "restart SearXNG."
        )
    if resp.is_redirect:
        where = resp.headers.get("location", "")[:200]
        return fail(f"SearXNG at {base} redirects to {where}. Use that address in Settings.")
    if resp.status_code >= 400:
        return fail(f"SearXNG at {base} answered HTTP {resp.status_code}.")
    try:
        data = resp.json()
    except ValueError:
        return fail(
            f"SearXNG at {base} didn't return JSON. Check the URL points at the instance root "
            "and that `json` is listed under `search: formats:` in its settings.yml."
        )
    out = []
    for r in data.get("results") or [] if isinstance(data, dict) else []:
        if isinstance(r, dict):
            hit = _result(r.get("title"), r.get("url"), r.get("content"))
            if hit:
                out.append(hit)
        if len(out) >= RESULTS:
            break
    return {"provider": "searxng", "query": query, "results": out}


def _brave_key() -> str | None:
    try:
        return secrets.get_key(BRAVE_KEY_ID)
    except secrets.SecretStoreUnavailable:
        return None


def _brave(ctx: ToolContext, query: str) -> dict[str, Any]:
    key = _brave_key()
    if not key:
        return fail("No Brave Search API key yet. Add one in Settings → Chat & search.")
    try:
        with http_client(ctx, timeout=15.0) as client:
            resp = client.get(
                BRAVE_API,
                params={"q": query, "count": RESULTS},
                headers={"X-Subscription-Token": key},
            )
    except httpx.TimeoutException:
        return fail("Brave Search timed out.")
    except httpx.HTTPError as e:
        return fail(f"Couldn't reach Brave Search ({type(e).__name__}).")
    if resp.status_code in (401, 403, 422):
        return fail(
            f"Brave Search rejected the API key (HTTP {resp.status_code}). "
            "Check it in Settings → Chat & search."
        )
    if resp.status_code == 429:
        return fail("Brave Search rate limit reached (HTTP 429). Try again later.")
    if resp.status_code >= 400:
        return fail(f"Brave Search answered HTTP {resp.status_code}.")
    try:
        data = resp.json()
        rows = (data.get("web") or {}).get("results") or []
    except (ValueError, AttributeError):
        return fail("Brave Search returned something that isn't JSON.")
    out = []
    for r in rows:
        if isinstance(r, dict):
            hit = _result(r.get("title"), r.get("url"), r.get("description"))
            if hit:
                out.append(hit)
        if len(out) >= RESULTS:
            break
    return {"provider": "brave", "query": query, "results": out}


def web_search(ctx: ToolContext, query: Any = "") -> dict[str, Any]:
    query = " ".join(str(query or "").split())[:300]
    if not query:
        return fail("Give a search query.")
    provider = ctx.cfg.search.provider
    try:
        if provider == "searxng":
            return _searxng(ctx, query)
        if provider == "brave":
            out = _brave(ctx, query)
            key = _brave_key()
            if "error" in out and key:  # belt and braces: never echo the key
                out["error"] = secrets.scrub(out["error"], key)
            return out
    except Exception as e:  # a tool never raises into the chat loop
        return fail(f"Web search failed ({type(e).__name__}).")
    return fail("Web search is off. Pick a provider in Settings → Chat & search.")
