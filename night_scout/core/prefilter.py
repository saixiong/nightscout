"""Cheap pre-filter: keyword and embedding similarity to the profile, so Gemma only
reads likely-relevant items. Feedback weights and cross-source buzz nudge the order."""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from dataclasses import dataclass

from .dedupe import cosine
from .feedback import Weights
from .profile import Profile

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    item_id: int
    score: float
    keyword_hits: dict[str, list[str]]  # project -> matched phrases
    similarity: float


def item_text(row: sqlite3.Row, limit: int = 1500) -> str:
    return f"{row['title']}\n{(row['text'] or '')[:limit]}"


def _phrase_re(phrase: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])" + re.escape(phrase) + r"(?![\w-])", re.I)


def keyword_hits(text: str, phrases: set[str]) -> list[str]:
    hits: list[str] = []
    for p in phrases:
        if len(p) < 2:
            continue
        if _phrase_re(p).search(text):
            hits.append(p)
            continue
        # multi-word phrases count if every significant word appears
        words = [w for w in re.findall(r"[\w.+#-]+", p) if len(w) > 3]
        if len(words) > 1 and all(_phrase_re(w).search(text) for w in words):
            hits.append(p)
    return sorted(hits)


def engagement_boost(row: sqlite3.Row) -> float:
    """A small tiebreaker for buzz; never enough to lift an off-topic item."""
    extra = json.loads(row["extra"] or "{}")
    signal = max(
        int(extra.get("points") or 0),
        int(extra.get("stars") or 0),
        int(extra.get("likes") or 0) + int(extra.get("reposts") or 0),
        int(extra.get("favourites") or 0) + int(extra.get("boosts") or 0),
    )
    buzz = len(json.loads(row["also_seen"] or "[]"))
    return min(0.03, math.log10(signal + 1) / 100) + 0.02 * min(buzz, 3)


def doc_text(row: sqlite3.Row) -> str:
    """EmbeddingGemma's document prompt format."""
    return f"title: {row['title']} | text: {(row['text'] or '')[:600]}"


def query_texts(profile: Profile) -> list[str]:
    """One retrieval query per interest, wish, key dependency and topic (EmbeddingGemma format)."""
    out: list[str] = []
    for p in profile.projects:
        phrases = [p.get("purpose") or ""]
        for key in ("interests", "would_want_to_know", "key_dependencies"):
            phrases += [str(x) for x in p.get(key) or []]
        out += [f"task: search result | query: {ph}" for ph in phrases if ph.strip()]
    out += [f"task: search result | query: {t}" for t in profile.topics if t.strip()]
    return out


def _normalize(sims: dict[int, float]) -> dict[int, float]:
    """Embedding cosines share a high floor; rescale so median -> 0 and p95 -> 1."""
    if len(sims) < 5:
        return {k: max(0.0, v) for k, v in sims.items()}
    vals = sorted(sims.values())
    med = vals[len(vals) // 2]
    p95 = vals[min(len(vals) - 1, int(len(vals) * 0.95))]
    span = max(p95 - med, 1e-6)
    return {k: max(0.0, min(1.2, (v - med) / span)) for k, v in sims.items()}


def rank_candidates(
    rows: list[sqlite3.Row],
    profile: Profile,
    weights: Weights,
    item_vecs: dict[int, list[float]] | None = None,
    query_vecs: list[list[float]] | None = None,
) -> list[Candidate]:
    phrases = profile.keywords()
    raw_sims: dict[int, float] = {}
    if item_vecs and query_vecs:
        for row in rows:
            vec = item_vecs.get(row["id"])
            if vec is not None:
                raw_sims[row["id"]] = max(cosine(vec, q) for q in query_vecs)
    sims = _normalize(raw_sims)
    out: list[Candidate] = []
    for row in rows:
        if weights.muted(row["source_name"]):
            continue
        text = item_text(row)
        title = row["title"]
        hits = {name: keyword_hits(text, ps) for name, ps in phrases.items()}
        hits = {k: v for k, v in hits.items() if v}
        title_hits = sum(1 for ps in phrases.values() for p in ps if _phrase_re(p).search(title))
        kw = min(1.0, (sum(len(v) for v in hits.values()) + title_hits) / 3)
        sim = sims.get(row["id"], 0.0)
        base = 0.65 * sim + 0.35 * kw if sims else kw
        score = base + engagement_boost(row) + weights.source_weight(row["source_name"])
        out.append(
            Candidate(row["id"], round(score, 4), hits, round(raw_sims.get(row["id"], 0), 4))
        )
    out.sort(key=lambda c: c.score, reverse=True)
    return out
