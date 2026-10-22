"""The Digest (Oct 22): topics without projects, topic filing in the judge, /api/digest."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from night_scout.core.config import Config, ProjectRef, Sources, save_config
from night_scout.core.db import Store
from night_scout.core.digest import split_digest
from night_scout.core.feedback import Weights, compute_weights
from night_scout.core.judge import JUDGE_SYSTEM, TOPIC_SYSTEM, digest_rules, judge_item, weak_title
from night_scout.core.models import Item, utcnow
from night_scout.core.pipeline import run_once
from night_scout.core.profile import Profile, build_profile_file, load_profile, save_topics
from night_scout.web.app import create_app

from .fakes import FakeLLM
from .test_pipeline import PROFILE, _actions_answer, _judge_answer, _seed

TOPICS = ["PDF tools", "Game dev"]
BASE = "http://127.0.0.1:8765"


def _digest_answer(user: str) -> dict:
    """Digest fields a model might send, including things the code must not trust."""
    item = user.split("Item:\n", 1)[1]
    if "pypdf" in item:
        return {
            "topic": "pdf TOOLS",
            "topic_relevance": 1.7,
            "outline": ["Faster parsing.", " ", "3x"],
            "headline": "Ignored: title is fine",
        }
    if "phaser" in item:
        return {
            "topic": "Game dev",
            "topic_relevance": 0.85,
            "outline": ["A tilemap plugin."],
            "headline": None,
        }
    if "FastAPI" in item:
        return {
            "topic": "Not a topic of yours",
            "topic_relevance": 0.99,
            "outline": [],
            "headline": None,
        }
    return {"topic": None, "topic_relevance": 0.0, "outline": [], "headline": None}


def _judge_with_digest(user: str) -> dict:
    return {**_judge_answer(user), **_digest_answer(user)}


def _topic_answer(user: str) -> dict:
    return {"summary": "What happened.", "kind": "news", "topics": ["x"], **_digest_answer(user)}


def fake(topics_only: bool = False) -> FakeLLM:
    answers = (
        {"TopicJudgement": _topic_answer}
        if topics_only
        else {"ItemJudgement": _judge_with_digest, "ActionProposal": _actions_answer}
    )
    return FakeLLM(answers)


def _row(store: Store, title: str = "ok title") -> object:
    run = store.start_run()
    it = Item(
        "rss", "Blog", f"rss:{title}", "https://e.example/x", title, "body", published_at=utcnow()
    )
    return store.item(store.upsert_item(it, run).item_id)


# -- weak titles ------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("title", "why"),
    [
        ("Anmol Pandey, Aditya Jain, Liang Chen, Carsten Maple", "only names"),
        ("Fine-tuning a local # LLM on # OBS submission data", "mostly hashtags"),
        ("Сдаст ли LLM‑агент компанию властям: повторили SnitchBench", "not in English"),
        ("x" * 101, "too long"),
        ("OpenAI CLI", None),
        ("ollama/ollama v0.40.9", None),
        ("Show HN: Aw-1 – Deterministic AST circuit breaker", None),
        ("Python, Rust", "only names"),
    ],
)
def test_weak_title(title: str, why: str | None) -> None:
    assert weak_title(title) == why


def test_prompts_ask_for_a_headline_only_when_the_title_is_weak() -> None:
    assert "null (the original title is fine)" in digest_rules("OpenAI CLI")
    assert "weak (only names)" in digest_rules("Anmol Pandey, Aditya Jain")
    for system in (JUDGE_SYSTEM, TOPIC_SYSTEM):
        text = system.format(digest=digest_rules("OpenAI CLI"))
        assert "never comment on the text itself" in " ".join(text.split()) and "{" not in text
        assert len(text) < 5200  # with the item text, still well inside the 8k context


# -- the judge's Digest fields -------------------------------------------------------------
def test_digest_fields_are_checked_not_trusted(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    prof = Profile(projects=PROFILE["projects"], topics=TOPICS)
    pdf = store.item(_seed(store)["rss:1"])
    j = judge_item(fake(), prof, pdf, None)
    assert j.digest_topic == "PDF tools"  # case-insensitive match, stored as you wrote it
    assert j.digest_relevance == 1.0  # clamped
    assert j.outline == ["Faster parsing.", "3x"]  # blanks dropped
    assert j.headline is None  # the title is fine, so Gemma's headline is ignored
    assert j.per_project  # projects still judged for Actions


def test_unknown_topic_is_dropped(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    prof = Profile(projects=PROFILE["projects"], topics=TOPICS)
    fastapi = store.item(_seed(store)["rss:4"])
    j = judge_item(fake(), prof, fastapi, None)
    assert j.digest_topic is None and j.digest_relevance == 0.0


def test_headline_kept_for_a_weak_title(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    row = _row(store, "Anmol Pandey, Aditya Jain, Liang Chen")
    llm = FakeLLM(
        {
            "TopicJudgement": lambda u: {
                "summary": "s",
                "kind": "news",
                "topics": [],
                "topic": "PDF tools",
                "topic_relevance": 0.9,
                "outline": ["a"],
                "headline": "  Why per-action auth fails in agents  ",
            }
        }
    )
    j = judge_item(llm, Profile(projects=[], topics=TOPICS), row, None)
    assert j.headline == "Why per-action auth fails in agents"


def test_topics_only_profile_uses_the_topic_prompt(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    llm = fake(topics_only=True)
    j = judge_item(
        llm, Profile(projects=[], topics=TOPICS), store.item(_seed(store)["rss:2"]), None
    )
    assert llm.calls[0][0] == "TopicJudgement"
    assert "PDF tools" in llm.calls[0][1] and "projects" not in llm.calls[0][1].lower()
    assert j.per_project == [] and j.digest_topic == "Game dev" and j.digest_relevance == 0.85


def test_project_names_stand_in_when_you_typed_no_topics(tmp_path: Path) -> None:
    prof = Profile(projects=PROFILE["projects"])
    assert prof.digest_topics == ["pdf-tool", "game"] and not prof.empty
    assert Profile(projects=[]).empty


# -- a run with topics and no projects ------------------------------------------------------
def _topics_cfg(tmp_path: Path) -> Config:
    cfg = Config(projects=[], sources=Sources(), data_dir=str(tmp_path))
    save_topics(cfg.profile_file(), TOPICS)
    return cfg


def test_run_without_projects_fills_the_digest_only(tmp_path: Path) -> None:
    cfg = _topics_cfg(tmp_path)
    store = Store(cfg.db_file())
    ids = _seed(store)
    result = run_once(cfg, store=store, llm=fake(topics_only=True), today=date(2026, 10, 22))
    assert result.ok, result.status_lines
    assert result.stats["scored"] == 4 and result.stats.get("with_actions", 0) == 0
    s = store.score(ids["rss:2"])
    assert s["digest_topic"] == "Game dev" and s["digest_relevance"] == 0.85
    assert json.loads(s["outline"]) == ["A tilemap plugin."]
    assert s["impact"] is None  # no projects, no Action labels
    assert store.actions_for(ids["rss:1"]) == []
    assert result.stats["digest_stories"] == 2  # pypdf (clamped 1.0) and phaser (0.85)


def test_run_with_nothing_to_scout_for_stops_cleanly(tmp_path: Path) -> None:
    cfg = Config(projects=[], sources=Sources(), data_dir=str(tmp_path))
    save_topics(cfg.profile_file(), [])
    store = Store(cfg.db_file())
    result = run_once(cfg, store=store, llm=fake(topics_only=True), today=date(2026, 10, 22))
    assert not result.ok
    assert any("add a topic or a project" in line for line in result.status_lines)


# -- topics in profile.yaml ---------------------------------------------------------------
def test_save_topics_keeps_projects_header_and_dedupes(tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text("# my header\n" + yaml.safe_dump(PROFILE, sort_keys=False))
    out = save_topics(path, ["  Local   LLMs ", "local llms", "", "AI safety", "x" * 80])
    assert out == ["Local LLMs", "AI safety", "x" * 60]
    assert path.read_text().startswith("# my header\ntopics:")
    prof = load_profile(path)
    assert prof.topics == out and [p["name"] for p in prof.projects] == ["pdf-tool", "game"]
    assert "topics" in prof.keywords() and "ai safety" in prof.keywords()["topics"]


def test_save_topics_refuses_a_broken_profile(tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text("projects: nope\n")
    with pytest.raises(ValueError):
        save_topics(path, ["a"])


def test_profile_rebuild_keeps_your_topics(tmp_path: Path, monkeypatch) -> None:
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "README.md").write_text("# App\nA thing.")
    cfg = Config(projects=[ProjectRef(path=str(proj))], data_dir=str(tmp_path / "d"))
    save_topics(cfg.profile_file(), TOPICS)
    build_profile_file(cfg, llm=FakeLLM({}))  # Gemma unavailable → heuristic profile
    prof = load_profile(cfg.profile_file())
    assert prof.topics == TOPICS and prof.names == ["app"]


# -- picking the stories --------------------------------------------------------------------
def test_split_digest_bar_cap_order_and_hiding(tmp_path: Path) -> None:
    cfg = _topics_cfg(tmp_path)
    store = Store(cfg.db_file())
    ids = _seed(store)
    run_once(cfg, store=store, llm=fake(topics_only=True), today=date(2026, 10, 22))
    rows = store.items_between("2000-01-01", "2100-01-01")
    w = compute_weights(store)
    stories, rest = split_digest(rows, TOPICS, w, 0.8, 25)
    assert [r["id"] for r in stories] == [ids["rss:1"], ids["rss:2"]]  # 1.0 then 0.85
    assert {r["id"] for r in rest} == {ids["rss:3"], ids["rss:4"]}
    assert [r["id"] for r in split_digest(rows, TOPICS, w, 0.8, 1)[0]] == [ids["rss:1"]]
    assert split_digest(rows, ["Game dev"], w, 0.8, 25)[0][0]["id"] == ids["rss:2"]
    hidden_s, hidden_r = split_digest(rows, TOPICS, w, 0.8, 25, hidden={ids["rss:1"]})
    assert ids["rss:1"] not in {r["id"] for r in hidden_s + hidden_r}
    muted = Weights(source={"Blog": -0.3})
    assert split_digest(rows, TOPICS, muted, 0.8, 25) == ([], [])


# -- the web API ------------------------------------------------------------------------------
@pytest.fixture
def denv(tmp_path: Path):
    cfg = _topics_cfg(tmp_path / "data")
    config_path = tmp_path / "config.yaml"
    save_config(cfg, config_path)
    store = Store(cfg.db_file())
    ids = _seed(store)
    run_once(cfg, store=store, llm=fake(topics_only=True), today=date(2026, 10, 22))
    store.close()
    client = TestClient(create_app(cfg, config_path, session_key="k"), base_url=BASE)
    page = client.get("/?key=k")
    token = page.text.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
    client.headers["X-CSRF-Token"] = token
    return client, ids, cfg


def test_api_digest_shape(denv) -> None:
    client, ids, _ = denv
    d = client.get("/api/digest").json()
    assert d["day"] and d["day"] in d["days"] and d["topics"] == TOPICS and d["projects"] == []
    assert d["bar"] == 0.8 and d["max"] == 25
    assert [s["id"] for s in d["stories"]] == [ids["rss:1"], ids["rss:2"]]
    s = d["stories"][1]
    assert (
        s["topic"] == "Game dev"
        and s["outline"] == ["A tilemap plugin."]
        and s["summary"] == "What happened."
    )
    assert (
        s["url"].startswith("https://")
        and "text" in s
        and s["saved"] is False
        and "action" not in s
    )
    assert {r["id"] for r in d["rest"]} == {ids["rss:3"], ids["rss:4"]}
    assert d["stats"] == {"new": 4, "judged": 4}
    assert client.get("/api/digest?day=not-a-day").status_code == 400
    assert client.get("/api/digest?day=2026-13-40").status_code == 400
    empty = client.get("/api/digest?day=1999-01-01").json()
    assert empty["stories"] == [] and empty["rest"] == []


def test_api_topics_round_trip_and_csrf(denv) -> None:
    client, _, cfg = denv
    assert client.get("/api/topics").json() == {"topics": TOPICS}
    r = client.put("/api/topics", json={"topics": ["Rust", "rust", " AI policy "]})
    assert r.json() == {"ok": True, "topics": ["Rust", "AI policy"]}
    assert load_profile(cfg.profile_file()).topics == ["Rust", "AI policy"]
    bare = TestClient(client.app, base_url=BASE)
    bare.cookies = client.cookies
    assert bare.put("/api/topics", json={"topics": []}).status_code == 403  # no CSRF token
    assert (
        client.put("/api/topics", json={"topics": ["x"] * 41}).status_code == 400
    )  # app maps validation errors to 400


def test_save_and_saved_list(denv) -> None:
    client, ids, _ = denv
    assert (
        client.post(f"/api/item/{ids['rss:3']}/save", json={"saved": True}).json()["saved"] is True
    )
    d = client.get("/api/digest").json()
    assert [s["id"] for s in d["saved"]] == [ids["rss:3"]]
    assert next(r for r in d["rest"] if r["id"] == ids["rss:3"])["saved"] is True
    client.post(f"/api/item/{ids['rss:3']}/save", json={"saved": False})
    assert client.get("/api/digest").json()["saved"] == []
    assert client.post("/api/item/99999/save", json={"saved": True}).status_code == 404


def test_not_for_me_hides_and_undo_restores(denv) -> None:
    client, ids, _ = denv
    client.post(f"/api/item/{ids['rss:1']}/vote", json={"value": "down"})
    d = client.get("/api/digest").json()
    assert ids["rss:1"] not in {s["id"] for s in d["stories"] + d["rest"]}
    client.post(f"/api/item/{ids['rss:1']}/vote", json={"value": "clear"})
    assert client.get("/api/digest").json()["stories"][0]["id"] == ids["rss:1"]


def test_mute_and_unmute_a_source(denv) -> None:
    client, _, _ = denv
    assert client.post("/api/source/mute", json={"name": "Blog"}).json()["muted"] is True
    d = client.get("/api/digest").json()
    assert d["stories"] == [] and d["rest"] == []
    assert (
        client.post("/api/source/mute", json={"name": "Blog", "muted": False}).json()["muted"]
        is False
    )
    assert len(client.get("/api/digest").json()["stories"]) == 2
    assert client.post("/api/source/mute", json={"name": "Nope"}).status_code == 404


def test_csp_allows_only_the_youtube_no_cookie_player(denv) -> None:
    client, _, _ = denv
    csp = client.get("/").headers["content-security-policy"]
    assert "frame-src https://www.youtube-nocookie.com" in csp
    assert "img-src 'self' data: https://i.ytimg.com" in csp
    assert "script-src 'self'" in csp and "unsafe" not in csp


def test_bootstrap_reports_topics_and_setup_done(denv) -> None:
    client, _, _ = denv
    b = client.get("/api/bootstrap").json()
    assert b["topics"] == TOPICS and b["projects"] == []
    assert b["setup"]["needed"] is False and b["thresholds"]["digest_bar"] == 0.8


def test_digest_fields_are_required_in_both_schemas() -> None:
    """Regression (Oct 22 E2E): with defaults these were optional, so schema-constrained Gemma
    left them out and every item landed in no topic. They must be required, and come last."""
    from night_scout.core.judge import ItemJudgement, TopicJudgement

    for schema in (ItemJudgement, TopicJudgement):
        req = schema.model_json_schema()["required"]
        assert {"topic", "topic_relevance", "outline", "headline"} <= set(req)
        assert req.index("summary") < req.index("topic")
