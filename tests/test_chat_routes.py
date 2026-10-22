"""Chat drawer and web-search settings routes."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from night_scout.core import secrets
from night_scout.core.config import Config, load_config, save_config
from night_scout.core.db import Store
from night_scout.core.llm import ToolCall, ToolReply
from night_scout.core.tools import BRAVE_KEY_ID, ToolContext
from night_scout.web import chat_routes
from night_scout.web.app import create_app

from .test_chat import ScriptLLM
from .test_chat_tools import resolver, seed_day

BASE = "http://127.0.0.1:8765"
KEY = "BSA-route-secret-0123456789"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    cfg = Config(data_dir=str(tmp_path / "data"))
    cfg.data_path().mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    save_config(cfg, config_path)
    store = Store(cfg.db_file())
    ids = seed_day(store)
    store.close()
    llm = ScriptLLM([])
    monkeypatch.setattr(chat_routes, "make_chat_llm", lambda _cfg: llm)
    transport: dict[str, httpx.MockTransport] = {}

    def tool_ctx(c: Config, s: Store) -> ToolContext:
        return ToolContext(cfg=c, store=s, resolve=resolver, transport=transport.get("t"))

    monkeypatch.setattr(chat_routes, "tool_context", tool_ctx)
    app = create_app(cfg, config_path, session_key="test-key")
    client = TestClient(app, base_url=BASE)
    page = client.get("/?key=test-key")
    token = page.text.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
    client.headers["X-CSRF-Token"] = token
    return client, llm, ids, config_path, transport


def wait(client: TestClient, thread: int) -> dict:
    for _ in range(200):
        data = client.get(f"/api/chat/{thread}").json()
        if data["status"] != "running":
            return data
        time.sleep(0.02)
    raise AssertionError("chat turn never finished")


def test_shell_loads_chat_js(env) -> None:
    client, *_ = env
    html = client.get("/").text
    assert '<script src="/static/chat.js?v=' in html
    assert html.index("setup.js") < html.index("chat.js")


def test_ask_runs_in_the_background_and_shows_tool_calls(env) -> None:
    client, llm, *_ = env
    llm.script = [
        ToolReply("", [ToolCall("search_tonight", {"query": "llama.cpp"})]),
        ToolReply("Ollama updated **llama.cpp**."),
    ]
    r = client.post(
        "/api/chat",
        json={"context": {"kind": "digest", "day": "2026-10-22"}, "question": "  What's new? "},
    )
    assert r.status_code == 200, r.text
    tid = r.json()["thread"]
    assert r.json()["turn"] == 1 and r.json()["status"] == "running"
    data = wait(client, tid)
    assert data["status"] == "done" and data["error"] is None and data["progress"] is None
    assert data["thread"]["context"] == {"kind": "digest", "day": "2026-10-22"}
    roles = [m["role"] for m in data["messages"]]
    assert roles == ["user", "tool", "assistant"]
    assert data["messages"][0]["content"] == "What's new?"
    assert data["messages"][1]["tool"]["name"] == "search_tonight"
    # a follow-up in the same thread is turn 2 and keeps the thread's context
    llm.script = [ToolReply("Still llama.cpp.")]
    r2 = client.post("/api/chat", json={"thread": tid, "question": "And?"})
    assert r2.json() == {"ok": True, "thread": tid, "turn": 2, "status": "running"}
    assert wait(client, tid)["messages"][-1]["content"] == "Still llama.cpp."


def test_item_context_and_meta(env) -> None:
    client, llm, ids, *_ = env
    meta = client.get(f"/api/chat/meta?kind=item&id={ids['a']}").json()
    assert meta["context"] == {
        "kind": "item",
        "id": ids["a"],
        "label": "Story · Ollama v0.40.9 released",
    }
    assert meta["model"]["model"] == "gemma4:12b" and meta["model"]["local"] is True
    assert meta["max_steps"] == 5
    tools = {t["name"]: t for t in meta["tools"]}
    assert tools["web_search"]["enabled"] is False and "Settings" in tools["web_search"]["why"]
    assert all(t["enabled"] for n, t in tools.items() if n != "web_search")
    digest = client.get("/api/chat/meta?kind=digest").json()["context"]
    assert digest["day"] == "2026-10-22" and digest["label"] == "Digest · Thu Oct 22 · 2 stories"
    llm.script = [ToolReply("It updates llama.cpp.")]
    r = client.post(
        "/api/chat", json={"context": {"kind": "item", "id": ids["a"]}, "question": "What changed?"}
    )
    assert wait(client, r.json()["thread"])["status"] == "done"


def test_model_failure_is_an_error_status(env) -> None:
    from night_scout.core.llm import ModelUnavailable

    client, llm, *_ = env
    llm.script = [ModelUnavailable("Ollama not reachable at http://127.0.0.1:11434")]
    r = client.post("/api/chat", json={"context": {"kind": "digest"}, "question": "hi"})
    data = wait(client, r.json()["thread"])
    assert data["status"] == "error"
    assert "isn't reachable" in data["messages"][-1]["content"]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"context": {"kind": "digest"}, "question": ""}, 400),
        ({"context": {"kind": "digest"}, "question": "   "}, 400),
        ({"context": {"kind": "digest"}, "question": "x" * 2001}, 400),
        ({"question": "no context"}, 400),
        ({"context": {"kind": "weird"}, "question": "q"}, 400),
        ({"context": {"kind": "digest", "day": "2026-13-45"}, "question": "q"}, 400),
        ({"context": {"kind": "digest", "day": "yesterday"}, "question": "q"}, 400),
        ({"context": {"kind": "item"}, "question": "q"}, 400),
        ({"context": {"kind": "item", "id": 999}, "question": "q"}, 404),
        ({"thread": 999, "question": "q"}, 404),
    ],
)
def test_ask_validates(env, body, status) -> None:
    client, *_ = env
    r = client.post("/api/chat", json=body)
    assert r.status_code == status, r.text
    assert "error" in r.json()


def test_unknown_thread_is_404(env) -> None:
    client, *_ = env
    assert client.get("/api/chat/12345").status_code == 404


def test_one_turn_at_a_time_per_thread(env, monkeypatch) -> None:
    import threading

    client, *_ = env
    gate = threading.Event()

    class Slow(ScriptLLM):
        def chat_tools(self, messages, tools):
            gate.wait(5)
            return ToolReply("done")

    slow = Slow([])
    monkeypatch.setattr(chat_routes, "make_chat_llm", lambda _cfg: slow)
    r = client.post("/api/chat", json={"context": {"kind": "digest"}, "question": "one"})
    tid = r.json()["thread"]
    busy = client.post("/api/chat", json={"thread": tid, "question": "two"})
    assert busy.status_code == 409
    running = client.get(f"/api/chat/{tid}").json()
    assert running["status"] == "running" and running["progress"]["stage"] == "thinking"
    gate.set()
    assert wait(client, tid)["status"] == "done"


def test_every_chat_and_search_write_needs_csrf(env) -> None:
    client, *_ = env
    bare = TestClient(client.app, base_url=BASE, cookies={"night_scout_session": "test-key"})
    writes = [
        ("POST", "/api/chat", {"context": {"kind": "digest"}, "question": "q"}),
        ("PUT", "/api/search", {"provider": "searxng", "searxng_url": "http://127.0.0.1:8888"}),
        ("PUT", "/api/search/brave-key", {"key": KEY}),
        ("DELETE", "/api/search/brave-key", None),
        ("POST", "/api/search/test", {}),
    ]
    for method, url, body in writes:
        for headers in ({}, {"X-CSRF-Token": "stale"}):
            r = bare.request(method, url, json=body, headers=headers)
            assert r.status_code == 403, (method, url)
        # a foreign Origin is refused even with the right token
        r = client.request(method, url, json=body, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403, (method, url)
    assert bare.get("/api/search").status_code == 200  # reads need no token
    assert secrets.get_key(BRAVE_KEY_ID) is None  # nothing got through
    assert load_config(env[3]).search.provider == "none"


def test_search_settings_save_validate_and_persist(env) -> None:
    client, _, _, config_path, _ = env
    assert client.get("/api/search").json()["provider"] == "none"
    for bad in ["ftp://127.0.0.1", "http://user:pw@127.0.0.1:8888", "not a url", "http://h/?q=x"]:
        r = client.put("/api/search", json={"provider": "searxng", "searxng_url": bad})
        assert r.status_code == 400, bad
    assert client.put("/api/search", json={"provider": "searxng"}).status_code == 400
    assert client.put("/api/search", json={"provider": "bing"}).status_code == 400
    assert client.put("/api/search", json={"provider": "brave"}).status_code == 400  # no key yet
    r = client.put(
        "/api/search", json={"provider": "searxng", "searxng_url": " http://127.0.0.1:8888/ "}
    )
    assert r.status_code == 200
    assert r.json()["searxng_url"] == "http://127.0.0.1:8888"
    on_disk = yaml.safe_load(config_path.read_text())["search"]
    assert on_disk == {"provider": "searxng", "searxng_url": "http://127.0.0.1:8888"}
    meta = client.get("/api/chat/meta").json()
    assert {t["name"]: t["enabled"] for t in meta["tools"]}["web_search"] is True


def test_brave_key_is_stored_masked_and_never_returned(env) -> None:
    client, _, _, config_path, _ = env
    r = client.put("/api/search/brave-key", json={"key": f"  {KEY}  "})
    assert r.status_code == 200
    assert secrets.get_key(BRAVE_KEY_ID) == KEY
    for resp in (r, client.get("/api/search")):
        text = resp.text
        assert KEY not in text and KEY[4:-4] not in text
        assert resp.json()["brave"] == {
            "configured": True,
            "source": "keychain",
            "masked": secrets.masked(KEY),
        }
    assert client.put("/api/search", json={"provider": "brave"}).status_code == 200
    assert KEY not in config_path.read_text()
    r = client.delete("/api/search/brave-key")
    assert r.status_code == 200 and r.json()["brave"] is None and r.json()["provider"] == "none"
    assert secrets.get_key(BRAVE_KEY_ID) is None
    assert client.put("/api/search/brave-key", json={"key": ""}).status_code == 400


def test_search_test_reports_results_or_a_clear_error(env) -> None:
    client, _, _, _, transport = env
    assert client.post("/api/search/test", json={}).status_code == 400  # off
    client.put("/api/search", json={"provider": "searxng", "searxng_url": "http://127.0.0.1:8899"})

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    transport["t"] = httpx.MockTransport(down)
    out = client.post("/api/search/test", json={}).json()
    assert out["ok"] is False
    assert out["error"] == (
        "Couldn't reach SearXNG at http://127.0.0.1:8899 (ConnectError). Is it running?"
    )

    def ok(request: httpx.Request) -> httpx.Response:
        results = [{"title": f"R{i}", "url": f"https://r.example/{i}"} for i in range(5)]
        return httpx.Response(200, json={"results": results})

    transport["t"] = httpx.MockTransport(ok)
    out = client.post("/api/search/test", json={"query": "gemma"}).json()
    assert out["ok"] is True and out["count"] == 5 and out["query"] == "gemma"
    assert out["results"][0] == {"title": "R0", "url": "https://r.example/0"}
    assert len(out["results"]) == 3


def test_chat_tables_live_outside_core_db(env, tmp_path: Path) -> None:
    import sqlite3

    client, *_ = env
    conn = sqlite3.connect(tmp_path / "data" / "scout.db")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"chat_threads", "chat_messages"} <= names
    assert json.loads(client.get("/api/bootstrap").text)["version"]
