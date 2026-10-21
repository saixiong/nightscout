"""A polite HTTP client: descriptive User-Agent, per-host spacing, bounded retries."""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

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
        self._last: dict[str, float] = {}
        self._max_retries = max_retries
        self._sleep = sleep

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
    ) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        attempt = 0
        while True:
            self._wait_turn(host)
            try:
                resp = self._client.get(url, params=params, headers=headers)
            except httpx.TimeoutException as e:
                err: SourceError = SourceError(f"timed out ({host})", "network")
                if attempt >= self._max_retries:
                    raise err from e
            except httpx.HTTPError as e:
                err = SourceError(f"network error ({type(e).__name__})", "network")
                if attempt >= self._max_retries:
                    raise err from e
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code == 429 or resp.status_code >= 500:
                    kind = "rate-limited" if resp.status_code == 429 else "error"
                    err = SourceError(f"HTTP {resp.status_code}", kind)
                    if attempt >= self._max_retries:
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

    def get_public(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        """GET a URL that came from a feed: public hosts only, checked on every redirect.

        Feed items are untrusted, so a link must not reach localhost or your LAN.
        """
        for _ in range(5):
            if not is_public_url(url):
                raise SourceError("refused: not a public http(s) address", "blocked")
            host = urlsplit(url).hostname or ""
            self._wait_turn(host)
            try:
                resp = self._client.get(url, headers=headers, follow_redirects=False)
            except httpx.HTTPError as e:
                raise SourceError(f"network error ({type(e).__name__})", "network") from e
            if resp.is_redirect and resp.headers.get("location"):
                url = urljoin(url, resp.headers["location"])
                continue
            if resp.status_code >= 400:
                raise SourceError(f"HTTP {resp.status_code}", "error")
            return resp
        raise SourceError("too many redirects", "error")

    def get_json(self, url: str, **kw: Any) -> Any:
        resp = self.get(url, **kw)
        try:
            return resp.json()
        except ValueError as e:
            raise SourceError("response was not JSON", "bad-data") from e


def is_public_url(url: str, resolve: Any = socket.getaddrinfo) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    host = parts.hostname
    try:
        infos = resolve(host, parts.port or (443 if parts.scheme == "https" else 80))
    except (socket.gaierror, UnicodeError, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        except ValueError:
            return False
        if not ip.is_global or ip.is_multicast:
            return False
    return True


def _retry_after(resp: httpx.Response) -> float | None:
    val = resp.headers.get("retry-after")
    if val and val.strip().isdigit():
        return float(val.strip())
    return None


def _github_rate_limited(resp: httpx.Response) -> bool:
    return resp.headers.get("x-ratelimit-remaining") == "0"
