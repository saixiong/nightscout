"""Setup walkthrough: project folders, profile entries, schedule, run now, first launch."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from night_scout import cli
from night_scout.core import setup as setup_mod
from night_scout.core.config import Config, ProjectRef, load_config, save_config
from night_scout.web import app as web
from night_scout.web.app import create_app

from .test_web import BASE, env  # noqa: F401  (pytest fixture)


def _proj(root: Path, name: str, files: tuple[str, ...] = ("README.md",)) -> Path:
    d = root / name
    d.mkdir(parents=True)
    for f in files:
        (d / f).write_text(f"# {name}\n")
    return d


# -- core ------------------------------------------------------------------------------
def test_add_project_validates(tmp_path: Path) -> None:
    cfg = Config(data_dir=str(tmp_path / "data"))
    good = _proj(tmp_path, "app", ("README.md", "pyproject.toml"))
    bare = tmp_path / "bare"
    bare.mkdir()
    for bad, why in [
        ("", "Type the folder"),
        ("relative/path", "full path"),
        (str(tmp_path / "missing"), "No folder"),
        (str(bare), "no README"),
    ]:
        with pytest.raises(ValueError, match=why):
            setup_mod.add_project(cfg, bad)
    ref, found = setup_mod.add_project(cfg, str(good))
    assert found == ["README.md", "pyproject.toml"] and cfg.projects == [ref]
    with pytest.raises(ValueError, match="already one of your projects"):
        setup_mod.add_project(cfg, str(good) + "/")
    other = _proj(tmp_path / "elsewhere", "app")
    with pytest.raises(ValueError, match="named 'app'"):
        setup_mod.add_project(cfg, str(other))


def test_suggestions_skip_added_hidden_and_non_projects(tmp_path: Path, monkeypatch) -> None:
    base = tmp_path / "projects"
    a = _proj(base, "alpha")
    _proj(base, "beta", ("package.json",))
    _proj(base, ".hidden")
    (base / "notes").mkdir()
    monkeypatch.setenv("NIGHT_SCOUT_PROJECT_DIRS", str(base))
    cfg = Config(data_dir=str(tmp_path / "data"), projects=[ProjectRef(path=str(a))])
    got = setup_mod.suggest_projects(cfg)
    assert [s["name"] for s in got] == ["beta"] and got[0]["found"] == ["package.json"]


def test_profile_entries_upsert_and_remove_keep_header(tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    setup_mod.upsert_profile_entry(path, {"name": "a", "purpose": "first"})
    path.write_text("# my header\n" + path.read_text())
    setup_mod.upsert_profile_entry(path, {"name": "b", "purpose": "second"})
    setup_mod.upsert_profile_entry(path, {"name": "a", "purpose": "re-read"})
    data = yaml.safe_load(path.read_text())
    assert [(p["name"], p["purpose"]) for p in data["projects"]] == [
        ("a", "re-read"),
        ("b", "second"),
    ]
    assert path.read_text().startswith("# my header\n")
    assert setup_mod.remove_profile_entry(path, "a") is True
    assert setup_mod.remove_profile_entry(path, "a") is False
    assert setup_mod.profiled_names(path) == {"b"}
    assert path.read_text().startswith("# my header\n")


def test_setup_status(tmp_path: Path) -> None:
    cfg = Config(data_dir=str(tmp_path / "data"))
    assert setup_mod.setup_status(cfg) == {
        "needed": True,
        "reasons": ["nothing_to_scout"],
        "topics": [],
    }
    p = _proj(tmp_path, "app")
    cfg.projects.append(ProjectRef(path=str(p)))
    assert setup_mod.setup_status(cfg)["reasons"] == ["no_profile"]
    cfg.data_path().mkdir(parents=True)
    setup_mod.upsert_profile_entry(cfg.profile_file(), {"name": "app", "purpose": "x"})
    assert setup_mod.setup_status(cfg) == {"needed": False, "reasons": [], "topics": []}


def test_setup_status_topics_without_projects(tmp_path: Path) -> None:
    """The Digest doesn't need projects: one typed topic is enough to finish Setup."""
    from night_scout.core.profile import save_topics

    cfg = Config(data_dir=str(tmp_path / "data"))
    save_topics(cfg.profile_file(), ["Local LLMs", " local llms ", "AI safety"])
    st = setup_mod.setup_status(cfg)
    assert st == {"needed": False, "reasons": [], "topics": ["Local LLMs", "AI safety"]}


def test_start_run_detaches_and_logs(tmp_path: Path, monkeypatch) -> None:
    seen: dict = {}

    class FakeProc:
        pid = 4242

        def wait(self) -> int:
            return 0

    def fake_popen(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return FakeProc()

    monkeypatch.setattr(setup_mod.subprocess, "Popen", fake_popen)
    cfg = Config(data_dir=str(tmp_path / "data"))
    assert setup_mod.start_run(cfg, tmp_path / "config.yaml") == 4242
    assert seen["cmd"][-1] == "run" and "--config" in seen["cmd"]
    assert seen["start_new_session"] is True
    assert (cfg.data_path() / "logs" / "run.log").exists()


# -- web -------------------------------------------------------------------------------
def test_bootstrap_and_setup_view(env, tmp_path: Path, monkeypatch) -> None:  # noqa: F811
    client, *_ = env
    monkeypatch.setenv("NIGHT_SCOUT_PROJECT_DIRS", str(tmp_path / "nothing-here"))
    assert client.get("/api/bootstrap").json()["setup"]["needed"] in (True, False)
    r = client.get("/api/setup").json()
    assert {"needed", "reasons", "projects", "suggestions", "schedule"} <= set(r)
    assert r["suggestions"] == []


def test_add_read_and_remove_project(env, tmp_path: Path, monkeypatch) -> None:  # noqa: F811
    client, _token, _ids, cfg, _proj_dir, config_path = env
    new = _proj(tmp_path / "work", "scraper", ("README.md", "package.json"))
    r = client.post("/api/projects/add", json={"path": str(new)})
    assert r.status_code == 200, r.text
    assert r.json()["project"]["found"] == ["README.md", "package.json"]
    assert any(p.path == str(new) for p in load_config(config_path).projects)
    bad = client.post("/api/projects/add", json={"path": str(new)})
    assert bad.status_code == 400 and "already" in bad.json()["error"]

    monkeypatch.setattr(web, "make_llm", lambda _cfg, _role="judge": object())
    monkeypatch.setattr(
        web,
        "profile_project",
        lambda ref, _llm: {"name": "x", "path": ref.path, "purpose": "Scrapes things."},
    )
    r = client.post("/api/projects/scraper/read", json={})
    assert r.status_code == 200 and r.json()["started"] is True
    for _ in range(100):
        s = client.get("/api/projects/scraper/read").json()
        if s["state"] != "running":
            break
        time.sleep(0.02)
    assert s["state"] == "done"
    rows = {p["name"]: p for p in client.get("/api/setup").json()["projects"]}
    assert rows["scraper"]["profiled"] is True
    names = [p["name"] for p in client.get("/api/projects").json()["projects"]]
    assert "scraper" in names  # entry stored under the config's name, not the model's

    assert client.post("/api/projects/nope/read", json={}).status_code == 404
    r = client.delete("/api/projects/scraper")
    assert r.status_code == 200
    assert all(p.path != str(new) for p in load_config(config_path).projects)
    assert "scraper" not in setup_mod.profiled_names(cfg.profile_file())
    assert new.is_dir()  # the folder itself is never touched
    assert client.delete("/api/projects/scraper").status_code == 404


def test_schedule_put_installs_and_removes(env, tmp_path: Path, monkeypatch) -> None:  # noqa: F811
    client, _token, _ids, _cfg, _p, config_path = env
    calls: list[str] = []
    plist = tmp_path / "LaunchAgents" / "dev.nightscout.run.plist"
    monkeypatch.setattr(web.schedule_mod, "launchd_plist_path", lambda: plist)

    def fake_install(cfg, _path):
        calls.append(f"install {cfg.schedule.time}")
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text("x")
        return "Installed."

    def fake_uninstall():
        calls.append("uninstall")
        plist.unlink()
        return "Removed."

    monkeypatch.setattr(web.schedule_mod, "install", fake_install)
    monkeypatch.setattr(web.schedule_mod, "uninstall", fake_uninstall)
    r = client.put("/api/schedule", json={"time": "2:30", "nightly": True})
    assert r.status_code == 200, r.text
    assert load_config(config_path).schedule.time == "02:30" and calls == ["install 02:30"]
    if web.sys.platform == "darwin":
        assert r.json()["schedule"]["installed"] is True
        view = client.get("/api/schedule").json()
        assert "dev.nightscout.run" in view["plist"] and str(Path.home()) not in view["plist"]
        r = client.put("/api/schedule", json={"time": "02:30", "nightly": False})
        assert calls[-1] == "uninstall" and r.json()["schedule"]["installed"] is False
    assert client.put("/api/schedule", json={"time": "25:99", "nightly": True}).status_code == 400


def test_run_now(env, monkeypatch) -> None:  # noqa: F811
    client, *_ = env
    started: list[int] = []
    monkeypatch.setattr(web.setup_mod, "start_run", lambda _c, _p: started.append(1) or 99)
    r = client.post("/api/run", json={})
    assert r.status_code == 200 and r.json()["pid"] == 99 and started == [1]


def test_run_now_needs_a_profile(tmp_path: Path, monkeypatch) -> None:
    cfg = Config(data_dir=str(tmp_path / "data"))
    cfg.data_path().mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    save_config(cfg, config_path)
    monkeypatch.setattr(web.setup_mod, "start_run", lambda _c, _p: pytest.fail("ran"))
    client = TestClient(create_app(cfg, config_path, session_key="k"), base_url=BASE)
    page = client.get("/?key=k")
    client.headers["X-CSRF-Token"] = page.text.split('csrf-token" content="', 1)[1].split('"')[0]
    assert client.get("/api/bootstrap").json()["setup"]["needed"] is True
    r = client.post("/api/run", json={})
    assert r.status_code == 400 and "project" in r.json()["error"]


def test_setup_writes_need_csrf(env) -> None:  # noqa: F811
    client, *_ = env
    bare = TestClient(client.app, base_url=BASE, cookies=client.cookies)
    for method, url, body in [
        ("POST", "/api/projects/add", {"path": "/tmp"}),
        ("DELETE", "/api/projects/pdf-tool", None),
        ("POST", "/api/projects/pdf-tool/read", {}),
        ("PUT", "/api/schedule", {"time": "04:30", "nightly": False}),
        ("POST", "/api/run", {}),
    ]:
        r = bare.request(method, url, json=body)
        assert r.status_code == 403, (method, url, r.status_code)


# -- cli -------------------------------------------------------------------------------
def test_setup_command_first_launch_writes_starter_config(tmp_path: Path, monkeypatch, capsys):
    config_path = tmp_path / "home" / "config.yaml"
    monkeypatch.setattr(cli, "_port_free", lambda _port: False)  # stop before serving
    rc = cli.main(["--config", str(config_path), "setup", "--no-browser"])
    assert rc == 1
    cfg = load_config(config_path)
    assert cfg.projects == [] and cfg.sources.rss  # the starter sources, no projects yet
    out = capsys.readouterr().out
    assert "First launch" in out and "already running on port 8765" in out


def test_port_check_sees_a_listener_but_not_time_wait() -> None:
    import socket

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen()
    try:
        assert cli._port_free(port) is False
        # leave a connection behind in TIME_WAIT on the server side, then stop listening
        c = socket.create_connection(("127.0.0.1", port))
        conn, _ = srv.accept()
        conn.close()
        c.close()
    finally:
        srv.close()
    assert cli._port_free(port) is True
