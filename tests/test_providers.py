from __future__ import annotations

import json
import logging
from collections.abc import Callable

import httpx
import pytest
from pydantic import BaseModel, Field

from night_scout.core import secrets
from night_scout.core.config import Config
from night_scout.core.llm import ModelError, ModelUnavailable
from night_scout.core.providers import (
    PRESETS,
    FallbackLLM,
    OpenAICompatClient,
    human_size,
    ollama_models,
    ollama_pull,
    parse_pull_line,
    preset,
    test_provider,
    valid_model_name,
)
from tests.fakes import FakeLLM

KEY = "sk-or-v1-0123456789abcdefSECRET"


class Answer(BaseModel):
    verdict: str
    score: float = Field(ge=0, le=1)


Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler, key: str | None = KEY, embed: str | None = None):
    seen: list[httpx.Request] = []

    def wrap(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    client = OpenAICompatClient(
        preset("openrouter"),
        "google/gemma-3-27b-it",
        api_key=key,
        embed_model=embed,
        transport=httpx.MockTransport(wrap),
    )
    return client, seen


def _reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _bodies(seen: list[httpx.Request]) -> list[dict]:
    return [json.loads(r.content) for r in seen if r.method == "POST"]


def test_json_schema_success_sends_response_format_and_bearer() -> None:
    client, seen = _client(lambda r: _reply('{"verdict": "yes", "score": 0.8}'))
    out = client.chat_json("sys", "user", Answer)
    assert out.score == 0.8
    [body] = _bodies(seen)
    rf = body["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["name"] == "Answer"
    assert rf["json_schema"]["schema"]["properties"]["score"]["maximum"] == 1
    assert seen[0].url.path == "/api/v1/chat/completions"
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in repr(client)


def test_unsupported_response_format_falls_back_to_schema_in_prompt() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if "response_format" in body:
            return httpx.Response(400, json={"error": "response_format json_schema not supported"})
        return _reply('```json\n{"verdict": "ok", "score": 0.5}\n```')

    client, seen = _client(handler)
    assert client.chat_json("sys", "user", Answer).verdict == "ok"
    bodies = _bodies(seen)
    assert len(bodies) == 2 and "response_format" not in bodies[1]
    assert "validates against this JSON schema" in bodies[1]["messages"][0]["content"]
    assert client.constrained is False
    # later calls skip straight to the prompt path
    client.chat_json("sys", "again", Answer)
    assert "response_format" not in _bodies(seen)[2]


def test_unrelated_400_is_a_model_error_not_a_fallback() -> None:
    client, seen = _client(lambda r: httpx.Response(400, text=f"context too long for {KEY}"))
    with pytest.raises(ModelError) as ei:
        client.chat_json("sys", "user", Answer)
    assert len(seen) == 1 and client.constrained
    assert KEY not in str(ei.value) and "••••" in str(ei.value)


def test_invalid_json_retries_once_then_model_error() -> None:
    replies = iter(["not json", '{"verdict": "x", "score": 7}'])
    client, seen = _client(lambda r: _reply(next(replies)))
    with pytest.raises(ModelError, match="after retry"):
        client.chat_json("sys", "user", Answer)
    bodies = _bodies(seen)
    assert len(bodies) == 2
    assert "didn't match the required JSON schema" in bodies[1]["messages"][-1]["content"]
    assert client.invalid == 2


def test_invalid_then_valid() -> None:
    replies = iter(['{"verdict": "x"}', '{"verdict": "x", "score": 0.1}'])
    client, _ = _client(lambda r: _reply(next(replies)))
    assert client.chat_json("sys", "user", Answer).score == 0.1


def test_auth_errors_and_unreachable_are_unavailable() -> None:
    client, _ = _client(lambda r: httpx.Response(401, json={"error": f"bad key {KEY}"}))
    with pytest.raises(ModelUnavailable, match="rejected the API key") as ei:
        client.chat_text("s", "u")
    assert KEY not in str(ei.value)

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=req)

    client, _ = _client(down)
    with pytest.raises(ModelUnavailable, match="not reachable"):
        client.chat_text("s", "u")
    with pytest.raises(ModelUnavailable):
        client.ensure_ready()


def test_chat_text_and_content_parts() -> None:
    client, _ = _client(
        lambda r: httpx.Response(
            200, json={"choices": [{"message": {"content": [{"type": "text", "text": "Hi"}]}}]}
        )
    )
    assert client.chat_text("s", "u") == "Hi"
    client, _ = _client(lambda r: _reply("   "))
    with pytest.raises(ModelError, match="empty"):
        client.chat_text("s", "u")


def test_embeddings_batched_and_ordered() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/embeddings")
        body = json.loads(req.content)
        assert body["model"] == "nomic-embed"
        n = len(body["input"])
        data = [{"index": i, "embedding": [float(i), 1.0]} for i in reversed(range(n))]
        return httpx.Response(200, json={"data": data})

    client, seen = _client(handler, embed="nomic-embed")
    assert client.has_embed_model()
    vecs = client.embed([f"t{i}" for i in range(40)])
    assert len(vecs) == 40 and vecs[0] == [0.0, 1.0] and vecs[33] == [1.0, 1.0]
    assert len(seen) == 2  # 32 + 8
    assert client.embed([]) == []
    no_embed, _ = _client(handler)
    assert not no_embed.has_embed_model()
    with pytest.raises(ModelError):
        no_embed.embed(["x"])


def test_embedding_count_mismatch() -> None:
    client, _ = _client(
        lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]}),
        embed="e",
    )
    with pytest.raises(ModelError, match="mismatch"):
        client.embed(["a", "b"])


def test_key_never_logged(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    replies = iter(["nope", '{"verdict": "x", "score": 0.1}'])
    client, _ = _client(lambda r: _reply(next(replies)))
    client.chat_json("s", "u", Answer)
    assert KEY not in caplog.text


# -- FallbackLLM -----------------------------------------------------------------
class _Down(FakeLLM):
    model_name = "primary"

    def __init__(self, exc: type[ModelError]) -> None:
        super().__init__()
        self.exc = exc
        self.tries = 0

    def chat_json(self, system, user, schema):
        self.tries += 1
        raise self.exc("primary down")

    def chat_text(self, system, user):
        self.tries += 1
        raise self.exc("primary down")


def test_fallback_llm_switches_for_good_when_primary_unavailable() -> None:
    primary = _Down(ModelUnavailable)
    fallback = FakeLLM({"Answer": lambda u: {"verdict": "fb", "score": 0.3}})
    llm = FallbackLLM(primary, fallback)
    assert llm.chat_json("s", "u", Answer).verdict == "fb"
    assert llm.model_name == "fake-gemma" and llm.switched
    llm.chat_json("s", "u2", Answer)
    assert primary.tries == 1  # not retried after the switch


def test_fallback_llm_retries_single_failures_on_fallback() -> None:
    primary = _Down(ModelError)
    llm = FallbackLLM(primary, FakeLLM())
    assert llm.chat_text("s", "u").startswith("## Brief")
    llm.chat_text("s", "u")
    assert primary.tries == 2 and not llm.switched


def test_fallback_llm_both_fail_raises_and_embeds_on_primary() -> None:
    llm = FallbackLLM(_Down(ModelUnavailable), _Down(ModelError))
    with pytest.raises(ModelError):
        llm.chat_text("s", "u")
    emb = FallbackLLM(FakeLLM(embed_fn=lambda t: [1.0]), FakeLLM())
    assert emb.has_embed_model() and emb.embed(["a"]) == [[1.0]]


def test_fallback_llm_ensure_ready() -> None:
    class Unready(FakeLLM):
        model_name = "big"

        def ensure_ready(self) -> str:
            raise ModelUnavailable("not installed")

    llm = FallbackLLM(Unready(), FakeLLM())
    assert llm.ensure_ready() == "fake-gemma" and llm.switched


# -- test_provider -----------------------------------------------------------------
def _or_cfg(**extra) -> Config:
    return Config.model_validate(
        {
            "providers": [PRESETS["openrouter"].model_dump(), PRESETS["lmstudio"].model_dump()],
            "roles": {"judge": "openrouter/google/gemma-3-27b-it", **extra},
        }
    )


def test_provider_test_happy_path_openrouter(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/models":
            return httpx.Response(200, json={"data": []})
        if req.url.path == "/api/v1/key":
            assert req.headers["authorization"] == f"Bearer {KEY}"
            return httpx.Response(200, json={"data": {"label": "x"}})
        if req.url.path == "/api/v1/chat/completions":
            assert json.loads(req.content)["model"] == "google/gemma-3-27b-it"
            return _reply("OK")
        raise AssertionError(req.url)

    steps = test_provider(_or_cfg(), "openrouter", transport=httpx.MockTransport(handler))
    assert [s.name for s in steps] == ["Health", "Auth", "Chat"]
    assert all(s.ok for s in steps), steps
    assert "sk-o••••••••CRET" in steps[1].detail and "env OPENROUTER_API_KEY" in steps[1].detail
    assert "'OK'" in steps[2].detail
    assert all(KEY not in s.detail for s in steps)


def test_provider_test_401_fails_auth_with_friendly_text() -> None:
    secrets.set_key("openrouter", KEY)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(401, json={"error": {"message": f"No auth credentials {KEY}"}})

    steps = test_provider(_or_cfg(), "openrouter", transport=httpx.MockTransport(handler))
    assert steps[0].ok and not steps[1].ok
    assert "rejected the API key (HTTP 401)" in steps[1].detail
    assert steps[2].name == "Chat" and not steps[2].ok and steps[2].detail.startswith("skipped")
    assert all(KEY not in s.detail for s in steps)


def test_provider_test_remote_without_key() -> None:
    steps = test_provider(
        _or_cfg(),
        "openrouter",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []})),
    )
    assert not steps[1].ok and "OPENROUTER_API_KEY" in steps[1].detail


def test_provider_test_unreachable_local_server() -> None:
    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    steps = test_provider(_or_cfg(), "lmstudio", transport=httpx.MockTransport(down))
    assert not steps[0].ok and "Is LM Studio running?" in steps[0].detail
    assert [s.name for s in steps] == ["Health", "Auth", "Chat"]


def test_provider_test_local_server_picks_first_model() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "qwen3-8b"}]})
        assert "authorization" not in req.headers
        return _reply("OK.")

    steps = test_provider(_or_cfg(), "lmstudio", transport=httpx.MockTransport(handler))
    assert all(s.ok for s in steps), steps
    assert "not needed" in steps[1].detail and "qwen3-8b" in steps[2].detail


def test_provider_test_ollama() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.12.3"})
        if req.url.path == "/api/chat":
            body = json.loads(req.content)
            assert body["model"] == "gemma4:12b" and body["options"]["num_predict"] == 16
            return httpx.Response(200, json={"message": {"content": "OK"}})
        raise AssertionError(req.url)

    steps = test_provider(Config(), "ollama", transport=httpx.MockTransport(handler))
    assert all(s.ok for s in steps), steps
    assert "0.12.3" in steps[0].detail and "gemma4:12b replied 'OK'" in steps[2].detail


def test_provider_test_ollama_model_missing() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.12.3"})
        return httpx.Response(404, json={"error": "model 'gemma4:12b' not found"})

    steps = test_provider(Config(), "ollama", transport=httpx.MockTransport(handler))
    assert not steps[2].ok and "isn't available" in steps[2].detail and "Pull" in steps[2].detail


# -- Ollama models and pull -----------------------------------------------------------
TAGS = {
    "models": [
        {
            "name": "gemma4:12b",
            "size": 8_100_000_000,
            "modified_at": "2026-10-01T10:00:00Z",
            "details": {
                "family": "gemma4",
                "parameter_size": "12.2B",
                "quantization_level": "Q4_K_M",
            },
        },
        {
            "name": "embeddinggemma-2:270m",
            "size": 621_000_000,
            "details": {"parameter_size": "270M", "quantization_level": "F16"},
        },
        {"bogus": True},
    ]
}


def test_ollama_models_parsing() -> None:
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=TAGS) if r.url.path == "/api/tags" else None  # type: ignore[arg-type,return-value]
    )
    models = ollama_models(Config(), transport=transport)
    assert [m.name for m in models] == ["embeddinggemma-2:270m", "gemma4:12b"]
    g = models[1]
    assert (g.size, g.size_bytes, g.parameter_size, g.quantization, g.family) == (
        "8.1 GB",
        8_100_000_000,
        "12.2B",
        "Q4_K_M",
        "gemma4",
    )
    assert models[0].size == "621.0 MB"


def test_ollama_models_unreachable() -> None:
    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(ModelUnavailable):
        ollama_models(Config(), transport=httpx.MockTransport(down))
    with pytest.raises(ValueError):
        ollama_models(_or_cfg(), "openrouter")


def test_human_size() -> None:
    assert human_size(512) == "512 B"
    assert human_size(1_500) == "1.5 KB"
    assert human_size(2_000_000_000_000) == "2.0 TB"


@pytest.mark.parametrize(
    "name",
    [
        "gemma4:12b",
        "llama3.2",
        "library/qwen3:8b",
        "hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M",
        "embeddinggemma-2:270m",
    ],
)
def test_valid_model_names(name: str) -> None:
    assert valid_model_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "-rf",
        "gemma4:12b; rm -rf ~",
        "../etc/passwd",
        "a/../b",
        "https://evil/x",
        "a b",
        "x:",
        ":tag",
        "a/b/c/d/e",
        "gemma\nx",
        "a" * 400,
    ],
)
def test_ollama_pull_rejects_bad_names_before_any_request(name: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(ValueError):
        ollama_pull(Config(), name, transport=httpx.MockTransport(handler))


def test_ollama_pull_streams_progress() -> None:
    lines = [
        {"status": "pulling manifest"},
        {"status": "pulling abc", "digest": "sha256:abc", "total": 200, "completed": 50},
        {"status": "pulling abc", "digest": "sha256:abc", "total": 200, "completed": 200},
        {"status": "verifying sha256 digest"},
        {"status": "success"},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/pull"
        assert json.loads(req.content) == {"model": "gemma4:e4b", "stream": True}
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines) + "\n\n")

    progress = list(ollama_pull(Config(), " gemma4:e4b ", transport=httpx.MockTransport(handler)))
    assert progress[0].status == "pulling manifest"
    assert progress[1].percent == 25.0 and progress[2].percent == 100.0
    assert progress[0].percent is None
    assert progress[-1].done and progress[-1].to_dict()["done"] is True


def test_ollama_pull_error_line() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content='{"status":"pulling manifest"}\n{"error":"file does not exist"}\n'
        )

    it = ollama_pull(Config(), "nope:1b", transport=httpx.MockTransport(handler))
    assert next(it).status == "pulling manifest"
    with pytest.raises(ModelError, match="file does not exist"):
        next(it)


def test_parse_pull_line() -> None:
    assert parse_pull_line("") is None
    assert parse_pull_line("garbage") is None
    p = parse_pull_line('{"status":"pulling x","total":10,"completed":5}')
    assert p is not None and p.percent == 50.0
