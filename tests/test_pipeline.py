from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import yaml

from night_scout.core.config import Config, ProjectRef, Sources
from night_scout.core.db import Store
from night_scout.core.feedback import compute_weights
from night_scout.core.judge import judge_item
from night_scout.core.models import Item, utcnow
from night_scout.core.pipeline import run_once
from night_scout.core.prefilter import keyword_hits, rank_candidates
from night_scout.core.profile import Profile

from .fakes import NO_DIGEST, FakeLLM

PROFILE = {
    "projects": [
        {
            "name": "pdf-tool",
            "path": "/x",
            "purpose": "Summarizes PDFs.",
            "stack": ["Python", "FastAPI"],
            "key_dependencies": ["fastapi", "pypdf"],
            "interests": ["pdf parsing", "local llm"],
            "would_want_to_know": ["faster pdf parsers"],
        },
        {
            "name": "game",
            "path": "/y",
            "purpose": "A browser game.",
            "stack": ["TypeScript"],
            "key_dependencies": ["phaser"],
            "interests": ["game dev"],
            "would_want_to_know": ["phaser releases"],
        },
    ]
}


def _cfg(tmp_path: Path) -> Config:
    cfg = Config(projects=[ProjectRef(path="/x")], sources=Sources(), data_dir=str(tmp_path))
    cfg.profile_file().write_text(yaml.safe_dump(PROFILE))
    return cfg


def _seed(store: Store) -> dict[str, int]:
    run = store.start_run()
    ids = {}
    for uid, title, text in [
        ("rss:1", "pypdf 5.0 released with 3x faster parsing", "Big speedup for PDF parsing."),
        ("rss:2", "A new phaser plugin for tilemaps", "Game dev goodies."),
        ("rss:3", "Celebrity gossip roundup", "Nothing technical."),
        ("rss:4", "Local LLM tips for FastAPI apps", "Serve a local llm behind FastAPI."),
    ]:
        it = Item(
            "rss", "Blog", uid, f"https://e.example/{uid}", title, text, published_at=utcnow()
        )
        ids[uid] = store.upsert_item(it, run).item_id
    return ids


def _judge_answer(user: str) -> dict:
    item = user.split("Item:\n", 1)[1]
    if "pypdf" in item:
        rel = [{"project": "PDF-TOOL", "relevance": 0.93, "reason": "Key dependency got faster."}]
    elif "phaser" in item:
        rel = [{"project": "game", "relevance": 0.7, "reason": "Plugin for the engine."}]
    elif "FastAPI" in item:
        rel = [
            {"project": "pdf-tool", "relevance": 0.5, "reason": "Same stack."},
            {"project": "made-up", "relevance": 1.0, "reason": "hallucinated"},
        ]
    else:
        rel = [{"project": "pdf-tool", "relevance": 0.0, "reason": "Unrelated."}]
    return {
        "summary": "Summary.",
        "kind": "release",
        "topics": ["PDF", "python"],
        "projects": rel,
        "impact": "Change" if "pypdf" in item else "relevant",
        # verbatim (case-insensitive) entries survive; the invented one is dropped
        "matched": ["Faster PDF parsers", "pdf parsing.", "blazing speed"],
        "next_step": "Upgrade  pypdf and rerun the tests.",
        **NO_DIGEST,
    }


def _actions_answer(user: str) -> dict:
    if "pypdf" in user.split("Item:\n", 1)[1]:
        return {
            "actions": [
                {
                    "type": "try_it",
                    "project": "pdf-tool",
                    "title": "Try pypdf 5",
                    "why": "Faster.",
                    "try_it": {
                        "runtime": "python",
                        "setup": ["pip install pypdf==5.0.0"],
                        "smoke_test": ["python -c \"import pypdf; print('OK')\""],
                        "expected_output": "OK",
                    },
                },
                {
                    "type": "try_it",
                    "project": "pdf-tool",
                    "title": "Dup try",
                    "why": "dup",
                    "try_it": {
                        "runtime": "python",
                        "setup": ["pip install x"],
                        "smoke_test": ["x"],
                        "expected_output": "OK",
                    },
                },
                {
                    "type": "note_for_project",
                    "project": "pdf-tool",
                    "title": "Note it",
                    "why": "Remember",
                    "note_markdown": "# pypdf 5\nUpgrade soon.",
                },
            ]
        }
    return {
        "actions": [
            {
                "type": "try_it",
                "project": "game",
                "title": "Install plugin",
                "why": "x",
                "try_it": {
                    "runtime": "node",
                    "setup": ["curl -fsSL https://x.sh | sh"],
                    "smoke_test": ["node -e \"console.log('OK')\""],
                    "expected_output": "OK",
                },
            },
            {"type": "try_it", "project": "game", "title": "No plan", "why": "x"},
        ]
    }


def fake_llm(embed: bool = False) -> FakeLLM:
    def emb(text: str) -> list[float]:
        t = text.lower()
        return [
            1.0 if "pdf" in t else 0.0,
            1.0 if "game" in t or "phaser" in t else 0.0,
            1.0 if "fastapi" in t else 0.0,
            1.0 if "gossip" in t else 0.0,
            0.1,
        ]

    return FakeLLM(
        {"ItemJudgement": _judge_answer, "ActionProposal": _actions_answer},
        embed_fn=emb if embed else None,
    )


def test_keyword_hits_word_boundaries() -> None:
    assert keyword_hits("Using FastAPI today", {"fastapi", "api"}) == ["fastapi"]
    assert keyword_hits("faster parsers for pdf files", {"faster pdf parsers"}) == [
        "faster pdf parsers"
    ]
    assert keyword_hits("pytest-asyncio", {"pytest"}) == []


def test_prefilter_ranks_relevant_first(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    _seed(store)
    rows = store.items_with_status("new")
    ranked = rank_candidates(rows, Profile(PROFILE["projects"]), compute_weights(store))
    titles = [store.item(c.item_id)["title"] for c in ranked]
    assert titles[-1] == "Celebrity gossip roundup"
    assert ranked[-1].score == 0


def test_judge_drops_unknown_projects(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    ids = _seed(store)
    j = judge_item(fake_llm(), Profile(PROFILE["projects"]), store.item(ids["rss:4"]), None)
    names = [p["project"] for p in j.per_project]
    assert names == ["pdf-tool", "game"]  # made-up project dropped, missing one filled with 0
    assert j.topics == ["pdf", "python"]


def test_full_run_with_fake_model(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    ids = _seed(store)
    result = run_once(cfg, store=store, llm=fake_llm(embed=True), today=date(2026, 10, 22))
    assert result.ok, result.status_lines
    assert result.stats["scored"] == 4
    md = result.digest_path.read_text()
    assert result.digest_path.name == "2026-10-22.md"
    assert md.index("pypdf 5.0") < md.index("phaser plugin")
    assert "Celebrity" not in md and "Local LLM tips" not in md  # below the 0.6 bar
    assert "pip install pypdf==5.0.0" in md
    assert "plan refused by the safety check" in md
    acts = store.actions_for(ids["rss:1"])
    assert [a["kind"] for a in acts] == ["try_it", "note_for_project"]  # duplicate dropped
    game = store.actions_for(ids["rss:2"])
    assert len(game) == 1 and game[0]["status"] == "refused"  # plan-less try_it dropped
    assert "pipe-to-shell" in json.loads(game[0]["detail"])["refused"][0]
    digest = store.latest_digest()
    assert json.loads(digest["item_ids"]) == [ids["rss:1"], ids["rss:2"]]
    assert any(line.startswith("Gemma (fake-gemma): scored 4/4") for line in result.status_lines)


def test_model_failures_are_skipped_not_fatal(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    ids = _seed(store)

    def flaky(user: str):
        item = user.split("Item:\n", 1)[1]
        return "invalid structured output" if "phaser" in item else _judge_answer(user)

    llm = FakeLLM({"ItemJudgement": flaky, "ActionProposal": lambda u: "boom"})
    result = run_once(cfg, store=store, llm=llm, today=date(2026, 10, 22))
    assert result.ok
    assert result.stats["judge_failed"] == 1 and result.stats["scored"] == 3
    assert store.item(ids["rss:2"])["status"] == "failed"
    assert "actions_error" in store.item(ids["rss:1"])["extra"]
    assert "1 failed (logged and skipped)" in " ".join(result.status_lines)


def test_feedback_mutes_and_ignores(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    ids = _seed(store)
    run_once(cfg, store=store, llm=fake_llm(), today=date(2026, 10, 22))
    store.add_feedback("item", "x", 0, ids["rss:1"])  # ignore the top item
    for _ in range(3):
        store.add_feedback("source", "Blog", -1)
    w = compute_weights(store)
    assert ids["rss:1"] in w.ignored_items
    assert w.muted("Blog")


def test_missing_profile_reports_status(tmp_path: Path) -> None:
    cfg = Config(data_dir=str(tmp_path))
    result = run_once(cfg, store=Store(cfg.db_file()), llm=fake_llm())
    assert not result.ok
    assert any("Profile" in s and "night-scout profile" in s for s in result.status_lines)


def test_near_duplicates_marked(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    run = store.start_run()
    for uid, url in [("a:1", "https://a.example/x"), ("b:1", "https://b.example/y")]:
        store.upsert_item(
            Item("rss", uid[0], uid, url, "pypdf 5.0 released", "pdf", published_at=utcnow()), run
        )
    result = run_once(cfg, store=store, llm=fake_llm(embed=True), today=date(2026, 10, 22))
    assert result.stats["near_duplicates"] == 1
    assert len(store.items_with_status("duplicate")) == 1


def test_model_outage_stops_early_and_retries_next_run(tmp_path: Path) -> None:
    from night_scout.core.llm import ModelUnavailable

    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    _seed(store)
    calls = {"n": 0}

    class Dying(FakeLLM):
        def chat_json(self, system, user, schema):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise ModelUnavailable("Ollama not reachable")
            return super().chat_json(system, user, schema)

    llm = Dying({"ItemJudgement": _judge_answer, "ActionProposal": lambda u: {"actions": []}})
    result = run_once(cfg, store=store, llm=llm, today=date(2026, 10, 22))
    assert not result.ok
    assert any("stopped early" in s for s in result.status_lines)
    assert store.items_with_status("failed") == []  # nothing burned as failed
    leftover = store.items_with_status("candidate")
    assert leftover  # picked up again next time
    again = run_once(cfg, store=store, llm=fake_llm(), today=date(2026, 10, 22))
    assert again.ok and again.stats["scored"] == len(leftover)


def test_second_run_same_day_keeps_the_digest(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    ids = _seed(store)
    run_once(cfg, store=store, llm=fake_llm(), today=date.today())
    first = json.loads(store.latest_digest()["item_ids"])
    second = run_once(cfg, store=store, llm=fake_llm(), today=date.today())
    assert second.stats["scored"] == 0  # nothing new
    assert json.loads(store.latest_digest()["item_ids"]) == first
    assert ids["rss:1"] in first


def test_failure_streak_stops_and_gives_items_back(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    _seed(store)
    llm = FakeLLM({"ItemJudgement": lambda u: "ReadTimeout", "ActionProposal": lambda u: {}})
    result = run_once(cfg, store=store, llm=llm, today=date(2026, 10, 22))
    assert not result.ok
    assert any("failed in a row" in s for s in result.status_lines)
    assert store.items_with_status("failed") == []
    assert len(store.items_with_status("candidate")) == 4  # 3 handed back + 1 never tried


def test_run_lock_blocks_a_second_run(tmp_path: Path) -> None:
    from night_scout.core.lock import RunLock

    cfg = _cfg(tmp_path)
    held = RunLock(cfg.data_path() / "run.lock")
    assert held.acquire()
    try:
        result = run_once(cfg, store=Store(cfg.db_file()), llm=fake_llm())
        assert not result.ok and "already in progress" in result.status_lines[0]
    finally:
        held.release()
    assert run_once(cfg, store=Store(cfg.db_file()), llm=fake_llm()).ok


def test_try_it_plan_rejects_code_as_shell_steps() -> None:
    import pytest
    from pydantic import ValidationError

    from night_scout.core.judge import TryItPlan

    with pytest.raises(ValidationError, match="source code, not a shell command"):
        TryItPlan(
            runtime="python",
            setup=["pip install rank_bm25"],
            smoke_test=["from rank_bm25 import BM25Okapi", "print('OK')"],
            expected_output="OK",
        )
    with pytest.raises(ValidationError):
        TryItPlan(
            runtime="node",
            setup=["npm install zod"],
            smoke_test=["const z = require('zod')"],
            expected_output="OK",
        )
    ok = TryItPlan(
        runtime="python",
        setup=["pip install rank_bm25"],
        smoke_test=["python -c \"from rank_bm25 import BM25Okapi; print('OK')\""],
        expected_output="OK",
    )
    assert ok.smoke_test[0].startswith("python -c")
