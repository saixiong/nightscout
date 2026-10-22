from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable

import httpx
import pytest
from pydantic import BaseModel, Field

from night_scout.core import secrets
from night_scout.core.anthropic import AnthropicClient, to_anthropic, to_anthropic_tools
from night_scout.core.config import Config, ProviderConfig
from night_scout.core.llm import ModelError, ModelUnavailable, ToolCall
from night_scout.core.providers import (
    PRESETS,
    FallbackLLM,
    context_label,
    fetch_models,
    make_embedder,
    make_llm,
    model_entry,
    preset,
    test_provider,
)

KEY = "sk-ant-api03-0123456789abcdefSECRET"
FAKE = "http://127.0.0.1:9"  # never contacted: every test uses a MockTransport

Handler = Callable[[httpx.Request], httpx.Response]


class Answer(BaseModel):
    verdict: str
    score: float = Field(ge=0, le=1)


def _provider() -> ProviderConfig:
    return preset("anthropic").model_copy(update={"base_url": FAKE})


def _client(handler: Handler, key: str | None = KEY, model: str = "claude-haiku-4-5"):
    seen: list[httpx.Request] = []

    def wrap(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    client = AnthropicClient(_provider(), model, api_key=key, transport=httpx.MockTransport(wrap))
    return client, seen


def _msg(*blocks: dict, stop: str = "end_turn") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": list(blocks),
            "stop_reason": stop,
        },
    )


def _text(t: str) -> dict:
    return {"type": "text", "text": t}


def _err(status: int, kind: str, message: str) -> httpx.Response:
    return httpx.Response(
        status, json={"type": "error", "error": {"type": kind, "message": message}}
    )


def _bodies(seen: list[httpx.Request]) -> list[dict]:
    return [json.loads(r.content) for r in seen if r.method == "POST"]


# -- presets ------------------------------------------------------------------------------
def test_preset_table() -> None:
    want = {
        "anthropic": ("anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY"),
        "google": (
            "openai_compatible",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "GEMINI_API_KEY",
        ),
        "deepseek": ("openai_compatible", "https://api.deepseek.com", "DEEPSEEK_API_KEY"),
        "groq": ("openai_compatible", "https://api.groq.com/openai/v1", "GROQ_API_KEY"),
        "mistral": ("openai_compatible", "https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
        "xai": ("openai_compatible", "https://api.x.ai/v1", "XAI_API_KEY"),
        "openai": ("openai_compatible", "https://api.openai.com/v1", "OPENAI_API_KEY"),
        "openrouter": ("openai_compatible", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    }
    for pid, (kind, url, env) in want.items():
        p = PRESETS[pid]
        assert (p.id, p.kind, p.base_url, p.api_key_env) == (pid, kind, url, env)
        assert not p.is_local
    assert PRESETS["lmstudio"].is_local
    assert preset("anthropic") is not PRESETS["anthropic"]  # a copy, safe to mutate


# -- chat_json / chat_text -----------------------------------------------------------------
def test_chat_json_sends_messages_api_shape_and_parses_text() -> None:
    client, seen = _client(lambda r: _msg(_text('```json\n{"verdict": "yes", "score": 0.7}\n```')))
    out = client.chat_json("You judge.", "Item text", Answer)
    assert out == Answer(verdict="yes", score=0.7)
    req = seen[0]
    assert req.url.path == "/v1/messages" and req.method == "POST"
    assert req.headers["x-api-key"] == KEY
    assert req.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in req.headers
    [body] = _bodies(seen)
    assert body["model"] == "claude-haiku-4-5" and body["max_tokens"] > 0
    assert "temperature" not in body  # current Claude models only accept the default
    assert body["system"].startswith("You judge.") and '"score"' in body["system"]  # schema hint
    assert body["messages"] == [{"role": "user", "content": "Item text"}]


def test_chat_json_keeps_just_the_object_and_retries_once() -> None:
    replies = iter(
        [
            _msg(_text("Sure! Here it is: not json")),
            _msg(_text('Here you go: {"verdict": "no", "score": 0.1} Hope that helps.')),
        ]
    )
    client, seen = _client(lambda r: next(replies))
    assert client.chat_json("s", "u", Answer).verdict == "no"
    second = _bodies(seen)[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert "didn't match" in second[2]["content"]
    assert client.invalid == 1


def test_chat_json_gives_up_after_retry() -> None:
    client, _ = _client(lambda r: _msg(_text('{"verdict": "x", "score": 7}')))
    with pytest.raises(ModelError, match="after retry"):
        client.chat_json("s", "u", Answer)


def test_chat_text() -> None:
    client, seen = _client(lambda r: _msg(_text("  Hello "), _text("there.")))
    assert client.chat_text("sys", "hi", max_tokens=50) == "Hello there."
    assert _bodies(seen)[0]["max_tokens"] == 50
    empty, _ = _client(lambda r: _msg())
    with pytest.raises(ModelError, match="empty"):
        empty.chat_text("s", "u")


# -- chat_tools ----------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_items",
            "description": "Search last night's items",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    },
    {"type": "function", "function": {"name": "today"}},
]


def test_tools_translate_to_input_schema() -> None:
    out = to_anthropic_tools(TOOLS)
    assert out[0] == {
        "name": "search_items",
        "description": "Search last night's items",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }
    assert out[1]["input_schema"] == {"type": "object", "properties": {}}
    assert to_anthropic_tools([{"type": "function"}, "junk"]) == []  # type: ignore[list-item]


def test_chat_tools_round_trip_with_tool_result() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if len(body["messages"]) == 1:
            return _msg(
                _text("Let me look."),
                {
                    "type": "tool_use",
                    "id": "toolu_01A",
                    "name": "search_items",
                    "input": {"q": "mcp"},
                },
                stop="tool_use",
            )
        return _msg(_text("Two items mention MCP."))

    client, seen = _client(handler)
    messages: list[dict] = [
        {"role": "system", "content": "You are Night Scout."},
        {"role": "user", "content": "Anything on MCP?"},
    ]
    first = client.chat_tools(messages, TOOLS)
    assert first.content == "Let me look."
    assert first.tool_calls == [ToolCall("search_items", {"q": "mcp"}, "toolu_01A")]
    messages += [
        {"role": "assistant", "content": first.content, "tool_calls": first.tool_calls},
        {"role": "tool", "name": "search_items", "tool_call_id": "toolu_01A", "content": "2 hits"},
    ]
    second = client.chat_tools(messages, TOOLS)
    assert second.content == "Two items mention MCP." and second.tool_calls == []
    body = _bodies(seen)[1]
    assert body["system"] == "You are Night Scout."
    assert body["tools"][0]["name"] == "search_items"
    assert body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Anything on MCP?"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me look."},
                {
                    "type": "tool_use",
                    "id": "toolu_01A",
                    "name": "search_items",
                    "input": {"q": "mcp"},
                },
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_01A", "content": "2 hits"}],
        },
    ]


def test_tool_results_pair_by_name_and_merge_into_one_user_turn() -> None:
    calls = [ToolCall("a", {}, ""), ToolCall("b", {"x": 1}, "")]  # ids from a server without them
    system, out = to_anthropic(
        [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": calls},
            {"role": "tool", "name": "b", "content": "B done"},
            {"role": "tool", "name": "a", "content": ""},
            {"role": "user", "content": "thanks"},
        ]
    )
    assert system == ""
    assistant = out[1]["content"]
    assert [b["type"] for b in assistant] == ["tool_use", "tool_use"]  # no empty text block
    ida, idb = assistant[0]["id"], assistant[1]["id"]
    assert ida and idb and ida != idb
    assert out[2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": idb, "content": "B done"},
            {"type": "tool_result", "tool_use_id": ida, "content": "(empty)"},
            {"type": "text", "text": "thanks"},
        ],
    }


def test_dangling_tool_use_gets_a_placeholder_result() -> None:
    _, out = to_anthropic(
        [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "x", "tool_calls": [ToolCall("a", {}, "id-1")]},
            {"role": "user", "content": "never mind"},
        ]
    )
    assert out[2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "id-1",
        "content": "(no result)",
    }


def test_images_become_base64_image_blocks() -> None:
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 20).decode()
    jpg = base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 20).decode()
    client, seen = _client(lambda r: _msg(_text("A chart.")))
    reply = client.chat_tools(
        [{"role": "user", "content": "What's this?", "images": [png, jpg]}], []
    )
    assert reply.content == "A chart."
    body = _bodies(seen)[0]
    assert "tools" not in body
    blocks = body["messages"][0]["content"]
    assert blocks[0] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": png},
    }
    assert blocks[1]["source"]["media_type"] == "image/jpeg"
    assert blocks[2] == {"type": "text", "text": "What's this?"}


# -- errors ----------------------------------------------------------------------------------
def test_401_is_invalid_api_key_and_unavailable() -> None:
    client, _ = _client(lambda r: _err(401, "authentication_error", f"invalid x-api-key {KEY}"))
    with pytest.raises(ModelUnavailable, match="Invalid API key") as e:
        client.chat_text("s", "u")
    assert KEY not in str(e.value)


def test_429_and_5xx_are_model_errors_with_key_scrubbed() -> None:
    client, _ = _client(lambda r: _err(429, "rate_limit_error", f"slow down {KEY}"))
    with pytest.raises(ModelError) as e:
        client.chat_text("s", "u")
    assert not isinstance(e.value, ModelUnavailable)
    assert "429" in str(e.value) and KEY not in str(e.value) and "slow down" in str(e.value)
    for status, kind in ((500, "api_error"), (529, "overloaded_error")):
        client, _ = _client(lambda r, s=status, k=kind: _err(s, k, f"oops {KEY}"))
        with pytest.raises(ModelError) as e:
            client.chat_text("s", "u")
        assert not isinstance(e.value, ModelUnavailable)
        assert str(status) in str(e.value) and KEY not in str(e.value)


def test_404_model_and_403_are_unavailable() -> None:
    client, _ = _client(lambda r: _err(404, "not_found_error", "model: nope"))
    with pytest.raises(ModelUnavailable, match="not found"):
        client.chat_text("s", "u")
    client, _ = _client(lambda r: _err(403, "permission_error", "no"))
    with pytest.raises(ModelUnavailable, match="403"):
        client.chat_text("s", "u")


def test_redirects_are_not_followed() -> None:
    client, seen = _client(
        lambda r: httpx.Response(307, headers={"location": "https://evil.example/v1/messages"})
    )
    with pytest.raises(ModelError, match="redirect"):
        client.chat_text("s", "u")
    assert len(seen) == 1  # the key was not replayed to the new location


def test_unreachable_and_bad_shape() -> None:
    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    client, _ = _client(down)
    with pytest.raises(ModelUnavailable, match="not reachable"):
        client.chat_text("s", "u")
    client, _ = _client(lambda r: httpx.Response(200, json={"what": 1}))
    with pytest.raises(ModelError, match="unexpected response"):
        client.chat_text("s", "u")


def test_key_never_in_repr_or_logs(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    client, _ = _client(lambda r: _msg(_text("not json at all")))
    with pytest.raises(ModelError):
        client.chat_json("s", "u", Answer)
    assert KEY not in repr(client) and "key=set" in repr(client)
    assert KEY not in caplog.text


def test_ensure_ready_and_embeddings() -> None:
    client, seen = _client(lambda r: httpx.Response(200, json={"data": [], "has_more": False}))
    assert client.ensure_ready() == "claude-haiku-4-5"
    assert seen[0].url.path == "/v1/models" and seen[0].headers["x-api-key"] == KEY
    client.ensure_ready()
    assert len(seen) == 1  # checked once
    assert client.has_embed_model() is False
    with pytest.raises(ModelError, match="embedding"):
        client.embed(["x"])
    bad, _ = _client(lambda r: _err(401, "authentication_error", "bad"))
    with pytest.raises(ModelUnavailable, match="Invalid API key"):
        bad.ensure_ready()


# -- factories --------------------------------------------------------------------------------
def _cfg(**roles) -> Config:
    return Config.model_validate(
        {
            "providers": [{**_provider().model_dump(), "models": ["claude-haiku-4-5"]}],
            "roles": roles,
        }
    )


def test_make_llm_builds_anthropic_client_with_ollama_fallback(memory_keyring) -> None:
    secrets.set_key("anthropic", KEY)
    llm = make_llm(_cfg(chat="anthropic/claude-haiku-4-5"), "chat")
    assert isinstance(llm, FallbackLLM)
    assert isinstance(llm.primary, AnthropicClient)
    assert llm.primary._key is not None and llm.primary._key.get() == KEY
    assert llm.fallback.model_name == "gemma4:e4b"
    # judge stays on Ollama when only chat is cloud
    assert not isinstance(make_llm(_cfg(chat="anthropic/claude-haiku-4-5"), "judge"), FallbackLLM)


def test_env_var_beats_keychain(monkeypatch, memory_keyring) -> None:
    secrets.set_key("anthropic", KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env-0000000000")
    llm = make_llm(_cfg(judge="anthropic/claude-haiku-4-5"), "judge")
    assert isinstance(llm, FallbackLLM) and isinstance(llm.primary, AnthropicClient)
    assert llm.primary._key is not None and llm.primary._key.get() == "sk-ant-from-env-0000000000"


def test_embedder_on_anthropic_refuses() -> None:
    cfg = _cfg()
    cfg.roles.embed = "anthropic/claude-haiku-4-5"  # the UI refuses this; config could say it
    emb = make_embedder(cfg)
    assert emb.has_embed_model() is False


# -- live model list ---------------------------------------------------------------------------
def test_fetch_models_anthropic_pages_and_fields() -> None:
    pages = {
        None: {
            "data": [
                {
                    "type": "model",
                    "id": "claude-opus-5-5",
                    "display_name": "Claude Opus 5.5",
                    "max_input_tokens": 1_000_000,
                    "capabilities": {"image_input": {"supported": True}},
                }
            ],
            "has_more": True,
            "last_id": "claude-opus-5-5",
        },
        "claude-opus-5-5": {
            "data": [
                {"type": "model", "id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5"}
            ],
            "has_more": False,
            "last_id": "claude-haiku-4-5",
        },
    }
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=pages[req.url.params.get("after_id")])

    out = fetch_models(_provider(), KEY, transport=httpx.MockTransport(handler))
    assert out == [
        {"id": "claude-opus-5-5", "name": "Claude Opus 5.5", "context": "1M", "vision": True},
        {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5"},
    ]
    assert seen[0].url.params["limit"] == "1000"
    assert seen[0].headers["x-api-key"] == KEY and seen[0].headers["anthropic-version"]


def test_fetch_models_openai_compatible_variants() -> None:
    data = [
        {"id": "models/gemini-2.5-flash", "object": "model"},  # Gemini's prefix is dropped
        {
            "id": "openai/gpt-5",
            "name": "OpenAI: GPT-5",
            "context_length": 400000,
            "architecture": {"input_modalities": ["text", "image"]},
        },
        {"id": "llama-3.3-70b", "context_window": 131072},
        {"id": "mistral-small", "max_context_length": 32768, "capabilities": {"vision": True}},
        {"id": "has space"},
        {"id": ""},
        "junk",
    ]
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"object": "list", "data": data})

    out = fetch_models(preset("openrouter"), KEY, transport=httpx.MockTransport(handler))
    assert out == [
        {"id": "gemini-2.5-flash"},
        {"id": "openai/gpt-5", "name": "OpenAI: GPT-5", "context": "400K", "vision": True},
        {"id": "llama-3.3-70b", "context": "131K"},
        {"id": "mistral-small", "context": "33K", "vision": True},
    ]
    assert seen[0].url.path == "/api/v1/models"
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"


@pytest.mark.parametrize(
    ("status", "exc", "text"),
    [
        (401, ModelUnavailable, "rejected the API key"),
        (403, ModelUnavailable, "rejected the API key"),
        (404, ModelError, "doesn't list"),
        (302, ModelError, "redirect"),
        (500, ModelError, "HTTP 500"),
    ],
)
def test_fetch_models_errors(status: int, exc: type, text: str) -> None:
    t = httpx.MockTransport(lambda r: httpx.Response(status, text=f"boom {KEY}"))
    with pytest.raises(exc, match=text) as e:
        fetch_models(preset("openai"), KEY, transport=t)
    assert KEY not in str(e.value)


def test_fetch_models_anthropic_401_and_unreachable() -> None:
    t = httpx.MockTransport(lambda r: _err(401, "authentication_error", "invalid x-api-key"))
    with pytest.raises(ModelUnavailable, match="Invalid API key"):
        fetch_models(_provider(), KEY, transport=t)

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(ModelUnavailable, match="Can't reach"):
        fetch_models(_provider(), KEY, transport=httpx.MockTransport(down))


def test_model_entry_and_context_label() -> None:
    assert model_entry({"id": "x", "name": "x"}) == {"id": "x"}
    assert model_entry({"id": "a" * 201}) is None
    assert context_label(200_000) == "200K" and context_label(1_048_576) == "1M"
    assert context_label(2_000_000) == "2M" and context_label(400) == "1K"


# -- provider test (Health → Auth → Chat) on Anthropic ---------------------------------------------
def test_provider_test_anthropic(memory_keyring) -> None:
    secrets.set_key("anthropic", KEY)
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/v1/models":
            if "x-api-key" not in req.headers:
                return _err(401, "authentication_error", "x-api-key header is required")
            return httpx.Response(
                200, json={"data": [{"id": "claude-opus-5-5"}, {"id": "claude-haiku-4-5"}]}
            )
        assert req.url.path == "/v1/messages"
        return _msg(_text("OK"))

    cfg = Config.model_validate({"providers": [_provider().model_dump()]})
    steps = test_provider(cfg, "anthropic", transport=httpx.MockTransport(handler))
    assert [(s.name, s.ok) for s in steps] == [("Health", True), ("Auth", True), ("Chat", True)]
    assert "claude-haiku-4-5 replied 'OK'" in steps[2].detail  # cheapest listed model
    assert KEY not in json.dumps([s.to_dict() for s in steps])
    chat = json.loads(seen[-1].content)
    assert chat["max_tokens"] == 16 and chat["messages"] == [{"role": "user", "content": "Say OK."}]
    assert seen[-1].headers["anthropic-version"] == "2023-06-01"


def test_provider_test_anthropic_bad_key() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "x-api-key" in req.headers:
            return _err(401, "authentication_error", "invalid x-api-key")
        return _err(401, "authentication_error", "x-api-key header is required")

    cfg = Config.model_validate({"providers": [_provider().model_dump()]})
    steps = test_provider(cfg, "anthropic", api_key=KEY, transport=httpx.MockTransport(handler))
    assert [(s.name, s.ok) for s in steps] == [("Health", True), ("Auth", False), ("Chat", False)]
    assert "Invalid API key" in steps[1].detail


def test_provider_test_draft_key_and_offered_model_first() -> None:
    """test-draft passes an unsaved key; the first offered model is tried, not /models[0]."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "dall-e-3"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    p = ProviderConfig(id="draft", name="Box", base_url="https://llm.example.com/v1", models=["m1"])
    cfg = Config(providers=[p])
    steps = test_provider(
        cfg, "draft", api_key="sk-draft-123456789", transport=httpx.MockTransport(handler)
    )
    assert all(s.ok for s in steps), steps
    assert "from the form" in steps[1].detail and "sk-draft" not in steps[1].detail
    assert json.loads(seen[-1].content)["model"] == "m1"
    assert seen[-1].headers["authorization"] == "Bearer sk-draft-123456789"
    none = test_provider(cfg, "draft", api_key=None, transport=httpx.MockTransport(handler))
    assert not none[1].ok and "No API key" in none[1].detail


def test_provider_test_redirect_is_not_key_accepted() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.headers.get("authorization"):
            return httpx.Response(301, headers={"location": "https://elsewhere.example/"})
        return httpx.Response(200, json={"data": []})

    cfg = Config(providers=[ProviderConfig(id="r", base_url="https://llm.example.com/v1")])
    steps = test_provider(
        cfg, "r", api_key="sk-0123456789abc", transport=httpx.MockTransport(handler)
    )
    assert not steps[1].ok and "redirect" in steps[1].detail


def test_cloud_presets_never_count_as_local() -> None:
    """A loopback base_url on a cloud preset (a local proxy, or a test fake) still leaves the
    machine; LM Studio, custom loopback servers and Ollama stay local."""
    from night_scout.core.providers import runs_locally

    assert runs_locally(_provider()) is False  # anthropic → http://127.0.0.1:9
    assert runs_locally(preset("google").model_copy(update={"base_url": FAKE})) is False
    assert runs_locally(preset("lmstudio")) is True
    assert runs_locally(ProviderConfig(id="box", base_url="http://127.0.0.1:8080/v1")) is True
    assert runs_locally(ProviderConfig(id="far", base_url="https://llm.example.com/v1")) is False
    assert runs_locally(Config().provider("ollama")) is True
