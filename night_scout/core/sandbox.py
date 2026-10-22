"""Try-it runs: a throwaway container if one is available, else a throwaway venv in a
temp dir. Never sudo, never a global install, never your projects. Always a timeout,
captured logs, a pass/fail against the expected output, and cleanup afterwards.

Only ever called after the user clicks Run in sandbox and confirms.
"""

from __future__ import annotations

import os
import re
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


TRACE = "[night-scout] "
# Only the shell's own complaints, e.g. "bash: line 7: foo: command not found" or
# "sh: 1: foo: not found"; a tool's output that merely contains "not found" still counts.
_SHELL_ERROR = re.compile(
    r"^(?:/\S+/)?(?:ba|z|da)?sh: (?:line \d+: |\d+: )?\S.*: "
    r"(?:command not found|not found|No such file or directory|Permission denied)$"
)


def pick_shell(mode: str) -> str:
    """bash when we can count on it, else POSIX sh."""
    if mode == "container":
        return "bash"  # both default images ship bash
    return "/bin/bash" if Path("/bin/bash").exists() else "/bin/sh"


def build_script(plan: Plan, mode: str, shell: str = "bash") -> str:
    """Set up the runtime, run each step visibly, stop on the first failure."""
    # No pipefail: `tool | head -1` would fail on SIGPIPE. A missing tool is caught by
    # judge(), which ignores the shell's "not found" errors when looking for the marker.
    lines = ["set -eu", f'echo "{TRACE}sandbox ready: $(basename "$(pwd)")"']
    if plan.runtime == "python":
        if mode == "container":
            lines += ["python -m venv .venv", ". .venv/bin/activate"]
        lines += ["python --version", "pip --version | cut -d' ' -f1-2"]
    else:
        lines += ["node --version", "npm --version", "npm init -y >/dev/null"]
    for cmd in plan.setup:
        lines += [f"echo {_sh_quote(TRACE + '$ ' + cmd)}", cmd]
    lines.append(f"echo {_sh_quote(SMOKE_MARK)}")
    for cmd in plan.smoke_test:
        lines += [f"echo {_sh_quote(TRACE + '$ ' + cmd)}", cmd]
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
        "bash",
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


def macos_profile(workdir: Path, readable: list[Path], private: list[Path] | None = None) -> str:
    """Seatbelt profile for the venv sandbox on macOS.

    No reads of home, other volumes or your project folders; no writes outside the
    sandbox; no talking to services on this machine (the Night Scout UI, Ollama,
    Docker's socket); no clipboard; no signals to your other processes; no launching
    apps, LaunchServices, Apple Events, launchctl or the keychain tool.
    """
    home = Path.home().resolve()
    allow_read = " ".join(f'(subpath "{p}")' for p in [workdir, *readable])
    hidden = " ".join(f'(subpath "{p}")' for p in [home, Path("/Volumes"), *(private or [])])
    blocked_tools = " ".join(
        f'(literal "{t}")'
        for t in (
            "/bin/launchctl",
            "/usr/bin/osascript",
            "/usr/bin/open",
            "/usr/bin/security",
            "/usr/bin/pbpaste",
            "/usr/bin/pbcopy",
            "/usr/sbin/screencapture",
        )
    )
    return "\n".join(
        [
            "(version 1)",
            "(allow default)",
            f"(deny file-read* {hidden})",
            f"(allow file-read* {allow_read})",
            "(deny file-write*)",
            f'(allow file-write* (subpath "{workdir}") (literal "/dev/null") '
            '(literal "/dev/tty") (regex #"^/dev/fd/"))',
            '(deny network-outbound (remote ip "localhost:*"))',
            "(deny network-outbound (remote unix-socket))",
            # ...except the system DNS resolver, or nothing can be installed.
            "(allow network-outbound (remote unix-socket (path-literal "
            '"/private/var/run/mDNSResponder")))',
            '(deny mach-lookup (global-name "com.apple.coreservices.launchservicesd") '
            '(global-name "com.apple.coreservices.appleevents") '
            '(global-name "com.apple.pasteboard.1"))',
            "(deny signal)",
            "(allow signal (target same-sandbox))",
            f"(deny process-exec {blocked_tools})",
        ]
    )


def bwrap_args(
    bwrap: str, workdir: Path, readable: list[Path], private: list[Path] | None = None
) -> list[str]:
    """Linux: read-only system, empty home, /tmp and /run (no D-Bus, ssh-agent or
    docker.sock), only the sandbox dir writable, no capabilities.

    The network stays shared (installs need it), so loopback services are reachable;
    the web UI's session key keeps the sandbox from driving Night Scout itself.
    """
    args = [
        bwrap,
        "--ro-bind", "/", "/",
        "--tmpfs", str(Path.home()),
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
        "--tmpfs", "/mnt",
        "--tmpfs", "/media",
        "--dev", "/dev",
        "--proc", "/proc",
    ]  # fmt: skip
    resolv = Path("/etc/resolv.conf")
    if resolv.is_symlink():  # often points into /run, which is now empty
        target = resolv.resolve()
        args += ["--ro-bind-try", str(target), str(target)]
    for p in private or []:
        args += ["--tmpfs", str(p)]
    for p in readable:
        args += ["--ro-bind", str(p), str(p)]
    args += [
        "--bind", str(workdir), str(workdir),
        "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--cap-drop", "ALL",
        "--die-with-parent", "--new-session",
    ]  # fmt: skip
    return args


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


def _existing(paths: list[Path] | None) -> list[Path]:
    return [p.resolve() for p in paths or [] if p.exists()]


def judge(output: str, exit_code: int | None, timed_out: bool, expected: str) -> tuple[bool, str]:
    if timed_out:
        return False, "timed out"
    if exit_code != 0:
        return False, f"a step failed (exit code {exit_code})"
    smoke = output.split(SMOKE_MARK, 1)[1] if SMOKE_MARK in output else ""
    # Our own echoed commands, and the shell complaining a command doesn't exist, must
    # never count as the tool's output.
    smoke = "\n".join(
        line
        for line in smoke.splitlines()
        if not line.startswith(TRACE) and not _SHELL_ERROR.search(line)
    )
    if not expected.strip():
        return True, "all steps exited 0 (no expected output was given)"
    if expected.strip().lower() in smoke.lower():
        return True, f"saw the expected output “{expected.strip()}”"
    return False, f"steps ran, but the expected output “{expected.strip()}” never appeared"


def scrub_paths(text: str, workdir: Path) -> str:
    """Logs get shared and screenshotted: show the sandbox as <sandbox> and home as ~."""
    for p in sorted({str(workdir.resolve()), str(workdir)}, key=len, reverse=True):
        text = text.replace(p, "<sandbox>")
    home = str(Path.home())
    return text.replace(home, "~")


def _clip_log(text: str) -> str:
    if len(text) <= MAX_LOG:
        return text
    half = MAX_LOG // 2
    return text[:half] + "\n… (log trimmed) …\n" + text[-half:]


def run_plan(
    plan: Plan,
    cfg: SandboxConfig,
    scratch_root: Path | None = None,
    private: list[Path] | None = None,
) -> Result:
    """Run a plan in the sandbox. `private` lists extra paths (your projects, Night
    Scout's data dir) to hide from the venv fences in case they live outside home."""
    verdict = check_commands(plan.commands(), smoke=plan.smoke_test)
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
        script.write_text(build_script(plan, mode, pick_shell(mode)), encoding="utf-8")
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
            args = [pick_shell("venv"), str(script)]
            real = workdir.resolve()
            seatbelt = shutil.which("sandbox-exec") if sys.platform == "darwin" else None
            bwrap = shutil.which("bwrap") if sys.platform.startswith("linux") else None
            if seatbelt:
                profile = workdir / "profile.sb"
                profile.write_text(
                    macos_profile(real, _readable_toolchains(), _existing(private)),
                    encoding="utf-8",
                )
                args = [seatbelt, "-f", str(profile), *args]
                mode = "venv + macOS sandbox"
            elif bwrap:
                args = [*bwrap_args(bwrap, real, _readable_toolchains(), _existing(private)), *args]
                mode = "venv + bubblewrap"
            elif not cfg.allow_unfenced_venv:
                return Result(
                    "none",
                    False,
                    None,
                    False,
                    0.0,
                    "No container engine and no OS fence (sandbox-exec or bwrap) here, so a "
                    "venv run could read your files. Start Docker/Podman, install bubblewrap, "
                    "or set sandbox.allow_unfenced_venv: true to accept that risk.",
                )
            output, code, timed_out = _run(args, run_dir, env, cfg.timeout_s)
        output = scrub_paths(output, workdir)
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
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    try:
        # Bounded: a process that escaped the group (setsid, --daemon) may still hold
        # the pipe open; don't wait on it forever.
        out, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        if proc.stdout:
            proc.stdout.close()
        proc.wait(timeout=5)
        out = b""
    return out.decode("utf-8", "replace") + "\n[night-scout] timed out, killed\n", None, True
