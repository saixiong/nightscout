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
    extra = json.loads(row["extra"] or "{}")
    signal = max(
        int(extra.get("points") or 0),
        int(extra.get("stars") or 0),
        int(extra.get("likes") or 0) + int(extra.get("reposts") or 0),
        int(extra.get("favourites") or 0) + int(extra.get("boosts") or 0),
    )
    buzz = len(json.loads(row["also_seen"] or "[]"))
    return min(0.1, math.log10(signal + 1) / 30) + 0.05 * min(buzz, 3)


def rank_candidates(
    rows: list[sqlite3.Row],
    profile: Profile,
    weights: Weights,
    item_vecs: dict[int, list[float]] | None = None,
    project_vecs: list[list[float]] | None = None,
) -> list[Candidate]:
    phrases = profile.keywords()
    out: list[Candidate] = []
    for row in rows:
        if weights.muted(row["source_name"]):
            continue
        text = item_text(row)
        title = row["title"]
        hits = {name: keyword_hits(text, ps) for name, ps in phrases.items()}
        hits = {k: v for k, v in hits.items() if v}
        title_hits = sum(1 for ps in phrases.values() for p in ps if _phrase_re(p).search(title))
        kw = min(1.0, (sum(len(v) for v in hits.values()) + title_hits) / 4)
        sim = 0.0
        if item_vecs and project_vecs and row["id"] in item_vecs:
            sim = max(cosine(item_vecs[row["id"]], pv) for pv in project_vecs)
        base = 0.5 * kw + 0.5 * sim if item_vecs else kw
        score = base + engagement_boost(row) + weights.source_weight(row["source_name"])
        out.append(Candidate(row["id"], round(score, 4), hits, round(sim, 4)))
    out.sort(key=lambda c: c.score, reverse=True)
    return out
