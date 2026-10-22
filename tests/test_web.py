from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from night_scout.core import actions
from night_scout.core.config import Config, save_config
from night_scout.core.db import Store
from night_scout.core.models import Item, utcnow
from night_scout.core.pipeline import run_once
from night_scout.web.app import NOT_YET_ROUTES, create_app, safe_url

from .test_pipeline import PROFILE, _seed, fake_llm

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def env(tmp_path: Path):
    cfg = Config(data_dir=str(tmp_path / "data"))
    cfg.data_path().mkdir(parents=True)
    proj = tmp_path / "pdf-tool"
    proj.mkdir()
    prof = json.loads(json.dumps(PROFILE))
    prof["projects"][0]["path"] = str(proj)
    cfg.profile_file().write_text(
        "# Night Scout profile. Edit freely.\n" + yaml.safe_dump(prof, sort_keys=False)
    )
    config_path = tmp_path / "config.yaml"
    save_config(cfg, config_path)
    store = Store(cfg.db_file())
    ids = _seed(store)
    run_once(cfg, store=store, llm=fake_llm(), today=date(2026, 10, 22))
    store.close()
    app = create_app(cfg, config_path, session_key="test-key")
    client = TestClient(app, base_url=BASE)
    page = client.get("/?key=test-key")
    token = page.text.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
    client.headers["X-CSRF-Token"] = token
    return client, token, ids, cfg, proj, config_path


def _card(client: TestClient, item_id: int) -> dict:
    cards = client.get("/api/morning").json()["cards"]
    return next(c for c in cards if c["id"] == item_id)


def _action(card: dict, kind: str) -> dict:
    return next(a for a in card["actions"] if a["kind"] == kind)


# -- guard ---------------------------------------------------------------------------
def test_shell_page_has_csrf_meta_and_strict_csp(env) -> None:
    client, token, *_ = env
    r = client.get("/")
    assert r.status_code == 200
    assert f'<meta name="csrf-token" content="{token}">' in r.text
    assert "/static/app.js" in r.text and "/static/pages.js" in r.text
    assert 'style="' not in r.text and "onclick" not in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "style-src 'self'" in csp
    assert r.headers["x-frame-options"] == "DENY"
    # "no-referrer" would make browsers send `Origin: null` and break every POST
    assert r.headers["referrer-policy"] == "same-origin"


def test_session_cookie_required(env) -> None:
    client, *_ = env
    stranger = TestClient(client.app, base_url=BASE)
    assert stranger.get("/").status_code == 401
    r = stranger.get("/api/morning")
    assert r.status_code == 401 and "error" in r.json()
    assert stranger.get("/api/bootstrap").status_code == 401
    assert stranger.get("/?key=wrong").status_code == 401
    assert stranger.get("/static/app.css").status_code == 200
    r = stranger.get("/?key=test-key&x=1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?x=1"
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    # the key never signs in API calls directly
    fresh = TestClient(client.app, base_url=BASE)
    assert fresh.get("/api/bootstrap?key=test-key").status_code == 401


def test_foreign_host_refused(env) -> None:
    client, *_ = env
    assert client.get("/api/bootstrap", headers={"host": "evil.example"}).status_code == 400
    assert client.get("/", headers={"host": "127.0.0.1.evil.example:8765"}).status_code == 400


def test_foreign_origin_refused(env) -> None:
    client, _, ids, *_ = env
    url = f"/api/item/{ids['rss:1']}/vote"
    r = client.post(url, json={"value": "up"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post(url, json={"value": "up"}, headers={"origin": BASE})
    assert r.status_code == 200


def test_mutations_require_csrf_header(env) -> None:
    client, token, ids, *_ = env
    bare = TestClient(client.app, base_url=BASE, cookies={"night_scout_session": "test-key"})
    item = ids["rss:1"]
    for method, url, body in [
        ("POST", f"/api/item/{item}/vote", {"value": "up"}),
        ("POST", f"/api/item/{item}/research", {}),
        ("POST", "/api/sources/yaml", {"yaml": "reddit: {subreddits: [x]}"}),
        ("PUT", "/api/projects/pdf-tool", {"purpose": "x"}),
        ("POST", "/api/projects/rebuild", {}),
    ]:
        r = bare.request(method, url, json=body)
        assert r.status_code == 403, url
        r = bare.request(method, url, json=body, headers={"X-CSRF-Token": "stale"})
        assert r.status_code == 403, url
    assert bare.get("/api/bootstrap").status_code == 200  # reads need no token
    ok = bare.post(f"/api/item/{item}/vote", json={"value": "up"}, headers={"X-CSRF-Token": token})
    assert ok.status_code == 200


def test_unknown_api_path_is_json_404(env) -> None:
    client, *_ = env
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.json()["error"]


# -- read API --------------------------------------------------------------------------
def test_bootstrap(env) -> None:
    client, *_ = env
    b = client.get("/api/bootstrap").json()
    assert b["model"]["name"] == "gemma4:12b"
    assert b["run"]["status"] == "done" and b["run"]["running"] is False
    assert b["projects"] == ["pdf-tool", "game"]
    assert b["features"] == {"impact": False, "ideas": False}
    assert "timeout_s" in b["sandbox"]
    assert b["schedule"]["time"] == "04:30"


def test_morning_cards(env) -> None:
    client, _, ids, *_ = env
    m = client.get("/api/morning").json()
    assert m["day"] == "2026-10-22"
    titles = [c["title"] for c in m["cards"]]
    assert titles[0].startswith("pypdf 5.0") and any("phaser" in t for t in titles)
    assert not any("Celebrity" in t for t in titles)
    c = m["cards"][0]
    assert c["best"] == "pdf-tool" and c["url"] == "https://e.example/rss:1"
    assert {a["kind"] for a in c["actions"]} >= {"try_it", "note_for_project"}
    note = _action(c, "note_for_project")
    assert note["note_path"].endswith(".md") and "notes/night-scout" in note["note_path"]
    # no v2 columns or ideas table yet: the UI hides those parts
    assert "impact" not in c and m["ideas"] is None
    refused = _card(client, ids["rss:2"])
    assert _action(refused, "try_it")["status"] == "refused"
    assert client.get("/api/morning?day=1999-01-01").status_code == 404


def test_card_urls_go_through_safe_url(env) -> None:
    client, _, ids, cfg, *_ = env
    store = Store(cfg.db_file())
    store.set_item_fields(ids["rss:1"], url="javascript:alert(document.cookie)")
    store.close()
    assert _card(client, ids["rss:1"])["url"] is None
    assert safe_url("javascript:alert(1)") is None
    assert safe_url("data:text/html,<script>") is None
    assert safe_url(" https://ok.example/a ") == "https://ok.example/a"
    assert safe_url("http://ok.example") == "http://ok.example"
    assert safe_url("https://") is None and safe_url(None) is None


def test_v2_columns_and_ideas_are_read_when_present(env) -> None:
    client, _, ids, cfg, *_ = env
    store = Store(cfg.db_file())
    with store.tx() as c:
        c.execute("ALTER TABLE scores ADD COLUMN impact TEXT")
        c.execute("ALTER TABLE scores ADD COLUMN matched TEXT")
        c.execute("ALTER TABLE scores ADD COLUMN next_step TEXT")
        c.execute(
            "UPDATE scores SET impact='change', matched=?, next_step='Upgrade pypdf.' "
            "WHERE item_id=?",
            (json.dumps(["faster pdf parsers"]), ids["rss:1"]),
        )
        c.execute(
            "CREATE TABLE ideas (id INTEGER PRIMARY KEY, run_id INTEGER, day TEXT, title TEXT, "
            "pitch TEXT, why_you TEXT, first_step TEXT, inspired_by TEXT, status TEXT, "
            "created_at TEXT)"
        )
        c.execute(
            "INSERT INTO ideas VALUES (1, 1, '2026-10-22', 'PDF speed lab', 'Bench it.', "
            "'You parse PDFs.', 'pip install pypdf', ?, 'new', '')",
            (json.dumps([ids["rss:1"], 99999]),),
        )
        c.execute(
            "INSERT INTO ideas VALUES (2, 1, '2026-10-22', 'Gone', 'x', 'x', 'x', '[]', "
            "'dismissed', '')"
        )
    store.close()
    m = client.get("/api/morning").json()
    c = next(c for c in m["cards"] if c["id"] == ids["rss:1"])
    assert c["impact"] == "change" and c["matched"] == ["faster pdf parsers"]
    assert c["next_step"] == "Upgrade pypdf."
    assert [i["title"] for i in m["ideas"]] == ["PDF speed lab"]
    assert list(m["idea_items"]) == [str(ids["rss:1"])]
    assert client.get("/api/bootstrap").json()["features"] == {"impact": True, "ideas": True}


# -- votes -------------------------------------------------------------------------------
def test_vote_flow_and_undo(env) -> None:
    client, _, ids, *_ = env
    item = ids["rss:1"]
    r = client.post(f"/api/item/{item}/vote", json={"value": "up"}).json()
    assert r["vote"] == 1 and r["source"]["name"] == "Blog" and r["source"]["weight"] > 0
    assert r["topics"][0][0].lower() == "pdf"
    assert client.get("/api/weights").json()["source"]["Blog"] > 0
    client.post(f"/api/item/{item}/vote", json={"value": "ignore"})
    m = client.get("/api/morning").json()
    assert item not in [c["id"] for c in m["cards"]] and m["ignored"] == 1
    r = client.post(f"/api/item/{item}/vote", json={"value": "clear"}).json()  # undo
    assert r["vote"] is None
    assert _card(client, item)["vote"] is None
    assert client.post(f"/api/item/{item}/vote", json={"value": "meh"}).status_code == 400
    assert client.post("/api/item/99999/vote", json={"value": "up"}).status_code == 404


# -- research ---------------------------------------------------------------------------
def test_research_job(env, monkeypatch) -> None:
    client, _, ids, *_ = env
    item = ids["rss:1"]

    def fake_research(_cfg, store, _llm, item_id):
        store.save_research(item_id, "fake-gemma", "## TL;DR\nFaster.")
        return "ok"

    monkeypatch.setattr(actions, "research_more", fake_research)
    assert client.get(f"/api/item/{item}/research").json()["state"] == "idle"
    r = client.post(f"/api/item/{item}/research", json={})
    assert r.status_code == 200 and r.json()["started"] is True
    for _ in range(100):
        s = client.get(f"/api/item/{item}/research").json()
        if s["state"] != "running":
            break
        time.sleep(0.02)
    assert s["state"] == "done" and s["brief"].startswith("## TL;DR") and s["model"] == "fake-gemma"
    assert _card(client, item)["research"].startswith("## TL;DR")
    assert client.post("/api/item/99999/research", json={}).status_code == 404


# -- try it -----------------------------------------------------------------------------
def test_try_it_plan_and_run(env, monkeypatch) -> None:
    client, _, ids, *_ = env
    try_id = _action(_card(client, ids["rss:1"]), "try_it")["id"]
    plan = client.get(f"/api/action/{try_id}/plan").json()
    assert plan["plan"]["setup"] == ["pip install pypdf==5.0.0"]
    assert plan["verdict"]["ok"] is True and plan["project"] == "pdf-tool"
    assert client.post(f"/api/action/{try_id}/run", json={}).status_code == 400

    started: list[int] = []

    def fake_start(_cfg, store, action_id):
        started.append(action_id)
        store.set_action(action_id, "running", {"log": "", "reason": "starting…"})

    monkeypatch.setattr(actions, "start_try_it", fake_start)
    r = client.post(f"/api/action/{try_id}/run", json={"confirmed": True})
    assert r.status_code == 200 and r.json()["status"] == "running" and started == [try_id]
    assert client.get(f"/api/action/{try_id}").json()["status"] == "running"

    def busy(_cfg, _store, _action_id):
        raise actions.ActionError("another Try-it is running; wait for it to finish")

    monkeypatch.setattr(actions, "start_try_it", busy)
    r = client.post(f"/api/action/{try_id}/run", json={"confirmed": True})
    assert r.status_code == 409 and "another Try-it" in r.json()["error"]
    assert client.get("/api/action/99999").status_code == 404


def test_refused_plan_reports_problems(env) -> None:
    client, _, ids, *_ = env
    a = _action(_card(client, ids["rss:2"]), "try_it")
    assert a["detail"]["refused"]
    plan = client.get(f"/api/action/{a['id']}/plan").json()
    assert plan["verdict"]["ok"] is False and plan["verdict"]["problems"]
    r = client.post(f"/api/action/{a['id']}/run", json={"confirmed": True})
    assert r.status_code == 409


# -- note -------------------------------------------------------------------------------
def test_note_written_inside_project_only(env) -> None:
    client, _, ids, _cfg, proj, _ = env
    note_id = _action(_card(client, ids["rss:1"]), "note_for_project")["id"]
    r = client.post(f"/api/action/{note_id}/note", json={})
    assert r.status_code == 200 and r.json()["path"].endswith(".md")
    notes = list((proj / "notes" / "night-scout").glob("*.md"))
    assert len(notes) == 1 and "pypdf 5" in notes[0].read_text()
    assert _action(_card(client, ids["rss:1"]), "note_for_project")["status"] == "done"
    again = client.post(f"/api/action/{note_id}/note", json={})
    assert again.status_code == 409 and "already written" in again.json()["error"]


# -- run status -------------------------------------------------------------------------
def test_run_status_idle_and_running(env) -> None:
    client, _, _, cfg, *_ = env
    r = client.get("/api/run").json()
    assert r["running"] is False and r["progress"] is None
    store = Store(cfg.db_file())
    run_id = store.start_run()
    store.close()
    (cfg.data_path() / "run.lock").write_text(str(os.getpid()))
    r = client.get("/api/run").json()
    assert r["running"] is True and r["run"]["id"] == run_id
    assert r["progress"]["stage"] in ("fetch", "judge")
    # live progress from the pipeline, when it writes it, is passed through
    store = Store(cfg.db_file())
    progress = {"stage": "judge", "done": 3, "total": 40, "stages_done": ["fetch"]}
    with store.tx() as c:
        c.execute(
            "UPDATE runs SET stats=? WHERE id=?", (json.dumps({"progress": progress}), run_id)
        )
    store.close()
    r = client.get("/api/run").json()
    assert r["progress"] == progress and "progress" not in r["run"]["stats"]


def test_stale_running_row_is_not_a_live_run(env) -> None:
    client, _, _, cfg, *_ = env
    store = Store(cfg.db_file())
    store.start_run()
    store.close()
    (cfg.data_path() / "run.lock").write_text("999999999")  # no such process
    assert client.get("/api/run").json()["running"] is False


# -- sources ----------------------------------------------------------------------------
def test_sources_view_and_yaml_save(env) -> None:
    client, _, _, _, _, config_path = env
    s = client.get("/api/sources").json()
    assert s["catalog"] == [] and "yaml" in s and "weights" in s
    bad = client.post("/api/sources/yaml", json={"yaml": "reddit: {limit: 9999}"})
    assert bad.status_code == 400 and "limit" in bad.json()["error"]
    bad = client.post("/api/sources/yaml", json={"yaml": "reddit: [unclosed"})
    assert bad.status_code == 400 and "YAML" in bad.json()["error"]
    good = client.post("/api/sources/yaml", json={"yaml": "reddit:\n  subreddits: [python]\n"})
    assert good.status_code == 200 and good.json()["count"] == 1
    saved = yaml.safe_load(config_path.read_text())
    assert saved["sources"]["reddit"]["subreddits"] == ["python"]
    cat = client.get("/api/sources").json()["catalog"]
    assert cat == [
        {
            "key": "reddit:python",
            "label": "Reddit r/python",
            "type": "reddit",
            "detail": "python",
            "enabled": True,
        }
    ]


# -- projects ---------------------------------------------------------------------------
def test_projects_view(env) -> None:
    client, *_ = env
    p = client.get("/api/projects").json()
    assert [x["name"] for x in p["projects"]] == ["pdf-tool", "game"]
    assert p["projects"][0]["would_want_to_know"] == ["faster pdf parsers"]
    assert p["job"]["state"] == "idle"


def test_project_put_validates_and_preserves_others(env) -> None:
    client, _, _, cfg, *_ = env
    path = cfg.profile_file()
    edit = {
        "purpose": "  Summarizes and searches PDFs. ",
        "stack": ["Python", "FastAPI", "Python"],
        "key_dependencies": ["pypdf"],
        "interests": ["pdf parsing", "ocr"],
        "would_want_to_know": ["faster pdf parsers"],
    }
    r = client.put("/api/projects/pdf-tool", json=edit)
    assert r.status_code == 200
    assert r.json()["project"]["purpose"] == "Summarizes and searches PDFs."
    text = path.read_text()
    assert text.startswith("# Night Scout profile. Edit freely.\n")
    data = yaml.safe_load(text)
    pdf, game = data["projects"]
    assert pdf["stack"] == ["Python", "FastAPI"] and pdf["interests"] == ["pdf parsing", "ocr"]
    assert pdf["path"].endswith("pdf-tool") and pdf["name"] == "pdf-tool"
    assert game == PROFILE["projects"][1]  # untouched

    assert client.put("/api/projects/nope", json=edit).status_code == 404
    for bad in (
        {**edit, "stack": "Python"},
        {**edit, "interests": ["x" * 201]},
        {**edit, "interests": ["  "]},
        {**edit, "purpose": "x" * 601},
        {**edit, "path": "/etc"},
        {**edit, "interests": [f"i{n}" for n in range(61)]},
    ):
        r = client.put("/api/projects/pdf-tool", json=bad)
        assert r.status_code == 400, bad
    assert yaml.safe_load(path.read_text())["projects"][0]["interests"] == ["pdf parsing", "ocr"]


def test_project_rebuild_job(env, monkeypatch) -> None:
    import night_scout.web.app as web

    client, *_ = env
    calls: list[int] = []
    monkeypatch.setattr(web, "build_profile_file", lambda _cfg, _llm: calls.append(1))
    monkeypatch.setattr(web, "OllamaClient", lambda _m: object())
    r = client.post("/api/projects/rebuild", json={})
    assert r.status_code == 200 and r.json()["started"] is True
    for _ in range(100):
        s = client.get("/api/projects/rebuild").json()
        if s["state"] != "running":
            break
        time.sleep(0.02)
    assert s["state"] == "done" and calls == [1]


# -- guide and not-yet endpoints ----------------------------------------------------------
def test_guide_has_no_chat(env) -> None:
    client, *_ = env
    text = client.get("/api/guide").json()["markdown"]
    assert text.startswith("# What Night Scout does") and "chat" not in text.lower()


@pytest.mark.parametrize(("method", "route"), NOT_YET_ROUTES)
def test_not_yet_endpoints_return_501(env, method: str, route: str) -> None:
    client, *_ = env
    url = route.replace("{idea_id}", "1").replace("{provider_id}", "ollama")
    r = client.request(method, url, json={} if method != "GET" else None)
    assert r.status_code == 501 and r.json() == {"error": "not available yet"}


def test_concurrent_reads_do_not_share_cursors(env) -> None:
    import threading

    client, *_ = env
    store = client.app.state.store
    bad: list[object] = []

    def work() -> None:
        try:
            for _ in range(300):
                run = store.last_run()
                if run is None or run["status"] != "done":
                    bad.append(run)
                store.source_statuses(run["id"])
        except Exception as e:  # sqlite3 "API misuse" when cursors interleave
            bad.append(e)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert bad == []


def test_interrupted_try_its_marked_failed(tmp_path: Path) -> None:
    cfg = Config(data_dir=str(tmp_path))
    store = Store(cfg.db_file())
    run = store.start_run()
    item_id = store.upsert_item(
        Item("rss", "b", "rss:x", "https://x.example", "t", published_at=utcnow()), run
    ).item_id
    aid = store.add_action(item_id, "try_it", None, "t", {})
    store.set_action(aid, "running", {})
    store.close()
    create_app(cfg, tmp_path / "config.yaml", session_key="k")
    assert Store(cfg.db_file()).action(aid)["status"] == "failed"
