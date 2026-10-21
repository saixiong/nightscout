"""`night-scout schedule`: print, install or remove the nightly job for this user.

macOS: a launchd agent. Linux: a systemd user timer (or a cron line if systemd
isn't there). Windows: Task Scheduler instructions. Printing is the default;
installing only happens with --install.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .config import Config

LABEL = "dev.nightscout.run"
UNIT = "night-scout"


def run_command(config_path: Path) -> list[str]:
    return [sys.executable, "-m", "night_scout.cli", "--config", str(config_path.resolve()), "run"]


def _env() -> dict[str, str]:
    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}
    if os.environ.get("NIGHT_SCOUT_HOME"):
        env["NIGHT_SCOUT_HOME"] = os.environ["NIGHT_SCOUT_HOME"]
    return env


# -- macOS ---------------------------------------------------------------------
def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def launchd_plist(cfg: Config, config_path: Path) -> bytes:
    hh, mm = (int(x) for x in cfg.schedule.time.split(":"))
    logs = cfg.data_path() / "logs"
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": run_command(config_path),
            # launchd runs a missed calendar job when the Mac wakes up.
            "StartCalendarInterval": {"Hour": hh, "Minute": mm},
            "EnvironmentVariables": _env(),
            "StandardOutPath": str(logs / "run.log"),
            "StandardErrorPath": str(logs / "run.log"),
            "ProcessType": "Background",
            "LowPriorityIO": True,
            "Nice": 10,
        }
    )


# -- Linux ---------------------------------------------------------------------
def systemd_units(cfg: Config, config_path: Path) -> tuple[str, str]:
    cmd = " ".join(_q(a) for a in run_command(config_path))
    env_lines = "".join(f"Environment={_q(f'{k}={v}')}\n" for k, v in _env().items())
    service = (
        "[Unit]\nDescription=Night Scout nightly run\nAfter=network-online.target\n\n"
        f"[Service]\nType=oneshot\n{env_lines}ExecStart={cmd}\nNice=10\n"
    )
    timer = (
        "[Unit]\nDescription=Night Scout nightly run\n\n"
        f"[Timer]\nOnCalendar=*-*-* {cfg.schedule.time}:00\nPersistent=true\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def cron_line(cfg: Config, config_path: Path) -> str:
    hh, mm = (int(x) for x in cfg.schedule.time.split(":"))
    cmd = " ".join(_q(a) for a in run_command(config_path))
    log = cfg.data_path() / "logs" / "run.log"
    line = f"{cmd} >> {_q(str(log))} 2>&1".replace("%", "\\%")  # cron treats % as newline
    return f"{mm} {hh} * * * {line}"


def _q(s: str) -> str:
    if s and all(c.isalnum() or c in "-_./:=@%+," for c in s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _systemd_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


# -- public ----------------------------------------------------------------------
def describe(cfg: Config, config_path: Path) -> str:
    """Human-readable plan. Home is shown as ~ (the installed job uses full paths)."""
    return _describe(cfg, config_path).replace(str(Path.home()), "~")


def _describe(cfg: Config, config_path: Path) -> str:
    t = cfg.schedule.time
    head = (
        f"Night Scout runs once a night at {t} (local time), reads your sources, and leaves a\n"
        "digest for the morning. It only proposes; nothing it suggests runs on its own.\n"
        "Ollama needs to be running at that time (the Ollama app, or `ollama serve`).\n\n"
    )
    if sys.platform == "darwin":
        return (
            head
            + f"macOS launchd agent ({launchd_plist_path()}):\n\n"
            + launchd_plist(cfg, config_path).decode()
            + "\nInstall it:  night-scout schedule --install\n"
            "Remove it:   night-scout schedule --uninstall\n"
            "If the Mac is asleep at that time, launchd runs the job when it wakes.\n"
        )
    if sys.platform.startswith("linux"):
        service, timer = systemd_units(cfg, config_path)
        return (
            head
            + f"systemd user units ({_systemd_dir()}):\n\n--- {UNIT}.service\n{service}\n"
            + f"--- {UNIT}.timer\n{timer}\n"
            + "Install them:  night-scout schedule --install\n"
            + f"No systemd? Add this to `crontab -e`:\n  {cron_line(cfg, config_path)}\n"
        )
    if sys.platform == "win32":
        hh, mm = cfg.schedule.time.split(":")
        cmd = subprocess.list2cmdline(run_command(config_path)).replace('"', '\\"')
        return (
            head
            + "Windows: create a daily task in Task Scheduler, or run in PowerShell:\n\n"
            + f'  schtasks /Create /SC DAILY /ST {hh}:{mm} /TN "Night Scout" /TR "{cmd}"\n'
        )
    return head + f"Add to cron:\n  {cron_line(cfg, config_path)}\n"


def install(cfg: Config, config_path: Path) -> str:
    (cfg.data_path() / "logs").mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        path = launchd_plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        uid = os.getuid()
        if path.exists():
            subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}", str(path)], capture_output=True, check=False
            )
        path.write_bytes(launchd_plist(cfg, config_path))
        res = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            return f"Wrote {path}, but launchctl said: {res.stderr.strip()}"
        return f"Installed. Night Scout will run nightly at {cfg.schedule.time}. ({path})"
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        d = _systemd_dir()
        d.mkdir(parents=True, exist_ok=True)
        service, timer = systemd_units(cfg, config_path)
        (d / f"{UNIT}.service").write_text(service)
        (d / f"{UNIT}.timer").write_text(timer)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        res = subprocess.run(
            ["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            return f"Wrote units to {d}, but systemctl said: {res.stderr.strip()}"
        return f"Installed systemd timer. Night Scout will run nightly at {cfg.schedule.time}."
    return (
        "Automatic install isn't supported here. Use the instructions from `night-scout schedule`."
    )


def uninstall() -> str:
    if sys.platform == "darwin":
        path = launchd_plist_path()
        if not path.exists():
            return "No launchd agent installed."
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(path)],
            capture_output=True,
            check=False,
        )
        path.unlink()
        return f"Removed {path}."
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"], check=False)
        d = _systemd_dir()
        for name in (f"{UNIT}.service", f"{UNIT}.timer"):
            (d / name).unlink(missing_ok=True)
        return "Removed the systemd timer."
    return "Nothing to remove automatically on this platform."
