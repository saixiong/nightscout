from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from urllib.parse import unquote

import pytest
import yaml
from fastapi.testclient import TestClient

from night_scout.core.config import Config, save_config
from night_scout.core.db import Store
from night_scout.core.pipeline import run_once
from night_scout.web.app import create_app

from .test_pipeline import PROFILE, _seed, fake_llm


@pytest.fixture
def env(tmp_path: Path):
    cfg = Config(data_dir=str(tmp_path / "data"))
    cfg.data_path().mkdir(parents=True)
    proj = tmp_path / "pdf-tool"
    proj.mkdir()
    prof = json.loads(json.dumps(PROFILE))
    prof["projects"][0]["path"] = str(proj)
    cfg.profile_file().write_text(yaml.safe_dump(prof))
    config_path = tmp_path / "config.yaml"
    save_config(cfg, config_path)
    store = Store(cfg.db_file())
    ids = _seed(store)
    run_once(cfg, store=store, llm=fake_llm(), today=date(2026, 10, 22))
    store.close()
    app = create_app(cfg, config_path)
    client = TestClient(app)
    page = client.get("/")
    token = page.text.split('data-csrf="', 1)[1].split('"', 1)[0]
    return client, token, ids, cfg, proj, config_path


def test_morning_lists_digest_cards(env) -> None:
    client, *_ = env
    r = client.get("/")
    assert r.status_code == 200
    assert "2 things worth doing" in r.text
    assert "pypdf 5.0 released" in r.text and "phaser plugin" in r.text
    assert "Celebrity" not in r.text
    assert "▶ Try it" in r.text and "⛔ Try it" in r.text  # proposed and refused plans
    assert "default-src 'self'" in r.headers["content-security-policy"]


def test_foreign_host_and_origin_refused(env) -> None:
    client, token, ids, *_ = env
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400
    r = client.post(
        f"/item/{ids['rss:1']}/feedback",
        data={"value": "up", "csrf_token": token},
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_post_requires_csrf(env) -> None:
    client, token, ids, *_ = env
    r = client.post(f"/item/{ids['rss:1']}/feedback", data={"value": "up"})
    assert r.status_code == 403
    r = client.post(f"/item/{ids['rss:1']}/feedback", data={"value": "up", "csrf_token": token})
    assert r.status_code == 200 and r.json()["value"] == "up"


def test_ignore_hides_card(env) -> None:
    client, token, ids, *_ = env
    client.post(f"/item/{ids['rss:1']}/feedback", data={"value": "ignore", "csrf_token": token})
    assert "pypdf 5.0 released" not in client.get("/").text


def test_item_detail_and_confirm(env) -> None:
    client, token, ids, *_ = env
    r = client.get(f"/item/{ids['rss:1']}")
    assert r.status_code == 200
    assert "pip install pypdf==5.0.0" in r.text and "Run in sandbox" in r.text
    try_id = int(r.text.split("/action/", 1)[1].split("/confirm", 1)[0])
    c = client.get(f"/action/{try_id}/confirm")
    assert "Supply-chain warning" in c.text and "I've read the commands" in c.text
    # running without ticking the box is refused
    r = client.post(f"/action/{try_id}/run", data={"csrf_token": token})
    assert r.status_code == 400


def test_refused_plan_shows_reasons(env) -> None:
    client, _, ids, *_ = env
    r = client.get(f"/item/{ids['rss:2']}")
    assert "Refused by the safety check" in r.text and "pipe-to-shell" in r.text


def test_note_written_inside_project_only(env) -> None:
    client, token, ids, _cfg, proj, _ = env
    page = client.get(f"/item/{ids['rss:1']}").text
    note_id = int(page.split('action="/action/', 1)[1].split("/note", 1)[0])
    r = client.post(f"/action/{note_id}/note", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and "Note saved" in unquote(r.headers["location"])
    notes = list((proj / "notes" / "night-scout").glob("*.md"))
    assert len(notes) == 1 and "pypdf 5" in notes[0].read_text()
    again = client.post(
        f"/action/{note_id}/note", data={"csrf_token": token}, follow_redirects=False
    )
    assert "already written" in unquote(again.headers["location"])


def test_sources_editor_validates(env) -> None:
    client, token, _, _, _, config_path = env
    bad = client.post(
        "/sources",
        data={"sources_yaml": "reddit: {limit: 9999}", "csrf_token": token},
        follow_redirects=False,
    )
    assert "Not saved" in unquote(bad.headers["location"])
    good = client.post(
        "/sources",
        data={"sources_yaml": "reddit:\n  subreddits: [python]\n", "csrf_token": token},
        follow_redirects=False,
    )
    assert "Saved" in unquote(good.headers["location"])
    saved = yaml.safe_load(config_path.read_text())
    assert saved["sources"]["reddit"]["subreddits"] == ["python"]


def test_profile_page_hides_paths_and_editor_validates(env) -> None:
    client, token, *_ = env
    r = client.get("/profile")
    assert "pdf-tool" in r.text and "faster pdf parsers" in r.text
    bad = client.post(
        "/profile",
        data={"profile_yaml": "projects: nope", "csrf_token": token},
        follow_redirects=False,
    )
    assert "Not saved" in unquote(bad.headers["location"])


def test_source_feedback_reports_weight(env) -> None:
    client, token, *_ = env
    for _ in range(3):
        r = client.post(
            "/feedback/source", data={"target": "Blog", "value": "down", "csrf_token": token}
        )
    assert r.json()["muted"] is True
