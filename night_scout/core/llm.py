"""Local model access through Ollama. Every judging call uses structured JSON output.

Contract: a call either returns a validated pydantic object or raises ModelError.
Callers log and skip on ModelError; a bad answer is never fatal to the run.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import ModelConfig

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class ModelError(Exception):
    pass


class ModelUnavailable(ModelError):
    """Ollama isn't reachable or neither configured model is installed."""


class LLM(Protocol):
    model_name: str

    def chat_json(self, system: str, user: str, schema: type[T]) -> T: ...

    def chat_text(self, system: str, user: str) -> str: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def has_embed_model(self) -> bool: ...


@dataclass
class ToolCall:
    """One function call the model asked for. `id` is empty when the server doesn't send one."""

    name: str
    arguments: dict[str, Any]
    id: str = ""


@dataclass
class ToolReply:
    """A tool-calling chat turn, the same shape for every provider."""

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)


class ToolChat(Protocol):
    """Clients that can run a multi-turn chat with tools (the chat drawer).

    Messages are provider-neutral dicts: {"role": "system"|"user"|"assistant"|"tool",
    "content": str}, plus "tool_calls": [ToolCall] on assistant turns and "tool_call_id" /
    "name" on tool results. `tools` are OpenAI-style function specs.
    """

    model_name: str

    def chat_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ToolReply: ...


def _arguments(raw: Any) -> dict[str, Any]:
    """Tool arguments arrive as a dict (Ollama) or a JSON string (OpenAI); garbage → {}."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _parse_tool_calls(raw: Any) -> list[ToolCall]:
    out: list[ToolCall] = []
    for tc in raw if isinstance(raw, list) else []:
        fn = tc.get("function") if isinstance(tc, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        out.append(
            ToolCall(
                name=str(fn["name"]),
                arguments=_arguments(fn.get("arguments")),
                id=str(tc.get("id") or ""),
            )
        )
    return out


class OllamaClient:
    def __init__(self, cfg: ModelConfig, transport: httpx.BaseTransport | None = None) -> None:
        self.cfg = cfg
        self._http = httpx.Client(
            base_url=cfg.host.rstrip("/"), timeout=cfg.timeout_s, transport=transport
        )
        self.model_name = cfg.name
        self._checked = False
        self.calls = 0
        self.invalid = 0
        # Some Ollama builds (e.g. Homebrew's formula) ship without the grammar library and
        # answer 501 to `format`. Then we put the schema in the prompt and still validate.
        self.constrained = True

    def close(self) -> None:
        self._http.close()

    # -- setup --------------------------------------------------------------
    def installed_models(self) -> list[str]:
        try:
            resp = self._http.get("/api/tags", timeout=10)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ModelUnavailable(f"Ollama not reachable at {self.cfg.host} ({e})") from e
        return [m.get("name", "") for m in resp.json().get("models", [])]

    def ensure_ready(self) -> str:
        """Pick the configured model, or the fallback if the default isn't installed."""
        if self._checked:
            return self.model_name
        names = self.installed_models()

        def have(model: str) -> bool:
            return model in names or (":" not in model and f"{model}:latest" in names)

        if have(self.cfg.name):
            self.model_name = self.cfg.name
        elif self.cfg.fallback and have(self.cfg.fallback):
            log.warning("%s not installed; using fallback %s", self.cfg.name, self.cfg.fallback)
            self.model_name = self.cfg.fallback
        else:
            raise ModelUnavailable(
                f"Neither {self.cfg.name} nor {self.cfg.fallback} is installed. "
                f"Run: ollama pull {self.cfg.name}"
            )
        self._checked = True
        return self.model_name

    def has_embed_model(self) -> bool:
        try:
            names = self.installed_models()
        except ModelUnavailable:
            return False
        e = self.cfg.embed
        return e in names or (":" not in e and f"{e}:latest" in names)

    # -- calls --------------------------------------------------------------
    def _chat(self, messages: list[dict[str, str]], fmt: dict[str, Any] | None) -> str:
        model = self.ensure_ready()
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
            "think": False,
            "options": {"temperature": 0.2 if fmt else 0.4, "num_ctx": 8192},
        }
        if fmt is not None:
            if self.constrained:
                body["format"] = fmt
            else:
                body["messages"] = _with_schema_hint(messages, fmt)
        self.calls += 1
        try:
            resp = self._http.post("/api/chat", json=body)
            if resp.status_code == 501 and "format" in body and "structured output" in resp.text:
                log.warning(
                    "This Ollama build can't constrain output to a schema (%s). Falling back "
                    "to schema-in-prompt with validation. The official Ollama build fixes it.",
                    resp.text[:120],
                )
                self.constrained = False
                body.pop("format")
                body["messages"] = _with_schema_hint(messages, fmt)
                resp = self._http.post("/api/chat", json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise ModelUnavailable(f"Ollama not reachable at {self.cfg.host}") from e
        except httpx.HTTPError as e:
            raise ModelError(f"Ollama request failed: {type(e).__name__}") from e
        if resp.status_code >= 400:
            if model != self.cfg.fallback and self.cfg.fallback and resp.status_code >= 500:
                # Usually out of memory loading the big model; the smaller one may fit.
                log.warning(
                    "%s failed (%s); switching to %s for this run",
                    model,
                    resp.status_code,
                    self.cfg.fallback,
                )
                self.model_name = self.cfg.fallback
            raise ModelError(f"Ollama HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            content = str(resp.json()["message"]["content"])
        except (ValueError, KeyError, TypeError) as e:
            raise ModelError("unexpected Ollama response shape") from e
        return _strip_fences(content) if fmt is not None else content

    def chat_json(self, system: str, user: str, schema: type[T]) -> T:
        """Ask for JSON matching `schema`; retry once with the validation error; else raise."""
        json_schema = schema.model_json_schema()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        last_error = ""
        for attempt in range(2):
            raw = self._chat(messages, json_schema)
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
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw[:4000]},
                    {
                        "role": "user",
                        "content": "That didn't match the required JSON schema: "
                        f"{last_error}. Reply again with only valid JSON for the schema.",
                    },
                ]
        raise ModelError(f"invalid structured output after retry: {last_error}")

    def chat_text(self, system: str, user: str) -> str:
        text = self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}], None
        ).strip()
        if not text:
            raise ModelError("empty response")
        return text

    def chat_tools(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ToolReply:
        """One turn of a chat with tools over /api/chat. Thinking stays off: Gemma returns
        empty answers with it on. A bigger context than judging: tool results are long."""
        model = self.ensure_ready()
        body: dict[str, Any] = {
            "model": model,
            "messages": [_ollama_message(m) for m in messages],
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
            "think": False,
            "options": {"temperature": 0.3, "num_ctx": 16384, "num_predict": 1200},
        }
        if tools:
            body["tools"] = tools
        self.calls += 1
        try:
            resp = self._http.post("/api/chat", json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise ModelUnavailable(f"Ollama not reachable at {self.cfg.host}") from e
        except httpx.HTTPError as e:
            raise ModelError(f"Ollama request failed: {type(e).__name__}") from e
        if resp.status_code >= 400:
            if model != self.cfg.fallback and self.cfg.fallback and resp.status_code >= 500:
                log.warning(
                    "%s failed (%s); switching to %s", model, resp.status_code, self.cfg.fallback
                )
                self.model_name = self.cfg.fallback
            raise ModelError(f"Ollama HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            msg = resp.json()["message"]
            content = str(msg.get("content") or "")
            calls = _parse_tool_calls(msg.get("tool_calls"))
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise ModelError("unexpected Ollama response shape") from e
        return ToolReply(content=content, tool_calls=calls)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for start in range(0, len(texts), 32):
            batch = [t[:2000] for t in texts[start : start + 32]]
            try:
                resp = self._http.post(
                    "/api/embed",
                    json={"model": self.cfg.embed, "input": batch, "keep_alive": "1m"},
                )
                resp.raise_for_status()
                vecs = resp.json()["embeddings"]
            except (httpx.HTTPError, ValueError, KeyError) as e:
                raise ModelError(f"embedding failed: {type(e).__name__}") from e
            if len(vecs) != len(batch):
                raise ModelError("embedding count mismatch")
            out.extend(vecs)
        return out


def _ollama_message(m: dict[str, Any]) -> dict[str, Any]:
    """A provider-neutral chat message in Ollama's /api/chat shape."""
    role = m["role"]
    if role == "tool":
        return {
            "role": "tool",
            "content": str(m.get("content") or ""),
            "tool_name": m.get("name", ""),
        }
    out: dict[str, Any] = {"role": role, "content": str(m.get("content") or "")}
    if role == "assistant" and m.get("tool_calls"):
        out["tool_calls"] = [
            {"function": {"name": tc.name, "arguments": tc.arguments}} for tc in m["tool_calls"]
        ]
    if role == "user" and m.get("images"):  # base64, no data: prefix (vision models)
        out["images"] = [str(i) for i in m["images"]]
    return out


def _with_schema_hint(
    messages: list[dict[str, str]], fmt: dict[str, Any] | None
) -> list[dict[str, str]]:
    hint = (
        "\n\nReply with a single JSON object that validates against this JSON schema, "
        f"and nothing else:\n{json.dumps(fmt)}"
    )
    out = [dict(m) for m in messages]
    out[0]["content"] = out[0]["content"] + hint
    return out


def _strip_fences(text: str) -> str:
    """Unconstrained models like to wrap JSON in ```json fences."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _short_error(e: Exception) -> str:
    if isinstance(e, ValidationError):
        parts = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()[:5]]
        return "; ".join(parts)
    return str(e)[:300]
