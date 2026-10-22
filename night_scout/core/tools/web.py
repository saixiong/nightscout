"""read_page: fetch one public web page and return its readable text.

The URL comes from the model, which read it in untrusted feed text or a tool result, so it
gets the same treatment as Research more: http(s) only, every redirect hop resolved and
checked with the public-address rule (the connection goes to the checked IP, so DNS can't
swap in a private one), a time limit, a size cap and text-like content types only.
"""

from __future__ import annotations

import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from ..http import resolve_public_ip, user_agent
from .base import ToolContext, clip, fail

MAX_BYTES = 1_500_000
TIMEOUT_S = 20.0
MAX_HOPS = 5
TEXT_LIMIT = 7000
TEXT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "text/markdown",
    "application/json",
)

# Content we never want in "readable text".
_SKIP = {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form"}
_SKIP |= {"iframe", "template", "button", "select", "canvas"}
_BLOCK = {"p", "div", "section", "li", "ul", "ol", "br", "tr", "h1", "h2", "h3", "h4", "h5"}
_BLOCK |= {"h6", "pre", "blockquote", "table", "article", "main", "dd", "dt", "figcaption"}
_VOID = {"br", "img", "hr", "input", "meta", "link", "source", "wbr", "area", "col", "embed"}


class _Readable(HTMLParser):
    """Collects text, separately for <main>/<article> and the whole body."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.body: list[str] = []
        self.main: list[str] = []
        self.title = ""
        self._skip = 0
        self._main = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            if tag == "br":
                self._add("\n")
            return
        if tag in _SKIP:
            self._skip += 1
        elif tag in ("main", "article"):
            self._main += 1
        elif tag == "title":
            self._in_title = True
        if tag in _BLOCK:
            self._add("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag in ("main", "article") and self._main:
            self._main -= 1
        elif tag == "title":
            self._in_title = False
        if tag in _BLOCK:
            self._add("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self._add(data)

    def _add(self, text: str) -> None:
        self.body.append(text)
        if self._main:
            self.main.append(text)


def _tidy(text: str) -> str:
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def html_text(html: str) -> tuple[str, str]:
    """(title, readable text): <main>/<article> when it holds real content, else the body."""
    p = _Readable()
    try:
        p.feed(html)
        p.close()
    except Exception:  # html.parser is lenient, but never let a page crash the tool
        pass
    main = _tidy("".join(p.main))
    text = main if len(main) >= 200 else _tidy("".join(p.body))
    return " ".join(p.title.split())[:200], text


def _charset(ctype: str) -> str:
    m = re.search(r"charset=([\w.-]+)", ctype, re.I)
    return m.group(1) if m else "utf-8"


def fetch_public(url: str, ctx: ToolContext) -> tuple[str, str, bytes, bool]:
    """GET a public URL by hand: (final_url, content_type, body, truncated).
    Raises ValueError with a user-facing reason when refused or failed."""
    deadline = time.monotonic() + TIMEOUT_S
    for _ in range(MAX_HOPS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"Refused: {url[:200]} is not an http(s) address.")
        if parts.username or parts.password:
            raise ValueError("Refused: the address contains a user name or password.")
        ip = resolve_public_ip(url, ctx.resolve)
        if ip is None:
            raise ValueError(
                f"Refused: {parts.hostname} is not a public internet address "
                "(local, private and unresolvable hosts are off limits)."
            )
        literal = f"[{ip}]" if ":" in ip else ip
        port = f":{parts.port}" if parts.port else ""
        pinned = urlunsplit((parts.scheme, literal + port, parts.path or "/", parts.query, ""))
        headers = {
            "Host": parts.netloc.rsplit("@", 1)[-1],
            "User-Agent": user_agent(ctx.contact),
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,application/json;q=0.8",
        }
        ext = {"sni_hostname": parts.hostname} if parts.scheme == "https" else {}
        left = max(1.0, deadline - time.monotonic())
        # A fresh client per hop: no connection or TLS session reuse across hosts.
        with (
            httpx.Client(
                transport=ctx.transport,
                timeout=httpx.Timeout(left, connect=min(10.0, left)),
                limits=httpx.Limits(max_keepalive_connections=0),
            ) as client,
            client.stream(
                "GET", pinned, headers=headers, extensions=ext, follow_redirects=False
            ) as resp,
        ):
            if resp.is_redirect and resp.headers.get("location"):
                nxt = urljoin(url, resp.headers["location"])
                if parts.scheme == "https" and urlsplit(nxt).scheme == "http":
                    raise ValueError("Refused: the page redirects from https to plain http.")
                url = nxt
                continue
            if resp.status_code >= 400:
                raise ValueError(f"HTTP {resp.status_code} from {parts.hostname}.")
            ctype = resp.headers.get("content-type", "").lower()
            kind = ctype.split(";")[0].strip()
            if kind not in TEXT_TYPES:
                what = kind or "unknown type"
                raise ValueError(f"Not a text page ({what}); only HTML, text and JSON are read.")
            body = bytearray()
            truncated = False
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    truncated = True
                    break
                if time.monotonic() > deadline:
                    raise ValueError(f"Timed out reading {parts.hostname}.")
            return url, ctype, bytes(body[:MAX_BYTES]), truncated
    raise ValueError("Too many redirects.")


def read_page(ctx: ToolContext, url: Any = "") -> dict[str, Any]:
    url = str(url or "").strip()
    if not url:
        return fail("Give a full http(s) URL to read.")
    if len(url) > 2000:
        return fail("That URL is too long.")
    try:
        final, ctype, body, cut = fetch_public(url, ctx)
    except ValueError as e:
        return fail(str(e))
    except httpx.TimeoutException:
        return fail(f"Timed out after {TIMEOUT_S:.0f} s fetching {urlsplit(url).hostname}.")
    except httpx.HTTPError as e:
        return fail(f"Couldn't fetch {urlsplit(url).hostname} ({type(e).__name__}).")
    try:
        raw = body.decode(_charset(ctype), errors="replace")
    except LookupError:  # an unknown charset name
        raw = body.decode("utf-8", errors="replace")
    title = ""
    if "html" in ctype:
        title, text = html_text(raw)
    else:
        text = _tidy(raw)
    if not text:
        return fail(f"{urlsplit(final).hostname} returned no readable text.")
    clipped, more = clip(text, TEXT_LIMIT)
    out: dict[str, Any] = {"url": final, "title": title, "text": clipped}
    if more or cut:
        out["truncated"] = True
    return out
