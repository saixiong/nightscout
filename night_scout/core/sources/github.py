"""GitHub releases of watched repos and a search for new repos (unauthenticated by default)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import GitHubSource
from ..http import PoliteClient
from ..models import Item
from .base import SourceTask, parse_time, strip_html

API = "https://api.github.com"


def _headers(cfg: GitHubSource) -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get(cfg.token_env, "").strip()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def item_from_release(rel: dict[str, Any], repo: str) -> Item | None:
    if rel.get("draft") or not rel.get("html_url"):
        return None
    tag = rel.get("tag_name", "")
    name = rel.get("name") or tag
    return Item(
        source="github",
        source_name=f"{repo} releases",
        uid=f"gh-release:{repo}:{rel.get('id') or tag}",
        url=rel["html_url"],
        title=f"{repo} {name}".strip() if repo not in name else name,
        text=strip_html(rel.get("body"), 6000),
        author=(rel.get("author") or {}).get("login"),
        published_at=parse_time(rel.get("published_at") or rel.get("created_at")),
        extra={"repo": repo, "tag": tag, "prerelease": bool(rel.get("prerelease"))},
    )


def item_from_repo(repo: dict[str, Any], query: str) -> Item | None:
    full = repo.get("full_name")
    if not full or not repo.get("html_url"):
        return None
    desc = repo.get("description") or ""
    topics = repo.get("topics") or []
    text = desc
    if topics:
        text += f"\nTopics: {', '.join(topics)}"
    if repo.get("language"):
        text += f"\nLanguage: {repo['language']}"
    return Item(
        source="github",
        source_name=f"GitHub search: {query}",
        uid=f"gh-repo:{full}",
        url=repo["html_url"],
        title=f"{full}: {desc}"[:200] if desc else full,
        text=text.strip(),
        author=(repo.get("owner") or {}).get("login"),
        published_at=parse_time(repo.get("created_at")),
        extra={"repo": full, "stars": repo.get("stargazers_count", 0), "topics": topics},
    )


def tasks(cfg: GitHubSource | None) -> list[SourceTask]:
    if cfg is None:
        return []
    out: list[SourceTask] = []

    for repo in cfg.releases:

        def fetch_rel(client: PoliteClient, repo: str = repo) -> list[Item]:
            data = client.get_json(
                f"{API}/repos/{repo}/releases", params={"per_page": 5}, headers=_headers(cfg)
            )
            items = [item_from_release(r, repo) for r in data or []]
            return [i for i in items if i]

        out.append(
            SourceTask(
                key=f"github:releases:{repo}", label=f"GitHub {repo} releases", fetch=fetch_rel
            )
        )

    for q in cfg.search:

        def fetch_search(client: PoliteClient, q: str = q) -> list[Item]:
            since = (datetime.now(UTC) - timedelta(days=cfg.search_days)).date()
            data = client.get_json(
                f"{API}/search/repositories",
                params={
                    "q": f"{q} created:>={since.isoformat()}",
                    "sort": "stars",
                    "order": "desc",
                    "per_page": cfg.limit,
                },
                headers=_headers(cfg),
            )
            items = [item_from_repo(r, q) for r in data.get("items", [])]
            return [i for i in items if i]

        out.append(
            SourceTask(key=f"github:search:{q}", label=f'GitHub search "{q}"', fetch=fetch_search)
        )
    return out
