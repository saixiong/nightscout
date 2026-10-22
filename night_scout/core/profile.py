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


def build_profile(cfg: Config, llm: LLM | None = None) -> dict[str, Any]:
    own = llm is None
    client: LLM = llm or make_llm(cfg, "judge")
    projects: list[dict[str, Any]] = []
    try:
        for ref in cfg.projects:
            try:
                scan = scan_project(ref)
            except FileNotFoundError as e:
                log.warning("%s", e)
                continue
            try:
                prof = client.chat_json(PROFILE_SYSTEM, profile_prompt(scan), ProjectProfile)
                by = client.model_name
            except ModelError as e:
                log.warning("profile for %s fell back to heuristics: %s", scan.name, e)
                prof = heuristic_profile(scan)
                by = "heuristic"
            projects.append(
                {"name": scan.name, "path": scan.path, **prof.model_dump(), "generated_by": by}
            )
    finally:
        if own:
            close_llm(client)
    return {"generated_at": utcnow().isoformat(timespec="seconds"), "projects": projects}


def build_profile_file(cfg: Config, llm: LLM | None = None) -> Path:
    data = build_profile(cfg, llm)
    out = cfg.profile_file()
    out.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Night Scout profile. Edit freely: this is what the scout thinks each project\n"
        "# cares about. `night-scout profile` regenerates it (and overwrites your edits).\n"
    )
    out.write_text(
        header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return out


@dataclass
class Profile:
    projects: list[dict[str, Any]]

    @property
    def names(self) -> list[str]:
        return [p["name"] for p in self.projects]

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


def load_profile(path: Path) -> Profile:
    if not path.exists():
        raise FileNotFoundError(f"No profile at {path}. Run `night-scout profile` first.")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    projects = [p for p in data.get("projects") or [] if isinstance(p, dict) and p.get("name")]
    return Profile(projects=projects)
