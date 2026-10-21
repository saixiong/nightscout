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
