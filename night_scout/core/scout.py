"""Scout key terms: find the posts on a source that mention the terms someone cares about.

Two moments use this:
  * when a source is added (or its terms change), a one-off search: a YouTube channel is
    searched as a whole, up to MAX_PER_TERM videos per term, any age; a website, newsletter or
    subreddit only has its feed checked (no scraping: posts the feed no longer lists are out
    of reach);
  * every night, `fetch.fetch_all` tags new items that mention a term (`tag_matches`).
A tagged item carries `extra["scout_terms"]` and the pipeline scores it before anything else.

A term matches a whole word or phrase in the title or text, in any case.

Whole-channel search uses the YouTube Data API when a key is stored (keychain id
YOUTUBE_KEY_ID, or the YOUTUBE_API_KEY environment variable), otherwise yt-dlp, which reads
YouTube's public pages and can break when YouTube changes them.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

from .config import Config
from .db import Store
from .http import PoliteClient, SourceError
from .models import Item
from .secrets import SecretStoreUnavailable, get_key, key_source, masked, scrub
from .sources import SourceTask, configured_tasks
from .sources.base import parse_time, strip_html

log = logging.getLogger(__name__)

YOUTUBE_KEY_ID = "youtube-data-api"
YOUTUBE_KEY_ENV = "YOUTUBE_API_KEY"
API_BASE = "https://www.googleapis.com/youtube/v3"
MAX_PER_TERM = 10
YTDLP_TIMEOUT = 90  # seconds per yt-dlp call
YTDLP_WORKERS = 4  # yt-dlp processes at once (polite: one channel, a handful of pages)
YTDLP_CHUNK = 3  # videos per yt-dlp call when reading descriptions

_CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")
_VIDEO_ID = re.compile(r"^[\w-]{11}$")

Via = Literal["api", "yt-dlp", "feed"]
Runner = Callable[[Sequence[str], int], str]  # (argv, timeout) -> stdout


class ScoutError(Exception):
    """A search that couldn't run. The message is fit to show the user."""


def match_terms(terms: Iterable[str], *texts: str | None) -> list[str]:
    """The terms that appear as a whole word or phrase in any of `texts`, in any case."""
    hay = "\n".join(t for t in texts if t)
    if not hay:
        return []
    return [t for t in terms if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", hay, re.IGNORECASE)]


def tag_matches(item: Item, terms: Sequence[str]) -> list[str]:
    """Tag `item` with the terms it mentions (title or text). Returns them."""
    hits = match_terms(terms, item.title, item.text) if terms else []
    if hits:
        item.extra["scout_terms"] = hits
    return hits


@dataclass
class TermResult:
    term: str
    checked: int = 0  # results looked at (YouTube's search results, or the feed's items)
    matched: int = 0  # of those, how many really mention the term

    def to_dict(self) -> dict[str, Any]:
        return {"term": self.term, "checked": self.checked, "matched": self.matched}


@dataclass
class ScoutResult:
    via: Via
    terms: list[TermResult]
    items: list[Item] = field(default_factory=list)  # unique matches, newest first
    queued: int = 0  # how many of them will be scored first (not already judged)

    def to_dict(self, look_back_days: int) -> dict[str, Any]:
        return {
            "via": self.via,
            "look_back_days": look_back_days,
            "terms": [t.to_dict() for t in self.terms],
            "queued": self.queued,
            "matches": [
                {
                    "title": i.title,
                    "url": i.url,
                    "date": i.published_at.date().isoformat() if i.published_at else "",
                    "terms": i.extra.get("scout_terms", []),
                }
                for i in self.items
            ],
        }


# -- YouTube: whole-channel search ---------------------------------------------------------
def youtube_api_key() -> str | None:
    return get_key(YOUTUBE_KEY_ID, YOUTUBE_KEY_ENV)


def youtube_key_view() -> dict[str, Any] | None:
    """{configured, source, masked} for the Settings page; None when there's no key."""
    try:
        key = youtube_api_key()
    except SecretStoreUnavailable:
        return {"configured": None, "unreadable": True, "masked": ""}
    if not key:
        return None
    return {
        "configured": True,
        "source": key_source(YOUTUBE_KEY_ID, YOUTUBE_KEY_ENV) or "keychain",
        "masked": masked(key),
    }


def ytdlp_command() -> list[str] | None:
    """How to run yt-dlp here: the Python module if installed, else a yt-dlp on PATH."""
    try:
        import yt_dlp  # type: ignore[import-not-found]  # noqa: F401

        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        exe = shutil.which("yt-dlp")
        return [exe] if exe else None


def _run(argv: Sequence[str], timeout: int) -> str:
    r = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    if r.returncode != 0 and not r.stdout.strip():
        raise ScoutError(f"yt-dlp failed: {(r.stderr or '').strip().splitlines()[-1:] or ['?']}")
    return r.stdout


def _video_item(
    vid: str, title: str, text: str, published: datetime | None, channel: str, transcripts: bool
) -> Item:
    return Item(
        source="youtube",
        source_name=channel,
        uid=f"yt:{vid}",
        url=f"https://www.youtube.com/watch?v={vid}",
        title=strip_html(title, 300) or "(untitled)",
        text=strip_html(text),
        published_at=published,
        extra={"video_id": vid, "want_transcript": transcripts},
    )


def _search_api(
    channel_id: str, term: str, key: str, client: PoliteClient
) -> list[tuple[str, str, str, datetime | None]]:
    headers = {"X-Goog-Api-Key": key}  # a header, so the key never lands in a URL or log
    try:
        resp = client.get(
            f"{API_BASE}/search",
            params={
                "part": "snippet",
                "channelId": channel_id,
                "q": term,
                "type": "video",
                "maxResults": MAX_PER_TERM,
                "order": "relevance",
            },
            headers=headers,
        )
        ids = [
            x["id"]["videoId"]
            for x in resp.json().get("items", [])
            if _VIDEO_ID.match(str(x.get("id", {}).get("videoId", "")))
        ]
        if not ids:
            return []
        # search.list cuts descriptions short; videos.list has them in full (1 quota unit).
        full = client.get(
            f"{API_BASE}/videos", params={"part": "snippet", "id": ",".join(ids)}, headers=headers
        ).json()
    except SourceError as e:
        why = scrub(str(e), key)
        if any(code in why for code in ("400", "401", "403")):
            raise ScoutError(
                f"YouTube rejected the API key or it's out of quota ({why}). "
                "Check it in Settings → Chat & search, or remove it to use yt-dlp."
            ) from e
        raise ScoutError(f"YouTube Data API: {why}") from e
    except (ValueError, KeyError, TypeError) as e:
        raise ScoutError("YouTube Data API sent an answer Night Scout couldn't read.") from e
    by_id = {v.get("id"): v.get("snippet", {}) for v in full.get("items", [])}
    out = []
    for vid in ids:
        sn = by_id.get(vid)
        if sn:
            out.append(
                (
                    vid,
                    sn.get("title", ""),
                    sn.get("description", ""),
                    parse_time(sn.get("publishedAt")),
                )
            )
    return out


def _ytdlp_base(cmd: list[str]) -> list[str]:
    return [*cmd, "--ignore-config", "--no-cache-dir", "--no-warnings"]


def _ytdlp_listing(channel_id: str, term: str, cmd: list[str], run: Runner) -> list[str]:
    """Video ids from the channel's own search page for `term` (YouTube's order)."""
    url = f"https://www.youtube.com/channel/{channel_id}/search?query={quote(term)}"
    argv = [*_ytdlp_base(cmd), "--flat-playlist", "--playlist-end", str(MAX_PER_TERM), "-J", url]
    try:
        listing = json.loads(run(argv, YTDLP_TIMEOUT) or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        raise ScoutError(f"yt-dlp couldn't search the channel ({type(e).__name__}).") from e
    return [
        str(e.get("id"))
        for e in listing.get("entries") or []
        if _VIDEO_ID.match(str(e.get("id", ""))) and e.get("ie_key", "Youtube") == "Youtube"
    ][:MAX_PER_TERM]


def _ytdlp_meta(ids: list[str], cmd: list[str], run: Runner) -> dict[str, dict[str, Any]]:
    """Title, description and date for each video, read a few at a time in parallel."""
    chunks = [ids[i : i + YTDLP_CHUNK] for i in range(0, len(ids), YTDLP_CHUNK)]

    def one(chunk: list[str]) -> str:
        argv = [*_ytdlp_base(cmd), "--skip-download", "--ignore-errors", "-j", "--", *chunk]
        return run(argv, YTDLP_TIMEOUT)

    meta: dict[str, dict[str, Any]] = {}
    try:
        with ThreadPoolExecutor(max_workers=YTDLP_WORKERS) as pool:
            outputs = list(pool.map(one, chunks))
    except subprocess.TimeoutExpired as e:
        raise ScoutError("yt-dlp took too long reading the videos.") from e
    for line in "\n".join(outputs).splitlines():
        try:
            v = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(v, dict) and v.get("id"):
            meta[str(v["id"])] = v
    return meta


def _search_ytdlp(
    channel_id: str, terms: Sequence[str], cmd: list[str], run: Runner
) -> dict[str, list[tuple[str, str, str, datetime | None]]]:
    """Per term: (id, title, description, date) of YouTube's results, each video read once."""
    with ThreadPoolExecutor(max_workers=YTDLP_WORKERS) as pool:
        listings = dict(
            zip(
                terms,
                pool.map(lambda t: _ytdlp_listing(channel_id, t, cmd, run), terms),
                strict=True,
            )
        )
    unique = list(dict.fromkeys(v for ids in listings.values() for v in ids))
    meta = _ytdlp_meta(unique, cmd, run) if unique else {}
    out: dict[str, list[tuple[str, str, str, datetime | None]]] = {}
    for term, ids in listings.items():
        rows = []
        for vid in ids:
            v = meta.get(vid)
            if not v:
                continue
            d = str(v.get("upload_date") or "")
            when = (
                datetime(int(d[:4]), int(d[4:6]), int(d[6:8]), tzinfo=UTC)
                if len(d) == 8 and d.isdigit()
                else None
            )
            rows.append((vid, str(v.get("title") or ""), str(v.get("description") or ""), when))
        out[term] = rows
    return out


def search_youtube_channel(
    channel_id: str,
    channel_name: str,
    terms: Sequence[str],
    client: PoliteClient,
    *,
    transcripts: bool = True,
    api_key: str | None = None,
    ytdlp: list[str] | None = None,
    run: Runner = _run,
) -> ScoutResult:
    """Search the whole channel for each term; keep the videos that really mention it."""
    if not _CHANNEL_ID.match(channel_id):
        raise ScoutError("That isn't a YouTube channel id.")
    via: Via = "api" if api_key else "yt-dlp"
    if not api_key and not ytdlp:
        raise ScoutError(
            "Searching a whole channel needs a YouTube Data API key (Settings → Chat & search) "
            "or yt-dlp (pip install yt-dlp)."
        )
    found: dict[str, Item] = {}
    results: list[TermResult] = []
    by_term = None if api_key else _search_ytdlp(channel_id, terms, ytdlp or [], run)
    for term in terms:
        hits = (
            by_term[term]
            if by_term is not None
            else _search_api(channel_id, term, api_key or "", client)
        )
        tr = TermResult(term, checked=len(hits))
        for vid, title, text, when in hits:
            if not match_terms([term], title, text):
                continue
            tr.matched += 1
            item = found.get(vid) or _video_item(vid, title, text, when, channel_name, transcripts)
            item.extra["scout_terms"] = match_terms(terms, item.title, item.text)
            found[vid] = item
        results.append(tr)
    items = sorted(
        found.values(),
        key=lambda i: i.published_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return ScoutResult(via, results, items)


# -- feeds: websites, newsletters, podcasts, arXiv, subreddits ------------------------------
def search_feed(task: SourceTask, terms: Sequence[str], client: PoliteClient) -> ScoutResult:
    """Check every item the feed still lists (any age) for the terms."""
    items = task.fetch(client)
    found: list[Item] = []
    results = [TermResult(t, checked=len(items)) for t in terms]
    for item in items:
        hits = tag_matches(item, terms)
        for tr in results:
            if tr.term in hits:
                tr.matched += 1
        if hits:
            found.append(item)
    found.sort(key=lambda i: i.published_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return ScoutResult("feed", results, found)


# -- storing ------------------------------------------------------------------------------
def queue_matches(store: Store, items: Iterable[Item]) -> int:
    """Store matches so the next run scores them first. Items already judged are left alone;
    ones a run saw but didn't pick are put back in the queue. Returns how many are queued."""
    queued = 0
    for item in items:
        terms = item.extra.get("scout_terms") or []
        res = store.upsert_item(item, None)
        row = store.item(res.item_id)
        if row is None or row["status"] == "duplicate":
            continue
        if res.new:
            queued += 1
            continue
        if row["status"] in ("new", "candidate", "filtered"):
            store.update_extra(row["id"], scout_terms=terms)
            store.requeue_item(row["id"])
            queued += 1
    return queued


def run_scout(
    cfg: Config,
    store: Store,
    key: str,
    terms: Sequence[str],
    client: PoliteClient,
    *,
    ytdlp: list[str] | None = None,
    run: Runner = _run,
) -> ScoutResult:
    """The one-off search for source `key` (a task key), with matches queued for tonight."""
    task = next((t for t in configured_tasks(cfg) if t.key == key), None)
    if task is None:
        raise ScoutError(f"No source with key {key!r}.")
    typ, _, detail = key.partition(":")
    if typ == "youtube":
        yt = next(y for y in cfg.sources.youtube if y.channel_id == detail)
        result = search_youtube_channel(
            yt.channel_id,
            yt.name,
            terms,
            client,
            transcripts=yt.transcripts,
            api_key=youtube_api_key(),
            ytdlp=ytdlp if ytdlp is not None else ytdlp_command(),
            run=run,
        )
    elif typ in ("rss", "reddit"):
        try:
            result = search_feed(task, terms, client)
        except SourceError as e:
            raise ScoutError(f"Couldn't read the feed: {e}") from e
    else:
        raise ScoutError("Key terms work on YouTube channels, feeds and subreddits.")
    result.queued = queue_matches(store, result.items)
    return result
