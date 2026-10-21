from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel, Field

from night_scout.core.config import ModelConfig
from night_scout.core.llm import ModelError, ModelUnavailable, OllamaClient


class Answer(BaseModel):
    verdict: str
    score: float = Field(ge=0, le=1)


def _ollama(chat_replies: list[str], models=("gemma4:12b", "gemma4:e4b"), chat_status=200):
    requests: list[dict] = []
    replies = iter(chat_replies)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in models]})
        if request.url.path == "/api/chat":
            body = json.loads(request.content)
            requests.append(body)
            if chat_status != 200:
                return httpx.Response(chat_status, text="model failed to load")
            return httpx.Response(200, json={"message": {"content": next(replies)}})
        if request.url.path == "/api/embed":
            body = json.loads(request.content)
            return httpx.Response(200, json={"embeddings": [[1.0, 0.0] for _ in body["input"]]})
        raise AssertionError(request.url)

    return OllamaClient(ModelConfig(), transport=httpx.MockTransport(handler)), requests


def test_valid_json_first_try_uses_schema_format() -> None:
    client, reqs = _ollama(['{"verdict": "yes", "score": 0.8}'])
    out = client.chat_json("sys", "user", Answer)
    assert out.score == 0.8
    assert reqs[0]["format"]["properties"]["score"]["maximum"] == 1
    assert reqs[0]["model"] == "gemma4:12b" and reqs[0]["stream"] is False
    assert reqs[0]["think"] is False


def test_invalid_then_valid_retries_once_with_error() -> None:
    client, reqs = _ollama(['{"verdict": "yes", "score": 7}', '{"verdict": "yes", "score": 0.7}'])
    out = client.chat_json("sys", "user", Answer)
    assert out.score == 0.7
    assert len(reqs) == 2
    assert "didn't match the required JSON schema" in reqs[1]["messages"][-1]["content"]
    assert client.invalid == 1


def test_invalid_twice_raises_model_error() -> None:
    client, reqs = _ollama(["not json", '{"verdict": 1}'])
    with pytest.raises(ModelError, match="after retry"):
        client.chat_json("sys", "user", Answer)
    assert len(reqs) == 2


def test_fallback_when_default_not_installed() -> None:
    client, reqs = _ollama(['{"verdict": "ok", "score": 0}'], models=("gemma4:e4b",))
    client.chat_json("s", "u", Answer)
    assert reqs[0]["model"] == "gemma4:e4b"


def test_unavailable_when_no_model() -> None:
    client, _ = _ollama([], models=("llama3:8b",))
    with pytest.raises(ModelUnavailable, match="ollama pull gemma4:12b"):
        client.chat_json("s", "u", Answer)


def test_server_error_switches_to_fallback() -> None:
    client, _ = _ollama([], chat_status=500)
    with pytest.raises(ModelError):
        client.chat_json("s", "u", Answer)
    assert client.model_name == "gemma4:e4b"


def test_unreachable() -> None:
    def boom(request):
        raise httpx.ConnectError("refused")

    client = OllamaClient(ModelConfig(), transport=httpx.MockTransport(boom))
    with pytest.raises(ModelUnavailable, match="not reachable"):
        client.ensure_ready()
    assert client.has_embed_model() is False


def test_embed_batches() -> None:
    client, _ = _ollama([], models=("gemma4:12b", "embeddinggemma-2:270m"))
    assert client.has_embed_model()
    vecs = client.embed([f"t{i}" for i in range(40)])
    assert len(vecs) == 40


def test_unconstrained_build_falls_back_to_schema_in_prompt() -> None:
    bodies: list[dict] = []
    replies = iter(
        ['```json\n{"verdict": "ok", "score": 0.4}\n```', '{"verdict": "b", "score": 1}']
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma4:12b"}]})
        body = json.loads(request.content)
        bodies.append(body)
        if "format" in body:
            return httpx.Response(501, json={"error": "structured output is unavailable"})
        return httpx.Response(200, json={"message": {"content": next(replies)}})

    client = OllamaClient(ModelConfig(), transport=httpx.MockTransport(handler))
    assert client.chat_json("sys", "u", Answer).score == 0.4  # fences stripped, validated
    assert client.constrained is False
    assert '"score"' in bodies[1]["messages"][0]["content"]  # schema moved into the prompt
    client.chat_json("sys", "u", Answer)
    assert len(bodies) == 3  # no second 501 probe once we know
    assert "format" not in bodies[2]
