"""Run now: a manual run from the Digest or Actions page, either Digest and Actions or the Digest
only, and a second run the same day never taking away the morning's picks."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from night_scout import cli
from night_scout.core import setup as setup_mod
from night_scout.core.config import Config, save_config
from night_scout.core.db import Store
from night_scout.core.feedback import compute_weights
from night_scout.core.models import Item, utcnow
from night_scout.core.pipeline import keep_earlier_picks, run_once
from night_scout.core.profile import save_topics
from night_scout.web import app as web
from night_scout.web.app import create_app

from .test_pipeline import _cfg, _seed, fake_llm

BASE = "http://127.0.0.1:8765"


def _client(tmp_path: Path, monkeypatch, calls: list) -> tuple[TestClient, Config]:
    cfg = Config(data_dir=str(tmp_path / "data"))
    cfg.data_path().mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    save_config(cfg, config_path)
    save_topics(cfg.profile_file(), ["Local LLMs"])
    monkeypatch.setattr(
        web.setup_mod, "start_run", lambda _c, _p, digest_only=False: calls.append(digest_only) or 5
    )
    client = TestClient(create_app(cfg, config_path, session_key="k"), base_url=BASE)
    page = client.get("/?key=k")
    client.headers["X-CSRF-Token"] = page.text.split('csrf-token" content="', 1)[1].split('"')[0]
    return client, cfg


def test_digest_only_run_judges_but_leaves_actions_alone(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    ids = _seed(store)
    result = run_once(cfg, store=store, llm=fake_llm(), today=date.today(), digest_only=True)
    assert result.ok, result.status_lines
    assert result.stats["scored"] == 4  # judged, so the Digest has them
    assert store.actions_for(ids["rss:1"]) == []  # no Actions planning
    assert store.latest_digest() is None  # the day's picks weren't written
    assert any("Actions: not updated (a Digest-only run)" in s for s in result.status_lines)
    run = store.last_run()
    assert json.loads(run["stats"])["mode"] == "digest" and run["status"] == "done"


def test_digest_only_after_a_full_run_keeps_todays_actions(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    ids = _seed(store)
    run_once(cfg, store=store, llm=fake_llm(), today=date.today())
    picks = json.loads(store.latest_digest()["item_ids"])
    acts = store.actions_for(ids["rss:1"])
    run = store.start_run()
    new = Item(
        "rss",
        "Blog",
        "rss:9",
        "https://e.example/9",
        "pypdf 6.0 parses even faster",
        "x",
        published_at=utcnow(),
    )
    nid = store.upsert_item(new, run).item_id
    run_once(cfg, store=store, llm=fake_llm(), today=date.today(), digest_only=True)
    assert json.loads(store.latest_digest()["item_ids"]) == picks  # untouched
    assert store.actions_for(ids["rss:1"]) == acts
    assert store.actions_for(nid) == []
    assert json.loads(store.last_run()["stats"])["mode"] == "digest"


def test_full_run_records_its_mode(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    _seed(store)
    result = run_once(cfg, store=store, llm=fake_llm(), today=date.today())
    stats = json.loads(store.last_run()["stats"])
    assert stats["mode"] == "all" and "planning_secs" in stats and result.stats["scored"] == 4


def test_later_run_never_drops_the_mornings_picks(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    ids = _seed(store)
    run_once(cfg, store=store, llm=fake_llm(), today=date.today())
    day = date.today()
    morning = json.loads(store.digest(day.isoformat())["item_ids"])
    assert ids["rss:1"] in morning
    weights = compute_weights(store)
    only_new = store.scored_items([ids["rss:4"]])  # as if the new run ranked something else
    merged = keep_earlier_picks(store, day, only_new, weights)
    got = [r["id"] for r in merged]
    assert set(morning) <= set(got) and ids["rss:4"] in got  # kept + added
    scores = [r["best_score"] for r in merged]
    assert scores == sorted(scores, reverse=True)  # best first
    weights.ignored_items.add(ids["rss:1"])  # hidden with ✕: stays gone
    assert ids["rss:1"] not in [r["id"] for r in keep_earlier_picks(store, day, only_new, weights)]
    assert keep_earlier_picks(store, day + timedelta(days=1), only_new, weights) == only_new


def test_finish_run_keeps_the_mode_from_the_start(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    rid = store.start_run(mode="digest")
    assert json.loads(store.last_run()["stats"]) == {"mode": "digest"}
    store.finish_run(rid, "done", {"scored": 3})
    stats = json.loads(store.last_run()["stats"])
    assert stats["mode"] == "digest" and stats["scored"] == 3
    rid2 = store.start_run()  # older callers: no mode, stats still valid JSON
    store.finish_run(rid2, "done", {})
    assert "mode" not in json.loads(store.last_run()["stats"])


def test_run_route_modes(tmp_path: Path, monkeypatch) -> None:
    calls: list = []
    client, _ = _client(tmp_path, monkeypatch, calls)
    r = client.post("/api/run", json={"mode": "digest"})
    assert r.status_code == 200 and r.json()["mode"] == "digest"
    assert client.post("/api/run", json={}).json()["mode"] == "all"
    assert client.post("/api/run").status_code == 200  # no body at all (Setup's old call)
    assert calls == [True, False, False]
    assert client.post("/api/run", json={"mode": "everything"}).status_code in (400, 422)


def test_run_status_estimates(tmp_path: Path, monkeypatch) -> None:
    client, cfg = _client(tmp_path, monkeypatch, [])
    assert client.get("/api/run").json()["estimate"] == {"all": None, "digest": None}
    store = Store(cfg.db_file())
    t0 = utcnow()

    def add(minutes: float, stats: dict, status: str = "done") -> None:
        with store.tx() as c:
            c.execute(
                "INSERT INTO runs(started_at, finished_at, status, stats) VALUES (?,?,?,?)",
                (
                    t0.isoformat(),
                    (t0 + timedelta(minutes=minutes)).isoformat(),
                    status,
                    json.dumps(stats),
                ),
            )

    add(12, {"mode": "all", "planning_secs": 180})
    r = client.get("/api/run").json()
    assert r["estimate"] == {"all": 720, "digest": 540} and r["running"] is False  # 12 min − 3
    add(7, {"mode": "digest"})
    add(30, {"mode": "all"}, status="failed")  # failed runs don't count
    assert client.get("/api/run").json()["estimate"] == {"all": 720, "digest": 420}
    store.close()


def test_cli_passes_digest_only(monkeypatch, tmp_path: Path) -> None:
    seen: dict = {}

    class R:
        ok, status_lines, digest_path = True, [], None

    def fake_run_once(c, fetch_only=False, digest_only=False):
        seen["digest_only"] = digest_only
        return R()

    monkeypatch.setattr("night_scout.core.pipeline.run_once", fake_run_once)
    monkeypatch.setattr(cli, "ready_by_morning", lambda c: None)
    cfgp = tmp_path / "c.yaml"
    save_config(Config(data_dir=str(tmp_path / "d")), cfgp)
    args = cli.build_parser().parse_args(["--config", str(cfgp), "run", "--digest-only"])
    assert args.func(args) == 0 and seen == {"digest_only": True}


def test_start_run_adds_the_flag(tmp_path: Path, monkeypatch) -> None:
    got: dict = {}

    class P:
        pid = 1

        def wait(self):
            return 0

    def fake_popen(cmd, **kw):
        got["cmd"] = cmd
        return P()

    monkeypatch.setattr(setup_mod.subprocess, "Popen", fake_popen)
    cfg = Config(data_dir=str(tmp_path))
    setup_mod.start_run(cfg, tmp_path / "c.yaml", digest_only=True)
    assert got["cmd"][-2:] == ["run", "--digest-only"]
    setup_mod.start_run(cfg, tmp_path / "c.yaml")
    assert got["cmd"][-1] == "run"


def test_a_better_item_later_doesnt_push_out_the_mornings_pick(tmp_path: Path) -> None:
    """Through the pipeline: the morning picks the only item (phaser, 0.7); a later run finds a
    stronger one (pypdf, 0.93) with room for one pick. Both stay, best first."""
    cfg = _cfg(tmp_path)
    cfg.thresholds.digest_size = 1
    store = Store(cfg.db_file())

    def add(uid: str, title: str) -> int:
        run = store.start_run()
        it = Item("rss", "Blog", uid, f"https://e.example/{uid}", title, "x", published_at=utcnow())
        return store.upsert_item(it, run).item_id

    phaser = add("rss:p", "A new phaser plugin for tilemaps")
    run_once(cfg, store=store, llm=fake_llm(), today=date.today())
    assert json.loads(store.latest_digest()["item_ids"]) == [phaser]
    pdf = add("rss:q", "pypdf 5.0 released with 3x faster parsing")
    run_once(cfg, store=store, llm=fake_llm(), today=date.today())
    assert json.loads(store.latest_digest()["item_ids"]) == [pdf, phaser]
