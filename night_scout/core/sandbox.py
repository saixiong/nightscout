"""Try-it runs: a throwaway container if one is available, else a throwaway venv in a
temp dir. Never sudo, never a global install, never your projects. Always a timeout,
captured logs, a pass/fail against the expected output, and cleanup afterwards.

Only ever called after the user clicks Run in sandbox and confirms.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import SandboxConfig
from .safety import check_commands

SMOKE_MARK = "=== night-scout smoke test ==="
MAX_LOG = 20_000
NODE_IMAGE = "node:22-slim"


@dataclass
class Plan:
    runtime: str  # python | node
    setup: list[str]
    smoke_test: list[str]
    expected_output: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Plan:
        return cls(
            runtime=str(d.get("runtime", "python")),
            setup=[str(c) for c in d.get("setup") or []],
            smoke_test=[str(c) for c in d.get("smoke_test") or []],
            expected_output=str(d.get("expected_output") or ""),
        )

    def commands(self) -> list[str]:
        return [*self.setup, *self.smoke_test]


@dataclass
class Result:
    mode: str
    passed: bool
    exit_code: int | None
    timed_out: bool
    seconds: float
    reason: str
    log: str = ""
    commands: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def container_engine() -> str | None:
    """docker or podman, if the CLI exists and its daemon answers."""
    for engine in ("docker", "podman"):
        path = shutil.which(engine)
        if not path:
            continue
        try:
            ok = subprocess.run(
                [path, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if ok.returncode == 0:
            return path
    return None


def choose_mode(cfg: SandboxConfig) -> tuple[str, str | None]:
    """Returns (mode, engine). mode is 'container' or 'venv'."""
    if cfg.prefer in ("auto", "container"):
        engine = container_engine()
        if engine:
            return "container", engine
        if cfg.prefer == "container":
            raise RuntimeError("sandbox.prefer is 'container' but no Docker/Podman daemon answers")
    return "venv", None


def build_script(plan: Plan, mode: str) -> str:
    """A POSIX sh script: set up the runtime, run each step visibly, stop on failure."""
    lines = ["set -eu", 'echo "+ sandbox: $(pwd)"']
    if plan.runtime == "python":
        if mode == "container":
            lines += ["python -m venv .venv", ". .venv/bin/activate"]
        lines += ["python --version", "pip --version | cut -d' ' -f1-2"]
    else:
        lines += ["node --version", "npm --version", "npm init -y >/dev/null"]
    for cmd in plan.setup:
        lines += [f"echo {_sh_quote('+ ' + cmd)}", cmd]
    lines.append(f"echo {_sh_quote(SMOKE_MARK)}")
    for cmd in plan.smoke_test:
        lines += [f"echo {_sh_quote('+ ' + cmd)}", cmd]
    return "\n".join(lines) + "\n"


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def container_args(engine: str, image: str, script: Path, name: str, timeout_s: int) -> list[str]:
    """No host mounts except the read-only script; no capabilities; capped resources."""
    return [
        engine,
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "bridge",  # installs need the network; nothing else is shared
        "--memory",
        "2g",
        "--cpus",
        "2",
        "--pids-limit",
        "512",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/work:rw,exec,size=2g",
        "--tmpfs",
        "/tmp:rw,exec,size=512m",
        "-e",
        "HOME=/work",
        "-e",
        "PIP_DISABLE_PIP_VERSION_CHECK=1",
        "-e",
        "npm_config_update_notifier=false",
        "-w",
        "/work",
        "-v",
        f"{script}:/night-scout-run.sh:ro",
        "--label",
        "night-scout=try-it",
        "--stop-timeout",
        "5",
        image,
        "timeout",
        str(timeout_s),
        "sh",
        "/night-scout-run.sh",
    ]


def venv_env(workdir: Path, venv_bin: Path | None) -> dict[str, str]:
    """A clean environment: no inherited tokens or keys, HOME pointed into the sandbox."""
    path_parts = [str(venv_bin)] if venv_bin else []
    for tool in ("node", "npm", "git"):
        found = shutil.which(tool)
        if found:
            path_parts.append(str(Path(found).parent))
    path_parts += ["/usr/bin", "/bin"]
    env = {
        "PATH": os.pathsep.join(dict.fromkeys(path_parts)),
        "HOME": str(workdir),
        "TMPDIR": str(workdir / ".tmp"),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_CACHE_DIR": str(workdir / ".pip-cache"),
        "npm_config_cache": str(workdir / ".npm"),
        "npm_config_update_notifier": "false",
        "PYTHONNOUSERSITE": "1",
    }
    if venv_bin:
        env["VIRTUAL_ENV"] = str(venv_bin.parent)
    return env


def macos_profile(workdir: Path, readable: list[Path]) -> str:
    """Seatbelt profile: no reads of home or other volumes, no writes outside the sandbox."""
    home = Path.home().resolve()
    allow_read = " ".join(f'(subpath "{p}")' for p in [workdir, *readable])
    return "\n".join(
        [
            "(version 1)",
            "(allow default)",
            f'(deny file-read* (subpath "{home}") (subpath "/Volumes"))',
            f"(allow file-read* {allow_read})" if allow_read else "",
            "(deny file-write*)",
            f'(allow file-write* (subpath "{workdir}") (literal "/dev/null") '
            '(literal "/dev/tty") (regex #"^/dev/fd/"))',
        ]
    )


def _readable_toolchains() -> list[Path]:
    """Interpreter and tool prefixes the run must read even if they live under home."""
    home = Path.home().resolve()
    paths = {Path(sys.base_prefix).resolve()}
    for tool in ("node", "npm", "git"):
        found = shutil.which(tool)
        if found:
            paths.add(Path(found).resolve().parent.parent)
    # Never let an allowance swallow the whole home directory (e.g. a tool in ~/bin).
    return sorted(p for p in paths if p != home and p not in home.parents)


def judge(output: str, exit_code: int | None, timed_out: bool, expected: str) -> tuple[bool, str]:
    if timed_out:
        return False, "timed out"
    if exit_code != 0:
        return False, f"a step failed (exit code {exit_code})"
    smoke = output.split(SMOKE_MARK, 1)[1] if SMOKE_MARK in output else ""
    if not expected.strip():
        return True, "all steps exited 0 (no expected output was given)"
    if expected.strip().lower() in smoke.lower():
        return True, f"saw the expected output “{expected.strip()}”"
    return False, f"steps ran, but the expected output “{expected.strip()}” never appeared"


def _clip_log(text: str) -> str:
    if len(text) <= MAX_LOG:
        return text
    half = MAX_LOG // 2
    return text[:half] + "\n… (log trimmed) …\n" + text[-half:]


def run_plan(plan: Plan, cfg: SandboxConfig, scratch_root: Path | None = None) -> Result:
    verdict = check_commands(plan.commands())
    if not verdict.ok:
        return Result("none", False, None, False, 0.0, verdict.explain(), "", plan.commands())
    if plan.runtime not in ("python", "node"):
        return Result("none", False, None, False, 0.0, f"unknown runtime {plan.runtime!r}")
    try:
        mode, engine = choose_mode(cfg)
    except RuntimeError as e:
        return Result("none", False, None, False, 0.0, str(e))
    if mode == "venv" and sys.platform == "win32":
        return Result(
            "none",
            False,
            None,
            False,
            0.0,
            "venv sandbox needs a POSIX shell; install Docker or use WSL on Windows",
        )
    if mode == "venv" and plan.runtime == "node" and not shutil.which("npm"):
        return Result("venv", False, None, False, 0.0, "node runtime needs npm on PATH")

    scratch_root = scratch_root or Path(tempfile.gettempdir())
    scratch_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="night-scout-try-", dir=scratch_root))
    name = f"night-scout-try-{uuid.uuid4().hex[:10]}"
    start = time.monotonic()
    try:
        script = workdir / "run.sh"
        script.write_text(build_script(plan, mode), encoding="utf-8")
        if mode == "container":
            assert engine is not None
            image = cfg.container_image if plan.runtime == "python" else NODE_IMAGE
            args = container_args(engine, image, script, name, cfg.timeout_s)
            # The container's own `timeout` stops the work; give docker itself a margin
            # (image pulls) before we kill the client and remove the container.
            output, code, timed_out = _run(args, None, None, cfg.timeout_s + 180)
            if timed_out and engine:
                subprocess.run([engine, "rm", "-f", name], capture_output=True, check=False)
            if code == 124:  # GNU timeout's exit code inside the container
                timed_out = True
        else:
            run_dir = workdir / "work"
            run_dir.mkdir()
            (workdir / ".tmp").mkdir()
            venv_bin = None
            if plan.runtime == "python":
                # The base interpreter, not ours: our venv may live under home, which the
                # macOS profile below makes unreadable.
                base_python = getattr(sys, "_base_executable", sys.executable)
                subprocess.run(
                    [base_python, "-m", "venv", str(workdir / ".venv")],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
                venv_bin = workdir / ".venv" / "bin"
            env = venv_env(workdir, venv_bin)
            args = ["/bin/sh", str(script)]
            seatbelt = shutil.which("sandbox-exec") if sys.platform == "darwin" else None
            if seatbelt:
                real = workdir.resolve()
                profile = workdir / "profile.sb"
                profile.write_text(macos_profile(real, _readable_toolchains()), encoding="utf-8")
                args = [seatbelt, "-f", str(profile), *args]
                mode = "venv + macOS sandbox"
            output, code, timed_out = _run(args, run_dir, env, cfg.timeout_s)
        passed, reason = judge(output, code, timed_out, plan.expected_output)
        return Result(
            mode,
            passed,
            code,
            timed_out,
            round(time.monotonic() - start, 1),
            reason,
            _clip_log(output),
            plan.commands(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        return Result(
            mode,
            False,
            None,
            False,
            round(time.monotonic() - start, 1),
            f"sandbox error: {type(e).__name__}: {e}",
            "",
            plan.commands(),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _run(
    args: list[str], cwd: Path | None, env: dict[str, str] | None, timeout: int
) -> tuple[str, int | None, bool]:
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # its own process group, so a timeout kills everything
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        return out.decode("utf-8", "replace"), proc.returncode, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        out, _ = proc.communicate()
        return out.decode("utf-8", "replace") + "\n[night-scout] timed out, killed\n", None, True
