"""YouTube channels via their public RSS feed, plus transcripts on demand."""

from __future__ import annotations

import logging

import feedparser

from ..config import YouTubeSource
from ..http import PoliteClient, SourceError
from ..models import Item
from .base import SourceTask, parse_time, strip_html

log = logging.getLogger(__name__)

FEED_URL = "https://www.youtube.com/feeds/videos.xml"


def parse_channel_feed(content: bytes, source_name: str, transcripts: bool) -> list[Item]:
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise SourceError("feed could not be parsed", "bad-data")
    items: list[Item] = []
    for e in feed.entries:
        vid = e.get("yt_videoid")
        if not vid:
            continue
        desc = e.get("media_description") or e.get("summary") or ""
        items.append(
            Item(
                source="youtube",
                source_name=source_name,
                uid=f"yt:{vid}",
                url=f"https://www.youtube.com/watch?v={vid}",
                title=strip_html(e.get("title", "(untitled)"), 300),
                text=strip_html(desc),
                author=e.get("author"),
                published_at=parse_time(e.get("published_parsed")),
                extra={"video_id": vid, "want_transcript": transcripts},
            )
        )
    return items


def fetch_transcript(video_id: str, max_chars: int = 20000) -> str | None:
    """Best-effort English transcript. Returns None when there isn't one."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        fetched = YouTubeTranscriptApi().fetch(video_id, languages=["en", "en-US", "en-GB"])
        text = " ".join(snippet.text.replace("\n", " ") for snippet in fetched)
        return text[:max_chars] or None
    except Exception as e:  # the library raises many specific types; all mean "no transcript"
        log.info("no transcript for %s: %s", video_id, type(e).__name__)
        return None


def tasks(cfgs: list[YouTubeSource]) -> list[SourceTask]:
    def make(c: YouTubeSource) -> SourceTask:
        def fetch(client: PoliteClient) -> list[Item]:
            resp = client.get(FEED_URL, params={"channel_id": c.channel_id})
            return parse_channel_feed(resp.content, c.name, c.transcripts)

        return SourceTask(key=f"youtube:{c.channel_id}", label=f"YouTube: {c.name}", fetch=fetch)

    return [make(c) for c in cfgs]
