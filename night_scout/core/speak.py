"""Digest text → what Kokoro should SAY, and the Digest → a spoken script of chapters.

Feeds are written to be read, not heard: emoji ("penguin", "chart decreasing"), links, "@",
"~", "#" and "owner/repo" all come out wrong when read aloud. `speakable()` cleans a line up,
then a short pronunciation list fixes names Kokoro gets wrong. Kokoro (misaki) takes
"[Word](/IPA/)" for a custom pronunciation; source text can never inject one, because square
brackets are removed from it first.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any

# Names Kokoro gets wrong, and what to say instead. Matched as whole words (case-sensitive
# unless listed in _ANY_CASE); a digit right after a name ("Qwen3.6") is split off first,
# because Kokoro drops whatever is glued to a "[Word](/IPA/)".
PRONOUNCE: dict[str, str] = {
    "Qwen": "[Qwen](/kwˈɛn/)",
    "Ollama": "[Ollama](/ˈOlˌɑmə/)",
    # misaki already says /hˈɜɹmiz/, which comes out as "Hermey's"; a secondary stress on
    # the second syllable is the closest Kokoro gets to "HER-meez".
    "Hermes": "[Hermes](/hˈɜɹmˌiz/)",
    "LocalLLaMA": "Local Llama",
    "LLaMA": "Llama",
    "vLLM": "V L L M",
    "SGLang": "S G Lang",
    "NVFP4": "N V F P 4",
    "LLMs": "L L Ms",
    "LLM": "L L M",
    "MoE": "M O E",
    "MOE": "M O E",
    "GGUF": "G G U F",
}
_ANY_CASE = {"Qwen", "Ollama", "Hermes"}

_URL = re.compile(r"(?:https?://|www\.)[^\s)\]]+", re.I)
_DROP_CHARS = {"️", "‍", "⃣"}
_RANGE_B = re.compile(r"(?<![\w.\-])(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*[bB]\b")
_ONE_B = re.compile(r"(?<![\w.\-])(\d+(?:\.\d+)?)[bB]\b")
_SLASH = re.compile(r"(?<![\w.:/])([A-Za-z][\w.-]*)/([A-Za-z][\w.-]*)\b(?!/)")


def _no_emoji(s: str) -> str:
    return "".join(
        ch
        for ch in s
        if unicodedata.category(ch) not in ("So", "Sk", "Cs", "Co") and ch not in _DROP_CHARS
    )


def _slash(m: re.Match[str]) -> str:
    """owner/repo and a/b: never "slash". The repo alone when it repeats the owner
    (ollama/ollama → ollama); a domain keeps its host (example.com/feed → example.com)."""
    a, b = m[1], m[2]
    if "." in a and not a.endswith("."):
        return a
    if a.lower() in b.lower():
        return b
    return f"{a} {b}"


def speakable(text: str | None) -> str:
    """One line of Digest text → words for Kokoro. Never raises; '' for nothing."""
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text or "")  # markdown links: their text
    s = _URL.sub("", s)
    s = re.sub(r"\(\s*\)", "", s)  # "(link)" with the link gone
    s = s.replace("[", " ").replace("]", " ")  # only our own pronunciations use [..](/../)
    s = _no_emoji(s)
    s = re.sub(r"\b([CF])#", r"\1 sharp", s)
    # Mastodon: "#llm@mastodon.social" (a hashtag) and "@user@fosstodon.org" (an account)
    s = re.sub(r"#(\w+)@mastodon\.[a-z]+\b", r"\1 on Mastodon", s, flags=re.I)
    s = re.sub(r"(?<![\w@])@?(\w+)@[\w-]+(?:\.[\w-]+)+\b", r"\1", s)
    # GitHub search qualifiers: "topic:llm language:python" → "llm"
    s = re.sub(r"\btopic:([\w-]+)", r"\1", s)
    s = re.sub(r"\s*\blanguage:[\w+#-]+", "", s)
    s = re.sub(r"\br/(\w+)", r"the \1 subreddit", s)
    s = _RANGE_B.sub(r"\1 to \2 billion", s)
    s = _ONE_B.sub(r"\1 billion", s)
    s = re.sub(r"\bI/O\b", "I O", s)
    s = _SLASH.sub(_slash, s)
    s = re.sub(r"(?<!\w)@(?=\w)", "", s)
    s = re.sub(r"~\s*(?=\d)", "about ", s)
    s = s.replace("#", "").replace("&", " and ")
    s = re.sub(r"\s+", " ", s).strip()
    for word, say in PRONOUNCE.items():
        flags = re.I if word in _ANY_CASE else 0
        s = re.sub(rf"\b{re.escape(word)}(?=\d)", f"{word} ", s, flags=flags)
        s = re.sub(rf"\b{re.escape(word)}\b", say, s, flags=flags)
    return re.sub(r"\s+", " ", s).strip()


def sentence(text: str | None) -> str:
    """Speakable text ending in exactly one stop ('flash next..' read as one)."""
    s = speakable(text).rstrip(" .;:,–-")
    if not s:
        return ""
    return s if s.endswith(("?", "!")) else s + "."


# -- the script -----------------------------------------------------------------------
@dataclass(frozen=True)
class Chapter:
    id: str  # "intro", "s<story id>", "outro"
    story_id: int | None
    title: str  # what the player shows
    text: str  # what Kokoro says
    gap: float  # seconds of silence after it


def story_order(stories: list[dict[str, Any]], topics: list[str]) -> list[dict[str, Any]]:
    """The Digest list's order: your topics in order, then 'Other' (as digest.js groups them)."""
    tof = [s.get("topic") or "Other" for s in stories]
    groups = [t for t in [*topics, "Other"] if t in tof]
    groups += sorted({t for t in tof if t not in groups})
    return [s for g in groups for s, t in zip(stories, tof, strict=True) if t == g]


def day_words(day: date) -> str:
    return f"{day:%A, %B} {day.day}"


def script(digest: dict[str, Any], headlines_only: bool = False) -> list[Chapter]:
    """/api/digest's payload → chapters: intro, each story (its topic said at the start of
    the topic's first story), outro."""
    stories = story_order(
        list(digest.get("stories") or []), list(digest.get("digest_topics") or [])
    )
    topics: list[str] = []
    for s in stories:
        t = s.get("topic") or "Other"
        if t not in topics:
            topics.append(t)
    n, k = len(stories), len(topics)
    day = date.fromisoformat(str(digest["day"]))
    intro = (
        f"Night Scout. Your Digest for {day_words(day)}. "
        f"{n} {'story' if n == 1 else 'stories'}, across {k} {'topic' if k == 1 else 'topics'}."
    )
    out = [Chapter("intro", None, "Night Scout · your Digest", intro, 0.7)]
    said: set[str] = set()
    for s in stories:
        topic = s.get("topic") or "Other"
        head = s.get("headline") or s.get("title") or ""
        parts = []
        if topic not in said:
            said.add(topic)
            parts.append("Other stories." if topic == "Other" else sentence(topic))
        parts.append(sentence(head))
        source = speakable(s.get("source_name"))
        if source:
            parts.append(f"From {source}.")
        if not headlines_only and s.get("summary"):
            parts.append(sentence(s["summary"]))
        out.append(Chapter(f"s{s['id']}", int(s["id"]), head, " ".join(p for p in parts if p), 0.8))
    outro = "That's the Digest." + (
        " Your next steps are in Actions." if digest.get("projects") else ""
    )
    out.append(Chapter("outro", None, "That's the Digest", outro, 0.3))
    return out
