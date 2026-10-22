"""config.yaml: model, thresholds, sources, project dirs and schedule time."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from .paths import data_dir


class ModelConfig(BaseModel):
    host: str = "http://127.0.0.1:11434"
    name: str = "gemma4:12b"
    fallback: str = "gemma4:e4b"
    embed: str = "embeddinggemma-2:270m"
    timeout_s: float = 300.0
    # Keep the model warm between calls in one run, then let Ollama unload it.
    keep_alive: str = "5m"


class Thresholds(BaseModel):
    # How many pre-filtered candidates Gemma scores per run (scoring is the slow part).
    max_scored: int = Field(default=40, ge=1)
    # Minimum best-project relevance (0-1) for an item to get actions and a digest slot.
    relevance: float = Field(default=0.6, ge=0.0, le=1.0)
    digest_size: int = Field(default=7, ge=1)
    # Items older than this on first sight are ignored.
    max_age_days: int = Field(default=3, ge=1)
    # Cosine similarity at or above which two items count as the same story.
    near_duplicate: float = Field(default=0.92, ge=0.5, le=1.0)
    # New side-project ideas proposed after each run's digest (0 turns them off).
    ideas: int = Field(default=2, ge=0, le=5)


class ProjectRef(BaseModel):
    path: str
    name: str | None = None

    def resolved(self) -> Path:
        return Path(self.path).expanduser()


class YouTubeSource(BaseModel):
    name: str
    channel_id: str
    transcripts: bool = True


RssKind = Literal["feed", "website", "newsletter", "podcast", "arxiv"]


class RssSource(BaseModel):
    name: str
    url: str
    # Only for display: how the feed was added. Every kind is fetched the same way.
    kind: RssKind = "feed"


HNList = Literal["top", "new", "best", "show", "ask"]


def _default_hn_lists() -> list[HNList]:
    return ["top"]


class HackerNewsSource(BaseModel):
    lists: list[HNList] = Field(default_factory=_default_hn_lists)
    keywords: list[str] = Field(default_factory=list)
    limit: int = Field(default=30, ge=1, le=200)
    min_points: int = 0


class RedditSource(BaseModel):
    subreddits: list[str] = Field(default_factory=list)
    sort: Literal["new", "hot", "top"] = "new"
    limit: int = Field(default=25, ge=1, le=100)


class BlueskySource(BaseModel):
    queries: list[str] = Field(default_factory=list)
    actors: list[str] = Field(default_factory=list)
    feeds: list[str] = Field(default_factory=list)  # at:// URIs of public feed generators
    limit: int = Field(default=25, ge=1, le=100)


class MastodonSource(BaseModel):
    instance: str
    hashtags: list[str] = Field(default_factory=list)
    limit: int = Field(default=20, ge=1, le=40)

    @field_validator("instance")
    @classmethod
    def _strip_scheme(cls, v: str) -> str:
        return v.removeprefix("https://").removeprefix("http://").strip("/")


class GitHubSource(BaseModel):
    releases: list[str] = Field(default_factory=list)  # "owner/repo"
    search: list[str] = Field(default_factory=list)  # GitHub search qualifiers
    search_days: int = Field(default=7, ge=1)
    limit: int = Field(default=10, ge=1, le=50)
    token_env: str = "GITHUB_TOKEN"


class Sources(BaseModel):
    youtube: list[YouTubeSource] = Field(default_factory=list)
    rss: list[RssSource] = Field(default_factory=list)
    hackernews: HackerNewsSource | None = None
    reddit: RedditSource | None = None
    bluesky: BlueskySource | None = None
    mastodon: list[MastodonSource] = Field(default_factory=list)
    github: GitHubSource | None = None
    # SourceTask keys (see `night-scout sources` / the Sources page) that are switched off.
    disabled: list[str] = Field(default_factory=list)


class Schedule(BaseModel):
    time: str = "04:30"  # local time, HH:MM

    @field_validator("time", mode="before")
    @classmethod
    def _hhmm(cls, v: object) -> str:
        if isinstance(v, int):  # YAML 1.1 reads an unquoted 4:30 as 270 (base 60)
            v = f"{v // 60}:{v % 60}"
        hh, _, mm = str(v).partition(":")
        if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60):
            raise ValueError("schedule.time must be HH:MM (24h)")
        return f"{int(hh):02d}:{int(mm):02d}"


class SandboxConfig(BaseModel):
    prefer: Literal["auto", "container", "venv"] = "auto"
    timeout_s: int = Field(default=300, ge=10, le=3600)
    container_image: str = "python:3.12-slim"
    # Without a container engine, venv runs are fenced by sandbox-exec (macOS) or bwrap
    # (Linux). Where neither exists, an unfenced venv can read your files; opt in here.
    allow_unfenced_venv: bool = False


class Config(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    projects: list[ProjectRef] = Field(default_factory=list)
    sources: Sources = Field(default_factory=Sources)
    schedule: Schedule = Field(default_factory=Schedule)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    # Shown to sources in the User-Agent so they can reach you; keep it generic if you like.
    contact: str = "local user"
    data_dir: str | None = None
    profile_path: str | None = None
    # Where "Note for project" writes, relative to each project's directory.
    notes_subdir: str = "notes/night-scout"

    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser() if self.data_dir else data_dir()

    def profile_file(self) -> Path:
        if self.profile_path:
            return Path(self.profile_path).expanduser()
        return self.data_path() / "profile.yaml"

    def db_file(self) -> Path:
        return self.data_path() / "scout.db"

    def digests_dir(self) -> Path:
        return self.data_path() / "digests"


def load_config(path: Path) -> Config:
    if not path.exists():
        raise FileNotFoundError(f"No config at {path}. Run `night-scout init` first.")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(raw)


def save_config(cfg: Config, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.model_dump(mode="json", exclude_none=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def starter_config(project_dirs: list[str]) -> Config:
    """A small, public-safe starting point. Edit config.yaml to taste."""
    return Config(
        projects=[ProjectRef(path=p) for p in project_dirs],
        sources=Sources(
            youtube=[YouTubeSource(name="Fireship", channel_id="UCsBjURrPoezykLs9EqgamOA")],
            rss=[
                RssSource(name="Simon Willison", url="https://simonwillison.net/atom/everything/"),
                RssSource(name="Ollama blog", url="https://ollama.com/blog/rss.xml"),
            ],
            hackernews=HackerNewsSource(lists=["top", "show"], keywords=["ollama", "gemma"]),
            reddit=RedditSource(subreddits=["LocalLLaMA"]),
            bluesky=BlueskySource(queries=["local llm"]),
            mastodon=[MastodonSource(instance="mastodon.social", hashtags=["llm"])],
            github=GitHubSource(releases=["ollama/ollama"], search=["topic:llm language:python"]),
        ),
    )
