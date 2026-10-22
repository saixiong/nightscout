"""config.yaml: model, providers, thresholds, sources, project dirs and schedule time."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .paths import data_dir


class ModelConfig(BaseModel):
    host: str = "http://127.0.0.1:11434"
    name: str = "gemma4:12b"
    fallback: str = "gemma4:e4b"
    embed: str = "embeddinggemma-2:270m"
    timeout_s: float = 300.0
    # Keep the model warm between calls in one run, then let Ollama unload it.
    keep_alive: str = "5m"


DEFAULT_PROVIDER = "ollama"
ProviderKind = Literal["ollama", "openai_compatible"]
RoleName = Literal["judge", "fallback", "chat", "embed"]
ROLE_NAMES: tuple[RoleName, ...] = ("judge", "fallback", "chat", "embed")

_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def is_local_host(host: str | None) -> bool:
    """Loopback only: 'localhost', 127.0.0.0/8 and ::1."""
    if not host:
        return False
    if host.lower() in ("localhost", "localhost."):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def check_base_url(url: str) -> str:
    """http(s) only; plain http only for loopback hosts (keys never cross a network unencrypted)."""
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("base_url must be an http:// or https:// URL")
    if parts.username or parts.password:
        raise ValueError("base_url must not contain credentials; store the API key instead")
    if parts.query or parts.fragment:
        raise ValueError("base_url must not have a query string or fragment")
    if parts.scheme == "http" and not is_local_host(parts.hostname):
        raise ValueError("base_url must use https:// for anything other than this computer")
    return url


class ProviderConfig(BaseModel):
    """A model server. The API key never lives here: it's in the OS keychain (or api_key_env)."""

    id: str
    kind: ProviderKind = "openai_compatible"
    name: str = ""
    base_url: str
    api_key_env: str | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _PROVIDER_ID.match(v):
            raise ValueError("provider id must be 1-32 chars of a-z, 0-9, '-' or '_'")
        return v

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str) -> str:
        return check_base_url(v)

    @field_validator("api_key_env")
    @classmethod
    def _env(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        if not _ENV_NAME.match(v):
            raise ValueError("api_key_env must be an environment variable name")
        return v

    @property
    def label(self) -> str:
        return self.name or self.id

    @property
    def is_local(self) -> bool:
        return is_local_host(urlsplit(self.base_url).hostname)


def split_ref(ref: str) -> tuple[str, str]:
    """'<provider_id>/<model>' → (provider_id, model). Only the first '/' splits, so
    'openrouter/google/gemma-3-27b-it' and 'ollama/gemma4:12b' both work."""
    provider, sep, model = ref.partition("/")
    if not sep or not _PROVIDER_ID.match(provider) or not model.strip() or model != model.strip():
        raise ValueError(f"model reference {ref!r} must look like '<provider_id>/<model>'")
    return provider, model


class Roles(BaseModel):
    """Which model does which job, as '<provider_id>/<model>'. Unset roles come from `model:`."""

    judge: str | None = None
    fallback: str | None = None
    chat: str | None = None
    embed: str | None = None

    @field_validator("judge", "fallback", "chat", "embed")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        split_ref(v)
        return v


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


class ProjectRef(BaseModel):
    path: str
    name: str | None = None

    def resolved(self) -> Path:
        return Path(self.path).expanduser()


class YouTubeSource(BaseModel):
    name: str
    channel_id: str
    transcripts: bool = True


class RssSource(BaseModel):
    name: str
    url: str


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
    # Optional. Without them everything runs on Ollama exactly as `model:` describes.
    providers: list[ProviderConfig] = Field(default_factory=list)
    roles: Roles = Field(default_factory=Roles)
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

    @model_validator(mode="after")
    def _check_providers(self) -> Config:
        ids = [p.id for p in self.providers]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate provider id(s): {', '.join(dupes)}")
        known = {p.id for p in self.all_providers()}
        for role in ROLE_NAMES:
            ref = getattr(self.roles, role)
            if ref and split_ref(ref)[0] not in known:
                raise ValueError(f"roles.{role}: unknown provider {split_ref(ref)[0]!r}")
        return self

    def all_providers(self) -> list[ProviderConfig]:
        """Configured providers, plus the built-in 'ollama' one (from `model.host`) unless
        a provider with that id is configured explicitly."""
        out = list(self.providers)
        if not any(p.id == DEFAULT_PROVIDER for p in out):
            # model_construct: an existing `model.host` keeps loading even if it isn't loopback.
            builtin = ProviderConfig.model_construct(
                id=DEFAULT_PROVIDER,
                kind="ollama",
                name="Ollama",
                base_url=self.model.host.rstrip("/"),
                api_key_env=None,
            )
            out.insert(0, builtin)
        return out

    def provider(self, provider_id: str) -> ProviderConfig:
        for p in self.all_providers():
            if p.id == provider_id:
                return p
        raise KeyError(f"no provider {provider_id!r}")

    def role_ref(self, role: RoleName) -> str | None:
        """The effective '<provider>/<model>' for a role; None only for an empty fallback."""
        ref = getattr(self.roles, role)
        if ref:
            return str(ref)
        m = self.model
        if role == "judge":
            return f"{DEFAULT_PROVIDER}/{m.name}"
        if role == "fallback":
            return f"{DEFAULT_PROVIDER}/{m.fallback}" if m.fallback else None
        if role == "chat":
            return self.role_ref("judge")
        return f"{DEFAULT_PROVIDER}/{m.embed}"

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
    # Older configs have neither section; don't grow them just by saving.
    for key in ("providers", "roles"):
        if not data.get(key):
            data.pop(key, None)
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
