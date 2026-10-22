"""youtube_transcript and hn_search."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from ..sources.youtube import fetch_transcript
from .base import ToolContext, clip, fail, http_client

TRANSCRIPT_LIMIT = 8000
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_HOSTS = frozenset(("youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"))


def video_id(video: str) -> str | None:
    """An 11-character id from a bare id or any common YouTube URL form."""
    v = video.strip()
    if _VIDEO_ID.match(v):
        return v
    parts = urlsplit(v if "://" in v else "https://" + v)
    host = (parts.hostname or "").lower().removeprefix("www.")
    candidate = ""
    if host == "youtu.be":
        candidate = parts.path.strip("/").split("/")[0]
    elif host in _YT_HOSTS:
        if parts.path == "/watch":
            candidate = (parse_qs(parts.query).get("v") or [""])[0]
        else:
            m = re.match(r"^/(?:shorts|embed|live|v)/([^/?#]+)", parts.path)
            candidate = m.group(1) if m else ""
    return candidate if _VIDEO_ID.match(candidate) else None


def _stored_transcript(ctx: ToolContext, vid: str) -> str:
    """The excerpt saved by the nightly run, if this video was one of its items."""
    if ctx.store is None:
        return ""
    rows = ctx.store.conn.execute(
        "SELECT extra FROM items WHERE source='youtube' AND extra LIKE ?", (f'%"{vid}"%',)
    ).fetchall()
    for r in rows:
        try:
            extra = json.loads(r["extra"] or "{}")
        except ValueError:
            continue
        if extra.get("video_id") == vid and extra.get("transcript_excerpt"):
            return str(extra["transcript_excerpt"])
    return ""


def youtube_transcript(ctx: ToolContext, video: Any = "") -> dict[str, Any]:
    vid = video_id(str(video or ""))
    if vid is None:
        return fail("Give a YouTube video URL or its 11-character id.")
    fetch = ctx.transcript_fn or fetch_transcript
    try:
        text = fetch(vid)
    except Exception as e:  # the library raises many types; all mean "no transcript"
        text, error = None, type(e).__name__
    else:
        error = ""
    source = "YouTube captions (English)"
    if not text:
        text = _stored_transcript(ctx, vid)
        source = "excerpt saved by last night's run (live captions unavailable)"
    if not text:
        why = f" ({error})" if error else ""
        return fail(f"No English transcript is available for video {vid}{why}.")
    text = " ".join(text.split())
    clipped, more = clip(text, TRANSCRIPT_LIMIT)
    out: dict[str, Any] = {
        "video_id": vid,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "source": source,
        "chars": len(text),
        "transcript": clipped,
    }
    if more:
        out["truncated"] = True
    return out


HN_API = "https://hn.algolia.com/api/v1/search"


def hn_search(ctx: ToolContext, query: Any = "") -> dict[str, Any]:
    query = " ".join(str(query or "").split())[:200]
    if not query:
        return fail("Give a search query.")
    try:
        with http_client(ctx) as client:
            resp = client.get(HN_API, params={"query": query, "tags": "story", "hitsPerPage": 6})
    except httpx.TimeoutException:
        return fail("Hacker News search timed out.")
    except httpx.HTTPError as e:
        return fail(f"Couldn't reach Hacker News search ({type(e).__name__}).")
    if resp.status_code >= 400:
        return fail(f"Hacker News search answered HTTP {resp.status_code}.")
    try:
        hits = resp.json().get("hits") or []
    except (ValueError, AttributeError):
        return fail("Hacker News search returned something that isn't JSON.")
    out = []
    for h in hits[:6]:
        if not isinstance(h, dict) or not h.get("objectID"):
            continue
        discussion = f"https://news.ycombinator.com/item?id={h['objectID']}"
        url = str(h.get("url") or "")
        out.append(
            {
                "title": " ".join(str(h.get("title") or "").split())[:200],
                "url": url if url.startswith(("http://", "https://")) else discussion,
                "points": h.get("points"),
                "comments": h.get("num_comments"),
                "date": str(h.get("created_at") or "")[:10],
                "discussion": discussion,
            }
        )
    return {"query": query, "hits": out}
