"""What every research tool shares: the context it runs in and the result helpers."""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Config
from ..db import Store
from ..http import user_agent


@dataclass
class ToolContext:
    """Where a tool runs. Tests swap `transport`, `resolve` and `transcript_fn`."""

    cfg: Config
    store: Store | None = None
    day: str | None = None  # the Digest day the chat is about, for search_tonight
    transport: httpx.BaseTransport | None = None
    resolve: Any = socket.getaddrinfo
    transcript_fn: Callable[[str], str | None] | None = None
    contact: str = field(default="local user")


def fail(message: str) -> dict[str, Any]:
    return {"error": message}


def clip(text: str, limit: int) -> tuple[str, bool]:
    """Cut at a word boundary near `limit`; (text, was_cut)."""
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    space = cut.rfind(" ", int(limit * 0.8))
    return (cut[:space] if space > 0 else cut).rstrip() + " …", True


def http_client(ctx: ToolContext, timeout: float = 15.0) -> httpx.Client:
    """For fixed, well-known APIs (Hacker News, Brave) and the user's own SearXNG. Redirects
    are not followed: a key header must never travel to another host."""
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
        transport=ctx.transport,
        follow_redirects=False,
        headers={"User-Agent": user_agent(ctx.contact), "Accept": "application/json"},
    )
