"""Fixes for bugs the rebuilt e2e kits found (chat history tool notes, GitHub repo check)."""

from __future__ import annotations

import httpx
import pytest

from night_scout.core.chat import add_message, create_thread, ensure_tables, history
from night_scout.core.config import Config
from night_scout.core.db import Store
from night_scout.core.discover import add_source

from .conftest import make_client


def test_history_notes_the_tools_an_answer_used(store: Store) -> None:
    ensure_tables(store)
    t = create_thread(store, {"kind": "app"}, "Ask")
    add_message(store, t, 1, "user", "Follow Fireship and add a Rust topic")
    add_message(
        store,
        t,
        1,
        "tool",
        tool_name="propose_source",
        tool_args={"type": "youtube", "value": "@fireship"},
        ok=True,
        result="{}",
    )
    add_message(
        store,
        t,
        1,
        "tool",
        tool_name="propose_topic",
        tool_args={"topic": "Rust"},
        ok=True,
        result="{}",
    )
    add_message(store, t, 1, "assistant", "I've put buttons below.")
    add_message(store, t, 2, "user", "What's new tonight?")
    add_message(store, t, 2, "assistant", "Three stories.")
    h = history(store, t, 3)
    assert [m["role"] for m in h] == ["user", "assistant", "user", "assistant"]
    first = h[1]["content"]
    assert first.startswith("I've put buttons below.")
    assert (
        "propose_source(type=youtube, value=@fireship)" in first
        and "propose_topic(topic=Rust)" in first
    )
    assert "call the tool again" in first
    assert h[3]["content"] == "Three stories."  # no tools, no note


def _gh(routes):
    client, seen = make_client(routes)
    return client, seen


def test_github_missing_repo_suggests_the_real_one() -> None:
    hits = {
        "items": [
            {"name": "rich", "full_name": "Textualize/rich"},
            {"name": "rich-cli", "full_name": "Textualize/rich-cli"},
            {"name": "rich", "full_name": "someone/rich"},
        ]
    }
    client, seen = _gh(
        {
            "api.github.com/repos/richtyp/rich": 404,
            "api.github.com/search/repositories": lambda r: httpx.Response(200, json=hits),
        }
    )
    with pytest.raises(
        ValueError,
        match=r"Did you mean Textualize/rich\? \(most-starred repos named 'rich': "
        r"Textualize/rich, someone/rich\)",
    ):
        add_source(Config(), "github", "richtyp/rich", client)
    assert any("q=rich+in%3Aname" in u or "q=rich%20in%3Aname" in u for u in seen)


def test_github_repo_must_exist() -> None:
    client, seen = _gh(
        {"api.github.com/repos/pallets/rich": 404, "api.github.com/search/repositories": 404}
    )
    with pytest.raises(ValueError, match="GitHub has no repository 'pallets/rich'"):
        add_source(Config(), "github", "pallets/rich", client)
    assert any("api.github.com/repos/pallets/rich" in u for u in seen)


def test_github_repo_uses_its_real_name() -> None:
    client, _ = _gh(
        {
            "api.github.com/repos/textualize/rich": lambda r: httpx.Response(
                200, json={"full_name": "Textualize/rich"}
            )
        }
    )
    cfg = Config()
    add_source(cfg, "github", "textualize/rich", client)
    assert cfg.sources.github is not None and cfg.sources.github.releases == ["Textualize/rich"]


def test_github_check_can_be_unavailable() -> None:
    def limited(r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, headers={"x-ratelimit-remaining": "0"}, json={"message": "API rate limit exceeded"}
        )

    client, _ = _gh({"api.github.com/repos/": limited})
    cfg = Config()
    add_source(cfg, "github", "astral-sh/uv", client)  # can't ask GitHub: keep the name
    assert cfg.sources.github is not None and "astral-sh/uv" in cfg.sources.github.releases


def test_github_search_is_not_checked() -> None:
    client, seen = _gh({})
    cfg = Config()
    add_source(cfg, "github", "topic:mcp language:python", client)
    assert not seen


def test_readd_resolves_from_the_duplicate_message_without_network() -> None:
    from night_scout.core.config import YouTubeSource
    from night_scout.core.discover import AlreadyFollowing, add_source_keyed

    cfg = Config()
    cfg.sources.youtube = [YouTubeSource(name="Fireship", channel_id="UCsBjURrPoezykLs9EqgamOA")]
    channel_page = (
        '<link rel="canonical" href="https://www.youtube.com/channel/UCsBjURrPoezykLs9EqgamOA">'
    )
    calls = {"n": 0}

    def flaky(r: httpx.Request) -> httpx.Response:
        calls["n"] += 1  # the first resolve works, any later one fails (YouTube flapping)
        return httpx.Response(200, text=channel_page) if calls["n"] == 1 else httpx.Response(500)

    client, _ = _gh({"youtube.com": flaky})
    with pytest.raises(AlreadyFollowing) as e:
        add_source_keyed(cfg, "youtube", "@Fireship", client)
    assert (
        e.value.key == "youtube:UCsBjURrPoezykLs9EqgamOA" and e.value.label == "YouTube: Fireship"
    )


def test_youtube_add_survives_a_feed_outage_when_the_page_resolves() -> None:
    page = (
        '<meta property="og:title" content="Higgsfield AI &amp; Friends">'
        '<link rel="canonical" href="https://www.youtube.com/channel/UCh13OyDSm-Kb8ij3yZArtFg">'
    )
    client, _ = _gh(
        {
            "/feeds/videos.xml": 404,
            "youtube.com/@HiggsfieldAI": lambda r: httpx.Response(200, text=page),
        }
    )
    cfg = Config()
    add_source(cfg, "youtube", "@HiggsfieldAI", client)
    assert [(y.name, y.channel_id) for y in cfg.sources.youtube] == [
        ("Higgsfield AI & Friends", "UCh13OyDSm-Kb8ij3yZArtFg")
    ]


def test_youtube_bare_channel_id_still_needs_the_feed() -> None:
    client, _ = _gh({"/feeds/videos.xml": 404})
    with pytest.raises(ValueError, match="no public feed"):
        add_source(Config(), "youtube", "UCh13OyDSm-Kb8ij3yZArtFg", client)


def test_youtube_plain_channel_name_tries_the_handle() -> None:
    page = '<meta property="og:title" content="Two Minute Papers"><link rel="canonical" href="https://www.youtube.com/channel/UCbfYPyITQ-7l4upoX8nvctg">'
    client, _ = _gh(
        {
            "youtube.com/@TwoMinutePapers": lambda r: httpx.Response(200, text=page),
            "/feeds/videos.xml": 500,
        }
    )
    cfg = Config()
    add_source(cfg, "youtube", "Two Minute Papers", client)
    assert cfg.sources.youtube[0].channel_id == "UCbfYPyITQ-7l4upoX8nvctg"
    client, _ = _gh({"youtube.com/@NoSuchChannelHere": 404})
    with pytest.raises(ValueError, match="guessed from the name"):
        add_source(Config(), "youtube", "No Such Channel Here", client)
