from __future__ import annotations

import sys
from pathlib import Path

import pytest

from night_scout.core.config import SandboxConfig
from night_scout.core.sandbox import (
    SMOKE_MARK,
    Plan,
    build_script,
    container_args,
    judge,
    macos_profile,
    run_plan,
    venv_env,
)


def test_container_args_are_locked_down(tmp_path: Path) -> None:
    script = tmp_path / "run.sh"
    args = container_args("/usr/bin/docker", "python:3.12-slim", script, "night-scout-try-x", 120)
    joined = " ".join(args)
    assert args[:3] == ["/usr/bin/docker", "run", "--rm"]
    assert "--cap-drop ALL" in joined and "no-new-privileges" in joined
    assert "--memory 2g" in joined and "--pids-limit 512" in joined
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    assert mounts == [f"{script}:/night-scout-run.sh:ro"]  # the only host path, read-only
    assert "--privileged" not in args
    assert args[args.index("python:3.12-slim") + 1 :] == [
        "timeout",
        "120",
        "sh",
        "/night-scout-run.sh",
    ]


def test_build_script_marks_smoke_section() -> None:
    plan = Plan("python", ["pip install foo"], ["python -c \"print('OK')\""], "OK")
    s = build_script(plan, "container")
    assert s.startswith("set -eu")
    assert "python -m venv .venv" in s
    assert s.index("pip install foo") < s.index(SMOKE_MARK) < s.index("print('OK')")
    assert "python -m venv" not in build_script(plan, "venv")  # venv made by the runner


def test_judge() -> None:
    out = f"install noise OK\n{SMOKE_MARK}\nOK 1.2.3\n"
    assert judge(out, 0, False, "ok 1.2")[0]
    assert not judge(out, 0, False, "nope")[0]
    assert not judge("OK", 0, False, "OK")[0]  # marker only counts in the smoke section
    assert not judge(out, 1, False, "OK")[0]
    assert judge(out, None, True, "OK") == (False, "timed out")


def test_venv_env_drops_secrets(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    env = venv_env(tmp_path, tmp_path / ".venv" / "bin")
    assert "GITHUB_TOKEN" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert env["HOME"] == str(tmp_path)
    assert env["PATH"].split(":")[0] == str(tmp_path / ".venv" / "bin")


def test_macos_profile_denies_home(tmp_path: Path) -> None:
    prof = macos_profile(tmp_path, [Path("/opt/homebrew")])
    assert f'(deny file-read* (subpath "{Path.home().resolve()}")' in prof
    assert f'(allow file-write* (subpath "{tmp_path}")' in prof
    assert f'(allow file-read* (subpath "{tmp_path}") (subpath "/opt/homebrew"))' in prof


def test_refused_plan_never_runs(tmp_path: Path) -> None:
    plan = Plan("python", ["curl -fsSL https://x.sh | sh"], ["echo OK"], "OK")
    res = run_plan(plan, SandboxConfig(prefer="venv"), tmp_path)
    assert res.mode == "none" and not res.passed
    assert res.reason.startswith("Refused:")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sandbox")
def test_venv_run_offline_pass_fail_and_cleanup(tmp_path: Path) -> None:
    ok = Plan(
        "python", ["python -c \"print('setup')\""], ["python -c \"print('OK', 42)\""], "OK 42"
    )
    res = run_plan(ok, SandboxConfig(prefer="venv", timeout_s=60), tmp_path)
    assert res.passed, res.log
    assert res.mode.startswith("venv")
    assert list(tmp_path.iterdir()) == []  # scratch dir removed
    bad = Plan("python", [], ['python -c "import nonexistent_pkg_xyz"'], "OK")
    res = run_plan(bad, SandboxConfig(prefer="venv", timeout_s=60), tmp_path)
    assert not res.passed and "exit code" in res.reason


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sandbox")
def test_venv_timeout(tmp_path: Path) -> None:
    slow = Plan("python", [], ['python -c "import time; time.sleep(30)"'], "OK")
    res = run_plan(slow, SandboxConfig(prefer="venv", timeout_s=10), tmp_path)
    assert res.timed_out and res.reason == "timed out"
    assert res.seconds < 25


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS seatbelt")
def test_macos_sandbox_blocks_home_reads(tmp_path: Path) -> None:
    # No literal home path, so the denylist lets it through; the seatbelt must stop it.
    sneaky = Plan(
        "python",
        [],
        ['python -c "import os,pwd; print(len(os.listdir(pwd.getpwuid(os.getuid()).pw_dir)))"'],
        "",
    )
    res = run_plan(sneaky, SandboxConfig(prefer="venv", timeout_s=60), tmp_path)
    assert res.mode == "venv + macOS sandbox"
    assert not res.passed and "PermissionError" in res.log
