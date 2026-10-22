"""Source adapters. Each turns one config section into fetchable tasks."""

from __future__ import annotations

from typing import Any

from ..config import Config
from . import bluesky, github, hackernews, mastodon, reddit, rss, youtube
from .base import SourceTask

__all__ = ["SourceTask", "all_tasks", "configured_tasks", "set_source_enabled", "source_catalog"]


def configured_tasks(cfg: Config) -> list[SourceTask]:
    """Every task in config.yaml, switched on or not."""
    s = cfg.sources
    return [
        *youtube.tasks(s.youtube),
        *rss.tasks(s.rss),
        *hackernews.tasks(s.hackernews, cfg.thresholds.max_age_days),
        *reddit.tasks(s.reddit),
        *bluesky.tasks(s.bluesky),
        *mastodon.tasks(s.mastodon),
        *github.tasks(s.github),
    ]


def all_tasks(cfg: Config) -> list[SourceTask]:
    """The tasks a run fetches: everything configured except `sources.disabled`."""
    off = set(cfg.sources.disabled)
    return [t for t in configured_tasks(cfg) if t.key not in off]


def source_catalog(cfg: Config) -> list[dict[str, Any]]:
    """One row per configured task for the Sources page, disabled ones included.

    `type` is the adapter (youtube, rss, hackernews, reddit, bluesky, mastodon, github);
    RSS rows also carry `kind` (feed, website, newsletter, podcast, arxiv). `detail` is the
    task's own part of the key: a feed URL, channel id, subreddit, query and so on.
    """
    off = set(cfg.sources.disabled)
    rss_kind = {f"rss:{r.url}": r.kind for r in cfg.sources.rss}
    out: list[dict[str, Any]] = []
    for t in configured_tasks(cfg):
        typ, _, detail = t.key.partition(":")
        row: dict[str, Any] = {
            "key": t.key,
            "label": t.label,
            "type": typ,
            "detail": detail,
            "enabled": t.key not in off,
        }
        if t.key in rss_kind:
            row["kind"] = rss_kind[t.key]
        out.append(row)
    return out


def set_source_enabled(cfg: Config, key: str, enabled: bool) -> None:
    """Switch one task on or off in `cfg` (the caller saves it). Unknown keys: ValueError."""
    if key not in {t.key for t in configured_tasks(cfg)}:
        raise ValueError(f"No source with key {key!r}.")
    disabled = [k for k in cfg.sources.disabled if k != key]
    if not enabled:
        disabled.append(key)
    cfg.sources.disabled = disabled
