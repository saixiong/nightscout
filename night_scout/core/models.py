"""Plain data types shared across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Item:
    """One thing a source found. `uid` is the source's own stable id (e.g. "hn:123")."""

    source: str  # adapter kind: youtube, rss, hackernews, reddit, bluesky, mastodon, github
    source_name: str  # human label, e.g. "r/LocalLLaMA" or "Simon Willison"
    uid: str
    url: str
    title: str
    text: str = ""
    author: str | None = None
    published_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceStatus:
    """Outcome of one source in one run, shown as a status line in the UI."""

    key: str  # e.g. "reddit:LocalLLaMA"
    label: str
    ok: bool
    fetched: int = 0
    new: int = 0
    message: str = ""

    def line(self) -> str:
        if self.ok:
            return f"{self.label}: {self.fetched} fetched, {self.new} new"
        return f"{self.label}: {self.message}, skipped"
