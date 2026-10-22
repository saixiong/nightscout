"""Research tools for the chat drawer. Each returns a JSON-able dict and, on failure,
{"error": "..."}; `run_tool` makes sure nothing raises into the chat loop and nothing
runs forever."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import Config
from .base import ToolContext, fail
from .media import hn_search, youtube_transcript
from .search import BRAVE_KEY_ID, web_search
from .tonight import search_tonight
from .web import read_page

__all__ = ["BRAVE_KEY_ID", "TOOLS", "Tool", "ToolContext", "offered", "run_tool", "specs"]

TOOL_TIMEOUT_S = 45.0


@dataclass(frozen=True)
class Tool:
    name: str
    label: str
    icon: str
    description: str
    params: dict[str, str]  # name → description; all strings
    required: tuple[str, ...]
    fn: Callable[..., dict[str, Any]]

    def spec(self) -> dict[str, Any]:
        """OpenAI-style function spec (Ollama takes the same shape)."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        k: {"type": "string", "description": d} for k, d in self.params.items()
                    },
                    "required": list(self.required),
                },
            },
        }


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "search_tonight",
            "Tonight's items",
            "🗂",
            "Search the items Night Scout fetched for a Digest day (titles, headlines, "
            "topics and summaries). Returns ids, titles, sources, URLs and summaries.",
            {
                "query": "Words to look for, e.g. 'local LLM inference'.",
                "day": "Digest day as YYYY-MM-DD. Leave out for the day being discussed.",
            },
            ("query",),
            search_tonight,
        ),
        Tool(
            "read_page",
            "Read a page",
            "📄",
            "Fetch a public web page (http or https) and return its readable text.",
            {"url": "The full URL to read."},
            ("url",),
            read_page,
        ),
        Tool(
            "youtube_transcript",
            "YouTube transcript",
            "▶",
            "Get the English transcript of a YouTube video.",
            {"video": "A YouTube URL or the 11-character video id."},
            ("video",),
            youtube_transcript,
        ),
        Tool(
            "hn_search",
            "Hacker News",
            "Y",
            "Search Hacker News stories: titles, points, comment counts and discussion links.",
            {"query": "What to search for."},
            ("query",),
            hn_search,
        ),
        Tool(
            "web_search",
            "Web search",
            "🔎",
            "Search the web. Returns titles, URLs and snippets; read_page the best ones.",
            {"query": "What to search for."},
            ("query",),
            web_search,
        ),
    )
}


def offered(cfg: Config) -> list[Tool]:
    """Tools the model may call. Without a search provider, web_search isn't offered at all."""
    return [t for t in TOOLS.values() if t.name != "web_search" or cfg.search.provider != "none"]


def specs(cfg: Config) -> list[dict[str, Any]]:
    return [t.spec() for t in offered(cfg)]


def run_tool(
    ctx: ToolContext, name: str, args: dict[str, Any], timeout: float = TOOL_TIMEOUT_S
) -> dict[str, Any]:
    """Run one tool call from the model. Never raises; gives up after `timeout` seconds."""
    tool = TOOLS.get(name)
    if tool is None or tool not in offered(ctx.cfg):
        return fail(f"There is no tool called {name!r}.")
    allowed = {k: v for k, v in (args or {}).items() if k in tool.params}
    missing = [k for k in tool.required if not str(allowed.get(k) or "").strip()]
    if missing:
        return fail(f"{name} needs {', '.join(missing)}.")
    box: dict[str, dict[str, Any]] = {}

    def work() -> None:
        try:
            box["out"] = tool.fn(ctx, **allowed)
        except Exception as e:
            box["out"] = fail(f"{tool.label} failed ({type(e).__name__}).")

    worker = threading.Thread(target=work, name=f"tool-{name}", daemon=True)
    worker.start()
    worker.join(timeout)
    if "out" not in box:
        return fail(f"Gave up on {tool.label} after {timeout:.0f} s.")
    out = box["out"]
    return out if isinstance(out, dict) else fail(f"{tool.label} returned nothing usable.")
