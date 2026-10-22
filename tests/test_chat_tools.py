"""Research tools for the chat drawer. No network: httpx.MockTransport and a fake resolver."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from night_scout.core import secrets
from night_scout.core.config import Config, SearchConfig
from night_scout.core.db import Store
from night_scout.core.models import Item, utcnow
from night_scout.core.tools import BRAVE_KEY_ID, ToolContext, offered, run_tool, specs
from night_scout.core.tools import web as web_tool
from night_scout.core.tools.media import hn_search, video_id, youtube_transcript
from night_scout.core.tools.search import web_search
from night_scout.core.tools.tonight import search_tonight
from night_scout.core.tools.web import html_text, read_page

PUBLIC = "93.184.215.14"
DNS = {
    "e.example": PUBLIC,
    "news.example": PUBLIC,
    "evil.example": "10.1.2.3",
    "münchen.example": PUBLIC,
}


def resolver(host: str, port: int, *a, **k):
    ip = DNS.get(host, host)  # IP literals resolve to themselves
    if ip == "nxdomain":
        raise OSError("no such host")
    return [(None, None, None, None, (ip, port))]


def ctx_with(handler, cfg: Config | None = None, store: Store | None = None) -> ToolContext:
    return ToolContext(
        cfg=cfg or Config(),
        store=store,
        transport=httpx.MockTransport(handler),
        resolve=resolver,
    )


def page_host(request: httpx.Request) -> str:
    """read_page connects to the checked IP; the real host travels in the Host header."""
    return request.headers["host"]


ARTICLE = (
    b"""<html><head><title>Ollama 0.40.9 notes</title><style>.x{}</style></head>
<body><nav>Home | Blog | Login</nav><header>Site header</header>
<article><h1>What changed</h1><p>The llama.cpp engine was updated to the latest build.</p>
<p>Windows on ARM now uses the released CUDA toolkit.</p>"""
    + b"<p>More detail. </p>" * 30
    + (b"""</article><script>alert('x')</script><footer>(c) 2026</footer></body></html>""")
)


# -- read_page -----------------------------------------------------------------------------
def test_read_page_returns_readable_main_text() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=ARTICLE, headers={"content-type": "text/html"})

    out = read_page(ctx_with(handler), "https://e.example/post")
    assert out["title"] == "Ollama 0.40.9 notes"
    assert "llama.cpp engine was updated" in out["text"]
    assert "Home | Blog" not in out["text"] and "alert" not in out["text"]
    assert "Site header" not in out["text"] and "(c) 2026" not in out["text"]
    # connected to the checked address, host name kept for Host + TLS
    assert seen[0].url.host == PUBLIC and page_host(seen[0]) == "e.example"
    assert seen[0].extensions.get("sni_hostname") == "e.example"


def test_read_page_sends_international_names_as_idna() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="Servus", headers={"content-type": "text/plain"})

    out = read_page(ctx_with(handler), "https://münchen.example:8443/a")
    assert out["text"] == "Servus"
    assert page_host(seen[0]) == "xn--mnchen-3ya.example:8443"
    assert seen[0].extensions["sni_hostname"] == "xn--mnchen-3ya.example"


def test_html_text_falls_back_to_body_without_main() -> None:
    title, text = html_text("<title>T</title><body><div>Just a body.</div><nav>x</nav></body>")
    assert title == "T" and text == "Just a body."


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434/api/tags",
        "http://localhost:8765/",
        "http://10.0.0.8/admin",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://evil.example/",  # a public-looking name that resolves to a private address
        "file:///etc/passwd",
        "ftp://e.example/file",
        "javascript:alert(1)",
        "https://user:pw@e.example/",
    ],
)
def test_read_page_refuses_non_public_or_non_http(url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"must not connect: {request.url}")

    out = read_page(ctx_with(handler), url)
    assert "error" in out and out["error"].startswith("Refused")


def test_read_page_checks_every_redirect_hop() -> None:
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(page_host(request))
        return httpx.Response(302, headers={"location": "http://10.0.0.5/secret"})

    out = read_page(ctx_with(handler), "http://e.example/go")
    assert out["error"].startswith("Refused") and "10.0.0.5" in out["error"]
    assert hops == ["e.example"]  # the private hop was never requested


def test_read_page_follows_public_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://news.example/new"})
        assert page_host(request) == "news.example"
        return httpx.Response(200, text="plain words here", headers={"content-type": "text/plain"})

    out = read_page(ctx_with(handler), "https://e.example/old")
    assert out == {"url": "https://news.example/new", "title": "", "text": "plain words here"}


def test_read_page_refuses_https_to_http_downgrade_and_loops() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://e.example/plain"})

    assert "https to plain http" in read_page(ctx_with(down), "https://e.example/")["error"]

    def loop(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/again"})

    assert read_page(ctx_with(loop), "https://e.example/")["error"] == "Too many redirects."


def test_read_page_refuses_binary_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"})

    out = read_page(ctx_with(handler), "https://e.example/paper.pdf")
    assert out["error"].startswith("Not a text page (application/pdf)")


def test_read_page_caps_size_and_clips_text(monkeypatch) -> None:
    monkeypatch.setattr(web_tool, "MAX_BYTES", 5000)
    body = ("word " * 10_000).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/plain"})

    out = read_page(ctx_with(handler), "https://e.example/huge")
    assert out["truncated"] is True
    assert len(out["text"]) <= 5002


def test_read_page_http_error_and_network_error() -> None:
    def gone(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="nope")

    assert read_page(ctx_with(gone), "https://e.example/x")["error"] == "HTTP 404 from e.example."

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert "ConnectError" in read_page(ctx_with(down), "https://e.example/x")["error"]
    assert "error" in read_page(ctx_with(down), "")


# -- youtube_transcript ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        "b2tjnxX8IHU",
        "https://www.youtube.com/watch?v=b2tjnxX8IHU&t=30s",
        "https://youtu.be/b2tjnxX8IHU",
        "youtube.com/shorts/b2tjnxX8IHU",
        "https://www.youtube-nocookie.com/embed/b2tjnxX8IHU",
    ],
)
def test_video_id_forms(value: str) -> None:
    assert video_id(value) == "b2tjnxX8IHU"


def test_video_id_rejects_other_hosts() -> None:
    assert video_id("https://evil.example/watch?v=b2tjnxX8IHU") is None
    assert video_id("not a video") is None


def test_youtube_transcript_clips_and_labels(store: Store) -> None:
    ctx = ToolContext(cfg=Config(), store=store, transcript_fn=lambda vid: "word " * 5000)
    out = youtube_transcript(ctx, "https://youtu.be/b2tjnxX8IHU")
    assert out["video_id"] == "b2tjnxX8IHU" and out["truncated"] is True
    assert len(out["transcript"]) <= 8002 and out["source"].startswith("YouTube captions")


def test_youtube_transcript_falls_back_to_stored_excerpt(store: Store) -> None:
    run = store.start_run()
    it = Item("youtube", "Fireship", "yt:1", "https://www.youtube.com/watch?v=b2tjnxX8IHU", "T", "")
    it.extra = {"video_id": "b2tjnxX8IHU", "transcript_excerpt": "Saved opening words."}
    store.upsert_item(it, run)

    def blocked(vid: str) -> str | None:
        raise RuntimeError("IP blocked")

    ctx = ToolContext(cfg=Config(), store=store, transcript_fn=blocked)
    out = youtube_transcript(ctx, "b2tjnxX8IHU")
    assert out["transcript"] == "Saved opening words." and "last night" in out["source"]
    none = ToolContext(cfg=Config(), store=store, transcript_fn=lambda v: None)
    assert "No English transcript" in youtube_transcript(none, "aaaaaaaaaaa")["error"]
    assert "error" in youtube_transcript(none, "https://e.example/")


# -- hn_search -----------------------------------------------------------------------------------
def test_hn_search_shapes_hits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "hn.algolia.com"
        assert request.url.params["query"] == "gemma 4" and request.url.params["tags"] == "story"
        return httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "objectID": "42",
                        "title": "Gemma 4 is out",
                        "url": "https://blog.example/g4",
                        "points": 300,
                        "num_comments": 120,
                        "created_at": "2026-10-21T10:00:00Z",
                    },
                    {"objectID": "43", "title": "Ask HN: Gemma?", "url": None, "points": 5},
                    {"title": "no id"},
                ]
            },
        )

    out = hn_search(ctx_with(handler), "  gemma   4 ")
    assert out["query"] == "gemma 4"
    assert out["hits"][0] == {
        "title": "Gemma 4 is out",
        "url": "https://blog.example/g4",
        "points": 300,
        "comments": 120,
        "date": "2026-10-21",
        "discussion": "https://news.ycombinator.com/item?id=42",
    }
    assert out["hits"][1]["url"] == "https://news.ycombinator.com/item?id=43"
    assert len(out["hits"]) == 2


def test_hn_search_errors() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    assert hn_search(ctx_with(down), "x")["error"] == "Hacker News search answered HTTP 503."
    assert "error" in hn_search(ctx_with(down), "   ")


# -- web_search ----------------------------------------------------------------------------------
def searx_cfg(url: str = "http://127.0.0.1:8888") -> Config:
    return Config(search=SearchConfig(provider="searxng", searxng_url=url))


def test_web_search_off_is_not_offered_and_refuses() -> None:
    cfg = Config()
    assert cfg.search.provider == "none"
    assert "web_search" not in [t.name for t in offered(cfg)]
    assert "web_search" not in json.dumps(specs(cfg))
    assert run_tool(ToolContext(cfg=cfg), "web_search", {"query": "x"})["error"].startswith(
        "There is no tool"
    )
    assert "off" in web_search(ToolContext(cfg=cfg), "x")["error"]
    assert "web_search" in [t.name for t in offered(searx_cfg())]


def test_searxng_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("http://127.0.0.1:8888/search?")
        assert request.url.params["format"] == "json" and request.url.params["q"] == "ollama"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Ollama", "url": "https://ollama.com", "content": "Run <b>LLMs</b>"},
                    {"title": "bad", "url": "javascript:alert(1)"},
                ]
            },
        )

    out = web_search(ctx_with(handler, searx_cfg()), "ollama")
    assert out == {
        "provider": "searxng",
        "query": "ollama",
        "results": [{"title": "Ollama", "url": "https://ollama.com", "snippet": "Run LLMs"}],
    }


def test_searxng_json_disabled_is_explained() -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    err = web_search(ctx_with(forbidden, searx_cfg()), "x")["error"]
    assert "refused JSON output" in err and "settings.yml" in err and "formats" in err

    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="<html>results</html>", headers={"content-type": "text/html"}
        )

    err = web_search(ctx_with(html, searx_cfg()), "x")["error"]
    assert "didn't return JSON" in err and "formats" in err


def test_searxng_unreachable_says_so() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    err = web_search(ctx_with(down, searx_cfg("http://127.0.0.1:8899")), "x")["error"]
    assert err == "Couldn't reach SearXNG at http://127.0.0.1:8899 (ConnectError). Is it running?"


def test_brave_search_uses_keychain_key_and_never_leaks_it() -> None:
    key = "BSA-very-secret-key-123456"
    secrets.set_key(BRAVE_KEY_ID, key)
    cfg = Config(search=SearchConfig(provider="brave"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.search.brave.com"
        assert request.headers["x-subscription-token"] == key
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Gemma",
                            "url": "https://ai.google.dev/gemma",
                            "description": "Open <strong>models</strong>",
                        }
                    ]
                }
            },
        )

    out = web_search(ctx_with(handler, cfg), "gemma")
    assert out["provider"] == "brave"
    assert out["results"] == [
        {"title": "Gemma", "url": "https://ai.google.dev/gemma", "snippet": "Open models"}
    ]
    assert key not in json.dumps(out)

    def rejected(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad token {key}")

    out = web_search(ctx_with(rejected, cfg), "gemma")
    assert "rejected the API key (HTTP 401)" in out["error"] and key not in out["error"]


def test_brave_without_key_says_so() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request without a key")

    cfg = Config(search=SearchConfig(provider="brave"))
    assert "No Brave Search API key" in web_search(ctx_with(handler, cfg), "x")["error"]


# -- run_tool ------------------------------------------------------------------------------------
def test_run_tool_never_raises_and_validates() -> None:
    ctx = ToolContext(cfg=Config())
    assert "no tool called" in run_tool(ctx, "rm_rf", {})["error"]
    assert run_tool(ctx, "read_page", {})["error"] == "read_page needs url."
    # unknown arguments are dropped, not passed through
    out = run_tool(ctx, "read_page", {"url": "file:///etc/passwd", "headers": {"x": 1}})
    assert out["error"].startswith("Refused")


def test_run_tool_turns_exceptions_and_hangs_into_errors(monkeypatch) -> None:
    import night_scout.core.tools as tools_mod

    def boom(ctx, url=""):
        raise RuntimeError("kaput")

    def slow(ctx, url=""):
        time.sleep(2)
        return {"ok": 1}

    tool = tools_mod.TOOLS["read_page"]
    monkeypatch.setitem(
        tools_mod.TOOLS, "read_page", tool.__class__(**{**tool.__dict__, "fn": boom})
    )
    ctx = ToolContext(cfg=Config())
    assert run_tool(ctx, "read_page", {"url": "https://e.example"}) == {
        "error": "Read a page failed (RuntimeError)."
    }
    monkeypatch.setitem(
        tools_mod.TOOLS, "read_page", tool.__class__(**{**tool.__dict__, "fn": slow})
    )
    out = run_tool(ctx, "read_page", {"url": "https://e.example"}, timeout=0.2)
    assert out == {"error": "Gave up on Read a page after 0 s."}


# -- search_tonight -------------------------------------------------------------------------------
def seed_day(store: Store, day: str = "2026-10-22") -> dict[str, int]:
    run = store.start_run()
    ids = {}
    for uid, title, summary, score in [
        ("a", "Ollama v0.40.9 released", "llama.cpp update for local inference.", 0.7),
        ("b", "OpenAI CLI", "A command line for the API.", 0.6),
        ("c", "Gossip roundup", "Nothing technical.", 0.1),
    ]:
        it = Item("rss", "Blog", uid, f"https://e.example/{uid}", title, "", published_at=utcnow())
        iid = store.upsert_item(it, run).item_id
        ids[uid] = iid
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO scores (item_id, model, summary, best_project, best_score, "
                "per_project, topics, scored_at) VALUES (?,?,?,?,?,?,?,?)",
                (iid, "m", summary, "llm", score, "[]", '["llm"]', "2026-10-22T00:00:00"),
            )
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO digests (day, run_id, path, item_ids, created_at) VALUES (?,?,?,?,?)",
            (day, run, None, json.dumps([ids["a"], ids["b"]]), "2026-10-22T05:00:00"),
        )
    return ids


def test_search_tonight_ranks_matches_without_digest_columns(store: Store) -> None:
    ids = seed_day(store)
    out = search_tonight(ToolContext(cfg=Config(), store=store), "llama.cpp local inference")
    assert out["day"] == "2026-10-22" and out["searched"] == 3
    assert out["items"][0]["id"] == ids["a"]
    assert out["items"][0]["url"] == "https://e.example/a"
    assert "topic" not in out["items"][0] and "headline" not in out["items"][0]


def test_search_tonight_uses_digest_columns_when_present(store: Store) -> None:
    ids = seed_day(store)
    with store.tx() as conn:
        for col in ("headline TEXT", "digest_topic TEXT", "digest_relevance REAL"):
            conn.execute(f"ALTER TABLE scores ADD COLUMN {col}")
        conn.execute(
            "UPDATE scores SET headline=?, digest_topic=? WHERE item_id=?",
            ("Command-line access to OpenAI", "Developer tools", ids["b"]),
        )
    out = search_tonight(ToolContext(cfg=Config(), store=store), "developer tools", "2026-10-22")
    top = out["items"][0]
    assert top["id"] == ids["b"] and top["topic"] == "Developer tools"
    assert top["headline"] == "Command-line access to OpenAI"


def test_search_tonight_without_matches_returns_best_and_validates(store: Store) -> None:
    ids = seed_day(store)
    ctx = ToolContext(cfg=Config(), store=store, day="2026-10-22")
    out = search_tonight(ctx, "the what tonight")  # only stop words
    assert out["items"][0]["id"] == ids["a"] and "No direct matches" in out["note"]
    assert "YYYY-MM-DD" in search_tonight(ctx, "x", "yesterday")["error"]
    assert search_tonight(ctx, "x", "1999-01-01")["items"] == []
    assert "error" in search_tonight(ToolContext(cfg=Config()), "x")


def test_search_tonight_on_an_empty_database(tmp_path: Path) -> None:
    s = Store(tmp_path / "empty.db")
    try:
        assert "No runs yet" in search_tonight(ToolContext(cfg=Config(), store=s), "x")["error"]
    finally:
        s.close()
