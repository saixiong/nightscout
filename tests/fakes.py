"""A fake LLM for pipeline tests: canned, schema-shaped answers, no Ollama needed."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from night_scout.core.llm import ModelError

T = TypeVar("T", bound=BaseModel)


class FakeLLM:
    model_name = "fake-gemma"

    def __init__(
        self,
        answers: dict[str, Callable[[str], dict[str, Any] | str]] | None = None,
        embed_fn: Callable[[str], list[float]] | None = None,
    ) -> None:
        self.answers = answers or {}
        self.embed_fn = embed_fn
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, system: str, user: str, schema: type[T]) -> T:
        self.calls.append((schema.__name__, user))
        fn = self.answers.get(schema.__name__)
        if fn is None:
            raise ModelError(f"no fake answer for {schema.__name__}")
        out = fn(user)
        if isinstance(out, str):
            raise ModelError(out)
        return schema.model_validate(out)

    def chat_text(self, system: str, user: str) -> str:
        self.calls.append(("text", user))
        fn = self.answers.get("text")
        return str(fn(user)) if fn else "## Brief\nNothing much."

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.embed_fn is None:
            raise ModelError("no embeddings")
        return [self.embed_fn(t) for t in texts]

    def has_embed_model(self) -> bool:
        return self.embed_fn is not None


def dumps(obj: Any) -> str:
    return json.dumps(obj)
