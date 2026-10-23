"""Choose folder… (Setup → Projects): the system dialog, and the same-machine rule for it."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from night_scout.core import folderpick
from night_scout.core.config import Config, save_config
from night_scout.web import app as web
from night_scout.web.app import create_app

from .test_web import BASE

FINDER = folderpick.Picker("finder", "Finder", ("osascript",))
ZENITY = folderpick.Picker("zenity", "a folder picker", ("zenity",))


def _run(stdout: str = "", stderr: str = "", code: int = 0, seen: list | None = None):
    def fake(argv, **kw):
        if seen is not None:
            seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    return fake


def test_finder_returns_the_chosen_folder(tmp_path: Path, monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr(subprocess, "run", _run(f"{tmp_path}/\n", seen=seen))
    assert folderpick.choose(str(tmp_path), FINDER) == str(tmp_path.resolve())
    argv, kw = seen[0]
    assert argv[:3] == ["osascript", "-e", "activate"]
    assert "choose folder" in argv[4] and f'POSIX file "{tmp_path}"' in argv[4]
    assert kw["timeout"] == folderpick.TIMEOUT and kw["stdin"] is subprocess.DEVNULL


def test_cancel_is_not_an_error(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", _run(stderr="execution error: User canceled. (-128)", code=1)
    )
    assert folderpick.choose(None, FINDER) is None
    monkeypatch.setattr(subprocess, "run", _run(code=1))
    assert folderpick.choose(None, ZENITY) is None


def test_a_dialog_closed_from_outside_counts_as_cancelled(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run", _run(code=-15))
    assert folderpick.choose(None, FINDER) is None


def test_other_failures_are_reported(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", _run(stderr="Not authorized to send Apple events. (-1743)", code=1)
    )
    with pytest.raises(folderpick.PickerError, match=r"Finder failed.*-1743"):
        folderpick.choose(None, FINDER)


def test_a_non_folder_answer_is_refused(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x")
    monkeypatch.setattr(subprocess, "run", _run(str(f)))
    with pytest.raises(folderpick.PickerError, match="isn't a folder"):
        folderpick.choose(None, FINDER)


def test_a_dialog_left_open_counts_as_cancelled(monkeypatch) -> None:
    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(subprocess, "run", slow)
    assert folderpick.choose(None, FINDER) is None
    assert not folderpick.busy()


def test_one_dialog_at_a_time(tmp_path: Path, monkeypatch) -> None:
    gate, inside = threading.Event(), threading.Event()

    def held(argv, **kw):
        inside.set()
        gate.wait(5)
        return subprocess.CompletedProcess(argv, 0, str(tmp_path), "")

    monkeypatch.setattr(subprocess, "run", held)
    t = threading.Thread(target=folderpick.choose, args=(None, FINDER))
    t.start()
    assert inside.wait(5)
    with pytest.raises(folderpick.PickerError, match="already open"):
        folderpick.choose(None, FINDER)
    gate.set()
    t.join(5)
    assert not folderpick.busy()


def test_start_folder_falls_back_to_home(monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr(subprocess, "run", _run(code=1, stderr="(-128)", seen=seen))
    folderpick.choose("/no/such/folder", FINDER)
    assert f'POSIX file "{Path.home()}"' in seen[0][0][4]


def test_applescript_quotes_are_escaped() -> None:
    argv, _ = folderpick._argv(FINDER, '/Users/x/my "odd" \\ folder')
    assert 'POSIX file "/Users/x/my \\"odd\\" \\\\ folder"' in argv[4]


def test_windows_uses_tk_then_powershell(monkeypatch) -> None:
    monkeypatch.setattr(folderpick.sys, "platform", "win32")
    monkeypatch.setattr(folderpick, "_has_tk", lambda: True)
    p = folderpick.find()
    assert p and p.kind == "explorer" and p.label == "File Explorer" and p.cmd[-1] == folderpick._TK
    argv, env = folderpick._argv(p, "C:\\Users\\me")
    assert argv[-2:] == [folderpick.PROMPT, "C:\\Users\\me"] and env is None
    monkeypatch.setattr(folderpick, "_has_tk", lambda: False)
    monkeypatch.setattr(
        folderpick.shutil, "which", lambda n: "C:\\ps.exe" if n == "powershell" else None
    )
    p = folderpick.find()
    assert p and p.cmd[:3] == ("C:\\ps.exe", "-NoProfile", "-STA")
    argv, env = folderpick._argv(p, "C:\\Users\\me")
    assert env and env["NS_START"] == "C:\\Users\\me" and env["NS_PROMPT"] == folderpick.PROMPT


def test_linux_needs_a_display_and_a_picker(monkeypatch) -> None:
    monkeypatch.setattr(folderpick.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(folderpick.shutil, "which", lambda n: f"/usr/bin/{n}")
    assert folderpick.find() is None  # a server with no screen
    monkeypatch.setenv("DISPLAY", ":0")
    p = folderpick.find()
    assert p and p.kind == "zenity"
    monkeypatch.setattr(folderpick.shutil, "which", lambda n: None)
    assert folderpick.find() is None


# -- routes ---------------------------------------------------------------------------------
def _client(
    tmp_path: Path, peer: str = "127.0.0.1", base: str = BASE, hosts: tuple = ()
) -> TestClient:
    cfg = Config(data_dir=str(tmp_path / "data"))
    cfg.data_path().mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.yaml"
    save_config(cfg, config_path)
    app = create_app(cfg, config_path, session_key="k", extra_hosts=hosts)
    c = TestClient(app, base_url=base, client=(peer, 50123))
    page = c.get("/?key=k")
    c.headers["X-CSRF-Token"] = page.text.split('csrf-token" content="', 1)[1].split('"')[0]
    return c


@pytest.fixture
def finder(monkeypatch):
    monkeypatch.setattr(web.folderpick, "find", lambda: FINDER)


def test_setup_says_the_picker_is_available_here(tmp_path: Path, finder) -> None:
    assert _client(tmp_path).get("/api/setup").json()["picker"] == {
        "available": True,
        "label": "Finder",
    }


def test_pick_returns_the_folder_then_add_works(tmp_path: Path, finder, monkeypatch) -> None:
    proj = tmp_path / "my-app"
    proj.mkdir()
    (proj / "README.md").write_text("# my-app")
    monkeypatch.setattr(web.folderpick, "choose", lambda start=None: str(proj))
    c = _client(tmp_path)
    r = c.post("/api/projects/pick")
    assert r.status_code == 200 and r.json() == {"path": str(proj)}
    assert c.post("/api/projects/add", json={"path": r.json()["path"]}).json()["ok"] is True


def test_pick_cancelled(tmp_path: Path, finder, monkeypatch) -> None:
    monkeypatch.setattr(web.folderpick, "choose", lambda start=None: None)
    assert _client(tmp_path).post("/api/projects/pick").json() == {"path": None}


def test_pick_already_open_is_409(tmp_path: Path, finder, monkeypatch) -> None:
    def busy(start=None):
        raise folderpick.PickerError("A folder picker is already open. Finish with that one first.")

    monkeypatch.setattr(web.folderpick, "choose", busy)
    r = _client(tmp_path).post("/api/projects/pick")
    assert r.status_code == 409 and "already open" in r.json()["error"]


@pytest.mark.parametrize(
    "how",
    ["tunnel host", "forwarded header", "cf header", "remote peer"],
)
def test_never_opens_for_another_device(tmp_path: Path, finder, monkeypatch, how: str) -> None:
    """A Finder window must never pop up on this Mac because of a request from elsewhere."""
    monkeypatch.setattr(web.folderpick, "choose", lambda start=None: pytest.fail("dialog opened"))
    if how == "tunnel host":
        c = _client(
            tmp_path, base="https://abc.trycloudflare.com", hosts=("abc.trycloudflare.com",)
        )
    elif how == "remote peer":
        c = _client(tmp_path, peer="192.168.1.50")
    else:
        c = _client(tmp_path)
        c.headers["X-Forwarded-For" if how == "forwarded header" else "CF-Connecting-IP"] = (
            "203.0.113.9"
        )
    assert c.get("/api/setup").json()["picker"] == {"available": False, "reason": "remote"}
    r = c.post("/api/projects/pick")
    assert r.status_code == 403 and "computer running Night Scout" in r.json()["error"]


def test_no_picker_on_this_computer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web.folderpick, "find", lambda: None)
    assert _client(tmp_path).get("/api/setup").json()["picker"] == {
        "available": False,
        "reason": "none",
    }


def test_pick_needs_csrf(tmp_path: Path, finder, monkeypatch) -> None:
    monkeypatch.setattr(web.folderpick, "choose", lambda start=None: pytest.fail("dialog opened"))
    c = _client(tmp_path)
    del c.headers["X-CSRF-Token"]
    assert c.post("/api/projects/pick").status_code == 403
