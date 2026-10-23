"""👍 / 👎 / ignore, stored locally, turned into pre-filter weights.

Votes on an item also nudge its source and topics, so the feed tunes itself
without you having to edit config. Weights are bounded so one grumpy morning
can't silence a source forever; an explicit source 👎 three times mutes it.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

from .db import Store

STEP = 0.1
ITEM_SHARE = 0.5  # an item vote counts half toward its source and topics
MUTE_AT = -0.3
MUTE_VOTES = -3  # one "Mute source" from the Digest = three source 👎 (STEP × -3 = MUTE_AT)


@dataclass
class Weights:
    source: dict[str, float] = field(default_factory=dict)
    topic: dict[str, float] = field(default_factory=dict)
    ignored_items: set[int] = field(default_factory=set)

    def source_weight(self, source_name: str) -> float:
        return self.source.get(source_name, 0.0)

    def topic_weight(self, topics: list[str]) -> float:
        if not topics:
            return 0.0
        return sum(self.topic.get(t.lower(), 0.0) for t in topics) / max(1, len(topics))

    def muted(self, source_name: str) -> bool:
        return self.source_weight(source_name) <= MUTE_AT


def _clamp(x: float) -> float:
    return max(-0.5, min(0.3, x))


def compute_weights(store: Store) -> Weights:
    src: dict[str, float] = defaultdict(float)
    top: dict[str, float] = defaultdict(float)
    ignored: set[int] = set()
    latest_item_vote: dict[int, int] = {}
    for row in store.feedback_rows():
        kind, target, value = row["target_type"], row["target"], int(row["value"])
        if kind == "source":
            src[target] += STEP * value
        elif kind == "topic":
            top[target.lower()] += STEP * value
        elif kind == "item" and row["item_id"] is not None:
            latest_item_vote[int(row["item_id"])] = value  # last vote on an item wins
    for item_id, value in latest_item_vote.items():
        if value == 0:
            ignored.add(item_id)
            continue
        item = store.item(item_id)
        if item is None:
            continue
        src[item["source_name"]] += STEP * ITEM_SHARE * value
        score = store.score(item_id)
        if score is not None:
            for t in json.loads(score["topics"]):
                top[str(t).lower()] += STEP * ITEM_SHARE * value
    return Weights(
        source={k: _clamp(v) for k, v in src.items()},
        topic={k: _clamp(v) for k, v in top.items()},
        ignored_items=ignored,
    )
