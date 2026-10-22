"""The morning digest: a short ranked list of things worth doing, as markdown."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

from .db import Store
from .feedback import Weights

ACTION_LABEL = {
    "try_it": "Try it",
    "research_more": "Research more",
    "note_for_project": "Note for project",
    "watch_later": "Watch later",
    "ignore": "Ignore",
}


def rank_for_digest(
    rows: list[sqlite3.Row], weights: Weights, threshold: float, size: int
) -> list[sqlite3.Row]:
    """Items at or above the relevance bar, nudged by feedback, best first."""
    scored: list[tuple[float, sqlite3.Row]] = []
    for r in rows:
        if r["id"] in weights.ignored_items or weights.muted(r["source_name"]):
            continue
        if r["best_score"] < threshold:
            continue
        adj = weights.source_weight(r["source_name"]) + weights.topic_weight(
            json.loads(r["topics"] or "[]")
        )
        scored.append((r["best_score"] + adj, r))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [r for _, r in scored[:size]]


def _reason_for(row: sqlite3.Row) -> str:
    for p in json.loads(row["per_project"]):
        if p["project"] == row["best_project"]:
            return str(p.get("reason") or "")
    return ""


def render_markdown(
    store: Store,
    day: date,
    picks: list[sqlite3.Row],
    total_new: int,
    status_lines: list[str],
    model: str,
) -> str:
    out = [f"# Night Scout · {day:%A, %b} {day.day}, {day.year}", ""]
    if picks:
        out.append(
            f"{len(picks)} thing{'s' if len(picks) != 1 else ''} worth doing, picked from "
            f"{total_new} new items. Judged locally by `{model}`."
        )
    else:
        out.append(
            f"Nothing cleared the bar today ({total_new} new items checked). "
            "A quiet morning is a good morning."
        )
    out.append("")
    for n, row in enumerate(picks, 1):
        link = row["url"]
        extra = json.loads(row["extra"] or "{}")
        discussion = extra.get("discussion")
        out.append(f"## {n}. {row['title']}")
        meta = f"**{row['source_name']}** · [open]({link})"
        if discussion and discussion != link:
            meta += f" · [discussion]({discussion})"
        meta += f" · for **{row['best_project']}** ({row['best_score']:.2f})"
        out.append(meta)
        out.append("")
        reason = _reason_for(row)
        if reason:
            out.append(f"> **Why it matters:** {reason}")
            out.append("")
        out.append(row["summary"])
        out.append("")
        actions = store.actions_for(row["id"])
        if actions:
            out.append("**Next steps**")
            for a in actions:
                label = ACTION_LABEL.get(a["kind"], a["kind"])
                line = f"- [ ] {label}: {a['title']}"
                if a["status"] == "refused":
                    line += " *(plan refused by the safety check)*"
                out.append(line)
                if a["kind"] == "try_it" and a["status"] != "refused":
                    plan = json.loads(a["detail"]).get("plan") or {}
                    cmds = [*plan.get("setup", []), *plan.get("smoke_test", [])]
                    if cmds:
                        out.append("  ```sh")
                        out.extend(f"  {c}" for c in cmds)
                        out.append("  ```")
            out.append("")
    out.append("---")
    out.append("")
    out.append("<details><summary>Run status</summary>")
    out.append("")
    out.extend(f"- {s}" for s in status_lines)
    out.append("")
    out.append("</details>")
    out.append("")
    return "\n".join(out)


def write_digest(
    store: Store,
    digests_dir: Path,
    run_id: int,
    day: date,
    picks: list[sqlite3.Row],
    total_new: int,
    status_lines: list[str],
    model: str,
) -> Path:
    digests_dir.mkdir(parents=True, exist_ok=True)
    path = digests_dir / f"{day.isoformat()}.md"
    path.write_text(
        render_markdown(store, day, picks, total_new, status_lines, model), encoding="utf-8"
    )
    store.save_digest(day.isoformat(), run_id, str(path), [r["id"] for r in picks])
    return path


# -- the Digest (by topic) -------------------------------------------------------------------
def day_window(day: date) -> tuple[str, str]:
    """[local midnight, next local midnight) of `day`, as UTC ISO strings like the DB stores."""
    start = datetime.combine(day, dtime.min).astimezone()
    return start.astimezone(UTC).isoformat(), (start + timedelta(days=1)).astimezone(
        UTC
    ).isoformat()


def split_digest(
    rows: list[sqlite3.Row],
    topics: list[str],
    weights: Weights,
    bar: float,
    size: int,
    hidden: set[int] | None = None,
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """(stories, everything else). Stories: Gemma filed them under one of your topics at or above
    the bar, best first, at most `size`. The rest: every other item, most similar first. Items you
    hid ("Not for me" or ✕) and muted sources are left out of both."""
    want = {t.casefold() for t in topics}
    gone = weights.ignored_items | (hidden or set())
    keep = [r for r in rows if r["id"] not in gone and not weights.muted(r["source_name"])]
    stories = [
        r
        for r in keep
        if r["digest_topic"]
        and r["digest_topic"].casefold() in want
        and (r["digest_relevance"] or 0.0) >= bar
    ]
    stories.sort(key=lambda r: (-(r["digest_relevance"] or 0.0), -(r["prefilter"] or 0.0), r["id"]))
    stories = stories[:size]
    chosen = {r["id"] for r in stories}
    rest = [r for r in keep if r["id"] not in chosen]
    rest.sort(key=lambda r: (-(r["prefilter"] or 0.0), r["id"]))
    return stories, rest
