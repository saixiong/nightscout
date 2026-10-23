"""The setup walkthrough's backend: project folders, profile entries, first-run checks, run now.

Everything here works on config.yaml and profile.yaml the same way the CLI does; the web
routes only add locking and HTTP errors on top.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import yaml

from .config import Config, ProjectRef
from .profile import append_profile_entry, clean_topics
from .schedule import run_command

# Files that tell us a folder is a project (and that Gemma has something to read).
MARKERS = (
    "README.md",
    "README.rst",
    "README.txt",
    "README",
    "readme.md",
    "pyproject.toml",
    "setup.py",
    "requirements.txt",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
    "pom.xml",
    "build.gradle",
    "composer.json",
    "Package.swift",
)
# Where setup looks for project folders to suggest. NIGHT_SCOUT_PROJECT_DIRS (os.pathsep
# separated) replaces the list, which keeps tests and demos away from the real home folder.
PROJECT_DIRS = ("~/projects", "~/code", "~/src", "~/dev", "~/Developer", "~/GitHub")
SUGGEST_LIMIT = 12

PROFILE_LOCK = threading.RLock()  # profile.yaml writers from setup run one at a time


def project_files(root: Path) -> list[str]:
    """The marker files found in a folder, README first, each listed once."""
    found: list[str] = []
    seen: set[str] = set()
    for m in MARKERS:
        # macOS and Windows filesystems ignore case, so README.md also answers readme.md
        if m.lower() not in seen and (root / m).is_file():
            found.append(m)
            seen.add(m.lower())
    return found


def project_name(ref: ProjectRef) -> str:
    return ref.name or ref.resolved().name


def _known_paths(cfg: Config) -> set[Path]:
    out = set()
    for ref in cfg.projects:
        try:
            out.add(ref.resolved().resolve())
        except OSError:
            continue
    return out


def suggest_projects(cfg: Config) -> list[dict[str, Any]]:
    """Folders under the usual project directories that look like projects and aren't added."""
    env = os.environ.get("NIGHT_SCOUT_PROJECT_DIRS")
    bases = env.split(os.pathsep) if env else list(PROJECT_DIRS)
    known = _known_paths(cfg)
    names = {project_name(r) for r in cfg.projects}
    out: list[dict[str, Any]] = []
    for base in bases:
        root = Path(base).expanduser()
        if not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for child in children:
            if child.name.startswith(".") or not child.is_dir():
                continue
            try:
                if child.resolve() in known or child.name in names:
                    continue
                found = project_files(child)
            except OSError:
                continue
            if found:
                out.append({"name": child.name, "path": str(child), "found": found})
            if len(out) >= SUGGEST_LIMIT:
                return out
    return out


def add_project(cfg: Config, path: str) -> tuple[ProjectRef, list[str]]:
    """Validate a folder and append it to cfg.projects. Raises ValueError with a plain reason."""
    raw = path.strip()
    if not raw:
        raise ValueError("Type the folder's path, like ~/projects/my-app.")
    if len(raw) > 1000 or "\x00" in raw:
        raise ValueError("That path doesn't look right.")
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise ValueError("Use a full path, like ~/projects/my-app.")
    if not root.is_dir():
        raise ValueError(f"No folder at {raw}.")
    found = project_files(root)
    if not found:
        raise ValueError(
            "That folder has no README or project manifest (pyproject.toml, package.json, …), "
            "so Gemma would have nothing to read."
        )
    real = root.resolve()
    if real in _known_paths(cfg):
        raise ValueError("That folder is already one of your projects.")
    if root.name in {project_name(r) for r in cfg.projects}:
        raise ValueError(f"You already have a project named {root.name!r}.")
    ref = ProjectRef(path=str(root))
    cfg.projects.append(ref)
    return ref, found


def remove_project(cfg: Config, name: str) -> ProjectRef:
    """Drop a project from cfg.projects (the folder itself is never touched)."""
    for i, ref in enumerate(cfg.projects):
        if project_name(ref) == name:
            return cfg.projects.pop(i)
    raise KeyError(name)


def find_project(cfg: Config, name: str) -> ProjectRef:
    for ref in cfg.projects:
        if project_name(ref) == name:
            return ref
    raise KeyError(name)


# -- profile.yaml entries ----------------------------------------------------------------
def _read_profile(path: Path) -> tuple[str, dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict) or not isinstance(data.get("projects", []), list):
        raise ValueError(f"{path} isn't a profile (no `projects:` list); fix it first.")
    return text, data


def _write_profile(path: Path, text: str, data: dict[str, Any]) -> None:
    header = ""
    for line in text.splitlines():  # keep the comment header at the top of the file
        if not line.startswith("#"):
            break
        header += line + "\n"
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(
        header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    tmp.replace(path)


def upsert_profile_entry(path: Path, entry: dict[str, Any]) -> None:
    """Replace the project's entry in profile.yaml, or add it if it isn't there yet."""
    with PROFILE_LOCK:
        if not path.exists():
            append_profile_entry(path, entry)
            return
        text, data = _read_profile(path)
        projects = data.setdefault("projects", [])
        for i, p in enumerate(projects):
            if isinstance(p, dict) and p.get("name") == entry["name"]:
                projects[i] = entry
                _write_profile(path, text, data)
                return
        append_profile_entry(path, entry)


def remove_profile_entry(path: Path, name: str) -> bool:
    with PROFILE_LOCK:
        if not path.exists():
            return False
        text, data = _read_profile(path)
        projects = data.get("projects") or []
        keep = [p for p in projects if not (isinstance(p, dict) and p.get("name") == name)]
        if len(keep) == len(projects):
            return False
        data["projects"] = keep
        _write_profile(path, text, data)
        return True


def profiled_names(path: Path) -> set[str]:
    try:
        _, data = _read_profile(path)
    except (FileNotFoundError, ValueError, yaml.YAMLError, OSError):
        return set()
    return {
        str(p["name"]) for p in data.get("projects") or [] if isinstance(p, dict) and "name" in p
    }


def profile_topics(path: Path) -> list[str]:
    try:
        _, data = _read_profile(path)
    except (FileNotFoundError, ValueError, yaml.YAMLError, OSError):
        return []
    return clean_topics(data.get("topics"))


def setup_status(cfg: Config) -> dict[str, Any]:
    """Does this install still need the walkthrough? It has something to scout for once it has
    a typed topic or a profiled project (the Digest works without projects)."""
    reasons: list[str] = []
    topics = profile_topics(cfg.profile_file())
    profiled = profiled_names(cfg.profile_file())
    has_project = any(project_name(r) in profiled for r in cfg.projects)
    if not topics and not cfg.projects:
        reasons.append("nothing_to_scout")
    elif not topics and not has_project:
        reasons.append("no_profile")
    return {"needed": bool(reasons), "reasons": reasons, "topics": topics}


# -- run now ------------------------------------------------------------------------------
def start_run(cfg: Config, config_path: Path, digest_only: bool = False) -> int:
    """Start one `night-scout run` in the background, detached from the web server.

    Same command the nightly schedule uses; output goes to <data>/logs/run.log. The run's
    own lock makes a second concurrent run exit straight away. `digest_only` adds
    --digest-only (Run now → Digest only).
    """
    cmd = run_command(config_path) + (["--digest-only"] if digest_only else [])
    logs = cfg.data_path() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / "run.log", "ab") as out:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # survives the web server stopping
        )
    threading.Thread(target=proc.wait, name="run-reaper", daemon=True).start()  # no zombie
    return proc.pid
