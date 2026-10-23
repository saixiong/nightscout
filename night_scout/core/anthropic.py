"""Anthropic's native Messages API (POST /v1/messages), behind the same interface as
OpenAICompatClient: ensure_ready, chat_json, chat_text, chat_tools, embed.

The key only ever goes in the x-api-key header. It never appears in repr(), logs or
exception messages, and redirects are never followed (they would replay the header).
Night Scout parses JSON out of the reply text (schema in the prompt, then validation), so
no beta features are needed. `temperature` is never sent: current Claude models only accept
the default.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from . import secrets
from .config import ProviderConfig
from .llm import (
    ModelError,
    ModelUnavailable,
    ToolCall,
    ToolReply,
    _arguments,
    _short_error,
    _strip_fences,
    _with_schema_hint,
)

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

API_VERSION = "2023-06-01"
DEFAULT_BASE_URL = "https://api.anthropic.com"
JSON_MAX_TOKENS = 4096
TEXT_MAX_TOKENS = 2048
TOOLS_MAX_TOKENS = 2048

_ID = re.compile(r"[^A-Za-z0-9_-]")


def headers(key: str | None) -> dict[str, str]:
    h = {"Accept": "application/json", "anthropic-version": API_VERSION}
    if key:
        h["x-api-key"] = key
    return h


def error_detail(resp: httpx.Response) -> str:
    """The message from Anthropic's {"type": "error", "error": {"message": ...}} body."""
    try:
        err = resp.json().get("error") or {}
        msg = str(err.get("message") or "")
    except (ValueError, AttributeError):
        msg = ""
    return " ".join((msg or resp.text).split())[:200]


def _clean(e: Exception) -> Exception | None:
    """httpx errors carry the request (and so its headers); don't chain them as-is."""
    return None if isinstance(e, httpx.RequestError) else e


def _media_type(b64: str) -> str:
    """Images arrive as bare base64 (no data: prefix); sniff the type from the magic bytes."""
    try:
        head = base64.b64decode(b64[:16] + "=" * (-len(b64[:16]) % 4), validate=False)
    except ValueError:
        return "image/jpeg"
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _tool_id(raw: str) -> str:
    return _ID.sub("_", raw)[:64]


def to_anthropic(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Provider-neutral chat messages → (system, Anthropic messages).

    System turns become the top-level `system`. Assistant tool calls become tool_use blocks,
    tool results become tool_result blocks in the next user turn, and consecutive turns of
    one role are merged (the API wants them alternating). Tool results without an id are
    paired with the open tool_use of the same name, else the oldest open one.
    """
    system: list[str] = []
    out: list[dict[str, Any]] = []
    pending: list[tuple[str, str]] = []  # (tool_use id, name) still waiting for a result

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    def close_pending() -> None:
        push(
            "user",
            [
                {"type": "tool_result", "tool_use_id": tid, "content": "(no result)"}
                for tid, _name in pending
            ],
        )
        pending.clear()

    for i, m in enumerate(messages):
        role = m.get("role")
        text = str(m.get("content") or "")
        if role == "system":
            if text.strip():
                system.append(text)
            continue
        if role == "tool":
            tid = _tool_id(str(m.get("tool_call_id") or ""))
            name = str(m.get("name") or "")
            match = next((p for p in pending if p[0] == tid), None)
            match = match or next((p for p in pending if p[1] == name), None)
            match = match or (pending[0] if pending else None)
            if match is None:  # nothing to attach it to: pass the result along as text
                push("user", [{"type": "text", "text": f"[{name or 'tool'} result]\n{text}"}])
                continue
            pending.remove(match)
            push(
                "user",
                [{"type": "tool_result", "tool_use_id": match[0], "content": text or "(empty)"}],
            )
            continue
        if pending:
            close_pending()
        if role == "assistant":
            blocks: list[dict[str, Any]] = [{"type": "text", "text": text}] if text.strip() else []
            for n, tc in enumerate(m.get("tool_calls") or []):
                tid = _tool_id(tc.id) or f"toolu_{i}_{n}"
                pending.append((tid, tc.name))
                blocks.append(
                    {"type": "tool_use", "id": tid, "name": tc.name, "input": tc.arguments or {}}
                )
            push("assistant", blocks)
            continue
        blocks = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": _media_type(str(b)), "data": str(b)},
            }
            for b in (m.get("images") or [])
        ]
        if text.strip():
            blocks.append({"type": "text", "text": text})
        push("user", blocks)
    if pending:
        close_pending()
    return "\n\n".join(system), out


def to_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI-style function specs → Anthropic tool definitions."""
    out: list[dict[str, Any]] = []
    for t in tools:
        fn = t.get("function", t) if isinstance(t, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        schema = fn.get("parameters") or {"type": "object", "properties": {}}
        out.append(
            {
                "name": str(fn["name"]),
                "description": str(fn.get("description") or ""),
                "input_schema": schema,
            }
        )
    return out


def reply_text(data: dict[str, Any]) -> str:
    return "".join(
        str(b.get("text") or "")
        for b in data.get("content") or []
        if isinstance(b, dict) and b.get("type") == "text"
    )


def reply_tool_calls(data: dict[str, Any]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for b in data.get("content") or []:
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name"):
            calls.append(
                ToolCall(
                    name=str(b["name"]),
                    arguments=_arguments(b.get("input")),
                    id=str(b.get("id") or ""),
                )
            )
    return calls


def _json_text(text: str) -> str:
    """Fences off; if the model wrote a sentence around the object, keep just the object."""
    t = _strip_fences(text)
    if not t.startswith("{") and "{" in t and "}" in t:
        t = t[t.find("{") : t.rfind("}") + 1]
    return t


class AnthropicClient:
    """The LLM protocol over Anthropic's Messages API. No embeddings."""

    def __init__(
        self,
        provider: ProviderConfig,
        model: str,
        *,
        api_key: str | None = None,
        timeout_s: float = 300.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.provider = provider
        self.model_name = model
        self._key = secrets.Secret(api_key) if api_key else None
        self._http = httpx.Client(
            base_url=provider.base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            transport=transport,
            follow_redirects=False,  # never replay x-api-key elsewhere
        )
        self._checked = False
        self.calls = 0
        self.invalid = 0

    def __repr__(self) -> str:
        return (
            f"AnthropicClient(provider={self.provider.id!r}, model={self.model_name!r}, "
            f"key={'set' if self._key else 'none'})"
        )

    def close(self) -> None:
        self._http.close()

    # -- plumbing -----------------------------------------------------------
    def _scrub(self, text: str) -> str:
        return secrets.scrub(text, self._key.get() if self._key else None)

    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        label = self.provider.label
        key = self._key.get() if self._key else None
        try:
            return self._http.request(method, path, headers=headers(key), **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise ModelUnavailable(
                f"{label} not reachable at {self.provider.base_url}"
            ) from _clean(e)
        except httpx.HTTPError as e:
            raise ModelError(f"{label} request failed: {type(e).__name__}") from _clean(e)

    def _check(self, resp: httpx.Response, model: str) -> None:
        code = resp.status_code
        if code < 300:
            return
        label = self.provider.label
        if code < 400:
            raise ModelError(f"{label} answered with a redirect (HTTP {code}); not following it")
        detail = self._scrub(error_detail(resp))
        if code == 401:
            raise ModelUnavailable(
                f"{label}: Invalid API key (HTTP 401). Check the key in Settings → AI Providers."
            )
        if code == 403:
            raise ModelUnavailable(
                f"{label}: this key isn't allowed to do that (HTTP 403). {detail}"
            )
        if code == 404:
            raise ModelUnavailable(f"{label}: model {model} not found (HTTP 404)")
        if code == 429:
            raise ModelError(f"{label}: rate limit or spend limit reached (HTTP 429). {detail}")
        if code == 529:
            raise ModelError(f"{label} is overloaded right now (HTTP 529). Try again later.")
        raise ModelError(f"{label} HTTP {code}: {detail}")

    # -- setup --------------------------------------------------------------
    def ensure_ready(self) -> str:
        """Fail fast when the server is down or refuses the key."""
        if self._checked:
            return self.model_name
        try:
            resp = self._request("GET", "/v1/models", params={"limit": 1}, timeout=15)
        except ModelUnavailable:
            raise
        except ModelError as e:  # slow /v1/models: let the real calls decide
            log.info("%s readiness check inconclusive: %s", self.provider.label, e)
            self._checked = True
            return self.model_name
        if resp.status_code in (401, 403):
            self._check(resp, self.model_name)
        self._checked = True
        return self.model_name

    def has_embed_model(self) -> bool:
        return False

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise ModelError(f"{self.provider.label} has no embedding models; embeddings stay local")

    # -- calls --------------------------------------------------------------
    def _messages(
        self,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools
        self.calls += 1
        resp = self._request("POST", "/v1/messages", json=body)
        self._check(resp, self.model_name)
        try:
            data = resp.json()
            if not isinstance(data, dict) or not isinstance(data.get("content"), list):
                raise TypeError("no content list")
        except (ValueError, TypeError) as e:
            raise ModelError(f"unexpected response shape from {self.provider.label}") from e
        return data

    def chat_json(self, system: str, user: str, schema: type[T]) -> T:
        """Ask for JSON matching `schema` (schema in the prompt); retry once; else raise."""
        hinted = _with_schema_hint(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            schema.model_json_schema(),
        )
        convo: list[dict[str, Any]] = [{"role": "user", "content": user}]
        last_error = ""
        for attempt in range(2):
            raw = _json_text(
                reply_text(self._messages(hinted[0]["content"], convo, JSON_MAX_TOKENS))
            )
            try:
                return schema.model_validate(json.loads(raw))
            except (json.JSONDecodeError, ValidationError) as e:
                self.invalid += 1
                last_error = _short_error(e)
                log.info(
                    "invalid JSON from %s (attempt %d): %s",
                    self.model_name,
                    attempt + 1,
                    last_error,
                )
                convo = [
                    *convo,
                    {"role": "assistant", "content": raw[:4000] or "(empty)"},
                    {
                        "role": "user",
                        "content": "That didn't match the required JSON schema: "
                        f"{last_error}. Reply again with only valid JSON for the schema.",
                    },
                ]
        raise ModelError(f"invalid structured output after retry: {last_error}")

    def chat_text(self, system: str, user: str, max_tokens: int | None = None) -> str:
        data = self._messages(
            system, [{"role": "user", "content": user}], max_tokens or TEXT_MAX_TOKENS
        )
        text = reply_text(data).strip()
        if not text:
            raise ModelError("empty response")
        return text

    def chat_tools(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ToolReply:
        """One turn of a chat with tools: tool_use / tool_result blocks both ways."""
        system, convo = to_anthropic(messages)
        data = self._messages(system, convo, TOOLS_MAX_TOKENS, to_anthropic_tools(tools))
        return ToolReply(content=reply_text(data), tool_calls=reply_tool_calls(data))
