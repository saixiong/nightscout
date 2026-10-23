"""Listen: Digest text → what Kokoro says, and the Digest → its spoken script."""

from __future__ import annotations

import pytest

from night_scout.core.speak import script, sentence, speakable, story_order


@pytest.mark.parametrize(
    ("text", "said"),
    [
        # emoji are dropped, never read out ("penguin", "chart decreasing")
        ("🐧 LlamaStash – terminal-based tool 📉", "LlamaStash – terminal-based tool"),
        ("Ship it 🚀🔥 now ❤️", "Ship it now"),
        ("flags 🇺🇸 and keycaps 1️⃣ go", "flags and keycaps 1 go"),
        # links go; a markdown link keeps its text
        ("see https://example.com/a?b=1 and http://x.io now", "see and now"),
        ("read www.example.com/post today", "read today"),
        ("a [nice post](https://x.y/z) here", "a nice post here"),
        ("Paper (https://arxiv.org/abs/1) out", "Paper out"),
        # @handles, ~N, #tags
        ("by @simonw on Bluesky", "by simonw on Bluesky"),
        ("(~50 params)", "(about 50 params)"),
        ("~ 3 hours", "about 3 hours"),
        ("#rust is fun", "rust is fun"),
        # Mastodon hashtags and accounts
        ("#llm@mastodon.social", "llm on Mastodon"),
        ("From #LocalLLM@mastodon.social today", "From LocalLLM on Mastodon today"),
        ("@karpathy@fosstodon.org wrote", "karpathy wrote"),
        # GitHub search qualifiers
        ("topic:llm language:python", "llm"),
        ("GitHub search topic:mcp language:typescript", "GitHub search mcp"),
        # Reddit
        ("r/LocalLLaMA", "the Local Llama subreddit"),
        ("From r/rust", "From the rust subreddit"),
        # owner/repo and a/b: never "slash"
        ("ollama/ollama releases", "[Ollama](/ˈOlˌɑmə/) releases"),
        ("openai/codex", "openai codex"),
        ("simonw/llm-tools", "simonw llm-tools"),
        ("and/or input/output", "and or input output"),
        ("I/O bound", "I O bound"),
        ("feeds.example.com/rss", "feeds.example.com"),
        ("24/7 support", "24/7 support"),
        # sizes: "50-150b" is a range of billions; not inside a model name
        ("medium (~50-150b params)", "medium (about 50 to 150 billion params)"),
        ("a 7b model and a 1.5B one", "a 7 billion model and a 1.5 billion one"),
        ("Gemma 4 12B", "Gemma 4 12 billion"),
        ("Based on Qwen3.6-35B MoE", "Based on [Qwen](/kwˈɛn/) 3.6-35B M O E"),
        ("Llama-3-8B-Instruct", "Llama-3-8B-Instruct"),
        # pronunciations (and a digit glued to a name is split off: Kokoro would drop it)
        ("Qwen3 and qwen", "[Qwen](/kwˈɛn/) 3 and [Qwen](/kwˈɛn/)"),
        ("Ollama 0.12", "[Ollama](/ˈOlˌɑmə/) 0.12"),
        ("vLLM and SGLang", "V L L M and S G Lang"),
        ("LLMs beat an LLM", "L L Ms beat an L L M"),
        ("at NVFP4 in GGUF", "at N V F P 4 in G G U F"),
        ("Hermes 4 is out", "[Hermes](/hˈɜɹmˌiz/) 4 is out"),
        ("MOE model", "M O E model"),
        ("C# and F#", "C sharp and F sharp"),
        ("AT&T", "AT and T"),
        ("", ""),
        (None, ""),
    ],
)
def test_speakable(text: str | None, said: str) -> None:
    assert speakable(text) == said


def test_source_text_cannot_inject_a_pronunciation() -> None:
    # Kokoro reads "[word](/ipa/)" as a pronunciation; only our own list may use it
    assert speakable("Say [this](/ðɪs/) twice") == "Say this twice"
    assert "[" not in speakable("an [unclosed bracket and a ] stray one")
    assert speakable("[Qwen] is fine") == "[Qwen](/kwˈɛn/) is fine"


def test_sentence_ends_in_one_stop() -> None:
    assert sentence("Aside from Qwen flash next..") == "Aside from [Qwen](/kwˈɛn/) flash next."
    assert sentence("Is it good?") == "Is it good?"
    assert sentence("Done!") == "Done!"
    assert sentence("Trailing colon:") == "Trailing colon."
    assert sentence("🐧") == ""


def _story(i: int, topic: str | None, **kw: object) -> dict[str, object]:
    return {
        "id": i,
        "topic": topic,
        "headline": kw.get("headline", f"Headline {i}"),
        "title": f"Title {i}",
        "source_name": kw.get("source_name", "r/LocalLLaMA"),
        "summary": kw.get("summary", f"Summary {i}."),
    }


DIGEST = {
    "day": "2026-10-22",
    "digest_topics": ["Local LLMs", "AI coding agents", "Model Context Protocol", "Security"],
    "projects": ["llm"],
    "stories": [
        _story(1, "Security"),
        _story(2, "Local LLMs", headline=None, summary=None),
        _story(3, "Local LLMs", source_name="#llm@mastodon.social"),
        _story(4, "AI coding agents", headline="🐧 Codex ships /goal"),
    ],
}


def test_story_order_follows_the_digest_list() -> None:
    # your topics in order, then "Other": the same grouping as the Digest's left list
    stories = [_story(1, None), _story(2, "B"), _story(3, "A"), _story(4, "B")]
    assert [s["id"] for s in story_order(stories, ["A", "B"])] == [3, 2, 4, 1]


def test_script_intro_stories_outro() -> None:
    ch = script(DIGEST)
    assert [c.id for c in ch] == ["intro", "s2", "s3", "s4", "s1", "outro"]
    assert ch[0].text == (
        "Night Scout. Your Digest for Thursday, October 22. 4 stories, across 3 topics."
    )
    # the topic is said at the start of its first story only
    assert ch[1].text == "Local L L Ms. Title 2. From the Local Llama subreddit."
    assert ch[2].text == "Headline 3. From llm on Mastodon. Summary 3."
    assert (
        ch[3].text
        == "AI coding agents. Codex ships /goal. From the Local Llama subreddit. Summary 4."
    )
    assert ch[3].title == "🐧 Codex ships /goal" and ch[3].story_id == 4
    assert ch[4].text.startswith("Security. Headline 1.")
    assert ch[-1].text == "That's the Digest. Your next steps are in Actions."
    assert all(c.gap > 0 for c in ch)


def test_script_headlines_only_and_no_projects() -> None:
    d = {**DIGEST, "projects": []}
    ch = script(d, headlines_only=True)
    assert ch[2].text == "Headline 3. From llm on Mastodon."
    assert all("Summary" not in c.text for c in ch)
    assert ch[-1].text == "That's the Digest."
    full = sum(len(c.text.split()) for c in script(d))
    assert sum(len(c.text.split()) for c in ch) < full


def test_script_singulars_and_other() -> None:
    d = {"day": "2026-10-23", "digest_topics": ["A"], "stories": [_story(9, None)]}
    ch = script(d)
    assert ch[0].text == "Night Scout. Your Digest for Friday, October 23. 1 story, across 1 topic."
    assert ch[1].text.startswith("Other stories. Headline 9.")
