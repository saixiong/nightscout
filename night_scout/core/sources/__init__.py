"""Source adapters. Each turns one config section into fetchable tasks."""

from __future__ import annotations

from ..config import Config
from . import bluesky, github, hackernews, mastodon, reddit, rss, youtube
from .base import SourceTask

__all__ = ["SourceTask", "all_tasks"]


def all_tasks(cfg: Config) -> list[SourceTask]:
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
