"""Tool-calling chat in the LLM layer: Ollama native API, OpenAI-compatible, FallbackLLM."""

from __future__ import annotations

import json

import httpx
import pytest

from night_scout.core.config import ModelConfig, ProviderConfig
from night_scout.core.llm import ModelError, OllamaClient, ToolCall, ToolReply
from night_scout.core.providers import FallbackLLM, OpenAICompatClient

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_page",
            "description": "Read a page",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    }
]

HISTORY = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "What changed?"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [ToolCall("read_page", {"url": "https://e.example/a"}, "call_1")],
    },
    {"role": "tool", "name": "read_page", "tool_call_id": "call_1", "content": '{"text": "hi"}'},
]


def _ollama(replies: list[dict], status: int = 200):
    seen: list[dict] = []
    it = iter(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma4:12b"}]})
        if request.url.path == "/api/chat":
            seen.append(json.loads(request.content))
            if status != 200:
                return httpx.Response(status, text="boom")
            return httpx.Response(200, json={"message": next(it)})
        raise AssertionError(request.url)

    return OllamaClient(ModelConfig(), transport=httpx.MockTransport(handler)), seen


def test_ollama_chat_tools_sends_tools_with_thinking_off_and_parses_calls() -> None:
    client, seen = _ollama(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "read_page", "arguments": {"url": "https://x.example"}}}
                ],
            }
        ]
    )
    reply = client.chat_tools(HISTORY[:2], TOOLS)
    assert reply == ToolReply("", [ToolCall("read_page", {"url": "https://x.example"}, "")])
    body = seen[0]
    assert body["think"] is False and body["stream"] is False
    assert body["tools"] == TOOLS
    assert body["options"]["num_ctx"] == 16384
    assert "format" not in body


def test_ollama_chat_tools_converts_history_to_native_shape() -> None:
    client, seen = _ollama([{"role": "assistant", "content": "Done."}])
    reply = client.chat_tools(HISTORY, [])
    assert reply.content == "Done." and reply.tool_calls == []
    msgs = seen[0]["messages"]
    assert msgs[2]["tool_calls"] == [
        {"function": {"name": "read_page", "arguments": {"url": "https://e.example/a"}}}
    ]
    assert msgs[3] == {"role": "tool", "content": '{"text": "hi"}', "tool_name": "read_page"}
    assert "tools" not in seen[0]  # no tools: a plain final answer


def test_ollama_chat_tools_http_error_is_model_error() -> None:
    client, _ = _ollama([], status=500)
    with pytest.raises(ModelError, match="HTTP 500"):
        client.chat_tools(HISTORY[:2], TOOLS)


def _openai(replies: list[dict]):
    seen: list[dict] = []
    it = iter(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": next(it)}]})

    p = ProviderConfig(id="lm", kind="openai_compatible", base_url="http://127.0.0.1:1234/v1")
    return OpenAICompatClient(p, "gemma", transport=httpx.MockTransport(handler)), seen


def test_openai_chat_tools_parses_json_string_arguments() -> None:
    client, seen = _openai(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_9",
                        "type": "function",
                        "function": {"name": "read_page", "arguments": '{"url": "https://y.io"}'},
                    }
                ],
            }
        ]
    )
    reply = client.chat_tools(HISTORY[:2], TOOLS)
    assert reply.content == ""
    assert reply.tool_calls == [ToolCall("read_page", {"url": "https://y.io"}, "call_9")]
    assert seen[0]["tools"] == TOOLS


def test_openai_chat_tools_sends_tool_call_ids_and_string_arguments() -> None:
    client, seen = _openai([{"role": "assistant", "content": "Answer."}])
    assert client.chat_tools(HISTORY, TOOLS).content == "Answer."
    msgs = seen[0]["messages"]
    assert msgs[2]["content"] is None
    call = msgs[2]["tool_calls"][0]
    assert call["id"] == "call_1" and call["type"] == "function"
    assert json.loads(call["function"]["arguments"]) == {"url": "https://e.example/a"}
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_1", "content": '{"text": "hi"}'}


def test_garbage_tool_arguments_become_empty_dict() -> None:
    client, _ = _openai(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c", "function": {"name": "x", "arguments": "{nope"}}],
            }
        ]
    )
    assert client.chat_tools(HISTORY[:2], TOOLS).tool_calls == [ToolCall("x", {}, "c")]


class _Tools:
    def __init__(self, name: str, fail: bool) -> None:
        self.model_name = name
        self.fail = fail

    def chat_tools(self, messages, tools) -> ToolReply:
        if self.fail:
            raise ModelError("down")
        return ToolReply(f"from {self.model_name}")


def test_fallback_llm_chat_tools_tries_the_fallback() -> None:
    fb = FallbackLLM(_Tools("big", fail=True), _Tools("small", fail=False))  # type: ignore[arg-type]
    assert fb.chat_tools([], []).content == "from small"
    assert fb.model_name == "small"
