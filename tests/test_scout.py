"""Look back + Scout key terms: matching, whole-channel search, queueing, routes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml

from night_scout.core.config import Config, ScoutSettings, YouTubeSource, clean_scout_terms
from night_scout.core.db import Store
from night_scout.core.fetch import fetch_all
from night_scout.core.models import Item, utcnow
from night_scout.core.pipeline import scout_first
from night_scout.core.scout import (
    ScoutError,
    ScoutResult,
    TermResult,
    match_terms,
    queue_matches,
    search_feed,
    search_youtube_channel,
)
from night_scout.core.sources import SourceTask, source_catalog
from night_scout.web import app as web

from .conftest import make_client
from .test_web import env  # noqa: F401  (fixture)

CHANNEL = "UCh13OyDSm-Kb8ij3yZArtFg"


# -- matching -------------------------------------------------------------------------------
def test_match_terms_whole_word_any_case() -> None:
    assert match_terms(["kling", "veo"], "Kling 3.0 Review", "vs Veo 3.1") == ["kling", "veo"]
    assert match_terms(["veo"], "Seedance 2.0 in 4K", "veolia water") == []
    assert match_terms(["gpt-6"], "GPT-6 Astra made this") == ["gpt-6"]
    assert match_terms(["claude code"], "Using Claude  Code daily") == []  # phrases match as typed
    assert match_terms(["claude code"], "Using claude code daily") == ["claude code"]
    assert match_terms(["mcp"], None, "") == []


def test_clean_scout_terms() -> None:
    assert clean_scout_terms(" kling, Veo ,, KLING , seedance 2 ") == ["kling", "Veo", "seedance 2"]
    assert clean_scout_terms(None) == []
    with pytest.raises(ValueError, match="at most 10"):
        clean_scout_terms([f"t{i}" for i in range(11)])
    with pytest.raises(ValueError, match="60 characters"):
        clean_scout_terms(["x" * 61])
    with pytest.raises(ValueError, match="control"):
        clean_scout_terms(["bad\x00term"])
    assert ScoutSettings(terms="a, b").terms == ["a", "b"]
    with pytest.raises(ValueError):
        ScoutSettings(look_back_days=15)


# -- YouTube via yt-dlp (fake runner) -------------------------------------------------------
VIDEOS = {
    "nUgx8ETiR1g": ("Kling 3.0 Can Do Anything", "Kling 3.0 tested.", "20260207"),
    "jQvHqKrGhy8": ("Kling 3.0 Review", "Realism and camera moves.", "20260204"),
    "AdjllfZuqYM": ("A $1,000,000 AD Using Just 2 tools", "Made with Kling and Nano.", "20251223"),
    "sXgWhKwiUdc": ("I Created a $1,000,000 Brand Using AI", "No model named here.", "20260424"),
    "XdK93RhPFAo": ("Kling 2.6: This Cheap Model DESTROYS Veo 3.1", "Shootout.", "20251216"),
}


def fake_ytdlp(listings: dict[str, list[str]]) -> tuple[Any, list[list[str]]]:
    calls: list[list[str]] = []

    def run(argv: Any, timeout: int) -> str:
        argv = list(argv)
        calls.append(argv)
        assert "--ignore-config" in argv and "--no-cache-dir" in argv
        if "--flat-playlist" in argv:
            url = argv[-1]
            term = url.split("query=", 1)[1]
            entries = [{"id": "PLPxC_d2iFXcs", "ie_key": "YoutubeTab"}] + [
                {"id": v, "ie_key": "Youtube"} for v in listings.get(term, [])
            ]
            return json.dumps({"entries": entries})
        ids = argv[argv.index("--") + 1 :]
        return "\n".join(
            json.dumps(
                {
                    "id": v,
                    "title": VIDEOS[v][0],
                    "description": VIDEOS[v][1],
                    "upload_date": VIDEOS[v][2],
                }
            )
            for v in ids
        )

    return run, calls


def test_youtube_search_ytdlp_keeps_only_real_matches() -> None:
    run, calls = fake_ytdlp(
        {
            "kling": ["nUgx8ETiR1g", "jQvHqKrGhy8", "sXgWhKwiUdc", "AdjllfZuqYM"],
            "veo": ["XdK93RhPFAo"],
        }
    )
    client, _ = make_client({})
    res = search_youtube_channel(
        CHANNEL, "Higgsfield AI", ["kling", "veo"], client, ytdlp=["yt-dlp"], run=run
    )
    assert res.via == "yt-dlp"
    assert [t.to_dict() for t in res.terms] == [
        {"term": "kling", "checked": 4, "matched": 3},
        {"term": "veo", "checked": 1, "matched": 1},
    ]
    ids = [i.extra["video_id"] for i in res.items]
    assert ids == ["nUgx8ETiR1g", "jQvHqKrGhy8", "AdjllfZuqYM", "XdK93RhPFAo"]  # newest first
    both = next(i for i in res.items if i.extra["video_id"] == "XdK93RhPFAo")
    assert both.extra["scout_terms"] == ["kling", "veo"]
    assert both.uid == "yt:XdK93RhPFAo" and both.source_name == "Higgsfield AI"
    assert both.published_at == datetime(2025, 12, 16, tzinfo=UTC)
    assert "PLPxC_d2iFXcs" not in " ".join(" ".join(c) for c in calls[1:2])  # playlists dropped


def test_youtube_search_needs_a_tool_and_a_real_channel() -> None:
    client, _ = make_client({})
    with pytest.raises(ScoutError, match="API key"):
        search_youtube_channel(CHANNEL, "x", ["kling"], client, ytdlp=None)
    with pytest.raises(ScoutError, match="channel id"):
        search_youtube_channel("not-a-channel", "x", ["kling"], client, ytdlp=["yt-dlp"])


def test_youtube_search_api_uses_header_key_and_full_descriptions() -> None:
    seen: list[httpx.Request] = []

    def search(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(
            200,
            json={
                "items": [{"id": {"videoId": "AdjllfZuqYM"}}, {"id": {"videoId": "sXgWhKwiUdc"}}]
            },
        )

    def videos(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "AdjllfZuqYM",
                        "snippet": {
                            "title": VIDEOS["AdjllfZuqYM"][0],
                            "description": "Made with Kling.",
                            "publishedAt": "2025-12-23T15:00:00Z",
                        },
                    },
                    {
                        "id": "sXgWhKwiUdc",
                        "snippet": {
                            "title": VIDEOS["sXgWhKwiUdc"][0],
                            "description": "Nothing.",
                            "publishedAt": "2026-04-24T15:00:00Z",
                        },
                    },
                ]
            },
        )

    client, _ = make_client({"/youtube/v3/search": search, "/youtube/v3/videos": videos})
    res = search_youtube_channel(
        CHANNEL, "Higgsfield AI", ["kling"], client, api_key="AIzaSECRETKEY0123456789"
    )
    assert res.via == "api" and [i.extra["video_id"] for i in res.items] == ["AdjllfZuqYM"]
    assert all(r.headers["x-goog-api-key"] == "AIzaSECRETKEY0123456789" for r in seen)
    assert all("AIzaSECRET" not in str(r.url) for r in seen)  # never in a URL
    assert "channelId=" + CHANNEL in str(seen[0].url) and "type=video" in str(seen[0].url)


def test_youtube_api_error_is_scrubbed() -> None:
    client, _ = make_client({"/youtube/v3/search": 403})
    with pytest.raises(ScoutError) as e:
        search_youtube_channel(CHANNEL, "x", ["kling"], client, api_key="AIzaSECRETKEY0123456789")
    assert "AIzaSECRET" not in str(e.value)
    assert "rejected the API key" in str(e.value) and "Settings" in str(e.value)


# -- feeds ----------------------------------------------------------------------------------
def _item(uid: str, title: str, days_old: float = 0, text: str = "") -> Item:
    return Item(
        source="rss",
        source_name="Blog",
        uid=uid,
        url=f"https://ex.com/{uid}",
        title=title,
        text=text,
        published_at=utcnow() - timedelta(days=days_old),
    )


def test_search_feed_any_age() -> None:
    task = SourceTask(
        "rss:https://ex.com/feed",
        "Blog",
        lambda c: [
            _item("a", "MCP servers explained", 40),
            _item("b", "Unrelated", 1),
            _item("c", "Ollama + MCP", 2),
        ],
    )
    client, _ = make_client({})
    res = search_feed(task, ["mcp", "ollama"], client)
    assert res.via == "feed"
    assert [t.to_dict() for t in res.terms] == [
        {"term": "mcp", "checked": 3, "matched": 2},
        {"term": "ollama", "checked": 3, "matched": 1},
    ]
    assert [i.uid for i in res.items] == ["c", "a"]
    assert res.items[0].extra["scout_terms"] == ["mcp", "ollama"]


def test_queue_matches_inserts_and_requeues(store: Store) -> None:
    fresh = _item("new1", "MCP news")
    fresh.extra["scout_terms"] = ["mcp"]
    filtered = _item("old1", "MCP old")
    judged = _item("old2", "MCP judged")
    rid = store.start_run()
    fid = store.upsert_item(filtered, rid).item_id
    jid = store.upsert_item(judged, rid).item_id
    store.set_item_fields(fid, status="filtered", first_seen="2020-01-01T00:00:00+00:00")
    store.set_item_fields(jid, status="scored")
    for i in (filtered, judged):
        i.extra["scout_terms"] = ["mcp"]
    assert queue_matches(store, [fresh, filtered, judged]) == 2
    f = store.item(fid)
    assert (
        f["status"] == "new"
        and f["first_seen"] > "2026"
        and json.loads(f["extra"])["scout_terms"] == ["mcp"]
    )
    assert store.item(jid)["status"] == "scored"


# -- nightly fetch: per-source look back + tagging -------------------------------------------
def test_fetch_uses_per_source_look_back_and_tags(store: Store) -> None:
    cfg = Config()
    cfg.sources.scout["rss:https://a"] = ScoutSettings(look_back_days=10, terms=["mcp"])
    items_a = [
        _item("a1", "MCP today", 0),
        _item("a2", "eight days old", 8),
        _item("a3", "too old", 12),
    ]
    items_b = [_item("b1", "MCP today", 0), _item("b2", "eight days old", 8)]
    tasks = [
        SourceTask("rss:https://a", "A", lambda c: items_a),
        SourceTask("rss:https://b", "B", lambda c: items_b),
    ]
    client, _ = make_client({})
    st = fetch_all(cfg, store, store.start_run(), client=client, tasks=tasks)
    assert [(s.key, s.new) for s in st] == [("rss:https://a", 2), ("rss:https://b", 1)]
    rows = {r["uid"]: json.loads(r["extra"]) for r in store.items_with_status("new")}
    assert rows["a1"].get("scout_terms") == ["mcp"]
    assert "scout_terms" not in rows["b1"]  # B has no terms


def test_scout_first_keeps_ranking_within_groups() -> None:
    ranked = [SimpleNamespace(item_id=i) for i in (1, 2, 3, 4)]
    pending = [
        {"id": 1, "extra": "{}"},
        {"id": 2, "extra": '{"scout_terms":["x"]}'},
        {"id": 3, "extra": None},
        {"id": 4, "extra": '{"scout_terms":["y"]}'},
    ]
    out, n = scout_first(ranked, pending)  # type: ignore[arg-type]
    assert [c.item_id for c in out] == [2, 4, 1, 3] and n == 2
    assert scout_first(ranked, [{"id": 1, "extra": "{}"}])[1] == 0  # type: ignore[list-item]


def test_catalog_shows_settings() -> None:
    cfg = Config()
    cfg.sources.youtube = [YouTubeSource(name="Higgsfield AI", channel_id=CHANNEL)]
    cfg.sources.scout[f"youtube:{CHANNEL}"] = ScoutSettings(look_back_days=7, terms=["kling"])
    row = next(r for r in source_catalog(cfg) if r["key"] == f"youtube:{CHANNEL}")
    assert row["look_back_days"] == 7 and row["scout_terms"] == ["kling"]


# -- routes -----------------------------------------------------------------------------------
def _fake_scout(calls: list[tuple[str, list[str]]]) -> Any:
    def run(
        cfg: Config, store: Store, key: str, terms: list[str], client: Any, **kw: Any
    ) -> ScoutResult:
        calls.append((key, list(terms)))
        it = _item("m1", f"{terms[0]} match")
        it.extra["scout_terms"] = list(terms)
        return ScoutResult("feed", [TermResult(t, 25, 1) for t in terms], [it], queued=1)

    return run


def test_add_with_settings_then_readd_updates(env, monkeypatch) -> None:  # noqa: F811
    client, *_rest, config_path = env
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(web, "run_scout", _fake_scout(calls))
    r = client.post(
        "/api/sources/add",
        json={
            "type": "reddit",
            "value": "rust",
            "look_back_days": 7,
            "scout_terms": ["Tokio", "async"],
        },
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["key"] == "reddit:rust" and d["existing"] is False
    assert d["scout"]["queued"] == 1 and d["scout"]["look_back_days"] == 7
    assert d["scout"]["matches"][0]["terms"] == ["Tokio", "async"]
    saved = yaml.safe_load(config_path.read_text())["sources"]["scout"]["reddit:rust"]
    assert saved == {"look_back_days": 7, "terms": ["Tokio", "async"]}
    # re-adding with settings updates; only the new term is searched
    r = client.post(
        "/api/sources/add",
        json={
            "type": "reddit",
            "value": "r/rust",
            "look_back_days": 3,
            "scout_terms": ["tokio", "axum"],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["existing"] is True and calls[-1] == ("reddit:rust", ["axum"])
    # re-adding without settings is still a plain duplicate
    assert (
        client.post("/api/sources/add", json={"type": "reddit", "value": "rust"}).status_code == 400
    )
    row = next(x for x in client.get("/api/sources").json()["catalog"] if x["key"] == "reddit:rust")
    assert row["look_back_days"] == 3 and row["scout_terms"] == ["tokio", "axum"]


def test_add_search_failure_keeps_the_source(env, monkeypatch) -> None:  # noqa: F811
    client, *_rest, config_path = env

    def boom(*a: Any, **k: Any) -> ScoutResult:
        raise ScoutError("Searching a whole channel needs a YouTube Data API key")

    monkeypatch.setattr(web, "run_scout", boom)
    r = client.post(
        "/api/sources/add", json={"type": "reddit", "value": "golang", "scout_terms": ["generics"]}
    )
    assert r.status_code == 200 and "API key" in r.json()["scout_error"]
    assert "reddit:golang" in yaml.safe_load(config_path.read_text())["sources"]["scout"]


def test_scout_route_and_validation(env, monkeypatch) -> None:  # noqa: F811
    client, *_ = env
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(web, "run_scout", _fake_scout(calls))
    client.post("/api/sources/add", json={"type": "reddit", "value": "LocalLLaMA"})
    r = client.post(
        "/api/sources/scout",
        json={"key": "reddit:LocalLLaMA", "look_back_days": 5, "scout_terms": ["gemma"]},
    )
    assert r.status_code == 200 and r.json()["scout"]["terms"][0]["term"] == "gemma"
    r = client.post(
        "/api/sources/scout",
        json={"key": "reddit:LocalLLaMA", "look_back_days": 5, "scout_terms": ["gemma"]},
    )
    assert "scout" not in r.json() and len(calls) == 1  # nothing new to search
    bad = [
        {"key": "nope:x", "scout_terms": []},
        {"key": "reddit:LocalLLaMA", "look_back_days": 15},
        {"key": "reddit:LocalLLaMA", "scout_terms": [f"t{i}" for i in range(11)]},
    ]
    for body in bad:
        assert client.post("/api/sources/scout", json=body).status_code in (400, 422), body
    hn = client.post("/api/sources/add", json={"type": "hackernews", "value": "rustlang"})
    hn_key = next(x["key"] for x in hn.json()["catalog"] if "rustlang" in x["key"])
    r = client.post("/api/sources/scout", json={"key": hn_key, "scout_terms": ["x"]})
    assert r.status_code == 400 and "YouTube channels, feeds and subreddits" in r.json()["error"]


def test_youtube_key_routes(env) -> None:  # noqa: F811
    client, *_ = env
    assert client.get("/api/search").json()["youtube"] == {"configured": False}
    assert client.get("/api/sources").json()["youtube_api_key"] is False
    r = client.put("/api/search/youtube-key", json={"key": "AIzaSyTESTKEY0123456789abcd"})
    assert r.status_code == 200 and r.json()["youtube"]["masked"].startswith("AIza")
    assert "TESTKEY" not in json.dumps(r.json())
    assert client.get("/api/sources").json()["youtube_api_key"] is True
    assert client.delete("/api/search/youtube-key").json()["youtube"] == {"configured": False}
