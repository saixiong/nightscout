from __future__ import annotations

import json
from pathlib import Path

import yaml

from night_scout.core.config import Config, ProjectRef
from night_scout.core.profile import (
    build_profile_file,
    load_profile,
    readme_digest,
    scan_project,
)

from .fakes import FakeLLM


def make_project(root: Path) -> Path:
    p = root / "demo-app"
    (p / "src").mkdir(parents=True)
    (p / "node_modules" / "junk").mkdir(parents=True)
    (p / "README.md").write_text(
        "[![badge](x)](y)\n# Demo App\n\nA tiny web app that summarizes PDFs.\nMore text.\n\n"
        "Second paragraph is skipped.\n\n```sh\nnpm run dev\n```\n## Usage\n\nRun it locally.\n"
    )
    (p / "package.json").write_text(
        json.dumps(
            {
                "description": "PDF summarizer",
                "dependencies": {"next": "15", "zod": "3"},
                "devDependencies": {"vitest": "2"},
            }
        )
    )
    (p / "pyproject.toml").write_text(
        '[project]\nname="x"\ndependencies=["fastapi>=0.110", "pydantic[email]"]\n'
        '[project.optional-dependencies]\ndev=["pytest"]\n'
    )
    (p / "requirements-dev.txt").write_text("ruff==0.5 # lint\n-r base.txt\ngit+https://x\n")
    (p / "go.mod").write_text("module x\n\nrequire (\n\tgithub.com/spf13/cobra v1.8.0\n)\n")
    (p / "Cargo.toml").write_text('[package]\nname="x"\n[dependencies]\nserde = "1"\n')
    for i in range(3):
        (p / "src" / f"m{i}.ts").write_text("x")
    (p / "src" / "tool.py").write_text("x")
    (p / "node_modules" / "junk" / "a.js").write_text("x")
    return p


def test_readme_digest_keeps_headings_and_first_paragraphs() -> None:
    d = readme_digest("# T\n\nfirst para\nline two\n\nsecond para\n\n```\ncode\n```\n## H2\n\nbody")
    assert d == "# T\nfirst para line two\n## H2\nbody"


def test_scan_project(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    scan = scan_project(ProjectRef(path=str(root)))
    assert scan.name == "demo-app"
    assert "A tiny web app that summarizes PDFs. More text." in scan.readme
    assert "Second paragraph" not in scan.readme and "npm run dev" not in scan.readme
    assert scan.dependencies["package.json"] == ["next", "zod", "vitest"]
    assert scan.dependencies["pyproject.toml"] == ["fastapi", "pydantic", "pytest"]
    assert scan.dependencies["requirements-dev.txt"] == ["ruff"]
    assert scan.dependencies["go.mod"] == ["github.com/spf13/cobra"]
    assert scan.dependencies["Cargo.toml"] == ["serde"]
    assert scan.languages[0] == "TypeScript" and "JavaScript" not in scan.languages
    assert scan.description == "PDF summarizer"


PROFILE_ANSWER = {
    "purpose": "Summarizes PDFs in the browser.",
    "stack": ["TypeScript", "Next.js"],
    "key_dependencies": ["next", "zod"],
    "interests": ["pdf parsing", "local llm"],
    "would_want_to_know": ["faster pdf parsers", "next.js releases"],
}


def test_build_profile_with_model_and_fallback(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    cfg = Config(
        projects=[ProjectRef(path=str(root)), ProjectRef(path=str(tmp_path / "gone"))],
        data_dir=str(tmp_path / "data"),
    )
    llm = FakeLLM({"ProjectProfile": lambda u: PROFILE_ANSWER})
    out = build_profile_file(cfg, llm)
    data = yaml.safe_load(out.read_text())
    assert out.read_text().startswith("# Night Scout profile")
    assert len(data["projects"]) == 1  # the missing directory is skipped, not fatal
    proj = data["projects"][0]
    assert proj["name"] == "demo-app" and proj["generated_by"] == "fake-gemma"
    assert "next" in llm.calls[0][1] and "summarizes PDFs" in llm.calls[0][1]

    broken = FakeLLM({"ProjectProfile": lambda u: "model fell over"})
    data = yaml.safe_load(build_profile_file(cfg, broken).read_text())
    proj = data["projects"][0]
    assert proj["generated_by"] == "heuristic"
    assert proj["purpose"] == "PDF summarizer"
    assert "next" in proj["key_dependencies"]

    prof = load_profile(cfg.profile_file())
    assert prof.names == ["demo-app"]
    assert "next" in prof.keywords()["demo-app"]
