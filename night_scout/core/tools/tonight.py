"""search_tonight: search the items Night Scout fetched for one Digest day.

Reads `items` + `scores`. The Digest columns (`headline`, `digest_topic`) arrive with the
Digest track and may be missing or NULL; everything here works without them.
"""

from __future__ import annotations

import re
from typing import Any

from ..db import Store
from .base import ToolContext, clip, fail

LIMIT = 8
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Words that say nothing about what to look for.
_STOP = frozenset(
    re.findall(
        r"\S+",
        "the and for with what about this that from into your are was were has have how why "
        "who which there their them they its it's any all can does did not but you tonight "
        "today story stories item items news main thread running through",
    )
)


def score_columns(store: Store) -> set[str]:
    return {str(r["name"]) for r in store.conn.execute("PRAGMA table_info(scores)").fetchall()}


def latest_day(store: Store) -> str | None:
    row = store.conn.execute("SELECT day FROM digests ORDER BY day DESC LIMIT 1").fetchone()
    return str(row["day"]) if row else None


def run_ids_for_day(store: Store, day: str) -> list[int]:
    """The run(s) behind a Digest day: the digest's run, else runs started that day."""
    row = store.conn.execute("SELECT run_id FROM digests WHERE day=?", (day,)).fetchone()
    if row is not None and row["run_id"] is not None:
        return [int(row["run_id"])]
    rows = store.conn.execute(
        "SELECT id FROM runs WHERE substr(started_at, 1, 10)=? ORDER BY id", (day,)
    ).fetchall()
    return [int(r["id"]) for r in rows]


def day_rows(store: Store, day: str) -> list[Any]:
    """Every item fetched for that day, with whatever score columns exist."""
    cols = score_columns(store)
    extra = [f"s.{c}" for c in ("headline", "digest_topic", "digest_relevance") if c in cols]
    select = ", ".join(
        [
            "i.id",
            "i.title",
            "i.url",
            "i.source",
            "i.source_name",
            "s.summary",
            "s.best_score",
            "s.topics",
            *extra,
        ]
    )
    runs = run_ids_for_day(store, day)
    if runs:
        marks = ",".join("?" * len(runs))
        sql = (
            f"SELECT {select} FROM items i LEFT JOIN scores s ON s.item_id=i.id "
            f"WHERE i.run_id IN ({marks}) AND i.dup_of IS NULL"
        )
        return list(store.conn.execute(sql, tuple(runs)).fetchall())
    sql = (
        f"SELECT {select} FROM items i LEFT JOIN scores s ON s.item_id=i.id "
        "WHERE substr(i.first_seen, 1, 10)=? AND i.dup_of IS NULL"
    )
    return list(store.conn.execute(sql, (day,)).fetchall())


def _get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _words(query: str) -> list[str]:
    return [w for w in re.findall(r"[\w.+#-]+", query.lower()) if len(w) > 2 and w not in _STOP]


def _match(row: Any, words: list[str]) -> float:
    fields = [
        (str(_get(row, "title") or ""), 3.0),
        (str(_get(row, "headline") or ""), 3.0),
        (str(_get(row, "digest_topic") or ""), 2.0),
        (str(_get(row, "topics") or ""), 1.5),
        (str(_get(row, "summary") or ""), 1.0),
        (str(_get(row, "source_name") or ""), 1.0),
    ]
    total = 0.0
    for text, weight in fields:
        low = text.lower()
        total += weight * sum(1 for w in words if w in low)
    return total


def _hit(row: Any) -> dict[str, Any]:
    url = str(_get(row, "url") or "")
    out: dict[str, Any] = {
        "id": _get(row, "id"),
        "title": " ".join(str(_get(row, "title") or "").split())[:160],
        "source": _get(row, "source_name"),
        "url": url if url.startswith(("http://", "https://")) else None,
    }
    for key in ("headline", "digest_topic"):
        if _get(row, key):
            out["topic" if key == "digest_topic" else key] = _get(row, key)
    summary = str(_get(row, "summary") or "")
    if summary:
        out["summary"] = clip(summary, 400)[0]
    if _get(row, "best_score") is not None:
        out["relevance"] = round(float(_get(row, "best_score")), 2)
    return out


def search_tonight(ctx: ToolContext, query: Any = "", day: Any = None) -> dict[str, Any]:
    if ctx.store is None:
        return fail("The item database isn't available.")
    query = str(query or "").strip()[:300]
    day = str(day or ctx.day or "").strip() or latest_day(ctx.store)
    if not day:
        return fail("No runs yet, so there are no items to search.")
    if not _DAY.match(day):
        return fail("day must look like YYYY-MM-DD.")
    rows = day_rows(ctx.store, day)
    if not rows:
        return {"day": day, "items": [], "note": f"Nothing was fetched for {day}."}
    words = _words(query)
    scored = [(_match(r, words), r) for r in rows] if words else []
    ranked = sorted(scored, key=lambda x: (-x[0], -float(_get(x[1], "best_score") or 0.0)))
    hits = [r for s, r in ranked if s > 0][:LIMIT]
    out: dict[str, Any] = {"day": day, "searched": len(rows)}
    if hits:
        out["items"] = [_hit(r) for r in hits]
    else:
        best = sorted(rows, key=lambda r: -(float(_get(r, "best_score") or 0.0)))[:LIMIT]
        out["items"] = [_hit(r) for r in best]
        out["note"] = "No direct matches; these are the day's highest-rated items."
    return out
