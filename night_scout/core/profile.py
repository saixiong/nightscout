"""Knowing your projects: scan each project dir, then let Gemma write profile.yaml.

The profile is plain, human-editable YAML. It's rebuilt only when you ask
(`night-scout profile`), so your edits stick.
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .config import Config, ProjectRef
from .llm import LLM, ModelError
from .models import utcnow
from .providers import close_llm, make_llm

log = logging.getLogger(__name__)

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", "target",
    "vendor", ".next", ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "Pods", ".gradle", "coverage", ".tox", "site-packages",
}  # fmt: skip
LANG_BY_EXT = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
    ".jsx": "JavaScript", ".mjs": "JavaScript", ".rs": "Rust", ".go": "Go", ".java": "Java",
    ".kt": "Kotlin", ".swift": "Swift", ".rb": "Ruby", ".php": "PHP", ".cs": "C#",
    ".cpp": "C++", ".cc": "C++", ".c": "C", ".h": "C", ".m": "Objective-C", ".scala": "Scala",
    ".dart": "Dart", ".lua": "Lua", ".ex": "Elixir", ".exs": "Elixir", ".zig": "Zig",
    ".vue": "Vue", ".svelte": "Svelte", ".sh": "Shell", ".sql": "SQL", ".ipynb": "Jupyter",
}  # fmt: skip
MAX_FILES_SCANNED = 5000


@dataclass
class ProjectScan:
    name: str
    path: str
    readme: str = ""
    languages: list[str] = field(default_factory=list)
    dependencies: dict[str, list[str]] = field(default_factory=dict)  # manifest -> names
    description: str = ""

    def all_deps(self) -> list[str]:
        seen: dict[str, None] = {}
        for names in self.dependencies.values():
            for n in names:
                seen.setdefault(n, None)
        return list(seen)


# -- scanning -----------------------------------------------------------------
def readme_digest(text: str, limit: int = 3000) -> str:
    """Headings plus the first paragraph under each, skipping badges, code and HTML."""
    out: list[str] = []
    in_code = False
    want_para = True
    para: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            if para:
                out.append(" ".join(para))
                para = []
            out.append(stripped)
            want_para = True
            continue
        if not stripped:
            if para:
                out.append(" ".join(para))
                para = []
                want_para = False
            continue
        if stripped.startswith(("<", "[![", "![", "|", "---")):
            continue
        if want_para:
            para.append(stripped)
    if para:
        out.append(" ".join(para))
    return "\n".join(out)[:limit]


def _req_name(line: str) -> str | None:
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith(("-", "git+", "http")):
        return None
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
    return m.group(1).lower() if m else None


def parse_manifests(root: Path) -> tuple[dict[str, list[str]], str]:
    deps: dict[str, list[str]] = {}
    description = ""
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            names = [*(data.get("dependencies") or {}), *(data.get("devDependencies") or {})]
            deps["package.json"] = names
            description = description or str(data.get("description") or "")
        except (ValueError, OSError) as e:
            log.info("skipping %s: %s", pkg, e)
    pyproj = root / "pyproject.toml"
    if pyproj.is_file():
        try:
            data = tomllib.loads(pyproj.read_text(encoding="utf-8"))
            proj = data.get("project") or {}
            names = [n for n in (_req_name(d) for d in proj.get("dependencies") or []) if n]
            for extra in (proj.get("optional-dependencies") or {}).values():
                names += [n for n in (_req_name(d) for d in extra) if n]
            poetry = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
            names += [n.lower() for n in poetry if n.lower() != "python"]
            deps["pyproject.toml"] = names
            description = description or str(proj.get("description") or "")
        except (tomllib.TOMLDecodeError, OSError) as e:
            log.info("skipping %s: %s", pyproj, e)
    for req in sorted(root.glob("requirements*.txt")):
        try:
            names = [n for n in (_req_name(x) for x in req.read_text().splitlines()) if n]
            deps[req.name] = names
        except OSError as e:
            log.info("skipping %s: %s", req, e)
    cargo = root / "Cargo.toml"
    if cargo.is_file():
        try:
            data = tomllib.loads(cargo.read_text(encoding="utf-8"))
            deps["Cargo.toml"] = [*(data.get("dependencies") or {})]
            description = description or str((data.get("package") or {}).get("description") or "")
        except (tomllib.TOMLDecodeError, OSError) as e:
            log.info("skipping %s: %s", cargo, e)
    gomod = root / "go.mod"
    if gomod.is_file():
        try:
            mods = re.findall(
                r"^\s*(?:require\s+)?([a-z0-9.-]+\.[a-z]+/[^\s]+)\s+v",
                gomod.read_text(encoding="utf-8"),
                re.M,
            )
            deps["go.mod"] = mods
        except OSError as e:
            log.info("skipping %s: %s", gomod, e)
    return deps, description


def detect_languages(root: Path, top: int = 4) -> list[str]:
    counts: Counter[str] = Counter()
    scanned = 0
    stack = [root]
    while stack and scanned < MAX_FILES_SCANNED:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_symlink():
                continue
            if p.is_dir():
                if p.name not in SKIP_DIRS and not p.name.startswith("."):
                    stack.append(p)
            elif p.suffix in LANG_BY_EXT:
                counts[LANG_BY_EXT[p.suffix]] += 1
                scanned += 1
    return [lang for lang, _ in counts.most_common(top)]


def scan_project(ref: ProjectRef) -> ProjectScan:
    root = ref.resolved()
    if not root.is_dir():
        raise FileNotFoundError(f"project directory not found: {root}")
    readme = ""
    for name in ("README.md", "README.rst", "README.txt", "README", "readme.md"):
        f = root / name
        if f.is_file():
            readme = readme_digest(f.read_text(encoding="utf-8", errors="replace"))
            break
    deps, description = parse_manifests(root)
    return ProjectScan(
        name=ref.name or root.name,
        path=str(root),
        readme=readme,
        languages=detect_languages(root),
        dependencies=deps,
        description=description,
    )


# -- the profile ----------------------------------------------------------------
class ProjectProfile(BaseModel):
    """What Gemma writes for one project. Also the shape of each entry in profile.yaml."""

    purpose: str = Field(description="One plain sentence: what the project is for.")
    stack: list[str] = Field(description="Languages, frameworks and runtimes, 2-8 short items.")
    key_dependencies: list[str] = Field(description="The 3-10 dependencies that matter most.")
    interests: list[str] = Field(
        description="3-8 topics the owner of this project would follow, as short phrases."
    )
    would_want_to_know: list[str] = Field(
        description="3-8 concrete kinds of news that would change what the owner does next, "
        "e.g. 'a faster drop-in replacement for <dependency>' or 'security issues in <x>'."
    )


PROFILE_SYSTEM = (
    "You write a short, factual profile of a software project for a personal news scout. "
    "Use only what the README, manifests and file types show. Don't invent features. "
    "Keep every list item short (2-8 words).\n"
    "`interests` are topics the owner would follow. `would_want_to_know` is about future "
    "NEWS, never features the project already has: e.g. 'new releases of <key dependency>', "
    "'security advisories for <dependency>', 'faster alternatives to <dependency>', "
    "'new open models with tool calling', 'changes to the <spec/API> it implements'. "
    "Reply with JSON only."
)


def profile_prompt(scan: ProjectScan) -> str:
    deps = ", ".join(scan.all_deps()[:60]) or "(none found)"
    return (
        f"Project name: {scan.name}\n"
        f"Manifest description: {scan.description or '(none)'}\n"
        f"Languages by file count: {', '.join(scan.languages) or '(unknown)'}\n"
        f"Dependencies: {deps}\n\n"
        f"README (headings and first paragraphs):\n{scan.readme or '(no README)'}\n"
    )


def heuristic_profile(scan: ProjectScan) -> ProjectProfile:
    """Used when the model is unavailable, so the profile is never empty."""
    deps = scan.all_deps()[:10]
    purpose = scan.description or (scan.readme.split("\n")[0].lstrip("# ") if scan.readme else "")
    return ProjectProfile(
        purpose=purpose or f"The {scan.name} project.",
        stack=scan.languages or ["unknown"],
        key_dependencies=deps,
        interests=[*scan.languages[:3], *deps[:3]],
        would_want_to_know=[f"new releases of {d}" for d in deps[:3]]
        or ["new tools for this stack"],
    )


def profile_project(ref: ProjectRef, client: LLM) -> dict[str, Any]:
    """Scan one project and let the model describe it (heuristics if the model fails).

    Raises FileNotFoundError if the folder is missing.
    """
    scan = scan_project(ref)
    try:
        prof = client.chat_json(PROFILE_SYSTEM, profile_prompt(scan), ProjectProfile)
        by = client.model_name
    except ModelError as e:
        log.warning("profile for %s fell back to heuristics: %s", scan.name, e)
        prof = heuristic_profile(scan)
        by = "heuristic"
    return {"name": scan.name, "path": scan.path, **prof.model_dump(), "generated_by": by}


def build_profile(cfg: Config, llm: LLM | None = None) -> dict[str, Any]:
    own = llm is None
    client: LLM = llm or make_llm(cfg, "judge")
    projects: list[dict[str, Any]] = []
    try:
        for ref in cfg.projects:
            try:
                projects.append(profile_project(ref, client))
            except FileNotFoundError as e:
                log.warning("%s", e)
                continue
    finally:
        if own:
            close_llm(client)
    return {"generated_at": utcnow().isoformat(timespec="seconds"), "projects": projects}


def build_profile_file(cfg: Config, llm: LLM | None = None) -> Path:
    out = cfg.profile_file()
    kept: list[str] = []
    if out.exists():  # your typed topics aren't Gemma's to regenerate: keep them
        try:
            kept = load_profile(out).topics
        except (OSError, yaml.YAMLError):
            kept = []
    data = build_profile(cfg, llm)
    if kept:
        data = {"topics": kept, **data}
    out.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Night Scout profile. Edit freely: this is what the scout thinks each project\n"
        "# cares about. `night-scout profile` regenerates it (and overwrites your edits).\n"
    )
    out.write_text(
        header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return out


def append_profile_entry(path: Path, entry: dict[str, Any]) -> None:
    """Add one project to profile.yaml, leaving every existing byte (and comment) alone.

    The new entry is appended as text to the `projects:` list when that list is the last
    block in the file (as `night-scout profile` writes it). Any other layout falls back to
    a full rewrite, which keeps the data but drops comments.
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump({"projects": [entry]}, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a mapping; fix it before adding projects")
    old = data.get("projects") or []
    if not isinstance(old, list):
        raise ValueError(f"{path}: `projects` is not a list; fix it before adding projects")
    if any(isinstance(p, dict) and p.get("name") == entry["name"] for p in old):
        raise ValueError(f"profile.yaml already has a project named {entry['name']!r}")
    block = yaml.safe_dump([entry], sort_keys=False, allow_unicode=True)
    m = re.search(r"^projects:[ \t]*\n(?P<indent>[ \t]*)- ", text, re.M)
    candidate = None
    if m and old:
        indent = m.group("indent")
        lines = block.splitlines()
        appended = "\n".join(indent + ln if ln else ln for ln in lines) + "\n"
        candidate = text + ("" if text.endswith("\n") else "\n") + appended
        try:
            check = yaml.safe_load(candidate) or {}
        except yaml.YAMLError:
            check = {}
        if not isinstance(check, dict) or check != {**data, "projects": [*old, entry]}:
            candidate = None
    if candidate is None:
        data["projects"] = [*old, entry]
        candidate = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(candidate, encoding="utf-8")
    tmp.replace(path)


@dataclass
class Profile:
    projects: list[dict[str, Any]]
    # The reader's own topics (typed in Setup or on the Digest). They drive the Digest; projects
    # drive Actions. A profile may have topics and no projects.
    topics: list[str] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [p["name"] for p in self.projects]

    @property
    def digest_topics(self) -> list[str]:
        """What the Digest groups by: your topics, or your project names if you typed none."""
        return self.topics or self.names

    @property
    def empty(self) -> bool:
        return not self.projects and not self.topics

    def keywords(self) -> dict[str, set[str]]:
        """Lowercased phrases per project, for the cheap pre-filter."""
        out: dict[str, set[str]] = {}
        for p in self.projects:
            words: set[str] = set()
            for key in ("stack", "key_dependencies", "interests", "would_want_to_know"):
                for phrase in p.get(key) or []:
                    phrase = str(phrase).lower().strip()
                    if phrase:
                        words.add(phrase)
            out[p["name"]] = words
        if self.topics:
            out["topics"] = {t.lower().strip() for t in self.topics if t.strip()}
        return out

    def describe(self, project: dict[str, Any]) -> str:
        return (
            f"{project['name']}: {project.get('purpose', '')} "
            f"Stack: {', '.join(project.get('stack') or [])}. "
            f"Key deps: {', '.join(project.get('key_dependencies') or [])}. "
            f"Interests: {', '.join(project.get('interests') or [])}. "
            f"Wants to know: {'; '.join(project.get('would_want_to_know') or [])}."
        )

    def prompt_block(self) -> str:
        return "\n".join(f"- {self.describe(p)}" for p in self.projects)


MAX_TOPICS = 20
MAX_TOPIC_LEN = 60


def clean_topics(raw: Any) -> list[str]:
    """Trimmed, de-duplicated (case-insensitively) topic strings, capped in count and length."""
    out: list[str] = []
    seen: set[str] = set()
    for t in raw if isinstance(raw, list) else []:
        t = " ".join(str(t).split())[:MAX_TOPIC_LEN].strip()
        if t and t.casefold() not in seen:
            seen.add(t.casefold())
            out.append(t)
    return out[:MAX_TOPICS]


def load_profile(path: Path) -> Profile:
    if not path.exists():
        raise FileNotFoundError(f"No profile at {path}. Run `night-scout profile` first.")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        data = {}
    projects = [p for p in data.get("projects") or [] if isinstance(p, dict) and p.get("name")]
    return Profile(projects=projects, topics=clean_topics(data.get("topics")))


def save_topics(path: Path, topics: list[str]) -> list[str]:
    """Write the top-level `topics:` list, keeping projects and any hand edits. Atomic."""
    data: dict[str, Any] = {}
    header = ""
    if path.exists():
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():  # keep the comment header at the top of the file
            if not line.startswith("#"):
                break
            header += line + "\n"
        loaded = yaml.safe_load(text) or {}
        if not isinstance(loaded, dict) or not isinstance(loaded.get("projects", []), list):
            raise ValueError(f"{path} isn't a profile (no `projects:` list); fix it first.")
        data = loaded
    cleaned = clean_topics(topics)
    out: dict[str, Any] = {"topics": cleaned}
    out.update({k: v for k, v in data.items() if k != "topics"})
    out.setdefault("projects", [])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        header + yaml.safe_dump(out, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    tmp.replace(path)
    return cleaned
