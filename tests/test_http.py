from __future__ import annotations

import httpx
import pytest

from night_scout.core.http import PoliteClient, SourceError


def _client(responses: list[httpx.Response]) -> tuple[PoliteClient, list[float]]:
    sleeps: list[float] = []
    it = iter(responses)
    client = PoliteClient(
        transport=httpx.MockTransport(lambda r: next(it)), sleep=sleeps.append, max_retries=2
    )
    return client, sleeps


def test_retries_then_succeeds_honoring_retry_after() -> None:
    client, sleeps = _client(
        [httpx.Response(429, headers={"retry-after": "3"}), httpx.Response(200, json={"ok": True})]
    )
    assert client.get_json("https://api.example/x") == {"ok": True}
    assert 3.0 in sleeps


def test_gives_up_after_retries() -> None:
    client, _ = _client([httpx.Response(503)] * 3)
    with pytest.raises(SourceError) as e:
        client.get("https://api.example/x")
    assert e.value.kind == "error"


def test_long_retry_after_is_reported_not_slept() -> None:
    client, sleeps = _client([httpx.Response(429, headers={"retry-after": "3600"})])
    with pytest.raises(SourceError) as e:
        client.get("https://api.example/x")
    assert e.value.kind == "rate-limited"
    assert 3600 not in sleeps


def test_blocked_and_github_rate_limit() -> None:
    client, _ = _client([httpx.Response(403)])
    with pytest.raises(SourceError) as e:
        client.get("https://www.reddit.com/r/x.json")
    assert e.value.kind == "blocked"
    client, _ = _client([httpx.Response(403, headers={"x-ratelimit-remaining": "0"})])
    with pytest.raises(SourceError) as e:
        client.get("https://api.github.com/x")
    assert e.value.kind == "rate-limited"


def test_not_json() -> None:
    client, _ = _client([httpx.Response(200, text="<html>")])
    with pytest.raises(SourceError) as e:
        client.get_json("https://api.example/x")
    assert e.value.kind == "bad-data"


def test_user_agent_is_descriptive() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200)

    PoliteClient(contact="me@example.com", transport=httpx.MockTransport(handler)).get(
        "https://x.example"
    )
    assert seen["ua"].startswith("NightScout/") and "me@example.com" in seen["ua"]


def test_is_public_url() -> None:
    from night_scout.core.http import is_public_url

    def fake(ip):
        return lambda host, port: [(None, None, None, None, (ip, port))]

    assert is_public_url("https://example.com/x", resolve=fake("93.184.215.14"))
    assert not is_public_url("http://router.local/", resolve=fake("192.168.1.1"))
    assert not is_public_url("http://x/", resolve=fake("127.0.0.1"))
    assert not is_public_url("http://x/", resolve=fake("169.254.169.254"))  # cloud metadata
    assert not is_public_url("http://x/", resolve=fake("::1"))
    assert not is_public_url("http://x/", resolve=fake("10.0.0.5"))
    assert not is_public_url("file:///etc/passwd", resolve=fake("93.184.215.14"))
    assert not is_public_url("javascript:alert(1)", resolve=fake("93.184.215.14"))


def test_get_public_checks_redirect_hops(monkeypatch) -> None:
    import night_scout.core.http as h

    monkeypatch.setattr(h, "is_public_url", lambda url, resolve=None: "internal" not in url)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://internal.lan/admin"})

    client = PoliteClient(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(SourceError, match="not a public"):
        client.get_public("https://public.example/x")
