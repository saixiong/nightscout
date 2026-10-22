"""Model providers: local Ollama plus any OpenAI-compatible server (LM Studio, OpenRouter, ...).

Build clients through `make_llm(cfg, role)` and `make_embedder(cfg)`; with no `providers:` or
`roles:` in config.yaml they return exactly the OllamaClient the app always used.

API keys come from `secrets.get_key` at construction time and are only ever placed in the
Authorization header. They never appear in repr(), log lines or exception messages.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from typing import Any, Protocol, TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from . import secrets
from .config import Config, ProviderConfig, RoleName, split_ref
from .llm import (
    LLM,
    ModelError,
    ModelUnavailable,
    OllamaClient,
    ToolReply,
    _parse_tool_calls,
    _short_error,
    _strip_fences,
    _with_schema_hint,
)

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")

PRESETS: dict[str, ProviderConfig] = {
    "lmstudio": ProviderConfig(
        id="lmstudio",
        kind="openai_compatible",
        name="LM Studio",
        base_url="http://127.0.0.1:1234/v1",
    ),
    "openai": ProviderConfig(
        id="openai",
        kind="openai_compatible",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
    ),
    "openrouter": ProviderConfig(
        id="openrouter",
        kind="openai_compatible",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
    ),
}


def preset(key: str) -> ProviderConfig:
    return PRESETS[key].model_copy(deep=True)


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def has_embed_model(self) -> bool: ...


class _Secret:
    """Holds an API key; prints as bullets."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def get(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<secret>"

    __str__ = __repr__


# A 400/422 that means "this server can't do response_format/json_schema".
_UNSUPPORTED = re.compile(
    r"response_format|json_schema|structured output|unsupported|not supported|"
    r"does not support|unknown (?:field|parameter)|extra (?:fields|inputs)",
    re.IGNORECASE,
)


class OpenAICompatClient:
    """The LLM protocol over /v1/chat/completions and /v1/embeddings."""

    def __init__(
        self,
        provider: ProviderConfig,
        model: str,
        *,
        api_key: str | None = None,
        embed_model: str | None = None,
        timeout_s: float = 300.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.provider = provider
        self.model_name = model
        self.embed_model = embed_model
        self._key = _Secret(api_key) if api_key else None
        self._http = httpx.Client(
            base_url=provider.base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            transport=transport,
            follow_redirects=False,  # never replay the Authorization header elsewhere
        )
        self._checked = False
        self.calls = 0
        self.invalid = 0
        # False once the server has refused response_format; then the schema goes in the prompt.
        self.constrained = True

    def __repr__(self) -> str:
        return (
            f"OpenAICompatClient(provider={self.provider.id!r}, model={self.model_name!r}, "
            f"key={'set' if self._key else 'none'})"
        )

    def close(self) -> None:
        self._http.close()

    # -- plumbing -----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json", "X-Title": "Night Scout"}
        if self._key:
            h["Authorization"] = f"Bearer {self._key.get()}"
        return h

    def _scrub(self, text: str) -> str:
        return secrets.scrub(text, self._key.get() if self._key else None)

    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        label = self.provider.label
        try:
            return self._http.request(method, path, headers=self._headers(), **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise ModelUnavailable(
                f"{label} not reachable at {self.provider.base_url}"
            ) from _clean(e)
        except httpx.HTTPError as e:
            raise ModelError(f"{label} request failed: {type(e).__name__}") from _clean(e)

    def _check(self, resp: httpx.Response, model: str) -> None:
        if resp.status_code < 400:
            return
        label = self.provider.label
        if resp.status_code in (401, 403):
            raise ModelUnavailable(
                f"{label} rejected the API key (HTTP {resp.status_code}). "
                "Check the key in Settings → AI Providers."
            )
        if resp.status_code == 404:
            raise ModelUnavailable(f"{label}: model {model} not found (HTTP 404)")
        raise ModelError(f"{label} HTTP {resp.status_code}: {self._scrub(resp.text[:200])}")

    # -- setup --------------------------------------------------------------
    def ensure_ready(self) -> str:
        """Fail fast when the server is down or refuses the key. Doesn't verify the model
        (not every server lists models); a missing one surfaces as ModelUnavailable later."""
        if self._checked:
            return self.model_name
        try:
            resp = self._request("GET", "/models", timeout=15)
        except ModelUnavailable:
            raise
        except ModelError as e:  # slow or odd /models: let the real calls decide
            log.info("%s readiness check inconclusive: %s", self.provider.label, e)
            self._checked = True
            return self.model_name
        if resp.status_code in (401, 403):
            self._check(resp, self.model_name)
        self._checked = True
        return self.model_name

    def has_embed_model(self) -> bool:
        return bool(self.embed_model)

    # -- calls --------------------------------------------------------------
    def _chat(
        self,
        messages: list[dict[str, str]],
        fmt: dict[str, Any] | None,
        schema_name: str = "answer",
        max_tokens: int | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "stream": False,
            "temperature": 0.2 if fmt else 0.4,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if fmt is not None:
            if self.constrained:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "schema": fmt, "strict": False},
                }
            else:
                body["messages"] = _with_schema_hint(messages, fmt)
        self.calls += 1
        resp = self._request("POST", "/chat/completions", json=body)
        if (
            resp.status_code in (400, 422)
            and "response_format" in body
            and _UNSUPPORTED.search(resp.text)
        ):
            log.warning(
                "%s can't constrain output to a JSON schema; falling back to "
                "schema-in-prompt with validation.",
                self.provider.label,
            )
            self.constrained = False
            body.pop("response_format")
            body["messages"] = _with_schema_hint(messages, fmt)
            resp = self._request("POST", "/chat/completions", json=body)
        self._check(resp, self.model_name)
        content = _message_content(resp)
        return _strip_fences(content) if fmt is not None else content

    def chat_json(self, system: str, user: str, schema: type[T]) -> T:
        """Ask for JSON matching `schema`; retry once with the validation error; else raise."""
        json_schema = schema.model_json_schema()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        name = re.sub(r"[^A-Za-z0-9_-]", "_", schema.__name__)[:64] or "answer"
        last_error = ""
        for attempt in range(2):
            raw = self._chat(messages, json_schema, name)
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

    def chat_text(self, system: str, user: str, max_tokens: int | None = None) -> str:
        text = self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            None,
            max_tokens=max_tokens,
        ).strip()
        if not text:
            raise ModelError("empty response")
        return text

    def chat_tools(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ToolReply:
        """One turn of a chat with tools over /chat/completions."""
        body: dict[str, Any] = {
            "model": self.model_name,
            "messages": [_openai_message(m) for m in messages],
            "stream": False,
            "temperature": 0.3,
            "max_tokens": 1200,
        }
        if tools:
            body["tools"] = tools
        self.calls += 1
        resp = self._request("POST", "/chat/completions", json=body)
        self._check(resp, self.model_name)
        try:
            msg = resp.json()["choices"][0]["message"]
            calls = _parse_tool_calls(msg.get("tool_calls"))
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
            raise ModelError("unexpected response shape from the model server") from e
        content = msg.get("content")
        if isinstance(content, list):  # some servers return content parts
            content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        return ToolReply(content=str(content or ""), tool_calls=calls)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.embed_model:
            raise ModelError(f"no embedding model configured for {self.provider.label}")
        out: list[list[float]] = []
        for start in range(0, len(texts), 32):
            batch = [t[:2000] for t in texts[start : start + 32]]
            resp = self._request(
                "POST", "/embeddings", json={"model": self.embed_model, "input": batch}
            )
            self._check(resp, self.embed_model)
            try:
                data = sorted(resp.json()["data"], key=lambda d: d.get("index", 0))
                vecs = [[float(x) for x in d["embedding"]] for d in data]
            except (ValueError, KeyError, TypeError) as e:
                raise ModelError(f"embedding failed: {type(e).__name__}") from None
            if len(vecs) != len(batch):
                raise ModelError("embedding count mismatch")
            out.extend(vecs)
        return out


def _clean(e: Exception) -> Exception | None:
    """httpx errors carry the request (and so its headers); don't chain them as-is."""
    return None if isinstance(e, httpx.RequestError) else e


def _openai_message(m: dict[str, Any]) -> dict[str, Any]:
    """A provider-neutral chat message in the OpenAI /chat/completions shape."""
    role = m["role"]
    if role == "tool":
        return {
            "role": "tool",
            "tool_call_id": m.get("tool_call_id") or m.get("name", ""),
            "content": str(m.get("content") or ""),
        }
    out: dict[str, Any] = {"role": role, "content": str(m.get("content") or "")}
    if role == "user" and m.get("images"):  # vision: text part + one image_url part per image
        out["content"] = [{"type": "text", "text": out["content"]}] + [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{i}"}}
            for i in m["images"]
        ]
    if role == "assistant" and m.get("tool_calls"):
        out["content"] = out["content"] or None
        out["tool_calls"] = [
            {
                "id": tc.id or f"call_{i}",
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for i, tc in enumerate(m["tool_calls"])
        ]
    return out


def _message_content(resp: httpx.Response) -> str:
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise ModelError("unexpected response shape from the model server") from e
    if isinstance(content, list):  # some servers return content parts
        content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    if content is None:
        raise ModelError("empty response")
    return str(content)


class FallbackLLM:
    """Use `primary`; if it's unavailable switch to `fallback` for the rest of the run,
    and if one call fails try that call again on `fallback`."""

    def __init__(self, primary: LLM, fallback: LLM) -> None:
        self.primary = primary
        self.fallback = fallback
        self.model_name = primary.model_name
        self.switched = False

    def __repr__(self) -> str:
        return f"FallbackLLM({self.primary!r}, {self.fallback!r})"

    def close(self) -> None:
        for m in (self.primary, self.fallback):
            closer = getattr(m, "close", None)
            if callable(closer):
                closer()

    def _switch(self, e: ModelError) -> None:
        log.warning(
            "%s unavailable (%s); using %s for this run",
            self.primary.model_name,
            e,
            self.fallback.model_name,
        )
        self.switched = True

    def ensure_ready(self) -> str:
        if not self.switched:
            try:
                ensure_ready(self.primary)
                self.model_name = self.primary.model_name
                return self.model_name
            except ModelUnavailable as e:
                self._switch(e)
        ensure_ready(self.fallback)
        self.model_name = self.fallback.model_name
        return self.model_name

    def _call(self, fn: Callable[[LLM], R]) -> R:
        if not self.switched:
            try:
                out = fn(self.primary)
                self.model_name = self.primary.model_name
                return out
            except ModelUnavailable as e:
                self._switch(e)
            except ModelError as e:
                log.warning(
                    "%s failed (%s); trying %s",
                    self.primary.model_name,
                    e,
                    self.fallback.model_name,
                )
        out = fn(self.fallback)
        self.model_name = self.fallback.model_name
        return out

    def chat_json(self, system: str, user: str, schema: type[T]) -> T:
        return self._call(lambda m: m.chat_json(system, user, schema))

    def chat_text(self, system: str, user: str) -> str:
        return self._call(lambda m: m.chat_text(system, user))

    def chat_tools(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ToolReply:
        def call(m: LLM) -> ToolReply:
            fn = getattr(m, "chat_tools", None)
            if fn is None:
                raise ModelError(f"{m.model_name} can't use tools")
            reply: ToolReply = fn(messages, tools)
            return reply

        return self._call(call)

    # Vectors from two different models don't compare, so embeddings stay on the primary.
    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.primary.embed(texts)

    def has_embed_model(self) -> bool:
        return self.primary.has_embed_model()


def ensure_ready(llm: object) -> None:
    """Call llm.ensure_ready() if it has one (fakes in tests don't)."""
    fn = getattr(llm, "ensure_ready", None)
    if callable(fn):
        fn()


def close_llm(llm: object) -> None:
    fn = getattr(llm, "close", None)
    if callable(fn):
        fn()


# -- factories ------------------------------------------------------------------
def _api_key(provider: ProviderConfig) -> str | None:
    try:
        return secrets.get_key(provider.id, provider.api_key_env)
    except secrets.SecretStoreUnavailable:
        log.warning("OS keychain unavailable; %s has no API key", provider.label)
        return None


def _ollama_client(
    cfg: Config,
    provider: ProviderConfig,
    model: str,
    fallback: str,
    embed: str,
    transport: httpx.BaseTransport | None,
) -> OllamaClient:
    host = cfg.model.host if provider.base_url == cfg.model.host.rstrip("/") else provider.base_url
    mc = cfg.model.model_copy(
        update={"host": host, "name": model, "fallback": fallback, "embed": embed}
    )
    return OllamaClient(mc, transport=transport)


def _embed_on(cfg: Config, provider_id: str) -> str | None:
    pid, model = split_ref(cfg.role_ref("embed") or "")
    return model if pid == provider_id else None


def _client_for(
    cfg: Config, ref: str, fallback_model: str, transport: httpx.BaseTransport | None
) -> LLM:
    pid, model = split_ref(ref)
    provider = cfg.provider(pid)
    embed = _embed_on(cfg, pid)
    if provider.kind == "ollama":
        return _ollama_client(
            cfg, provider, model, fallback_model, embed or cfg.model.embed, transport
        )
    return OpenAICompatClient(
        provider,
        model,
        api_key=_api_key(provider),
        embed_model=embed,
        timeout_s=cfg.model.timeout_s,
        transport=transport,
    )


def make_llm(
    cfg: Config, role: RoleName = "judge", transport: httpx.BaseTransport | None = None
) -> LLM:
    """The client for a role. judge/chat get the fallback role behind them: inside the same
    Ollama server that's OllamaClient's own fallback (today's behaviour), across providers
    it's a FallbackLLM."""
    if role == "embed":
        raise ValueError("use make_embedder() for the embed role")
    ref = cfg.role_ref(role)
    if ref is None:
        raise ModelUnavailable(f"no model configured for the {role} role")
    fb_ref = cfg.role_ref("fallback") if role in ("judge", "chat") else None
    if fb_ref == ref:
        fb_ref = None
    pid = split_ref(ref)[0]
    inner_fallback = ""
    if fb_ref and cfg.provider(pid).kind == "ollama" and split_ref(fb_ref)[0] == pid:
        inner_fallback, fb_ref = split_ref(fb_ref)[1], None
    primary = _client_for(cfg, ref, inner_fallback, transport)
    if fb_ref is None:
        return primary
    return FallbackLLM(primary, _client_for(cfg, fb_ref, "", transport))


def make_embedder(cfg: Config, transport: httpx.BaseTransport | None = None) -> Embedder:
    ref = cfg.role_ref("embed")
    assert ref is not None  # embed always has a default
    pid, model = split_ref(ref)
    provider = cfg.provider(pid)
    if provider.kind == "ollama":
        judge_pid, judge_model = split_ref(cfg.role_ref("judge") or ref)
        name = judge_model if judge_pid == pid else cfg.model.name
        fb = cfg.role_ref("fallback")
        fallback = split_ref(fb)[1] if fb and split_ref(fb)[0] == pid else ""
        return _ollama_client(cfg, provider, name, fallback, model, transport)
    return OpenAICompatClient(
        provider,
        model,
        api_key=_api_key(provider),
        embed_model=model,
        timeout_s=cfg.model.timeout_s,
        transport=transport,
    )


def embed_model_name(cfg: Config) -> str:
    return split_ref(cfg.role_ref("embed") or "")[1]


# -- provider test (Settings → AI Providers → Test) ------------------------------
@dataclass
class Step:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


STEP_NAMES = ("Health", "Auth", "Chat")


def _skip(steps: list[Step], why: str) -> list[Step]:
    for name in STEP_NAMES[len(steps) :]:
        steps.append(Step(name, False, f"skipped: {why}"))
    return steps


def _secs(t0: float) -> str:
    return f"{time.monotonic() - t0:.1f}s"


def _model_for(cfg: Config, provider_id: str) -> str | None:
    for role in ("judge", "chat", "fallback"):
        ref = cfg.role_ref(role)  # type: ignore[arg-type]
        if ref and split_ref(ref)[0] == provider_id:
            return split_ref(ref)[1]
    return None


def test_provider(
    cfg: Config,
    provider_id: str,
    *,
    model: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[Step]:
    """Health → Auth → Chat (a tiny prompt). Stops at the first failure; the rest are
    reported as skipped. Never raises for provider problems and never echoes the key."""
    provider = cfg.provider(provider_id)
    steps: list[Step] = []
    key: str | None = None
    with httpx.Client(
        base_url=provider.base_url.rstrip("/"),
        timeout=httpx.Timeout(15.0, connect=5.0),
        transport=transport,
        follow_redirects=False,
    ) as http:

        def safe(text: str) -> str:
            return secrets.scrub(text, key)

        # Health
        t0 = time.monotonic()
        path = "/api/version" if provider.kind == "ollama" else "/models"
        try:
            resp = http.get(path)
        except httpx.HTTPError as e:
            hint = (
                f"Is {provider.label} running?"
                if provider.is_local
                else "Check the URL and your connection."
            )
            steps.append(
                Step(
                    "Health", False, f"Can't reach {provider.base_url} ({type(e).__name__}). {hint}"
                )
            )
            return _skip(steps, "server not reachable")
        if resp.status_code >= 500:
            steps.append(Step("Health", False, f"Server error HTTP {resp.status_code}"))
            return _skip(steps, "server error")
        if provider.kind == "ollama":
            try:
                version = str(resp.json().get("version", "?"))
            except ValueError:
                version = "?"
            steps.append(Step("Health", True, f"Ollama {version} answered in {_secs(t0)}"))
        else:
            steps.append(Step("Health", True, f"Server answered in {_secs(t0)}"))

        # Auth
        headers: dict[str, str] = {}
        if provider.kind == "ollama":
            steps.append(Step("Auth", True, "Not needed for Ollama"))
        else:
            try:
                key = secrets.get_key(provider.id, provider.api_key_env)
            except secrets.SecretStoreUnavailable as e:
                steps.append(Step("Auth", False, str(e)))
                return _skip(steps, "no API key")
            source = secrets.key_source(provider.id, provider.api_key_env) if key else None
            if not key:
                if provider.is_local:
                    steps.append(Step("Auth", True, "No key set; not needed for a local server"))
                else:
                    env = f", or set {provider.api_key_env}" if provider.api_key_env else ""
                    steps.append(Step("Auth", False, f"No API key yet. Add one in Settings{env}."))
                    return _skip(steps, "no API key")
            else:
                headers["Authorization"] = f"Bearer {key}"
                host = (urlsplit(provider.base_url).hostname or "").lower()
                auth_path = "/key" if host.endswith("openrouter.ai") else "/models"
                where = f"env {provider.api_key_env}" if source == "env" else "keychain"
                try:
                    resp = http.get(auth_path, headers=headers)
                except httpx.HTTPError as e:
                    steps.append(Step("Auth", False, f"Auth check failed ({type(e).__name__})"))
                    return _skip(steps, "auth check failed")
                if resp.status_code in (401, 403):
                    steps.append(
                        Step(
                            "Auth",
                            False,
                            f"The server rejected the API key (HTTP {resp.status_code}). "
                            "Check it was copied in full and is still active.",
                        )
                    )
                    return _skip(steps, "key rejected")
                if resp.status_code < 400:
                    detail = f"Key accepted ({secrets.masked(key)}, from {where})"
                else:
                    detail = (
                        f"Key sent ({secrets.masked(key)}, from {where}); the server can't "
                        f"check it separately (HTTP {resp.status_code})"
                    )
                steps.append(Step("Auth", True, safe(detail)))

        # Chat
        name = model or _model_for(cfg, provider.id) or _first_model(http, provider, headers)
        if not name:
            steps.append(Step("Chat", False, "No model to try: pick one for a role first."))
            return steps
        messages = [
            {"role": "system", "content": "You are a connection test. Reply with one word."},
            {"role": "user", "content": "Say OK."},
        ]
        t0 = time.monotonic()
        chat_timeout = httpx.Timeout(max(cfg.model.timeout_s, 30.0), connect=5.0)
        try:
            if provider.kind == "ollama":
                resp = http.post(
                    "/api/chat",
                    json={
                        "model": name,
                        "messages": messages,
                        "stream": False,
                        "think": False,
                        "keep_alive": cfg.model.keep_alive,
                        "options": {"temperature": 0, "num_predict": 16},
                    },
                    timeout=chat_timeout,
                )
            else:
                resp = http.post(
                    "/chat/completions",
                    json={
                        "model": name,
                        "messages": messages,
                        "max_tokens": 16,
                        "temperature": 0,
                        "stream": False,
                    },
                    headers=headers,
                    timeout=chat_timeout,
                )
        except httpx.HTTPError as e:
            steps.append(Step("Chat", False, f"{name}: request failed ({type(e).__name__})"))
            return steps
        if resp.status_code in (401, 403):
            steps.append(
                Step(
                    "Chat",
                    False,
                    f"{name}: the server rejected the API key (HTTP {resp.status_code}).",
                )
            )
        elif resp.status_code == 404:
            hint = " Pull it from the Ollama models list." if provider.kind == "ollama" else ""
            steps.append(Step("Chat", False, f"{name} isn't available on {provider.label}.{hint}"))
        elif resp.status_code >= 400:
            steps.append(
                Step("Chat", False, safe(f"{name}: HTTP {resp.status_code}: {resp.text[:160]}"))
            )
        else:
            try:
                data = resp.json()
                reply = (
                    data["message"]["content"]
                    if provider.kind == "ollama"
                    else _message_content(resp)
                )
            except (ValueError, KeyError, TypeError, ModelError):
                steps.append(Step("Chat", False, f"{name}: unexpected response shape"))
                return steps
            reply = " ".join(str(reply).split())[:60]
            steps.append(Step("Chat", True, safe(f"{name} replied {reply!r} in {_secs(t0)}")))
    return steps


test_provider.__test__ = False  # type: ignore[attr-defined]  # not a pytest test


def _first_model(http: httpx.Client, provider: ProviderConfig, headers: dict[str, str]) -> str:
    try:
        if provider.kind == "ollama":
            models = http.get("/api/tags").json().get("models", [])
            return str(models[0]["name"]) if models else ""
        data = http.get("/models", headers=headers).json().get("data", [])
        return str(data[0]["id"]) if data else ""
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        return ""


# -- Ollama model management -----------------------------------------------------
@dataclass
class OllamaModel:
    name: str
    size_bytes: int
    size: str
    parameter_size: str
    quantization: str
    family: str = ""
    modified_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"


def _ollama_http(
    cfg: Config, provider_id: str, transport: httpx.BaseTransport | None, timeout: httpx.Timeout
) -> tuple[ProviderConfig, httpx.Client]:
    provider = cfg.provider(provider_id)
    if provider.kind != "ollama":
        raise ValueError(f"{provider.label} isn't an Ollama provider")
    client = httpx.Client(
        base_url=provider.base_url.rstrip("/"), timeout=timeout, transport=transport
    )
    return provider, client


def ollama_models(
    cfg: Config, provider_id: str = "ollama", transport: httpx.BaseTransport | None = None
) -> list[OllamaModel]:
    provider, http = _ollama_http(cfg, provider_id, transport, httpx.Timeout(10.0))
    with http:
        try:
            resp = http.get("/api/tags")
            resp.raise_for_status()
            models = resp.json().get("models", [])
        except httpx.HTTPError as e:
            raise ModelUnavailable(
                f"Ollama not reachable at {provider.base_url} ({type(e).__name__})"
            ) from None
        except ValueError as e:
            raise ModelError("unexpected /api/tags response") from e
    out: list[OllamaModel] = []
    for m in models:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        details = m.get("details") or {}
        size = int(m.get("size") or 0)
        out.append(
            OllamaModel(
                name=str(m["name"]),
                size_bytes=size,
                size=human_size(size),
                parameter_size=str(details.get("parameter_size") or ""),
                quantization=str(details.get("quantization_level") or ""),
                family=str(details.get("family") or ""),
                modified_at=str(m.get("modified_at") or ""),
            )
        )
    return sorted(out, key=lambda m: m.name)


_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_MODEL_NAME = re.compile(rf"^{_SEGMENT}(?:/{_SEGMENT}){{0,3}}(?::{_SEGMENT})?$")


def valid_model_name(name: str) -> bool:
    """Ollama names like 'gemma4:12b', 'library/qwen3:8b', 'hf.co/org/Repo-GGUF:Q4_K_M'."""
    return len(name) <= 300 and ".." not in name and bool(_MODEL_NAME.fullmatch(name))


@dataclass
class PullProgress:
    status: str
    digest: str = ""
    completed: int = 0
    total: int = 0

    @property
    def percent(self) -> float | None:
        return round(100 * self.completed / self.total, 1) if self.total else None

    @property
    def done(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "percent": self.percent, "done": self.done}


def parse_pull_line(line: str) -> PullProgress | None:
    """One NDJSON line of /api/pull. Raises ModelError for an error line."""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("error"):
        raise ModelError(f"pull failed: {str(obj['error'])[:200]}")
    return PullProgress(
        status=str(obj.get("status") or ""),
        digest=str(obj.get("digest") or ""),
        completed=int(obj.get("completed") or 0),
        total=int(obj.get("total") or 0),
    )


def ollama_pull(
    cfg: Config,
    name: str,
    provider_id: str = "ollama",
    transport: httpx.BaseTransport | None = None,
) -> Iterator[PullProgress]:
    """Validate now, then stream /api/pull progress lazily. Raises ValueError for a bad
    name before anything touches the network."""
    name = name.strip()
    if not valid_model_name(name):
        raise ValueError(
            "That doesn't look like an Ollama model name (e.g. gemma4:12b or library/qwen3:8b)."
        )
    provider, http = _ollama_http(cfg, provider_id, transport, httpx.Timeout(30.0, read=600.0))

    def stream() -> Iterator[PullProgress]:
        with http:
            try:
                with http.stream("POST", "/api/pull", json={"model": name, "stream": True}) as r:
                    if r.status_code >= 400:
                        r.read()
                        raise ModelError(f"pull failed: HTTP {r.status_code}: {r.text[:200]}")
                    for line in r.iter_lines():
                        p = parse_pull_line(line)
                        if p is not None:
                            yield p
            except httpx.HTTPError as e:
                raise ModelUnavailable(
                    f"Ollama not reachable at {provider.base_url} ({type(e).__name__})"
                ) from None

    return stream()
