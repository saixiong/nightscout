"""Morning labels (impact / matched / next_step), the scores migration, and live progress."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from night_scout.core.db import Store, migrate
from night_scout.core.judge import JUDGE_SYSTEM, ItemJudgement, judge_item, validate_matched
from night_scout.core.pipeline import run_once
from night_scout.core.profile import Profile

from .fakes import NO_DIGEST, FakeLLM
from .test_pipeline import PROFILE, _cfg, _judge_answer, _seed, fake_llm

PDF = PROFILE["projects"][0]


def test_judge_returns_labels_for_best_project(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    ids = _seed(store)
    j = judge_item(fake_llm(), Profile(PROFILE["projects"]), store.item(ids["rss:1"]), None)
    assert j.best and j.best["project"] == "pdf-tool"
    assert j.impact == "change"  # "Change" normalised
    # profile spelling kept, trailing punctuation tolerated, invented entry dropped
    assert j.matched == ["faster pdf parsers", "pdf parsing"]
    assert j.next_step == "Upgrade pypdf and rerun the tests."
    assert j.kind == "release"  # the item-type field is untouched


def test_matched_validated_against_the_best_project_only(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    ids = _seed(store)

    def answer(user: str) -> dict:
        out = _judge_answer(user)
        # "game dev" belongs to the other project; pdf-tool is best for this item
        out["matched"] = [
            "game dev",
            "PDF PARSING",
            "pdf parsing",
            "local llm",
            "faster pdf parsers",
        ]
        return out

    j = judge_item(
        FakeLLM({"ItemJudgement": answer}),
        Profile(PROFILE["projects"]),
        store.item(ids["rss:1"]),
        None,
    )
    assert j.matched == ["pdf parsing", "local llm", "faster pdf parsers"]  # ≤3, deduped


def test_validate_matched_edge_cases() -> None:
    assert validate_matched(["pdf parsing"], None) == []
    assert validate_matched([], PDF) == []
    assert validate_matched(["  Faster   PDF parsers "], PDF) == ["faster pdf parsers"]
    assert validate_matched(["pdf"], PDF) == []  # a substring is not an entry
    assert validate_matched(["fastapi"], PDF) == []  # stack isn't interests/would_want_to_know


def test_impact_must_be_one_of_four() -> None:
    import pytest
    from pydantic import ValidationError

    base = {
        "summary": "s",
        "kind": "news",
        "topics": [],
        "projects": [],
        "matched": [],
        "next_step": "x",
        **NO_DIGEST,
    }
    assert ItemJudgement.model_validate({**base, "impact": " Issue "}).impact == "issue"
    with pytest.raises(ValidationError):
        ItemJudgement.model_validate({**base, "impact": "catastrophe"})
    schema = ItemJudgement.model_json_schema()
    assert {"impact", "matched", "next_step"} <= set(schema["required"])


def test_judge_prompt_stays_small() -> None:
    # gemma4:12b runs with num_ctx 8192; system + schema must leave room for the item.
    schema = json.dumps(ItemJudgement.model_json_schema())
    assert len(JUDGE_SYSTEM) + len(schema) < 4500  # ~1.2k tokens


OLD_SCHEMA = """
CREATE TABLE items (id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE, url TEXT NOT NULL,
    url_key TEXT NOT NULL, source TEXT NOT NULL, source_name TEXT NOT NULL, title TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '', author TEXT, published_at TEXT, first_seen TEXT NOT NULL,
    run_id INTEGER, extra TEXT NOT NULL DEFAULT '{}', also_seen TEXT NOT NULL DEFAULT '[]',
    dup_of INTEGER, embedding TEXT, prefilter REAL, status TEXT NOT NULL DEFAULT 'new');
CREATE TABLE scores (item_id INTEGER PRIMARY KEY REFERENCES items(id), model TEXT NOT NULL,
    summary TEXT NOT NULL, best_project TEXT, best_score REAL NOT NULL, per_project TEXT NOT NULL,
    topics TEXT NOT NULL DEFAULT '[]', scored_at TEXT NOT NULL);
INSERT INTO items(uid,url,url_key,source,source_name,title,first_seen,status)
    VALUES ('rss:old','https://o.example/1','o.example/1','rss','Blog','Old item',
            '2026-10-01','scored');
INSERT INTO scores VALUES (1,'gemma4:12b','Old summary.','pdf-tool',0.8,'[]','[]','2026-10-01');
"""


def test_migration_on_an_old_database(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.commit()
    conn.close()

    store = Store(path)  # migrates on open
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(scores)")}
    assert {"impact", "matched", "next_step"} <= cols
    old = store.scored_since("2000-01-01")[0]
    assert old["summary"] == "Old summary."
    assert old["impact"] is None and old["matched"] is None and old["next_step"] is None
    assert store.ideas_for_day("2026-10-01") == []  # new table exists too
    assert migrate(store.conn) == []  # idempotent
    store.close()
    Store(path).close()  # and opening again is fine

    store = Store(path)
    run = store.start_run()
    from night_scout.core.models import Item

    new_id = store.upsert_item(
        Item("rss", "Blog", "rss:new", "https://o.example/2", "New"), run
    ).item_id
    store.save_score(
        new_id,
        "m",
        "New summary.",
        [{"project": "pdf-tool", "relevance": 0.9, "reason": "r"}],
        ["pdf"],
        impact="issue",
        matched=["pdf parsing"],
        next_step="Check it.",
    )
    row = store.scored_for_run(run)[0]
    assert row["impact"] == "issue" and json.loads(row["matched"]) == ["pdf parsing"]
    assert row["next_step"] == "Check it." and row["score_model"] == "m"


def test_fresh_db_gets_columns_too(tmp_path: Path) -> None:
    store = Store(tmp_path / "fresh.db")
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(scores)")}
    assert {"impact", "matched", "next_step"} <= cols


def test_full_run_stores_labels(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    ids = _seed(store)
    run_once(cfg, store=store, llm=fake_llm(), today=date(2026, 10, 22))
    s = store.score(ids["rss:1"])
    assert s["impact"] == "change"
    assert json.loads(s["matched"]) == ["faster pdf parsers", "pdf parsing"]
    assert s["next_step"] == "Upgrade pypdf and rerun the tests."
    other = store.score(ids["rss:2"])  # best project is "game": pdf entries don't count there
    assert other["impact"] == "relevant" and json.loads(other["matched"]) == []


# -- live progress ---------------------------------------------------------------------------
def test_progress_updates_every_stage(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    _seed(store)
    calls: list[tuple[str, int, int]] = []
    snapshots: list[dict] = []
    original = store.set_run_progress

    def spy(run_id: int, stage: str, done: int, total: int) -> None:
        original(run_id, stage, done, total)
        calls.append((stage, done, total))
        snapshots.append(store.run_progress(run_id) or {})

    store.set_run_progress = spy  # type: ignore[method-assign]
    llm = fake_llm(embed=True)
    llm.answers["IdeaProposal"] = lambda u: {"ideas": []}
    result = run_once(cfg, store=store, llm=llm, today=date(2026, 10, 22))
    assert result.ok

    stages = list(dict.fromkeys(c[0] for c in calls))
    assert stages == ["fetch", "dedupe", "prefilter", "judge", "actions", "digest", "ideas"]
    judge = [c for c in calls if c[0] == "judge"]
    assert [c[1] for c in judge] == [0, 1, 2, 3, 4] and {c[2] for c in judge} == {4}
    actions = [c for c in calls if c[0] == "actions"]
    assert actions[0] == ("actions", 0, 2) and actions[-1] == ("actions", 2, 2)

    mid = snapshots[calls.index(("judge", 2, 4))]
    assert mid["stage"] == "judge" and mid["done"] == 2 and mid["total"] == 4
    assert mid["stages_done"] == ["fetch", "dedupe", "prefilter"]
    assert mid["updated_at"]

    final = store.run_progress(result.run_id)
    assert final is not None and final["stage"] == "done"
    assert final["stages_done"] == stages
    stats = json.loads(store.last_run()["stats"])
    assert stats["scored"] == 4 and stats["progress"]["stage"] == "done"


def test_progress_for_fetch_only_and_unknown_run(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    result = run_once(cfg, fetch_only=True, store=store)
    prog = store.run_progress(result.run_id)
    assert prog is not None and prog["stage"] == "done" and prog["stages_done"] == ["fetch"]
    assert store.run_progress(9999) is None
    store.set_run_progress(9999, "fetch", 0, 1)  # no such run: ignored
