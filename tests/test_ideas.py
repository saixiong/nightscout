from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from night_scout.core.config import Config, ProjectRef, Sources, load_config, save_config
from night_scout.core.db import Store
from night_scout.core.ideas import Idea, propose_ideas, slugify, start_project_from_idea
from night_scout.core.pipeline import run_once
from night_scout.core.profile import Profile, append_profile_entry, load_profile

from .fakes import FakeLLM
from .test_pipeline import PROFILE, _cfg, _seed, fake_llm

DAY = date(2026, 10, 22)


def _scored(store: Store, cfg: Config) -> list:
    run_once(cfg, store=store, llm=fake_llm(), today=DAY)
    return store.scored_since("2000-01-01")


def _idea(title: str, ids: list[int]) -> dict:
    return {
        "title": title,
        "pitch": "A   small thing.",
        "why_you": "You know PDFs.",
        "first_step": "pip install pypdf",
        "inspired_by": ids,
    }


def test_propose_ideas_keeps_only_valid_inspired_by(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    ids = _seed(store)
    rows = _scored(store, cfg)
    good = ids["rss:1"]
    llm = FakeLLM(
        {
            "IdeaProposal": lambda u: {
                "ideas": [
                    _idea("PDF speed bench", [good, 99999, good]),
                    _idea("Hallucinated", [99999]),
                    _idea("pdf-tool", [good]),  # an existing project's name
                    _idea("Phaser tile tool", [ids["rss:2"]]),
                    _idea("A third one", [good]),
                ]
            }
        }
    )
    ideas = propose_ideas(llm, Profile(PROFILE["projects"]), rows, 2)
    assert [i.title for i in ideas] == ["PDF speed bench", "Phaser tile tool"]
    assert ideas[0].inspired_by == [good]  # invalid id dropped, duplicate collapsed
    assert ideas[0].pitch == "A small thing."
    prompt = llm.calls[0][1]
    assert f"[{good}]" in prompt and "exactly 2 new side projects" in prompt


def test_propose_ideas_zero_or_no_rows_makes_no_call(tmp_path: Path) -> None:
    llm = FakeLLM({})
    assert propose_ideas(llm, Profile(PROFILE["projects"]), [], 2) == []
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    _seed(store)
    rows = _scored(store, cfg)
    assert propose_ideas(llm, Profile(PROFILE["projects"]), rows, 0) == []
    assert llm.calls == []


def test_run_saves_ideas_with_status_line(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    ids = _seed(store)
    llm = fake_llm()
    llm.answers["IdeaProposal"] = lambda u: {
        "ideas": [_idea("PDF speed bench", [ids["rss:1"]]), _idea("Tile tool", [ids["rss:2"]])]
    }
    result = run_once(cfg, store=store, llm=llm, today=DAY)
    assert result.ok and result.stats["ideas"] == 2
    assert "Ideas: 2 new project ideas" in result.status_lines
    rows = store.ideas_for_day("2026-10-22")
    assert [r["title"] for r in rows] == ["PDF speed bench", "Tile tool"]
    assert json.loads(rows[0]["inspired_by"]) == [ids["rss:1"]]
    assert rows[0]["status"] == "new" and rows[0]["run_id"] == result.run_id
    assert store.idea(rows[0]["id"])["why_you"] == "You know PDFs."


def test_ideas_failure_does_not_fail_the_run(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    _seed(store)
    llm = fake_llm()
    llm.answers["IdeaProposal"] = lambda u: "invalid structured output after retry"
    result = run_once(cfg, store=store, llm=llm, today=DAY)
    assert result.ok
    assert result.digest_path is not None and result.digest_path.exists()
    assert any(s.startswith("Ideas: failed") for s in result.status_lines)
    assert store.ideas_for_day("2026-10-22") == []


def test_ideas_disabled_when_zero(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.thresholds.ideas = 0
    store = Store(cfg.db_file())
    _seed(store)
    llm = fake_llm()
    result = run_once(cfg, store=store, llm=llm, today=DAY)
    assert result.ok
    assert not any(name == "IdeaProposal" for name, _ in llm.calls)
    assert not any(s.startswith("Ideas") for s in result.status_lines)


def test_idea_store_status_and_same_day_replace(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    a, b = store.save_ideas(
        1, "2026-10-22", [Idea("A", "p", "w", "f", [1]), Idea("B", "p", "w", "f", [])]
    )
    store.set_idea_status(a, "dismissed")
    with pytest.raises(ValueError):
        store.set_idea_status(b, "deleted")
    store.save_ideas(2, "2026-10-22", [Idea("C", "p", "w", "f", [2])])
    assert [(r["title"], r["status"]) for r in store.ideas_for_day("2026-10-22")] == [
        ("A", "dismissed"),
        ("C", "new"),
    ]
    assert store.idea(b) is None
    assert store.recent_idea_titles() == ["C", "A"]


# -- start_project_from_idea ---------------------------------------------------------------
PROFILE_TEXT = (
    "# Night Scout profile. Edit freely.\n"
    "generated_at: '2026-10-21T22:53:41+00:00'\n"
    "projects:\n"
    "- name: pdf-tool\n"
    "  path: {root}/pdf-tool\n"
    "  purpose: Summarizes PDFs.   # my own comment\n"
    "  interests:\n"
    "  - pdf parsing\n"
    "  would_want_to_know: [faster pdf parsers]\n"
)


def _workspace(tmp_path: Path) -> tuple[Config, Path, Store, int]:
    root = tmp_path / "projects"
    (root / "pdf-tool").mkdir(parents=True)
    (root / "pdf-tool" / "README.md").write_text("# pdf-tool\n")
    home = tmp_path / "home"
    home.mkdir()
    cfg = Config(
        projects=[ProjectRef(path=str(root / "pdf-tool"))], sources=Sources(), data_dir=str(home)
    )
    config_path = home / "config.yaml"
    save_config(cfg, config_path)
    cfg.profile_file().write_text(PROFILE_TEXT.format(root=root))
    store = Store(cfg.db_file())
    run = store.start_run()
    from night_scout.core.models import Item

    item_id = store.upsert_item(
        Item("rss", "Blog", "rss:1", "https://e.example/bm25", "BM25 beats LLMs [at tools]"), run
    ).item_id
    [idea_id] = store.save_ideas(
        run,
        "2026-10-22",
        [
            Idea(
                "BM25 Tool Selector!",
                "Pick tools fast.",
                "Fits your agents.",
                "pip install rank_bm25",
                [item_id],
            )
        ],
    )
    return cfg, config_path, store, idea_id


def _profile_llm() -> FakeLLM:
    return FakeLLM(
        {
            "ProjectProfile": lambda u: {
                "purpose": "Pick tools with BM25.",
                "stack": ["Python"],
                "key_dependencies": ["rank_bm25"],
                "interests": ["tool retrieval"],
                "would_want_to_know": ["new releases of rank_bm25"],
            }
        }
    )


def test_start_project_from_idea(tmp_path: Path) -> None:
    cfg, config_path, store, idea_id = _workspace(tmp_path)
    before = cfg.profile_file().read_text()
    llm = _profile_llm()
    folder = start_project_from_idea(cfg, store, llm, idea_id, config_path=config_path)

    assert folder == tmp_path / "projects" / "bm25-tool-selector"  # parent of the first project
    readme = (folder / "README.md").read_text()
    assert readme.startswith("# BM25 Tool Selector!\n\nPick tools fast.")
    assert "pip install rank_bm25" in readme
    assert "[BM25 beats LLMs (at tools)](https://e.example/bm25)" in readme
    assert [p.path for p in load_config(config_path).projects] == [
        str(tmp_path / "projects" / "pdf-tool"),
        str(folder),
    ]
    assert cfg.projects[-1].path == str(folder)
    after = cfg.profile_file().read_text()
    assert after.startswith(before)  # other entries (and the comment) untouched, byte for byte
    prof = load_profile(cfg.profile_file())
    assert prof.names == ["pdf-tool", "bm25-tool-selector"]
    assert prof.projects[1]["purpose"] == "Pick tools with BM25."
    assert prof.projects[1]["path"] == str(folder)
    assert "BM25 Tool Selector" in llm.calls[0][1]  # profiled from the new README only
    assert len(llm.calls) == 1
    assert store.idea(idea_id)["status"] == "started"
    with pytest.raises(ValueError, match="already started"):
        start_project_from_idea(cfg, store, llm, idea_id, config_path=config_path)


def test_start_project_refuses_existing_folder(tmp_path: Path) -> None:
    cfg, config_path, store, idea_id = _workspace(tmp_path)
    existing = tmp_path / "projects" / "bm25-tool-selector"
    existing.mkdir()
    (existing / "keep.txt").write_text("mine")
    config_before = config_path.read_text()
    profile_before = cfg.profile_file().read_text()
    with pytest.raises(ValueError, match="already exists"):
        start_project_from_idea(cfg, store, _profile_llm(), idea_id, config_path=config_path)
    assert sorted(p.name for p in existing.iterdir()) == ["keep.txt"]
    assert config_path.read_text() == config_before
    assert cfg.profile_file().read_text() == profile_before
    assert store.idea(idea_id)["status"] == "new"


def test_start_project_explicit_parent_and_bad_inputs(tmp_path: Path) -> None:
    cfg, config_path, store, idea_id = _workspace(tmp_path)
    with pytest.raises(ValueError, match="doesn't exist"):
        start_project_from_idea(
            cfg, store, _profile_llm(), idea_id, tmp_path / "nope", config_path=config_path
        )
    with pytest.raises(ValueError, match="No idea"):
        start_project_from_idea(cfg, store, _profile_llm(), 999, config_path=config_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    folder = start_project_from_idea(
        cfg, store, _profile_llm(), idea_id, other, config_path=config_path
    )
    assert folder.parent == other


def test_slugify_is_safe() -> None:
    assert slugify("../../etc/passwd ✨ Hack") == "etc-passwd-hack"
    assert slugify("...") == "new-project"
    assert slugify("A" * 100) == "a" * 48
    assert slugify("Über Café — v2.0") == "ber-caf-v2-0"


def test_append_profile_entry_falls_back_to_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text("projects:\n- name: a\n  path: /a\nextra: kept\n")  # projects isn't last
    append_profile_entry(path, {"name": "b", "path": "/b"})
    data = yaml.safe_load(path.read_text())
    assert [p["name"] for p in data["projects"]] == ["a", "b"] and data["extra"] == "kept"
    with pytest.raises(ValueError, match="already has"):
        append_profile_entry(path, {"name": "a", "path": "/a2"})
    fresh = tmp_path / "new" / "profile.yaml"
    append_profile_entry(fresh, {"name": "c", "path": "/c"})
    assert yaml.safe_load(fresh.read_text())["projects"][0]["name"] == "c"


def test_a_rerun_with_no_usable_ideas_keeps_the_mornings(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    ids = _seed(store)
    llm = fake_llm()
    llm.answers["IdeaProposal"] = lambda u: {"ideas": [_idea("PDF speed bench", [ids["rss:1"]])]}
    run_once(cfg, store=store, llm=llm, today=DAY)
    llm.answers["IdeaProposal"] = lambda u: {"ideas": [_idea("Made up", [999999])]}
    run_once(cfg, store=store, llm=llm, today=DAY)  # same day, nothing valid this time
    assert [r["title"] for r in store.ideas_for_day("2026-10-22")] == ["PDF speed bench"]


def test_readme_links_only_http(tmp_path: Path) -> None:
    from night_scout.core.ideas import _readme

    cfg = _cfg(tmp_path)
    store = Store(cfg.db_file())
    ids = _seed(store)
    store.set_item_fields(ids["rss:1"], url="javascript:alert(1)")
    iid = store.save_ideas(1, "2026-10-22", [Idea(**_idea("X", [ids["rss:1"], ids["rss:2"]]))])[0]
    text = _readme(store.idea(iid), store)
    assert "javascript:" not in text
    assert "](http" in text  # the other item still links
