"""Tools for the general chat ("Night Scout" context): the Guide, the reader's own setup and runs,
every night's items, and proposals to add a source or a topic.

The propose_* tools change nothing. They return a proposal the drawer shows as a card; only the
reader's click (POST /api/chat/{thread}/proposal/{message}) adds anything.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..http import PoliteClient, SourceError
from ..profile import MAX_TOPIC_LEN, Profile, clean_topics, load_profile
from .base import ToolContext, clip, fail
from .tonight import score_columns

_PKG = Path(__file__).resolve().parents[2]
GUIDE_FILES = (
    ("Guide", _PKG / "web" / "static" / "guide.md"),
    ("README", _PKG.parent / "README.md"),
)
SECTION_CHARS = 2200
_WORD = re.compile(r"[a-z0-9][a-z0-9.+#-]*")


def _profile(ctx: ToolContext) -> Profile:
    try:
        return load_profile(ctx.cfg.profile_file())
    except (FileNotFoundError, OSError, ValueError):
        return Profile(projects=[])


# -- night_scout_help ------------------------------------------------------------------------
def _sections() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for name, path in GUIDE_FILES:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue  # README isn't shipped in a wheel; the Guide always is
        for part in re.split(r"(?m)^#{1,3} ", text):
            head, _, body = part.partition("\n")
            if head.strip() and body.strip():
                out.append((f"{name} · {head.strip()}", body.strip()))
    return out


def night_scout_help(ctx: ToolContext, query: str) -> dict[str, Any]:
    words = {w for w in _WORD.findall(query.lower()) if len(w) > 2}
    if not words:
        return fail("Say what to look up, e.g. 'add a YouTube channel'.")
    sections = _sections()
    if not sections:
        return fail("The Guide isn't available in this install.")

    def score(s: tuple[str, str]) -> int:
        head, body = s[0].lower(), s[1].lower()
        return sum(3 * head.count(w) + body.count(w) for w in words)

    ranked = sorted(sections, key=score, reverse=True)
    return {"sections": [{"section": h, "text": clip(b, SECTION_CHARS)[0]} for h, b in ranked[:3]]}


# -- my_setup --------------------------------------------------------------------------------
def _local(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).astimezone().strftime("%a %b %d, %H:%M %Z")
    except ValueError:
        return ts


def _runs(ctx: ToolContext) -> list[dict[str, Any]]:
    assert ctx.store is not None
    con = ctx.store.conn
    has_rel = "digest_relevance" in score_columns(ctx.store)
    bar = ctx.cfg.thresholds.digest_bar
    out = []
    rows = con.execute(
        "SELECT id, started_at, finished_at, status, stats FROM runs ORDER BY id DESC LIMIT 5"
    ).fetchall()
    for r in rows:
        try:
            st = json.loads(r["stats"] or "{}")
        except ValueError:
            st = {}
        st.pop("progress", None)
        run: dict[str, Any] = {
            "id": r["id"],
            "started": _local(r["started_at"]),
            "finished": _local(r["finished_at"]),
            "status": r["status"],
            **{k: v for k, v in st.items() if isinstance(v, (int, float, str))},
        }
        if has_rel:
            rel = [
                float(x[0])
                for x in con.execute(
                    "SELECT s.digest_relevance FROM scores s JOIN items i ON i.id = s.item_id "
                    "WHERE i.run_id = ? AND s.digest_relevance IS NOT NULL",
                    (r["id"],),
                )
            ]
            if rel:
                run["digest_score_spread"] = {
                    f"{round(bar * 100)}_and_up (made the Digest)": sum(v >= bar for v in rel),
                    "70_to_79 (just missed)": sum(0.7 <= v < bar for v in rel),
                    "under_70": sum(v < 0.7 for v in rel),
                    "judged_by_gemma": len(rel),
                }
        new, scored = st.get("new"), st.get("scored")
        if isinstance(new, int) and isinstance(scored, int):
            run["fetched_but_not_judged"] = max(0, new - scored - int(st.get("judge_failed") or 0))
        out.append(run)
    return out


def _sources(ctx: ToolContext) -> list[dict[str, Any]]:
    assert ctx.store is not None
    row = ctx.store.conn.execute("SELECT MAX(run_id) AS r FROM source_status").fetchone()
    if row is None or row["r"] is None:
        return []
    return [
        {
            "source": s["label"],
            "ok": bool(s["ok"]),
            "fetched": s["fetched"],
            "new": s["new"],
            **({"problem": s["message"]} if s["message"] else {}),
        }
        for s in ctx.store.conn.execute(
            "SELECT label, ok, fetched, new, message FROM source_status WHERE run_id = ? "
            "ORDER BY label",
            (row["r"],),
        )
    ]


PARTS = ("all", "sources", "runs", "topics", "projects", "models")


def my_setup(ctx: ToolContext, part: str = "all") -> dict[str, Any]:
    part = (part or "all").strip().lower()
    if part not in PARTS:
        return fail(f"part must be one of: {', '.join(PARTS)}.")
    if ctx.store is None:
        return fail("Night Scout's database isn't available.")
    cfg, prof = ctx.cfg, _profile(ctx)
    t = cfg.thresholds
    rules = (
        f"A story makes the Digest only if Gemma scores it {round(t.digest_bar * 100)} % or more "
        f"for one of the topics; at most {t.digest_max} stories. Gemma reads only the "
        f"{t.max_scored} most promising items a night; the rest are listed under Everything "
        "else, ranked by similarity."
    )
    out: dict[str, Any] = {}
    if part in ("all", "topics"):
        out["topics"] = prof.topics
        out["digest_rules"] = rules
    if part in ("all", "projects"):
        out["projects"] = [
            {"name": p.get("name"), "purpose": clip(str(p.get("purpose") or ""), 200)[0]}
            for p in prof.projects
        ]
    if part in ("all", "models"):
        out["models"] = {
            "judge": cfg.model.name,
            "fallback": cfg.model.fallback,
            "embeddings": cfg.model.embed,
        }
    if part in ("all", "sources"):
        out["sources_last_run"] = _sources(ctx)
        out["switched_off"] = list(cfg.sources.disabled)
    if part in ("all", "runs"):
        out["recent_runs"] = _runs(ctx)
        out["digest_rules"] = rules
        out["schedule"] = f"every night at {cfg.schedule.time}, if the schedule is installed"
    return out


# -- search_all_nights -----------------------------------------------------------------------
def search_all_nights(ctx: ToolContext, query: str) -> dict[str, Any]:
    if ctx.store is None:
        return fail("Night Scout's database isn't available.")
    words = [w for w in _WORD.findall(query.lower()) if len(w) > 2][:6]
    if not words:
        return fail("Give a few words to look for.")
    cols = score_columns(ctx.store)
    topic = "s.digest_topic" if "digest_topic" in cols else "NULL"
    rel = "s.digest_relevance" if "digest_relevance" in cols else "NULL"
    hay = "lower(i.title || ' ' || coalesce(s.summary, '') || ' ' || coalesce(i.text, ''))"
    where = " OR ".join([f"instr({hay}, ?) > 0"] * len(words))
    rows = ctx.store.conn.execute(
        f"SELECT i.id, i.title, i.url, i.source_name, substr(i.first_seen, 1, 10) AS day, "
        f"s.summary, {topic} AS topic, {rel} AS relevance "
        f"FROM items i LEFT JOIN scores s ON s.item_id = i.id WHERE {where} "
        "ORDER BY i.first_seen DESC LIMIT 10",
        words,
    ).fetchall()
    return {
        "matches": [
            {
                "id": r["id"],
                "title": clip(r["title"] or "", 200)[0],
                "url": r["url"],
                "source": r["source_name"],
                "day": r["day"],
                "topic": r["topic"],
                "score": None if r["relevance"] is None else round(float(r["relevance"]) * 100),
                "summary": clip(r["summary"] or "", 300)[0],
            }
            for r in rows
        ]
    }


# -- proposals -------------------------------------------------------------------------------
def propose_source(ctx: ToolContext, type: str, value: str, why: str = "") -> dict[str, Any]:
    from ..discover import SOURCE_TYPES, add_source

    kind = (type or "").strip().lower()
    val = (value or "").strip()
    if kind == "x":
        return fail("X (Twitter) can't be followed: it has no free public API. Suggest Bluesky.")
    if kind not in SOURCE_TYPES:
        return fail(f"Unknown type {kind!r}. Use one of: {', '.join(SOURCE_TYPES)}.")
    if not val:
        return fail("Give what to follow: a URL, @handle, subreddit, keyword or owner/repo.")
    # Dry run on a copy of the config: checks the source resolves and isn't already followed,
    # and gives its real name. Nothing is saved here.
    try:
        with PoliteClient(ctx.cfg.contact, transport=ctx.transport) as client:
            label = add_source(copy.deepcopy(ctx.cfg), kind, val, client)
    except (ValueError, SourceError) as e:
        return fail(f"That can't be added as a {kind} source: {e}")
    return {
        "proposal": {
            "kind": "source",
            "type": kind,
            "value": val,
            "label": label,
            "why": clip(" ".join((why or "").split()), 200)[0],
        },
        "note": "The reader now sees an 'Add source' button. Nothing is added until they click it.",
    }


def propose_topic(ctx: ToolContext, topic: str) -> dict[str, Any]:
    t = " ".join((topic or "").split())[:MAX_TOPIC_LEN]
    if not clean_topics([t]):
        return fail("Give the topic in a few words.")
    have = {x.lower() for x in _profile(ctx).topics}
    if t.lower() in have:
        return fail(f"{t!r} is already one of their topics.")
    return {
        "proposal": {"kind": "topic", "topic": t},
        "note": "The reader now sees an 'Add topic' button. Nothing changes until they click it.",
    }
