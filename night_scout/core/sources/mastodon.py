"""Mastodon public hashtag timelines (no login)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..config import MastodonSource
from ..http import PoliteClient
from ..models import Item
from .base import SourceTask, first_line, parse_time, strip_html


def item_from_status(st: dict[str, Any], instance: str, label: str) -> Item | None:
    if st.get("reblog"):
        st = st["reblog"]
    sid = st.get("id")
    url = st.get("url") or st.get("uri")
    text = strip_html(st.get("content"))
    if not sid or not url or not text:
        return None
    card = st.get("card") or {}
    extra: dict[str, Any] = {
        "discussion": url,
        "boosts": st.get("reblogs_count", 0),
        "favourites": st.get("favourites_count", 0),
    }
    if card.get("url"):
        text = f"{text}\n\nLinked: {card.get('title', '')} — {card.get('description', '')}"
    acct = (st.get("account") or {}).get("acct")
    return Item(
        source="mastodon",
        source_name=label,
        uid=f"mastodon:{st.get('uri') or f'{instance}/{sid}'}",
        url=card.get("url") or url,
        title=first_line(text),
        text=text,
        author=acct,
        published_at=parse_time(st.get("created_at")),
        extra=extra,
    )


def tasks(cfgs: list[MastodonSource]) -> list[SourceTask]:
    out: list[SourceTask] = []
    for c in cfgs:
        for tag in c.hashtags:
            tag = tag.lstrip("#")

            def fetch(client: PoliteClient, c: MastodonSource = c, tag: str = tag) -> list[Item]:
                data = client.get_json(
                    f"https://{c.instance}/api/v1/timelines/tag/{quote(tag)}",
                    params={"limit": c.limit},
                )
                label = f"#{tag}@{c.instance}"
                items = [item_from_status(s, c.instance, label) for s in data or []]
                return [i for i in items if i]

            out.append(
                SourceTask(
                    key=f"mastodon:{c.instance}:{tag}",
                    label=f"Mastodon #{tag} ({c.instance})",
                    fetch=fetch,
                )
            )
    return out
