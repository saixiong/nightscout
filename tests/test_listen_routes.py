"""Listen + Settings → Voice over HTTP: add-on missing, settings, recording, audio with Range,
and the nightly run's Ready by morning. The worker runs with fake Kokoro (tests/fake_tts)."""

from __future__ import annotations

import argparse
import logging
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from night_scout import cli
from night_scout.core import listen
from night_scout.core.config import Config, load_config, save_config
from night_scout.core.db import Store
from night_scout.core.pipeline import run_once
from night_scout.web.app import create_app, digest_payload

from .test_digest import _seed, _topics_cfg, fake

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def lenv(tmp_path: Path):
    cfg = _topics_cfg(tmp_path / "data")
    config_path = tmp_path / "config.yaml"
    save_config(cfg, config_path)
    store = Store(cfg.db_file())
    ids = _seed(store)
    run_once(cfg, store=store, llm=fake(topics_only=True), today=date(2026, 10, 22))
    store.close()

    def client() -> TestClient:
        app = create_app(load_config(config_path), config_path, session_key="k")
        c = TestClient(app, base_url=BASE)
        page = c.get("/?key=k")
        c.headers["X-CSRF-Token"] = page.text.split('name="csrf-token" content="', 1)[1].split(
            '"', 1
        )[0]
        return c

    return client, ids, cfg, config_path


def ready(c: TestClient, day: str | None = None, timeout: float = 20) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        s = c.get("/api/listen" + (f"?day={day}" if day else "")).json()
        if s["status"] in ("ready", "failed"):
            return s
        time.sleep(0.1)
    raise AssertionError("the recording never finished")


# -- without the add-on: nothing changes --------------------------------------------------------
def test_without_the_addon(lenv) -> None:
    make, *_ = lenv
    c = make()
    assert c.get("/api/listen").json() == {
        "available": False,
        "install": 'pip install "night-scout[voice]"',
    }
    r = c.post("/api/listen", json={})
    assert r.status_code == 409 and "night-scout[voice]" in r.json()["error"]
    v = c.get("/api/voice").json()
    assert v["available"] is False and v["install"] == 'pip install "night-scout[voice]"'
    assert c.get("/api/voice/preview/af_heart").status_code == 409
    assert c.get("/api/digest").status_code == 200  # the Digest is untouched


def test_voice_settings_refresh_rechecks(lenv, fake_voice: Path) -> None:
    make, *_ = lenv
    c = make()
    assert c.get("/api/voice").json()["available"] is True
    listen._avail[listen.voice_python()] = (False, "stale answer")
    assert c.get("/api/voice").json()["available"] is False
    assert c.get("/api/voice?refresh=1").json()["available"] is True  # opening Settings → Voice


# -- Settings → Voice --------------------------------------------------------------------------
def test_voice_view_and_defaults(lenv, fake_voice: Path) -> None:
    make, *_ = lenv
    v = make().get("/api/voice").json()
    assert v["available"] is True and v["engine"] == "Kokoro-82M"
    assert len(v["voices"]) == 28 and v["default"] == "af_heart"
    assert v["suggested"] == ["af_heart", "af_bella", "bf_emma", "bm_george", "am_michael"]
    assert [len(ids) for _, ids in v["groups"]] == [11, 9, 4, 4]
    assert v["settings"] == {
        "voice": "af_heart",
        "speed": 1.0,
        "reads": "full",
        "ready_by_morning": True,
    }
    assert v["speeds"] == [0.9, 1.0, 1.25, 1.5] and v["keep_days"] == 7 and v["last"] is None


def test_voice_settings_persist_in_config_yaml(lenv, fake_voice: Path) -> None:
    make, _, _, config_path = lenv
    c = make()
    assert "voice" not in yaml.safe_load(config_path.read_text())  # defaults aren't written
    r = c.put("/api/voice", json={"voice": "bm_george", "speed": 1.25})
    assert r.status_code == 200 and r.json()["settings"]["voice"] == "bm_george"
    r = c.put("/api/voice", json={"reads": "headlines", "ready_by_morning": False})
    assert r.json()["settings"] == {
        "voice": "bm_george",
        "speed": 1.25,
        "reads": "headlines",
        "ready_by_morning": False,
    }
    assert load_config(config_path).voice.model_dump() == r.json()["settings"]
    assert make().get("/api/voice").json()["settings"]["voice"] == "bm_george"  # a restart
    assert c.put("/api/voice", json={"voice": "xx_nobody"}).status_code == 400
    assert c.put("/api/voice", json={"speed": 3}).status_code == 400
    assert c.put("/api/voice", json={"reads": "everything"}).status_code == 400
    assert c.put("/api/voice", json={"volume": 11}).status_code == 400
    bare = TestClient(c.app, base_url=BASE)
    bare.cookies = c.cookies
    assert bare.put("/api/voice", json={"voice": "af_bella"}).status_code == 403  # no CSRF


def test_preview_plays_and_is_cached(lenv, fake_voice: Path) -> None:
    make, _, cfg, _ = lenv
    c = make()
    r = c.get("/api/voice/preview/bf_emma")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    assert r.content.startswith(b"ID3FAKE")
    assert (cfg.data_path() / "listen" / "previews" / "bf_emma.mp3").is_file()
    assert c.get("/api/voice/preview/nobody").status_code == 404


# -- Listen ---------------------------------------------------------------------------------------
def test_listen_records_on_demand_and_serves_audio_with_range(lenv, fake_voice: Path) -> None:
    make, ids, _, _ = lenv
    c = make()
    s = c.get("/api/listen").json()
    assert s["available"] and s["status"] == "none"
    assert s["day"] == c.get("/api/digest").json()["day"]
    assert s["stories"] == 2 and s["voice_name"] == "Heart" and s["estimated"]
    assert [ch["story_id"] for ch in s["chapters"]] == [None, ids["rss:1"], ids["rss:2"], None]
    assert all(ch["url"] is None and ch["est"] > 0 for ch in s["chapters"])
    r = c.post("/api/listen", json={"story": ids["rss:2"]})
    assert r.status_code == 200 and r.json()["status"] in ("waiting", "recording", "ready")
    s = ready(c)
    assert s["status"] == "ready" and s["done"] == 4 and not s["estimated"]
    url = s["chapters"][1]["url"]
    full = c.get(url)
    assert full.status_code == 200 and full.headers["content-type"] == "audio/mpeg"
    assert full.headers["accept-ranges"] == "bytes"
    part = c.get(url, headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert part.headers["content-range"] == f"bytes 0-99/{len(full.content)}"
    assert part.content == full.content[:100]
    assert c.get(url, headers={"Range": "bytes=999999-"}).status_code == 416
    # the same Digest is not recorded twice
    assert c.post("/api/listen", json={}).json()["status"] == "ready"


def test_listen_audio_path_safety(lenv, fake_voice: Path) -> None:
    make, *_ = lenv
    c = make()
    c.post("/api/listen", json={})
    s = ready(c)
    key, day = s["key"], s["day"]
    for bad in [
        f"/api/listen/audio/{day}/{key}/manifest.json",
        f"/api/listen/audio/{day}/{key}/job.json",
        f"/api/listen/audio/{day}/{key}/..%2Fmanifest.json",
        f"/api/listen/audio/{day}/{'0' * 16}/00-intro.mp3",
        f"/api/listen/audio/..%2F..%2F/{key}/00-intro.mp3",
    ]:
        r = c.get(bad)
        assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
    stranger = TestClient(c.app, base_url=BASE)
    assert stranger.get(s["chapters"][0]["url"]).status_code == 401


def test_listen_errors(lenv, fake_voice: Path) -> None:
    make, *_ = lenv
    c = make()
    assert c.post("/api/listen", json={"story": 999999}).status_code == 404
    assert c.post("/api/listen", json={"day": "1999-01-01"}).status_code == 404
    assert c.post("/api/listen", json={"day": "nope"}).status_code == 400
    assert c.get("/api/listen?day=nope").status_code == 400
    assert c.get("/api/listen?day=1999-01-01").json()["status"] == "none"


def test_headlines_only_rerecords_shorter(lenv, fake_voice: Path) -> None:
    make, *_ = lenv
    c = make()
    c.post("/api/listen", json={})
    full = ready(c)
    c.put("/api/voice", json={"reads": "headlines"})
    s = c.get("/api/listen").json()
    assert s["key"] != full["key"] and s["status"] == "none" and s["mode"] == "headlines"
    c.post("/api/listen", json={})
    short = ready(c)
    assert short["seconds"] < full["seconds"]


def test_bootstrap_and_digest_unchanged_shape(lenv, fake_voice: Path) -> None:
    make, *_ = lenv
    c = make()
    assert "stories" in c.get("/api/digest").json()
    assert c.get("/api/bootstrap").status_code == 200


# -- Ready by morning (the nightly run) ---------------------------------------------------------
def test_ready_by_morning_records_after_the_run(lenv, fake_voice: Path, capsys) -> None:
    _, _, cfg, _ = lenv
    cli.ready_by_morning(cfg)
    out = capsys.readouterr().out
    assert "Listen: recorded the Digest (Heart, 2 stories" in out
    store = Store(cfg.db_file())
    plan = listen.plan_for(cfg, digest_payload(cfg, store))
    store.close()
    assert listen.view(plan)["status"] == "ready"


def test_ready_by_morning_off_or_missing_does_nothing(
    lenv, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, cfg, _ = lenv
    called: list[object] = []
    monkeypatch.setattr(listen, "record", lambda *a, **k: called.append(a) or {})
    cli.ready_by_morning(cfg)  # the add-on isn't installed (conftest)
    cfg.voice.ready_by_morning = False
    monkeypatch.setattr(listen, "check_available", lambda refresh=False: (True, None))
    cli.ready_by_morning(cfg)
    assert called == []


def test_ready_by_morning_never_breaks_the_run(
    lenv, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _, _, cfg, config_path = lenv
    monkeypatch.setattr(listen, "check_available", lambda refresh=False: (True, None))

    def boom(*a: object, **k: object) -> dict:
        raise RuntimeError("disk full")

    monkeypatch.setattr(listen, "record", boom)
    with caplog.at_level(logging.ERROR):
        cli.ready_by_morning(cfg)
    assert "couldn't record the Digest" in caplog.text
    # cmd_run: the run's own result decides the exit code, after the recording attempt
    from night_scout.core import pipeline

    seen: list[str] = []
    monkeypatch.setattr(
        pipeline,
        "run_once",
        lambda c, fetch_only=False: SimpleNamespace(ok=True, status_lines=[], digest_path=None),
    )
    monkeypatch.setattr(cli, "ready_by_morning", lambda c: seen.append("rec"))
    args = argparse.Namespace(config=config_path, fetch_only=False)
    assert cli.cmd_run(args) == 0 and seen == ["rec"]
    assert cli.cmd_run(argparse.Namespace(config=config_path, fetch_only=True)) == 0
    assert seen == ["rec"]  # --fetch-only has no Digest to read


def test_config_voice_section_validates(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("voice:\n  voice: bf_emma\n  speed: 1.5\n  reads: headlines\n")
    assert load_config(p).voice.voice == "bf_emma"
    p.write_text("voice:\n  voice: '../x'\n")
    with pytest.raises(ValueError):
        load_config(p)
    assert Config().voice.ready_by_morning is True
