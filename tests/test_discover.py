from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from night_scout.core.config import (
    Config,
    GitHubSource,
    HackerNewsSource,
    MastodonSource,
    RssSource,
    Sources,
    YouTubeSource,
)
from night_scout.core.discover import (
    add_source,
    arxiv_category,
    discover_feed,
    resolve_youtube,
)
from night_scout.core.http import PoliteClient
from night_scout.core.sources import all_tasks, set_source_enabled, source_catalog

from .conftest import fixture_bytes

ATOM = fixture_bytes("blog_atom.xml")  # titled "Example Blog"
YT_FEED = fixture_bytes("youtube_channel.xml")  # titled "Example Channel"
CHANNEL = "UC" + "a" * 22
HOSTS = {
    "blog.example.com": "93.184.215.14",
    "www.blog.example.com": "93.184.215.14",
    "nofeed.example.com": "93.184.215.15",
    "paths.example.com": "93.184.215.16",
    "www.youtube.com": "142.250.1.1",
    "pod.example.net": "93.184.215.17",
    "evil.example": "10.0.0.7",
    "localhost": "127.0.0.1",
}
Answer = bytes | int | Callable[[], httpx.Response]


def public_client(routes: dict[str, Answer]) -> tuple[PoliteClient, list[str]]:
    """Routes are keyed by 'host/path?query' (as the site sees it, not the pinned IP)."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = f"?{request.url.query.decode()}" if request.url.query else ""
        where = f"{request.headers['host']}{request.url.path}{query}"
        seen.append(where)
        if where not in routes:
            return httpx.Response(404, text="not found")
        answer = routes[where]
        if isinstance(answer, int):
            return httpx.Response(answer)
        if callable(answer):
            return answer()
        return httpx.Response(200, content=answer)

    client = PoliteClient(transport=httpx.MockTransport(handler), sleep=lambda s: None)

    def resolve(host: str, port: int) -> list[tuple]:
        if host not in HOSTS:
            raise OSError("unknown host")
        return [(None, None, None, None, (HOSTS[host], port))]

    client._resolve = resolve
    return client, seen


# -- discover_feed -------------------------------------------------------------------------
def test_discover_feed_url_is_itself_a_feed() -> None:
    client, seen = public_client({"blog.example.com/feed.atom": ATOM})
    assert discover_feed("https://blog.example.com/feed.atom", client) == (
        "https://blog.example.com/feed.atom"
    )
    assert seen == ["blog.example.com/feed.atom"]


def test_discover_feed_link_rel_alternate_relative_href() -> None:
    page = (
        b"<html><head><title>Blog</title>"
        b'<link rel="stylesheet" href="/s.css">'
        b'<link rel="alternate" type="application/atom+xml" title="Feed" href="/atom/everything/">'
        b"</head><body>hi</body></html>"
    )
    client, _ = public_client(
        {"blog.example.com/": page, "blog.example.com/atom/everything/": ATOM}
    )
    # scheme-less input gets https://
    assert discover_feed("blog.example.com", client) == "https://blog.example.com/atom/everything/"


def test_discover_feed_common_path_fallback() -> None:
    client, seen = public_client(
        {"paths.example.com/blog": b"<html>no links</html>", "paths.example.com/rss.xml": ATOM}
    )
    assert discover_feed("https://paths.example.com/blog", client) == (
        "https://paths.example.com/rss.xml"
    )
    assert seen == ["paths.example.com/blog", "paths.example.com/feed", "paths.example.com/rss.xml"]


def test_discover_feed_none_found_is_a_friendly_error() -> None:
    client, seen = public_client({"nofeed.example.com/": b"<html>plain page</html>"})
    with pytest.raises(ValueError, match="No RSS or Atom feed found"):
        discover_feed("https://nofeed.example.com", client)
    assert len(seen) == 1 + 5  # the page, then at most five common paths


@pytest.mark.parametrize(
    "url",
    ["http://localhost:8080/feed", "https://evil.example/feed", "http://10.0.0.1/feed"],
)
def test_discover_feed_refuses_non_public_addresses(url: str) -> None:
    client, seen = public_client({})
    with pytest.raises(ValueError, match=r"(not|isn't) a public"):
        discover_feed(url, client)
    assert seen == []


@pytest.mark.parametrize("bad", ["", "ftp://x.example/feed", "file:///etc/passwd", "not a url"])
def test_discover_feed_rejects_non_web_input(bad: str) -> None:
    client, seen = public_client({})
    with pytest.raises(ValueError):
        discover_feed(bad, client)
    assert seen == []


def test_discover_feed_alternate_to_private_host_is_skipped() -> None:
    page = b'<link rel="alternate" type="application/rss+xml" href="https://evil.example/feed">'
    client, seen = public_client({"blog.example.com/": page, "blog.example.com/feed": ATOM})
    assert discover_feed("https://blog.example.com/", client) == "https://blog.example.com/feed"
    assert not any(s.startswith("evil.example") for s in seen)


# -- YouTube -------------------------------------------------------------------------------
def test_resolve_youtube_handle() -> None:
    page = (
        f'<html><head><link rel="canonical" href="https://www.youtube.com/channel/{CHANNEL}">'
        f'</head><body>"channelId":"UC{"b" * 22}"</body></html>'
    ).encode()
    client, seen = public_client(
        {
            "www.youtube.com/@fireship": page,
            f"www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}": YT_FEED,
        }
    )
    assert resolve_youtube("@fireship", client) == (CHANNEL, "Example Channel")
    assert seen[0] == "www.youtube.com/@fireship"


def test_resolve_youtube_channel_url_needs_no_page_fetch() -> None:
    client, seen = public_client(
        {f"www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}": YT_FEED}
    )
    url = f"https://www.youtube.com/channel/{CHANNEL}/videos"
    assert resolve_youtube(url, client) == (CHANNEL, "Example Channel")
    assert seen == [f"www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}"]


def test_resolve_youtube_handle_url_and_external_id() -> None:
    page = f'<script>var x = {{"externalId":"{CHANNEL}"}};</script>'.encode()
    client, _ = public_client(
        {
            "www.youtube.com/@someone": page,
            f"www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}": YT_FEED,
        }
    )
    assert resolve_youtube("youtube.com/@someone", client)[0] == CHANNEL


@pytest.mark.parametrize(
    ("value", "msg"),
    [
        ("https://www.youtube.com/watch?v=abcdefghijk", "not a video"),
        ("https://vimeo.com/@x", "isn't a YouTube channel"),
        ("@a", "valid YouTube @handle"),
    ],
)
def test_resolve_youtube_rejects(value: str, msg: str) -> None:
    client, seen = public_client({})
    with pytest.raises(ValueError, match=msg):
        resolve_youtube(value, client)
    assert seen == []


def test_resolve_youtube_unverified_channel_is_refused() -> None:
    client, _ = public_client({})  # the feed 404s
    with pytest.raises(ValueError, match="no public feed"):
        resolve_youtube(CHANNEL, client)


# -- add_source ----------------------------------------------------------------------------
def _cfg() -> Config:
    return Config(sources=Sources())


def test_add_website_newsletter_podcast() -> None:
    page = b'<link rel="alternate" type="application/rss+xml" href="https://blog.example.com/rss">'
    client, _ = public_client(
        {
            "blog.example.com/": page,
            "blog.example.com/rss": ATOM,
            "pod.example.net/show.xml": fixture_bytes("blog_rss.xml"),
        }
    )
    cfg = _cfg()
    assert add_source(cfg, "website", "https://blog.example.com", client) == "Example Blog"
    assert cfg.sources.rss[0].url == "https://blog.example.com/rss"
    assert cfg.sources.rss[0].kind == "website"
    with pytest.raises(ValueError, match="Already following"):
        add_source(cfg, "newsletter", "http://www.blog.example.com/rss/", client)
    assert add_source(cfg, "podcast", "pod.example.net/show.xml", client) == "RSS Blog"
    assert cfg.sources.rss[1].kind == "podcast"
    assert len(cfg.sources.rss) == 2


def test_add_arxiv() -> None:
    cfg = _cfg()
    assert add_source(cfg, "arxiv", "cs.cl") == "arXiv cs.CL"
    assert cfg.sources.rss[0].url == "https://rss.arxiv.org/rss/cs.CL"
    assert cfg.sources.rss[0].kind == "arxiv"
    assert add_source(cfg, "arxiv", "arxiv:stat.ML") == "arXiv stat.ML"
    with pytest.raises(ValueError, match=r"Already following arXiv cs\.CL"):
        add_source(cfg, "arxiv", "CS.CL")
    for bad in ("cs", "zz.AB", "cs.CLX", "cs/CL", "quant-ph.XY", "cs.CL; rm -rf"):
        with pytest.raises(ValueError):
            add_source(cfg, "arxiv", bad)
    assert arxiv_category("cond-mat.Mes-Hall") == "cond-mat.mes-hall"
    assert arxiv_category("quant-ph") == "quant-ph"


def test_add_youtube_and_duplicate() -> None:
    client, _ = public_client(
        {
            "www.youtube.com/@fireship": f'"externalId":"{CHANNEL}"'.encode(),
            f"www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}": YT_FEED,
        }
    )
    cfg = _cfg()
    assert add_source(cfg, "youtube", "@fireship", client) == "YouTube: Example Channel"
    assert cfg.sources.youtube == [YouTubeSource(name="Example Channel", channel_id=CHANNEL)]
    with pytest.raises(ValueError, match="Already following YouTube"):
        add_source(cfg, "youtube", f"https://www.youtube.com/channel/{CHANNEL}", client)


def test_add_reddit_hn_bluesky_mastodon_github() -> None:
    cfg = _cfg()
    assert add_source(cfg, "reddit", "r/LocalLLaMA") == "Reddit r/LocalLLaMA"
    assert add_source(cfg, "reddit", "https://www.reddit.com/r/ollama/") == "Reddit r/ollama"
    with pytest.raises(ValueError, match="Already following r/localllama"):
        add_source(cfg, "reddit", "localllama")
    assert add_source(cfg, "hackernews", "  mcp  ") == 'Hacker News search "mcp"'
    assert cfg.sources.hackernews == HackerNewsSource(lists=[], keywords=["mcp"])
    with pytest.raises(ValueError, match="Already searching"):
        add_source(cfg, "hackernews", "MCP")
    assert add_source(cfg, "bluesky", "@simonwillison.net") == "Bluesky @simonwillison.net"
    assert add_source(cfg, "bluesky", "mcp server") == 'Bluesky search "mcp server"'
    assert add_source(cfg, "mastodon", "#ollama") == "Mastodon #ollama (mastodon.social)"
    assert add_source(cfg, "mastodon", "llm") == "Mastodon #llm (mastodon.social)"
    assert add_source(cfg, "mastodon", "rust@fosstodon.org") == "Mastodon #rust (fosstodon.org)"
    assert cfg.sources.mastodon == [
        MastodonSource(instance="mastodon.social", hashtags=["ollama", "llm"]),
        MastodonSource(instance="fosstodon.org", hashtags=["rust"]),
    ]
    with pytest.raises(ValueError, match=r"(?i)already following #ollama"):
        add_source(cfg, "mastodon", "#Ollama@mastodon.social")
    assert (
        add_source(cfg, "github", "https://github.com/modelcontextprotocol/typescript-sdk")
        == "GitHub modelcontextprotocol/typescript-sdk releases"
    )
    assert add_source(cfg, "github", "topic:mcp language:python") == (
        'GitHub search "topic:mcp language:python"'
    )
    assert cfg.sources.github == GitHubSource(
        releases=["modelcontextprotocol/typescript-sdk"], search=["topic:mcp language:python"]
    )
    with pytest.raises(ValueError, match="Already following"):
        add_source(cfg, "github", "modelcontextprotocol/typescript-sdk")


@pytest.mark.parametrize(
    ("typ", "value"),
    [
        ("reddit", "r/../../etc"),
        ("reddit", "a"),
        ("hackernews", "x"),
        ("hackernews", "bad\nkeyword"),
        ("bluesky", "@not a handle"),
        ("mastodon", "#two words"),
        ("mastodon", "tag@localhost"),
        ("github", "owner/repo/extra"),
        ("github", "-bad/repo"),
        ("website", "javascript:alert(1)"),
        ("youtube", "https://www.youtube.com/watch?v=x"),
        ("rss", "https://x.example/feed"),
        ("x", "@someone"),
        ("reddit", ""),
        ("hackernews", "a" * 400),
    ],
)
def test_add_source_rejects_invalid(typ: str, value: str) -> None:
    client, seen = public_client({})
    cfg = _cfg()
    before = cfg.model_dump()
    with pytest.raises(ValueError):
        add_source(cfg, typ, value, client)
    assert cfg.model_dump() == before  # nothing half-added
    assert seen == []


def test_add_source_failed_discovery_changes_nothing() -> None:
    client, _ = public_client({"nofeed.example.com/": b"<html></html>"})
    cfg = _cfg()
    with pytest.raises(ValueError, match="No RSS"):
        add_source(cfg, "website", "nofeed.example.com", client)
    assert cfg.sources.rss == []


# -- disabled + catalog --------------------------------------------------------------------
def test_disabled_sources_are_skipped_and_listed() -> None:
    cfg = Config(
        sources=Sources(
            rss=[
                RssSource(name="A", url="https://a.example/feed"),
                RssSource(name="arXiv cs.CL", url="https://rss.arxiv.org/rss/cs.CL", kind="arxiv"),
            ],
            hackernews=HackerNewsSource(lists=["top"], keywords=["gemma"]),
            disabled=["rss:https://a.example/feed", "hackernews:q:gemma"],
        )
    )
    keys = [t.key for t in all_tasks(cfg)]
    assert keys == ["rss:https://rss.arxiv.org/rss/cs.CL", "hackernews:top"]
    cat = source_catalog(cfg)
    assert [(c["key"], c["enabled"]) for c in cat] == [
        ("rss:https://a.example/feed", False),
        ("rss:https://rss.arxiv.org/rss/cs.CL", True),
        ("hackernews:top", True),
        ("hackernews:q:gemma", False),
    ]
    assert cat[0] == {
        "key": "rss:https://a.example/feed",
        "label": "A",
        "type": "rss",
        "detail": "https://a.example/feed",
        "enabled": False,
        "kind": "feed",
    }
    assert cat[1]["kind"] == "arxiv"
    assert cat[3]["type"] == "hackernews" and cat[3]["label"] == 'Hacker News search "gemma"'

    set_source_enabled(cfg, "hackernews:q:gemma", True)
    set_source_enabled(cfg, "hackernews:top", False)
    assert cfg.sources.disabled == ["rss:https://a.example/feed", "hackernews:top"]
    with pytest.raises(ValueError, match="No source"):
        set_source_enabled(cfg, "rss:https://nope.example/", False)


def test_disabled_round_trips_through_yaml(tmp_path) -> None:
    from night_scout.core.config import load_config, save_config

    cfg = Config(sources=Sources(disabled=["reddit:LocalLLaMA"]))
    save_config(cfg, tmp_path / "config.yaml")
    assert load_config(tmp_path / "config.yaml").sources.disabled == ["reddit:LocalLLaMA"]


def test_re_adding_a_disabled_source_says_so() -> None:
    cfg = Config(sources=Sources(disabled=["reddit:LocalLLaMA"]))
    add_source(cfg, "reddit", "LocalLLaMA")
    assert "reddit:LocalLLaMA" not in cfg.sources.disabled  # a fresh add is switched on
    set_source_enabled(cfg, "reddit:LocalLLaMA", False)
    with pytest.raises(ValueError, match="Already following"):
        add_source(cfg, "reddit", "LocalLLaMA")
