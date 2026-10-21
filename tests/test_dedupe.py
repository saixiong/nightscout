from __future__ import annotations

from datetime import timedelta

import pytest

from night_scout.core.config import Config, HackerNewsSource, RssSource, Sources
from night_scout.core.dedupe import canonical_url, cosine, near_duplicate_groups
from night_scout.core.fetch import fetch_all
from night_scout.core.models import Item, utcnow
from night_scout.core.sources import SourceTask

from .conftest import make_client


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("http://www.Example.com/post/?utm_source=x&utm_medium=y#frag", "https://example.com/post"),
        ("https://youtu.be/abcdefghijk?si=xyz", "https://www.youtube.com/watch?v=abcdefghijk"),
        ("https://m.youtube.com/shorts/abcdefghijk", "https://youtube.com/watch?v=abcdefghijk&t=3"),
        ("https://old.reddit.com/r/x/comments/1/", "https://www.reddit.com/r/x/comments/1"),
        ("https://example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2"),
    ],
)
def test_canonical_url_collides(a: str, b: str) -> None:
    assert canonical_url(a) == canonical_url(b)


def test_canonical_url_keeps_meaningful_params() -> None:
    assert canonical_url("https://example.com/search?q=gemma") != canonical_url(
        "https://example.com/search?q=llama"
    )
    assert canonical_url("") == ""


def test_cosine_and_groups() -> None:
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine([], []) == 0.0
    assert cosine([0, 0], [1, 1]) == 0.0
    groups = near_duplicate_groups({1: [1, 0], 2: [0.99, 0.01], 3: [0, 1]}, 0.95)
    assert groups == {2: 1}


def _item(uid: str, url: str, source: str = "rss", age_days: float = 0) -> Item:
    return Item(
        source=source,
        source_name=source,
        uid=uid,
        url=url,
        title=uid,
        published_at=utcnow() - timedelta(days=age_days),
    )


def test_store_dedupes_by_uid_and_url(store) -> None:
    run = store.start_run()
    first = store.upsert_item(_item("rss:1", "https://example.com/post?utm_source=a"), run)
    assert first.new
    again = store.upsert_item(_item("rss:1", "https://example.com/post"), run)
    assert not again.new and again.item_id == first.item_id
    cross = store.upsert_item(_item("hn:9", "http://www.example.com/post/", "hackernews"), run)
    assert not cross.new and cross.item_id == first.item_id
    row = store.item(first.item_id)
    assert row["also_seen"] == '["hackernews:hackernews"]'
    # the duplicate uid is remembered so the next run doesn't count it as new
    assert not store.upsert_item(
        _item("hn:9", "http://www.example.com/post/", "hackernews"), run
    ).new
    assert len(store.items_with_status("new")) == 1
    assert len(store.items_with_status("duplicate")) == 1


def test_fetch_all_isolates_failures_and_skips_old_items(store) -> None:
    def ok(_client):
        return [
            _item("rss:fresh", "https://a.example/1"),
            _item("rss:old", "https://a.example/2", age_days=30),
        ]

    def boom(_client):
        raise RuntimeError("adapter bug")

    client, _ = make_client({"reddit.com": 429})
    from night_scout.core.config import RedditSource
    from night_scout.core.sources import reddit

    tasks = [
        SourceTask("t:ok", "OK source", ok),
        SourceTask("t:boom", "Broken source", boom),
        *reddit.tasks(RedditSource(subreddits=["x"])),
    ]
    run = store.start_run()
    statuses = fetch_all(Config(), store, run, client=client, tasks=tasks)
    by_key = {s.key: s for s in statuses}
    assert by_key["t:ok"].ok and by_key["t:ok"].fetched == 2 and by_key["t:ok"].new == 1
    assert not by_key["t:boom"].ok and "adapter bug" in by_key["t:boom"].message
    assert not by_key["reddit:x"].ok
    assert by_key["reddit:x"].line() == "Reddit r/x: HTTP 429, skipped"
    assert len(store.source_statuses(run)) == 3


def test_fetch_all_cross_source_dedupe_with_fixtures(store) -> None:
    client, _ = make_client(
        {"blog.example.com/feed": "blog_atom.xml", "hn.algolia.com": "hn_algolia.json"}
    )
    cfg = Config(
        sources=Sources(
            rss=[RssSource(name="Blog", url="https://blog.example.com/feed")],
            hackernews=HackerNewsSource(lists=[], keywords=["gemma"]),
        )
    )
    cfg.thresholds.max_age_days = 3650
    run = store.start_run()
    statuses = fetch_all(cfg, store, run, client=client)
    assert [s.new for s in statuses] == [1, 0]  # HN story links to the same blog post
