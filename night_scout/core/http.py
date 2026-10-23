"""A polite HTTP client: descriptive User-Agent, per-host spacing, bounded retries."""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from night_scout import __version__

log = logging.getLogger(__name__)

# Minimum seconds between requests to the same host. Reddit asks for the most care.
HOST_SPACING = {
    "www.reddit.com": 2.0,
    "reddit.com": 2.0,
    "api.github.com": 1.0,
    "hacker-news.firebaseio.com": 0.05,
}
DEFAULT_SPACING = 0.5


class SourceError(Exception):
    """A fetch failed in a way the run should report and move past."""

    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind  # "rate-limited", "blocked", "not-found", "network", "bad-data", "error"


def user_agent(contact: str) -> str:
    return f"NightScout/{__version__} (personal overnight news scout; {contact})"


class PoliteClient:
    def __init__(
        self,
        contact: str = "local user",
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
        max_retries: int = 2,
        sleep: Any = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": user_agent(contact)},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        self._transport = transport
        self._timeout = timeout
        self._ua = user_agent(contact)
        self._last: dict[str, float] = {}
        self._max_retries = max_retries
        self._sleep = sleep
        self._resolve: Any = socket.getaddrinfo

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _wait_turn(self, host: str) -> None:
        spacing = HOST_SPACING.get(host, DEFAULT_SPACING)
        last = self._last.get(host)
        if last is not None:
            gap = time.monotonic() - last
            if gap < spacing:
                self._sleep(spacing - gap)
        self._last[host] = time.monotonic()

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        *,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> httpx.Response:
        """`timeout`/`retries` override the client's defaults for this call (interactive checks
        want one quick try; the nightly run keeps the patient defaults)."""
        host = urlsplit(url).hostname or ""
        attempt = 0
        max_retries = self._max_retries if retries is None else retries
        while True:
            self._wait_turn(host)
            try:
                resp = (
                    self._client.get(url, params=params, headers=headers)
                    if timeout is None
                    else self._client.get(url, params=params, headers=headers, timeout=timeout)
                )
            except httpx.TimeoutException as e:
                err: SourceError = SourceError(f"timed out ({host})", "network")
                if attempt >= max_retries:
                    raise err from e
            except httpx.HTTPError as e:
                err = SourceError(f"network error ({type(e).__name__})", "network")
                if attempt >= max_retries:
                    raise err from e
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code == 429 or resp.status_code >= 500:
                    kind = "rate-limited" if resp.status_code == 429 else "error"
                    err = SourceError(f"HTTP {resp.status_code}", kind)
                    if attempt >= max_retries:
                        raise err
                    retry_after = _retry_after(resp)
                    if retry_after is not None and retry_after > 60:
                        # Don't park a nightly run for minutes; report and move on.
                        raise err
                    if retry_after is not None:
                        self._sleep(retry_after)
                elif resp.status_code == 403 and _github_rate_limited(resp):
                    raise SourceError("rate-limited (GitHub)", "rate-limited")
                elif resp.status_code in (401, 403):
                    raise SourceError(f"HTTP {resp.status_code} (blocked)", "blocked")
                elif resp.status_code == 404:
                    raise SourceError("HTTP 404 (not found)", "not-found")
                else:
                    raise SourceError(f"HTTP {resp.status_code}", "error")
            attempt += 1
            backoff = 2.0**attempt
            log.info("retrying %s in %.0fs (%s)", host, backoff, err)
            self._sleep(backoff)

    def _fresh_client(self) -> httpx.Client:
        """No connection reuse: a TLS session verified for one host must never be reused
        for a different host that happens to share the pinned IP."""
        return httpx.Client(
            headers={"User-Agent": self._ua},
            timeout=self._timeout,
            transport=self._transport,
            limits=httpx.Limits(max_keepalive_connections=0),
        )

    def get_public(
        self, url: str, headers: dict[str, str] | None = None, max_bytes: int = 2_000_000
    ) -> httpx.Response:
        """GET a URL that came from a feed: public hosts only, on every redirect hop.

        Feed items are untrusted, so a link must not reach localhost or your LAN. The
        host is resolved once, checked, and the connection goes to that exact address
        (TLS is still verified against the hostname), so DNS rebinding can't swap in a
        private address between the check and the request. Bodies are capped.
        """
        for _ in range(5):
            parts = urlsplit(url)
            ip = resolve_public_ip(url, self._resolve)
            if ip is None or not parts.hostname:
                raise SourceError("refused: not a public http(s) address", "blocked")
            self._wait_turn(parts.hostname)
            literal = f"[{ip}]" if ":" in ip else ip
            port = f":{parts.port}" if parts.port else ""
            pinned = urlunsplit((parts.scheme, literal + port, parts.path or "/", parts.query, ""))
            req_headers = {"Host": parts.netloc.rsplit("@", 1)[-1], **(headers or {})}
            ext = {"sni_hostname": parts.hostname} if parts.scheme == "https" else {}
            try:
                with (
                    self._fresh_client() as fresh,
                    fresh.stream(
                        "GET", pinned, headers=req_headers, extensions=ext, follow_redirects=False
                    ) as resp,
                ):
                    if resp.is_redirect and resp.headers.get("location"):
                        nxt = urljoin(url, resp.headers["location"])
                        if parts.scheme == "https" and urlsplit(nxt).scheme != "https":
                            raise SourceError("refused: redirect from https to http", "blocked")
                        url = nxt
                        continue
                    if resp.status_code >= 400:
                        raise SourceError(f"HTTP {resp.status_code}", "error")
                    body = bytearray()
                    for chunk in resp.iter_bytes():
                        body += chunk
                        if len(body) >= max_bytes:
                            break
                    ctype = resp.headers.get("content-type", "application/octet-stream")
                    return httpx.Response(
                        resp.status_code,
                        headers={"content-type": ctype},
                        content=bytes(body[:max_bytes]),
                    )
            except httpx.HTTPError as e:
                raise SourceError(f"network error ({type(e).__name__})", "network") from e
        raise SourceError("too many redirects", "error")

    def get_json(self, url: str, **kw: Any) -> Any:
        resp = self.get(url, **kw)
        try:
            return resp.json()
        except ValueError as e:
            raise SourceError("response was not JSON", "bad-data") from e


def resolve_public_ip(url: str, resolve: Any = socket.getaddrinfo) -> str | None:
    """The address to connect to, or None if the URL isn't http(s) to a public host.

    Every address the name resolves to must be public; one private answer refuses it.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        infos = resolve(parts.hostname, port)
    except (socket.gaierror, UnicodeError, OSError, ValueError):
        return None
    first: str | None = None
    for info in infos or []:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError:
            return None
        if not ip.is_global or ip.is_multicast or _tunnels_to_private(ip):
            return None
        first = first or str(ip)
    return first


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _tunnels_to_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """IPv6 forms that embed an IPv4 address: judge them by the address inside."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return False
    inner = ip.ipv4_mapped or ip.sixtofour
    if inner is None and ip in _NAT64:
        inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if inner is None and int(ip) >> 32 == 0:  # deprecated IPv4-compatible ::a.b.c.d
        inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return inner is not None and not inner.is_global


def is_public_url(url: str, resolve: Any = socket.getaddrinfo) -> bool:
    return resolve_public_ip(url, resolve) is not None


def _retry_after(resp: httpx.Response) -> float | None:
    val = resp.headers.get("retry-after")
    if val and val.strip().isdigit():
        return float(val.strip())
    return None


def _github_rate_limited(resp: httpx.Response) -> bool:
    return resp.headers.get("x-ratelimit-remaining") == "0"
