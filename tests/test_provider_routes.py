"""Settings → AI Providers routes: live model lists, chat role, custom provider edit/remove,
probe-models and test-draft. Upstream servers are MockTransports; no network, no real keys."""

from __future__ import annotations

import json

import httpx
import pytest
import yaml

from night_scout.core import providers as prov
from night_scout.core import secrets
from night_scout.web import app as web

from . import test_web

env = test_web.env  # fixtures shared with test_web.py
offline = test_web.offline

KEY = "sk-ant-api03-ROUTES-0123456789abcdef"
CUSTOM_KEY = "sk-custom-0123456789abcdef"


@pytest.fixture
def upstream(monkeypatch):
    """Every provider call the routes make goes to one fake server; returns the request log."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        host, path = req.url.host, req.url.path
        auth = req.headers.get("authorization", "")
        if host == "api.anthropic.com":
            if req.headers.get("x-api-key") != KEY:
                return httpx.Response(
                    401, json={"type": "error", "error": {"message": "invalid x-api-key"}}
                )
            if path == "/v1/models":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5"},
                            {"id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5"},
                        ],
                        "has_more": False,
                    },
                )
            return httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}]})
        if host == "llm.example.com":
            if auth != f"Bearer {CUSTOM_KEY}":
                return httpx.Response(401, text="no")
            if path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "qwen3"}, {"id": "gemma-4"}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
        if host in ("other.example.com", "a.example"):
            if path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "m"}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
        if host == "127.0.0.1" and req.url.port == 8080:
            if path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "local-gemma"}]})
            return httpx.Response(500, text="model crashed")
        raise AssertionError(f"unexpected upstream request: {req.url}")

    t = httpx.MockTransport(handler)
    real_fetch, real_test = prov.fetch_models, prov.test_provider
    monkeypatch.setattr(web, "fetch_models", lambda p, k: real_fetch(p, k, transport=t))
    monkeypatch.setattr(
        web, "test_provider", lambda c, pid, **kw: real_test(c, pid, transport=t, **kw)
    )
    return seen


def _cfg(config_path) -> dict:
    return yaml.safe_load(config_path.read_text())


def _prov(client, pid: str) -> dict:
    return next(p for p in client.get("/api/providers").json()["providers"] if p["id"] == pid)


# -- live models -----------------------------------------------------------------------------
def test_anthropic_key_models_offer_and_chat_role(env, offline, upstream) -> None:
    client, *_rest, config_path = env
    assert client.get("/api/providers/anthropic/models").status_code == 400  # no key yet
    assert client.put("/api/keys/anthropic", json={"key": KEY}).json()["ok"]
    assert KEY not in config_path.read_text()
    a = _prov(client, "anthropic")
    assert a["kind"] == "anthropic" and a["local"] is False and a["key"]["configured"]
    assert a["key"]["masked"] == "sk-a••••••••cdef"

    models = client.get("/api/providers/anthropic/models").json()["models"]
    assert models == [
        {"id": "claude-opus-5-5", "name": "Claude Opus 5.5", "offered": False},
        {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5", "offered": False},
    ]
    r = client.put("/api/providers/anthropic/models", json={"models": ["claude-haiku-4-5"]})
    assert r.json() == {"ok": True, "models": ["claude-haiku-4-5"]}
    models = client.get("/api/providers/anthropic/models").json()["models"]
    assert [m["offered"] for m in models] == [False, True]
    assert [m["name"] for m in _prov(client, "anthropic")["models"]] == ["claude-haiku-4-5"]

    roles = client.get("/api/providers").json()["roles"]
    assert roles["chat"] == ""  # follows the Judge
    r = client.put("/api/roles", json={"chat": "anthropic/claude-haiku-4-5"})
    assert r.status_code == 200 and r.json()["roles"]["chat"] == "anthropic/claude-haiku-4-5"
    assert r.json()["roles"]["judge"].startswith("ollama/")  # judging stays local
    assert _cfg(config_path)["roles"]["chat"] == "anthropic/claude-haiku-4-5"
    labels = {m["name"]: m["role"] for m in _prov(client, "anthropic")["models"]}
    assert labels["claude-haiku-4-5"] == "Chat"
    assert client.get("/api/chat/meta").json()["model"]["local"] is False

    steps = client.post("/api/providers/anthropic/test").json()["steps"]
    assert [s["ok"] for s in steps] == [True, True, True]
    assert "claude-haiku-4-5 replied 'OK'" in steps[2]["detail"]
    assert KEY not in json.dumps(steps)

    # back to "Same as Judge"
    assert client.put("/api/roles", json={"chat": ""}).json()["roles"]["chat"] == ""
    assert "chat" not in (_cfg(config_path).get("roles") or {})


def test_models_offer_validation_and_legacy_append(env, offline, upstream) -> None:
    client, *_ = env
    bad = client.put("/api/providers/openai/models", json={"models": ["ok", "has space"]})
    assert bad.status_code == 400 and "doesn't look right" in bad.json()["error"]
    assert client.put("/api/providers/ollama/models", json={"models": []}).status_code == 400
    assert client.put("/api/providers/nope/models", json={"models": []}).status_code == 404
    # LM Studio's old {model} append still works, and {models: []} clears
    assert client.put("/api/providers/lmstudio/models", json={"model": "a"}).json()["ok"]
    r = client.put("/api/providers/lmstudio/models", json={"model": "b"})
    assert r.json()["models"] == ["a", "b"]
    assert client.put("/api/providers/lmstudio/models", json={}).status_code == 400
    r = client.put("/api/providers/lmstudio/models", json={"models": [" b ", "b", ""]})
    assert r.json()["models"] == ["b"]
    assert client.get("/api/providers/ollama/models").status_code == 400
    assert client.get("/api/providers/nope/models").status_code == 404


def test_live_models_upstream_errors_are_502_with_scrubbed_text(env, offline, upstream) -> None:
    client, *_ = env
    secrets.set_key("anthropic", "sk-ant-wrong-key-000000000")
    r = client.get("/api/providers/anthropic/models")
    assert r.status_code == 502 and "Invalid API key" in r.json()["error"]
    assert "sk-ant-wrong" not in r.text


def test_anthropic_on_a_loopback_url_still_leaves_the_machine(env, offline) -> None:
    client, _t, _ids, _cfg0, _proj, config_path = env
    data = _cfg(config_path)
    data["providers"] = [
        {"id": "anthropic", "kind": "anthropic", "base_url": "http://127.0.0.1:9", "models": ["m"]}
    ]
    config_path.write_text(yaml.safe_dump(data))
    secrets.set_key("anthropic", KEY)
    r = client.put("/api/roles", json={"chat": "anthropic/m"})  # loads config.yaml from disk
    assert r.status_code == 200
    assert _prov(client, "anthropic")["local"] is False
    assert client.get("/api/chat/meta").json()["model"]["local"] is False


def test_preset_with_env_key_shows_before_saving(env, offline, monkeypatch) -> None:
    client, *_ = env
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-env-0123456789abcdef")
    g = _prov(client, "google")
    assert g["key"]["source"] == "env" and g["key"]["masked"].startswith("AIza")
    assert g["configured"] is False and g["name"] == "Google Gemini"


# -- roles --------------------------------------------------------------------------------------
def test_chat_role_validation(env, offline) -> None:
    client, *_ = env
    assert client.put("/api/roles", json={"chat": "nope/x"}).status_code == 400
    assert client.put("/api/roles", json={"chat": "garbage"}).status_code == 400
    ok = client.put("/api/roles", json={"chat": "ollama/gemma4:e4b"})
    assert ok.json()["roles"]["chat"] == "ollama/gemma4:e4b"
    secrets.set_key("anthropic", KEY)
    client.put("/api/providers/anthropic/models", json={"models": ["claude-haiku-4-5"]})
    r = client.put("/api/roles", json={"embed": "anthropic/claude-haiku-4-5"})
    assert r.status_code == 400 and "stay on this machine" in r.json()["error"]


def test_removing_a_key_resets_chat_to_follow_the_judge(env, offline) -> None:
    client, *_ = env
    client.put("/api/keys/anthropic", json={"key": KEY})
    client.put("/api/providers/anthropic/models", json={"models": ["claude-haiku-4-5"]})
    ref = "anthropic/claude-haiku-4-5"
    client.put("/api/roles", json={"chat": ref, "judge": ref})
    assert client.delete("/api/keys/anthropic").json()["ok"]
    roles = client.get("/api/providers").json()["roles"]
    assert roles["chat"] == "" and roles["judge"].startswith("ollama/")


# -- custom providers ------------------------------------------------------------------------------
def test_custom_provider_probe_draft_add_edit_remove(env, offline, upstream) -> None:
    client, *_rest, config_path = env
    url = "https://llm.example.com/v1"
    # Fetch models before saving
    r = client.post("/api/providers/probe-models", json={"base_url": url, "api_key": CUSTOM_KEY})
    assert r.json() == {"models": [{"id": "qwen3"}, {"id": "gemma-4"}]}
    r = client.post("/api/providers/probe-models", json={"base_url": url})
    assert r.status_code == 502 and "rejected the API key" in r.json()["error"]
    # test-draft: a failing test saves nothing
    before = config_path.read_text()
    body = {
        "name": "Gateway",
        "base_url": url,
        "api_key": "sk-wrong-000000000",
        "models": ["qwen3"],
    }
    steps = client.post("/api/providers/test-draft", json=body).json()["steps"]
    assert [s["ok"] for s in steps] == [True, False, False]
    assert config_path.read_text() == before
    assert secrets.key_source("draft-test") is None
    body["api_key"] = CUSTOM_KEY
    steps = client.post("/api/providers/test-draft", json=body).json()["steps"]
    assert all(s["ok"] for s in steps) and "qwen3 replied" in steps[2]["detail"]
    assert config_path.read_text() == before and CUSTOM_KEY not in json.dumps(steps)
    # then save (the sheet sends the models list)
    r = client.post("/api/providers", json={**body, "models": ["qwen3", "gemma-4"]})
    pid = r.json()["id"]
    assert pid == "gateway" and CUSTOM_KEY not in config_path.read_text()
    p = _prov(client, pid)
    assert [m["name"] for m in p["models"]] == ["qwen3", "gemma-4"] and p["local"] is False
    assert p["key"]["configured"]

    # Edit: blank key keeps the stored one (probe and test-draft use it too, by id)
    r = client.post("/api/providers/probe-models", json={"base_url": url, "id": pid})
    assert r.status_code == 200
    draft = {"name": "Gateway 2", "base_url": url, "models": ["gemma-4"], "id": pid}
    steps = client.post("/api/providers/test-draft", json=draft).json()["steps"]
    assert all(s["ok"] for s in steps)
    r = client.put(f"/api/providers/{pid}", json={**draft, "api_key": ""})
    assert r.json() == {"ok": True, "id": pid, "key_dropped": False}
    p = _prov(client, pid)
    assert p["name"] == "Gateway 2" and [m["name"] for m in p["models"]] == ["gemma-4"]
    assert secrets.get_key(pid) == CUSTOM_KEY

    # A role on it, then remove: provider, key and role all go
    assert client.put("/api/roles", json={"judge": f"{pid}/gemma-4"}).status_code == 200
    assert client.put("/api/roles", json={"chat": f"{pid}/gemma-4"}).status_code == 200
    assert client.delete(f"/api/providers/{pid}").json()["ok"]
    assert pid not in [p["id"] for p in client.get("/api/providers").json()["providers"]]
    assert secrets.key_source(pid) is None
    roles = client.get("/api/providers").json()["roles"]
    assert roles["judge"].startswith("ollama/") and roles["chat"] == ""
    assert pid not in config_path.read_text()
    assert client.delete(f"/api/providers/{pid}").status_code == 404


def test_changed_host_never_gets_the_stored_key(env, offline, upstream) -> None:
    client, *_ = env
    r = client.post(
        "/api/providers",
        json={"name": "Gw", "base_url": "https://llm.example.com/v1", "api_key": CUSTOM_KEY},
    )
    pid = r.json()["id"]
    elsewhere = "https://other.example.com/v1"
    upstream.clear()
    client.post("/api/providers/probe-models", json={"base_url": elsewhere, "id": pid})
    client.post(
        "/api/providers/test-draft",
        json={"name": "Gw", "base_url": elsewhere, "models": ["m"], "id": pid},
    )
    assert upstream and all("authorization" not in r.headers for r in upstream)
    r = client.put(f"/api/providers/{pid}", json={"name": "Gw", "base_url": elsewhere})
    assert r.json()["key_dropped"] is True
    assert secrets.key_source(pid) is None


def test_custom_provider_validation(env, offline, upstream) -> None:
    client, *_ = env
    for bad_url, why in [
        ("ftp://x", "http:// or https://"),
        ("http://example.com/v1", "https://"),
        ("https://user:pw@example.com/v1", "credentials"),
        ("https://example.com/v1?key=1", "query string"),
    ]:
        for path, body in [
            ("/api/providers/probe-models", {"base_url": bad_url}),
            ("/api/providers/test-draft", {"name": "x", "base_url": bad_url}),
            ("/api/providers", {"name": "x", "base_url": bad_url}),
        ]:
            r = client.post(path, json=body)
            assert r.status_code == 400 and why in r.json()["error"], (path, bad_url, r.text)
    r = client.post(
        "/api/providers/test-draft", json={"name": " ", "base_url": "https://a.example"}
    )
    assert r.status_code == 400 and "name" in r.json()["error"]
    r = client.post(
        "/api/providers/test-draft",
        json={"name": "x", "base_url": "https://a.example", "models": ["a b"]},
    )
    assert r.status_code == 400
    assert client.post("/api/providers/test-draft", json={}).status_code == 400  # missing fields
    # edit/delete only for custom providers
    r = client.put("/api/providers/anthropic", json={"name": "x", "base_url": "https://a.example"})
    assert r.status_code == 404
    assert (
        client.put(
            "/api/providers/ollama", json={"name": "x", "base_url": "https://a.example"}
        ).status_code
        == 404
    )
    assert client.delete("/api/providers/ollama").status_code == 400
    assert client.delete("/api/providers/nope").status_code == 404
    r = client.post(
        "/api/providers/test-draft",
        json={"name": "x", "base_url": "https://a.example", "id": "openai"},
    )
    assert r.status_code == 200  # "openai" isn't configured: tested as a new draft


def test_loopback_http_draft_failing_chat(env, offline, upstream) -> None:
    client, *_ = env
    r = client.post(
        "/api/providers/test-draft",
        json={"name": "box", "base_url": "http://127.0.0.1:8080/v1", "models": []},
    )
    steps = r.json()["steps"]
    assert [s["name"] for s in steps] == ["Health", "Auth", "Chat"]
    assert steps[0]["ok"] and steps[1]["ok"] and not steps[2]["ok"]  # local-gemma: HTTP 500
    assert "500" in steps[2]["detail"]


def test_reserved_ids_are_never_provider_ids(env, offline) -> None:
    client, *_ = env
    r = client.post(
        "/api/providers", json={"name": "Brave search", "base_url": "https://b.example"}
    )
    assert r.json()["id"] != "brave-search"
    r = client.post(
        "/api/providers", json={"name": "YouTube data API", "base_url": "https://y.example"}
    )
    assert r.json()["id"] != "youtube-data-api"
