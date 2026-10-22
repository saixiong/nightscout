"""Adding sources from the Sources page: find feeds, resolve YouTube channels, validate input.

Every fetch goes through PoliteClient.get_public, so a pasted address can never make Night
Scout reach localhost or the LAN. Errors meant for the user are ValueErrors with a plain
message; nothing is added unless the input checks out.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser

from .config import (
    BlueskySource,
    Config,
    GitHubSource,
    HackerNewsSource,
    MastodonSource,
    RedditSource,
    RssKind,
    RssSource,
    Sources,
    YouTubeSource,
)
from .http import PoliteClient, SourceError
from .sources import configured_tasks

SOURCE_TYPES = (
    "website",
    "newsletter",
    "podcast",
    "arxiv",
    "youtube",
    "reddit",
    "hackernews",
    "bluesky",
    "mastodon",
    "github",
)
COMMON_FEED_PATHS = ("/feed", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml")
FEED_LINK_TYPES = {"application/rss+xml", "application/atom+xml"}
MAX_ALTERNATES = 5
MAX_VALUE_LEN = 300
DEFAULT_MASTODON = "mastodon.social"
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
YOUTUBE_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id="

_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_HANDLE = re.compile(r"^[A-Za-z0-9._-]{3,30}$")
_SUBREDDIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]{1,20}$")
_HOSTNAME = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_BSKY_HANDLE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_BSKY_DID = re.compile(r"^did:(?:plc|web):[a-z0-9._:-]{8,100}$")
_GH_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_HASHTAG = re.compile(r"^\w{1,100}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# arXiv archives. Sub-categories are two capital letters, except physics.* and cond-mat.*,
# which are lowercase words; the archives in _ARXIV_FLAT have none.
_ARXIV_UPPER = {"cs", "math", "stat", "eess", "econ", "q-bio", "q-fin", "astro-ph", "nlin"}
_ARXIV_LOWER = {"physics", "cond-mat"}
_ARXIV_FLAT = {
    "gr-qc", "hep-ex", "hep-lat", "hep-ph", "hep-th", "math-ph", "nucl-ex", "nucl-th", "quant-ph",
}  # fmt: skip


# -- feeds -------------------------------------------------------------------------------
def normalize_web_url(value: str) -> str:
    """https:// is assumed when the scheme is missing; only http(s) with a real host."""
    v = value.strip()
    if not v or len(v) > MAX_VALUE_LEN or _CONTROL.search(v) or " " in v:
        raise ValueError("That doesn't look like a web address.")
    if "://" not in v:
        v = "https://" + v
    parts = urlsplit(v)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("Only http:// and https:// addresses can be followed.")
    if "@" in parts.netloc:
        raise ValueError("Addresses with a user name or password can't be followed.")
    if "." not in host and ":" not in host:
        raise ValueError(f"{host} isn't a public web address.")
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))


def _feed_key(url: str) -> str:
    """For duplicate checks: scheme, `www.` and a trailing slash don't make a new feed."""
    p = urlsplit(url.strip())
    host = (p.netloc or "").lower().removeprefix("www.")
    q = f"?{p.query}" if p.query else ""
    return f"{host}{p.path.rstrip('/')}{q}"


def _parse_feed(content: bytes) -> tuple[bool, str]:
    parsed = feedparser.parse(content)
    if not parsed.get("version"):
        return False, ""
    return True, str(parsed.feed.get("title") or "").strip()


class _AlternateLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        rels = a.get("rel", "").lower().split()
        if "alternate" in rels and a.get("type", "").lower().strip() in FEED_LINK_TYPES:
            href = a.get("href", "").strip()
            if href:
                self.hrefs.append(href)


def _alternates(html: bytes, base: str) -> list[str]:
    parser = _AlternateLinks()
    with contextlib.suppress(Exception):  # malformed HTML: use what was found so far
        parser.feed(html.decode("utf-8", errors="replace"))
    out: list[str] = []
    for href in parser.hrefs:
        url = urljoin(base, href)
        if urlsplit(url).scheme in ("http", "https") and url not in out:
            out.append(url)
    return out[:MAX_ALTERNATES]


def find_feed(url: str, client: PoliteClient) -> tuple[str, str]:
    """(feed URL, feed title) for a site or feed address. ValueError if there is none."""
    url = normalize_web_url(url)
    try:
        resp = client.get_public(url)
    except SourceError as e:
        raise ValueError(f"Couldn't open {url} ({e}).") from e
    ok, title = _parse_feed(resp.content)
    if ok:
        return url, title
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    candidates = _alternates(resp.content, url)
    candidates += [origin + p for p in COMMON_FEED_PATHS if origin + p not in candidates]
    for cand in candidates:
        try:
            r = client.get_public(cand)
        except SourceError:
            continue
        ok, title = _parse_feed(r.content)
        if ok:
            return cand, title
    raise ValueError(
        f"No RSS or Atom feed found at {url}. Sites without a feed can't be followed "
        "(Night Scout doesn't scrape pages)."
    )


def discover_feed(url: str, client: PoliteClient) -> str:
    """The feed URL for `url`: the URL itself if it is a feed, else the page's
    `<link rel="alternate">` feed, else one of a few common feed paths."""
    return find_feed(url, client)[0]


# -- YouTube -----------------------------------------------------------------------------
_YT_ID_PATTERNS = (
    re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"'),
    re.compile(r'"externalId":"(UC[\w-]{22})"'),
    re.compile(r'<meta itemprop="identifier" content="(UC[\w-]{22})"'),
    re.compile(r'"browseId":"(UC[\w-]{22})"'),
)


def _youtube_target(value: str) -> tuple[str | None, str | None]:
    """(channel_id, page_url): exactly one is set. ValueError for anything else."""
    v = value.strip()
    if not v or len(v) > MAX_VALUE_LEN or _CONTROL.search(v):
        raise ValueError("Paste a channel address or an @handle.")
    if _CHANNEL_ID.match(v):
        return v, None
    if v.startswith("@") or (_HANDLE.match(v) and "." not in v):
        handle = v.removeprefix("@")
        if not _HANDLE.match(handle):
            raise ValueError(f"{v!r} isn't a valid YouTube @handle.")
        return None, f"https://www.youtube.com/@{handle}"
    url = v if "://" in v else "https://" + v
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() not in YOUTUBE_HOSTS:
        raise ValueError("That isn't a YouTube channel address (youtube.com/@name).")
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) >= 2 and segs[0] == "channel" and _CHANNEL_ID.match(segs[1]):
        return segs[1], None
    if segs and segs[0].startswith("@") and _HANDLE.match(segs[0][1:]):
        return None, f"https://www.youtube.com/{segs[0]}"
    if len(segs) >= 2 and segs[0] in ("c", "user") and _HANDLE.match(segs[1]):
        return None, f"https://www.youtube.com/{segs[0]}/{segs[1]}"
    raise ValueError(
        "Paste a channel address (youtube.com/@name or youtube.com/channel/UC…), "
        "not a video or playlist link."
    )


def resolve_youtube(handle_or_url: str, client: PoliteClient) -> tuple[str, str]:
    """(channel_id, channel title) for an @handle, channel URL or channel id.

    The id is checked against the channel's public feed, which also supplies the title.
    """
    channel_id, page = _youtube_target(handle_or_url)
    if channel_id is None and page is not None:
        try:
            html = client.get_public(page).content.decode("utf-8", errors="replace")
        except SourceError as e:
            raise ValueError(f"Couldn't open {page} ({e}).") from e
        for pat in _YT_ID_PATTERNS:
            m = pat.search(html)
            if m:
                channel_id = m.group(1)
                break
        if channel_id is None:
            raise ValueError(f"Couldn't find a channel id on {page}.")
    assert channel_id is not None
    try:
        feed = client.get_public(YOUTUBE_FEED + channel_id)
    except SourceError as e:
        raise ValueError(f"YouTube has no public feed for channel {channel_id} ({e}).") from e
    ok, title = _parse_feed(feed.content)
    if not ok:
        raise ValueError(f"YouTube's feed for channel {channel_id} couldn't be read.")
    return channel_id, title or channel_id


# -- arXiv -------------------------------------------------------------------------------
def arxiv_category(value: str) -> str:
    """'cs.cl' → 'cs.CL'. ValueError for anything that isn't an arXiv category."""
    v = value.strip()
    v = re.sub(r"(?i)^arxiv[:\s]+", "", v).strip()
    m = re.match(r"^([A-Za-z-]+)(?:\.([A-Za-z-]{2,12}))?$", v)
    hint = "Use a category like cs.CL, cs.LG or stat.ML."
    if not m:
        raise ValueError(f"{value.strip()!r} isn't an arXiv category. {hint}")
    archive, sub = m.group(1).lower(), m.group(2)
    if archive in _ARXIV_FLAT:
        if sub:
            raise ValueError(f"arXiv's {archive} has no sub-categories. {hint}")
        return archive
    if sub is None and archive in _ARXIV_UPPER | _ARXIV_LOWER:
        # A whole archive is hundreds of papers a day; ask for one category.
        raise ValueError(f"Pick one category inside {archive}, e.g. {archive}.XX. {hint}")
    if archive in _ARXIV_UPPER and sub and re.fullmatch(r"[A-Za-z]{2}", sub):
        return f"{archive}.{sub.upper()}"
    if archive in _ARXIV_LOWER and sub:
        return f"{archive}.{sub.lower()}"
    raise ValueError(f"Unknown arXiv category {value.strip()!r}. {hint}")


# -- add_source --------------------------------------------------------------------------
def _clean_text(value: str, what: str, lo: int = 2, hi: int = 80) -> str:
    v = " ".join(value.split())
    if _CONTROL.search(value) or not (lo <= len(v) <= hi):
        raise ValueError(f"{what} must be {lo}-{hi} characters.")
    return v


def _has(values: list[str], v: str) -> bool:
    return v.casefold() in {x.casefold() for x in values}


def _unique_name(cfg: Config, name: str, url: str) -> str:
    taken = {r.name for r in cfg.sources.rss}
    if name not in taken:
        return name
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    return f"{name} ({host})"


def _add_rss(
    cfg: Config, kind: RssKind, value: str, client: PoliteClient, direct: bool = False
) -> None:
    pasted = _feed_key(normalize_web_url(value))
    for r in cfg.sources.rss:
        if _feed_key(r.url) == pasted:  # no need to fetch what's already followed
            raise ValueError(f"Already following that feed as {r.name!r}.")
    if direct:
        url = normalize_web_url(value)
        try:
            ok, title = _parse_feed(client.get_public(url).content)
        except SourceError as e:
            raise ValueError(f"Couldn't open {url} ({e}).") from e
        if not ok:
            # A podcast page rather than its feed: look for the feed the same way.
            url, title = find_feed(url, client)
    else:
        url, title = find_feed(value, client)
    for r in cfg.sources.rss:
        if _feed_key(r.url) == _feed_key(url):
            raise ValueError(f"Already following that feed as {r.name!r}.")
    host = (urlsplit(url).hostname or url).removeprefix("www.")
    name = _unique_name(cfg, " ".join(title.split())[:60] or host, url)
    cfg.sources.rss.append(RssSource(name=name, url=url, kind=kind))


def _add_arxiv(cfg: Config, value: str) -> None:
    cat = arxiv_category(value)
    url = f"https://rss.arxiv.org/rss/{cat}"
    for r in cfg.sources.rss:
        if _feed_key(r.url) == _feed_key(url):
            raise ValueError(f"Already following arXiv {cat}.")
    cfg.sources.rss.append(RssSource(name=f"arXiv {cat}", url=url, kind="arxiv"))


def _add_youtube(cfg: Config, value: str, client: PoliteClient) -> None:
    channel_id, title = resolve_youtube(value, client)
    for y in cfg.sources.youtube:
        if y.channel_id == channel_id:
            raise ValueError(f"Already following YouTube: {y.name}.")
    cfg.sources.youtube.append(
        YouTubeSource(name=" ".join(title.split())[:60], channel_id=channel_id)
    )


def _add_reddit(cfg: Config, value: str) -> None:
    v = value.strip()
    m = re.match(r"^(?:https?://)?(?:(?:www|old|new)\.)?reddit\.com/r/([^/?#\s]+)", v, re.I)
    sub = m.group(1) if m else re.sub(r"^/?r/", "", v)
    if not _SUBREDDIT.match(sub):
        raise ValueError(f"{v!r} isn't a subreddit name (letters, digits and _ only).")
    rd = cfg.sources.reddit or RedditSource()
    if _has([s.removeprefix("r/") for s in rd.subreddits], sub):
        raise ValueError(f"Already following r/{sub}.")
    rd.subreddits.append(sub)
    cfg.sources.reddit = rd


def _add_hackernews(cfg: Config, value: str) -> None:
    kw = _clean_text(value, "A Hacker News keyword", 2, 60)
    hn = cfg.sources.hackernews or HackerNewsSource(lists=[])
    if _has(hn.keywords, kw):
        raise ValueError(f'Already searching Hacker News for "{kw}".')
    hn.keywords.append(kw)
    cfg.sources.hackernews = hn


def _add_bluesky(cfg: Config, value: str) -> None:
    v = value.strip()
    m = re.match(r"^(?:https?://)?bsky\.app/profile/([^/?#\s]+)", v, re.I)
    bs = cfg.sources.bluesky or BlueskySource()
    if m or v.startswith("@"):
        actor = (m.group(1) if m else v[1:]).lower()
        if not (_BSKY_HANDLE.match(actor) or _BSKY_DID.match(actor)):
            raise ValueError(f"{v!r} isn't a Bluesky handle like @name.bsky.social.")
        if _has(bs.actors, actor):
            raise ValueError(f"Already following @{actor} on Bluesky.")
        bs.actors.append(actor)
    else:
        q = _clean_text(v, "A Bluesky search", 2, 80)
        if _has(bs.queries, q):
            raise ValueError(f'Already searching Bluesky for "{q}".')
        bs.queries.append(q)
    cfg.sources.bluesky = bs


def _add_mastodon(cfg: Config, value: str) -> None:
    v = value.strip()
    tag, _, instance = v.partition("@")
    tag = tag.strip().lstrip("#")
    instance = (instance.strip() or DEFAULT_MASTODON).lower()
    instance = instance.removeprefix("https://").removeprefix("http://").strip("/")
    if not _HASHTAG.match(tag):
        raise ValueError(f"{v!r} isn't a hashtag. Use #tag or tag@server.example.")
    if not _HOSTNAME.match(instance):
        raise ValueError(f"{instance!r} isn't a Mastodon server name like mastodon.social.")
    for m in cfg.sources.mastodon:
        if m.instance.lower() == instance:
            if _has([h.lstrip("#") for h in m.hashtags], tag):
                raise ValueError(f"Already following #{tag} on {instance}.")
            m.hashtags.append(tag)
            return
    cfg.sources.mastodon.append(MastodonSource(instance=instance, hashtags=[tag]))


def _add_github(cfg: Config, value: str) -> None:
    v = value.strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/([^/\s]+/[^/?#\s]+)", v, re.I)
    gh = cfg.sources.github or GitHubSource()
    repo = (m.group(1) if m else v).removesuffix(".git")
    if m or ("/" in v and " " not in v and ":" not in v):
        if not _GH_REPO.match(repo):
            raise ValueError(f"{v!r} isn't a GitHub owner/repo.")
        if _has(gh.releases, repo):
            raise ValueError(f"Already following {repo} releases.")
        gh.releases.append(repo)
    else:
        q = _clean_text(v, "A GitHub search", 2, 100)
        if _has(gh.search, q):
            raise ValueError(f'Already searching GitHub for "{q}".')
        gh.search.append(q)
    cfg.sources.github = gh


class AlreadyFollowing(ValueError):
    """The source resolves to one that's already configured (its task `key` and `label`)."""

    def __init__(self, key: str | None, label: str | None, message: str | None = None) -> None:
        super().__init__(message or "That source is already in the list.")
        self.key = key
        self.label = label


def _adders(work: Config, value: str, http: PoliteClient) -> dict[str, Callable[[], None]]:
    return {
        "website": lambda: _add_rss(work, "website", value, http),
        "newsletter": lambda: _add_rss(work, "newsletter", value, http),
        "podcast": lambda: _add_rss(work, "podcast", value, http, direct=True),
        "arxiv": lambda: _add_arxiv(work, value),
        "youtube": lambda: _add_youtube(work, value, http),
        "reddit": lambda: _add_reddit(work, value),
        "hackernews": lambda: _add_hackernews(work, value),
        "bluesky": lambda: _add_bluesky(work, value),
        "mastodon": lambda: _add_mastodon(work, value),
        "github": lambda: _add_github(work, value),
    }


def add_source(cfg: Config, type: str, value: str, client: PoliteClient | None = None) -> str:
    """Validate `value` for source `type`, add it to `cfg` (the caller saves config.yaml)
    and return the new source's label as the Sources page shows it.

    website/newsletter: a site address (its feed is discovered). podcast: the feed URL
    (or a page that links it). arxiv: a category like cs.CL. youtube: @handle or channel
    URL. reddit: a subreddit. hackernews: a keyword. bluesky: @handle or a search.
    mastodon: #tag (mastodon.social) or tag@server. github: owner/repo, else a repo search.
    Raises ValueError with a message fit to show the user (AlreadyFollowing for a duplicate).
    """
    return add_source_keyed(cfg, type, value, client)[1]


def add_source_keyed(
    cfg: Config, type: str, value: str, client: PoliteClient | None = None
) -> tuple[str, str]:
    """`add_source`, returning (task key, label) of the new source."""
    if type == "x":
        raise ValueError(
            "X has no free public API and its terms don't allow scraping, so Night Scout "
            "can't follow X accounts. Many of the same people post on Bluesky or Mastodon."
        )
    if type not in SOURCE_TYPES:
        raise ValueError(f"Unknown source type {type!r}. Choose one of: {', '.join(SOURCE_TYPES)}.")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Enter something to follow.")
    if len(value) > MAX_VALUE_LEN:
        raise ValueError("That's too long to be a source.")

    before = {t.key for t in configured_tasks(cfg)}
    work = cfg.model_copy(deep=True)
    own_client = client is None
    http = client or PoliteClient(contact=cfg.contact)

    def existing() -> tuple[str | None, str | None]:
        """Which configured source `value` is: resolve it against an empty source list."""
        probe = cfg.model_copy(deep=True)
        probe.sources = Sources()
        try:
            _adders(probe, value, http)[type]()
        except (ValueError, SourceError):
            return None, None
        found = configured_tasks(probe)
        return (found[0].key, found[0].label) if found and found[0].key in before else (None, None)

    try:
        try:
            _adders(work, value, http)[type]()
        except ValueError as e:
            if str(e).startswith("Already following"):  # the adders' own duplicate check
                raise AlreadyFollowing(*existing(), message=str(e)) from e
            raise
        new = [t for t in configured_tasks(work) if t.key not in before]
        if not new:
            raise AlreadyFollowing(*existing())
    finally:
        if own_client:
            http.close()
    work.sources.disabled = [k for k in work.sources.disabled if k not in {t.key for t in new}]
    cfg.sources = work.sources
    return new[0].key, new[0].label
