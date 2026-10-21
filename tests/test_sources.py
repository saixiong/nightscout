from __future__ import annotations

from night_scout.core.config import (
    BlueskySource,
    GitHubSource,
    HackerNewsSource,
    MastodonSource,
    RedditSource,
    RssSource,
    YouTubeSource,
)
from night_scout.core.sources import bluesky, github, hackernews, mastodon, reddit, rss, youtube

from .conftest import make_client


def test_youtube_feed() -> None:
    client, seen = make_client({"feeds/videos.xml": "youtube_channel.xml"})
    [task] = youtube.tasks([YouTubeSource(name="Example", channel_id="UC_example")])
    items = task.fetch(client)
    assert "channel_id=UC_example" in seen[0]
    assert [i.uid for i in items] == ["yt:abcdefghijk", "yt:zyxwvutsrqp"]
    first = items[0]
    assert first.title == "Running Gemma locally in 5 minutes"
    assert "pull a Gemma model" in first.text
    assert first.extra["video_id"] == "abcdefghijk"
    assert first.published_at is not None and first.published_at.year == 2026


def test_rss_atom_and_rss2() -> None:
    client, _ = make_client(
        {"blog.example.com": "blog_atom.xml", "rss.example.org": "blog_rss.xml"}
    )
    tasks = rss.tasks(
        [
            RssSource(name="Atom", url="https://blog.example.com/feed"),
            RssSource(name="RSS", url="https://rss.example.org/feed"),
        ]
    )
    atom = tasks[0].fetch(client)
    assert atom[0].title == "Structured output with local models"
    assert "small models reliable" in atom[0].text and "<b>" not in atom[0].text
    assert atom[0].author == "Jane Doe"
    rss2 = tasks[1].fetch(client)
    assert rss2[0].title == "Hello & welcome"
    assert rss2[0].published_at is not None


def test_hackernews_lists_and_keywords() -> None:
    client, seen = make_client(
        {
            "topstories.json": "hn_topstories.json",
            "item/1001.json": "hn_item_1001.json",
            "item/1002.json": "hn_item_1002.json",
            "item/1003.json": "hn_item_1003.json",
            "hn.algolia.com": "hn_algolia.json",
        }
    )
    tasks = hackernews.tasks(HackerNewsSource(lists=["top"], keywords=["gemma"], limit=10))
    top = tasks[0].fetch(client)
    assert [i.uid for i in top] == ["hn:1001", "hn:1002"]  # the job post is skipped
    assert top[0].extra["points"] == 250
    assert top[1].url == "https://news.ycombinator.com/item?id=1002"  # Ask HN has no url
    assert "drowning" in top[1].text
    kw = tasks[1].fetch(client)
    assert [i.uid for i in kw] == ["hn:2001"]  # hit without a title dropped
    assert "typoTolerance=false" in seen[-1] and "numericFilters" in seen[-1]


def test_hackernews_min_points() -> None:
    client, _ = make_client(
        {
            "topstories.json": "hn_topstories.json",
            "item/1001.json": "hn_item_1001.json",
            "item/1002.json": "hn_item_1002.json",
            "item/1003.json": "hn_item_1003.json",
        }
    )
    [task] = hackernews.tasks(HackerNewsSource(lists=["top"], min_points=100))
    assert [i.uid for i in task.fetch(client)] == ["hn:1001"]


def test_reddit_rss_external_link_and_self_post() -> None:
    client, seen = make_client({"reddit.com/r/LocalLLaMA/new/.rss": "reddit_sub.xml"})
    [task] = reddit.tasks(RedditSource(subreddits=["r/LocalLLaMA"]))
    items = task.fetch(client)
    assert "limit=25" in seen[0]
    link_post, self_post = items
    assert link_post.uid == "reddit:t3_abc123"
    assert link_post.url == "https://example.net/quant-trick"
    assert link_post.extra["discussion"].endswith("/abc123/new_quant/")
    assert link_post.text == "New quantization trick halves memory."
    assert link_post.author == "someone"
    assert self_post.url == "https://www.reddit.com/r/LocalLLaMA/comments/def456/question/"


def test_bluesky_search_and_author() -> None:
    client, seen = make_client(
        {
            "searchPosts": "bsky_search.json",
            "getAuthorFeed": "bsky_author.json",
        }
    )
    tasks = bluesky.tasks(BlueskySource(queries=["gemma"], actors=["news.example.org"]))
    found = tasks[0].fetch(client)
    assert len(found) == 1  # empty post dropped
    post = found[0]
    assert post.url == "https://example.dev/rag-demo"
    assert post.extra["discussion"] == "https://bsky.app/profile/dev.example.com/post/3kxyz"
    assert post.title == "Just shipped a local RAG demo with Gemma"
    assert "api.bsky.app" in seen[0]
    author = tasks[1].fetch(client)
    assert author[0].url == "https://bsky.app/profile/news.example.org/post/3kq"
    assert "public.api.bsky.app" in seen[1]


def test_mastodon_hashtag() -> None:
    client, seen = make_client({"/api/v1/timelines/tag/llm": "mastodon_tag.json"})
    [task] = mastodon.tasks(
        [MastodonSource(instance="https://mastodon.example/", hashtags=["#llm"])]
    )
    items = task.fetch(client)
    assert seen[0].startswith("https://mastodon.example/api/v1/timelines/tag/llm")
    assert len(items) == 1
    assert items[0].url == "https://example.dev/tool"
    assert "a new tool" in items[0].text
    assert items[0].uid == "mastodon:https://mastodon.example/users/a/statuses/111"


def test_github_releases_and_search(monkeypatch) -> None:
    monkeypatch.setenv("NS_TEST_TOKEN", "t0ken")
    captured = {}

    def releases(request):
        captured["auth"] = request.headers.get("authorization")
        return None  # fall through to the fixture

    client, seen = make_client(
        {
            "/releases": releases,
            "/repos/ollama/ollama/releases": "gh_releases.json",
            "/search/repositories": "gh_search.json",
        }
    )
    tasks = github.tasks(
        GitHubSource(releases=["ollama/ollama"], search=["topic:llm"], token_env="NS_TEST_TOKEN")
    )
    rels = tasks[0].fetch(client)
    assert captured["auth"] == "Bearer t0ken"
    assert len(rels) == 1 and rels[0].extra["tag"] == "v0.40.7"  # draft skipped
    repos = tasks[1].fetch(client)
    assert repos[0].uid == "gh-repo:someone/tiny-agent"
    assert repos[0].extra["stars"] == 321
    assert "created%3A%3E%3D" in seen[-1] or "created:>=" in seen[-1]


def test_hackernews_tolerates_single_story_failure() -> None:
    client, _ = make_client(
        {
            "topstories.json": "hn_topstories.json",
            "item/1001.json": "hn_item_1001.json",
            "item/1002.json": 500,
            "item/1003.json": "hn_item_1003.json",
        }
    )
    [task] = hackernews.tasks(HackerNewsSource(lists=["top"]))
    assert [i.uid for i in task.fetch(client)] == ["hn:1001"]
