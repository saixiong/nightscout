"""Hacker News via the official Firebase API (lists) and the Algolia API (keywords)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import HackerNewsSource
from ..http import PoliteClient, SourceError
from ..models import Item
from .base import SourceTask, parse_time, strip_html

FIREBASE = "https://hacker-news.firebaseio.com/v0"
ALGOLIA = "https://hn.algolia.com/api/v1/search_by_date"
LIST_ENDPOINT = {
    "top": "topstories",
    "new": "newstories",
    "best": "beststories",
    "show": "showstories",
    "ask": "askstories",
}


def hn_url(item_id: int | str) -> str:
    return f"https://news.ycombinator.com/item?id={item_id}"


def item_from_firebase(data: dict[str, Any], label: str) -> Item | None:
    if not data or data.get("type") != "story" or data.get("dead") or data.get("deleted"):
        return None
    hid = data["id"]
    return Item(
        source="hackernews",
        source_name=label,
        uid=f"hn:{hid}",
        url=data.get("url") or hn_url(hid),
        title=data.get("title", "(untitled)"),
        text=strip_html(data.get("text")),
        author=data.get("by"),
        published_at=parse_time(data.get("time")),
        extra={
            "points": data.get("score", 0),
            "comments": data.get("descendants", 0),
            "discussion": hn_url(hid),
        },
    )


def item_from_algolia(hit: dict[str, Any], label: str) -> Item | None:
    hid = hit.get("objectID")
    if not hid or not hit.get("title"):
        return None
    return Item(
        source="hackernews",
        source_name=label,
        uid=f"hn:{hid}",
        url=hit.get("url") or hn_url(hid),
        title=hit["title"],
        text=strip_html(hit.get("story_text")),
        author=hit.get("author"),
        published_at=parse_time(hit.get("created_at_i") or hit.get("created_at")),
        extra={
            "points": hit.get("points") or 0,
            "comments": hit.get("num_comments") or 0,
            "discussion": hn_url(hid),
        },
    )


def tasks(cfg: HackerNewsSource | None, max_age_days: int = 3) -> list[SourceTask]:
    if cfg is None:
        return []
    out: list[SourceTask] = []

    for name in cfg.lists:

        def fetch_list(client: PoliteClient, name: str = name) -> list[Item]:
            ids = client.get_json(f"{FIREBASE}/{LIST_ENDPOINT[name]}.json") or []
            items: list[Item] = []
            failures = 0
            wanted = ids[: cfg.limit]
            for hid in wanted:
                try:
                    data = client.get_json(f"{FIREBASE}/item/{hid}.json")
                except SourceError:
                    failures += 1  # one missing story shouldn't lose the whole list
                    continue
                item = item_from_firebase(data, f"HN {name}")
                if item and item.extra["points"] >= cfg.min_points:
                    items.append(item)
            if wanted and failures == len(wanted):
                raise SourceError("every story fetch failed", "network")
            return items

        out.append(
            SourceTask(key=f"hackernews:{name}", label=f"Hacker News {name}", fetch=fetch_list)
        )

    for kw in cfg.keywords:

        def fetch_kw(client: PoliteClient, kw: str = kw) -> list[Item]:
            since = datetime.now(UTC) - timedelta(days=max_age_days)
            data = client.get_json(
                ALGOLIA,
                params={
                    "query": kw,
                    "tags": "story",
                    "typoTolerance": "false",
                    "hitsPerPage": cfg.limit,
                    "numericFilters": f"created_at_i>{int(since.timestamp())}",
                },
            )
            items = [item_from_algolia(h, f"HN search: {kw}") for h in data.get("hits", [])]
            return [i for i in items if i and i.extra["points"] >= cfg.min_points]

        out.append(
            SourceTask(key=f"hackernews:q:{kw}", label=f'Hacker News search "{kw}"', fetch=fetch_kw)
        )
    return out
