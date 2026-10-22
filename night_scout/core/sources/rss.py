"""RSS/Atom blogs and sites via feedparser."""

from __future__ import annotations

import hashlib

import feedparser

from ..config import RssSource
from ..http import PoliteClient, SourceError
from ..models import Item
from .base import SourceTask, parse_time, strip_html


def parse_feed(content: bytes, source: str, source_name: str) -> list[Item]:
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise SourceError("feed could not be parsed", "bad-data")
    items: list[Item] = []
    for e in feed.entries:
        link = e.get("link") or ""
        ident = e.get("id") or link or e.get("title", "")
        if not ident:
            continue
        body = ""
        if e.get("content"):
            body = e["content"][0].get("value", "")
        body = body or e.get("summary", "")
        published = parse_time(e.get("published_parsed") or e.get("updated_parsed"))
        digest = hashlib.sha1(ident.encode("utf-8")).hexdigest()[:16]
        items.append(
            Item(
                source=source,
                source_name=source_name,
                uid=f"{source}:{digest}",
                url=link,
                title=strip_html(e.get("title", "(untitled)"), 300),
                text=strip_html(body),
                author=e.get("author"),
                published_at=published,
            )
        )
    return items


def tasks(cfgs: list[RssSource]) -> list[SourceTask]:
    def make(c: RssSource) -> SourceTask:
        def fetch(client: PoliteClient) -> list[Item]:
            # User-added feeds can later redirect anywhere: public hosts only, every hop.
            resp = client.get_public(c.url, max_bytes=8_000_000)
            return parse_feed(resp.content, "rss", c.name)

        return SourceTask(key=f"rss:{c.url}", label=c.name, fetch=fetch)

    return [make(c) for c in cfgs]
