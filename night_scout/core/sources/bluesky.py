"""Bluesky via the public AppView (no login): search, author feeds and feed generators."""

from __future__ import annotations

from typing import Any

from ..config import BlueskySource
from ..http import PoliteClient
from ..models import Item
from .base import SourceTask, first_line, parse_time

SEARCH_HOST = "https://api.bsky.app/xrpc"  # searchPosts is open here without auth
PUBLIC_HOST = "https://public.api.bsky.app/xrpc"


def post_web_url(uri: str, handle: str) -> str:
    rkey = uri.rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


def item_from_post(post: dict[str, Any], label: str) -> Item | None:
    uri = post.get("uri")
    record = post.get("record") or {}
    text = (record.get("text") or "").strip()
    author = post.get("author") or {}
    handle = author.get("handle") or author.get("did") or "unknown"
    if not uri or not text:
        return None
    web = post_web_url(uri, handle)
    embed = post.get("embed") or {}
    external = (embed.get("external") or {}).get("uri") or ""
    extra: dict[str, Any] = {
        "discussion": web,
        "likes": post.get("likeCount", 0),
        "reposts": post.get("repostCount", 0),
    }
    if external:
        ext = embed["external"]
        extra["link_title"] = ext.get("title", "")
        text = f"{text}\n\nLinked: {ext.get('title', '')} — {ext.get('description', '')}".strip()
    return Item(
        source="bluesky",
        source_name=label,
        uid=f"bsky:{uri}",
        url=external or web,
        title=first_line(record.get("text") or ""),
        text=text,
        author=handle,
        published_at=parse_time(record.get("createdAt") or post.get("indexedAt")),
        extra=extra,
    )


def tasks(cfg: BlueskySource | None) -> list[SourceTask]:
    if cfg is None:
        return []
    out: list[SourceTask] = []

    for q in cfg.queries:

        def fetch_q(client: PoliteClient, q: str = q) -> list[Item]:
            data = client.get_json(
                f"{SEARCH_HOST}/app.bsky.feed.searchPosts",
                params={"q": q, "limit": cfg.limit, "sort": "latest"},
            )
            items = [item_from_post(p, f"Bluesky “{q}”") for p in data.get("posts", [])]
            return [i for i in items if i]

        out.append(SourceTask(key=f"bluesky:q:{q}", label=f'Bluesky search "{q}"', fetch=fetch_q))

    for actor in cfg.actors:

        def fetch_a(client: PoliteClient, actor: str = actor) -> list[Item]:
            data = client.get_json(
                f"{PUBLIC_HOST}/app.bsky.feed.getAuthorFeed",
                params={"actor": actor, "limit": cfg.limit, "filter": "posts_no_replies"},
            )
            items = [
                item_from_post(f.get("post", {}), f"Bluesky @{actor}") for f in data.get("feed", [])
            ]
            return [i for i in items if i]

        out.append(
            SourceTask(key=f"bluesky:actor:{actor}", label=f"Bluesky @{actor}", fetch=fetch_a)
        )

    for feed in cfg.feeds:

        def fetch_f(client: PoliteClient, feed: str = feed) -> list[Item]:
            data = client.get_json(
                f"{PUBLIC_HOST}/app.bsky.feed.getFeed", params={"feed": feed, "limit": cfg.limit}
            )
            name = feed.rsplit("/", 1)[-1]
            items = [
                item_from_post(f.get("post", {}), f"Bluesky feed {name}")
                for f in data.get("feed", [])
            ]
            return [i for i in items if i]

        out.append(
            SourceTask(
                key=f"bluesky:feed:{feed}",
                label=f"Bluesky feed {feed.rsplit('/', 1)[-1]}",
                fetch=fetch_f,
            )
        )
    return out
