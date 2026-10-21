"""Reddit via each subreddit's official public RSS feed.

Reddit's unauthenticated .json listings now answer 403 to non-browser clients, while
the RSS feeds stay open. We fetch politely (2 s spacing per host, descriptive UA).
"""

from __future__ import annotations

import re

import feedparser

from ..config import RedditSource
from ..http import PoliteClient, SourceError
from ..models import Item
from .base import SourceTask, parse_time, strip_html

_LINK = re.compile(r'<a href="([^"]+)">\[link\]</a>')


def parse_subreddit_feed(content: bytes, subreddit: str) -> list[Item]:
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise SourceError("feed could not be parsed", "bad-data")
    items: list[Item] = []
    for e in feed.entries:
        post_id = (e.get("id") or "").strip()
        permalink = e.get("link") or ""
        if not post_id or not permalink:
            continue
        body_html = e["content"][0]["value"] if e.get("content") else e.get("summary", "")
        m = _LINK.search(body_html)
        external = m.group(1) if m else ""
        if "reddit.com" in external or external.startswith("/"):
            external = ""
        # Reddit's RSS appends "submitted by /u/x [link] [comments]"; drop that footer.
        text = strip_html(body_html.split("&#32; submitted by")[0].split(" submitted by ")[0])
        items.append(
            Item(
                source="reddit",
                source_name=f"r/{subreddit}",
                uid=f"reddit:{post_id}",
                url=external or permalink,
                title=strip_html(e.get("title", "(untitled)"), 300),
                text=text,
                author=(e.get("author") or "").removeprefix("/u/") or None,
                published_at=parse_time(e.get("published_parsed") or e.get("updated_parsed")),
                extra={"discussion": permalink},
            )
        )
    return items


def tasks(cfg: RedditSource | None) -> list[SourceTask]:
    if cfg is None:
        return []

    def make(sub: str) -> SourceTask:
        def fetch(client: PoliteClient) -> list[Item]:
            url = f"https://www.reddit.com/r/{sub}/{cfg.sort}/.rss"
            resp = client.get(url, params={"limit": cfg.limit})
            return parse_subreddit_feed(resp.content, sub)

        return SourceTask(key=f"reddit:{sub}", label=f"Reddit r/{sub}", fetch=fetch)

    return [make(s.removeprefix("r/")) for s in cfg.subreddits]
