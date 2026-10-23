"""Local web app: a single page (static JS) over a small JSON API.

Binds to 127.0.0.1 only. Every request needs the per-launch session cookie, requests with
a foreign Host or Origin are refused, and every mutating API call needs the per-launch
CSRF token in the X-CSRF-Token header, so other websites can't drive it.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlsplit

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from ..core import actions
from ..core import schedule as schedule_mod
from ..core import setup as setup_mod
from ..core.config import (
    CONFIG_LOCK,
    LOOK_BACK_MAX,
    SCOUTABLE_TYPES,
    Config,
    ProviderConfig,
    Schedule,
    ScoutSettings,
    Sources,
    clean_scout_terms,
    load_config,
    save_config,
    split_ref,
)
from ..core.db import Store
from ..core.digest import day_window, split_digest
from ..core.discover import AlreadyFollowing, add_source_keyed
from ..core.feedback import MUTE_AT, MUTE_VOTES, compute_weights
from ..core.http import PoliteClient, SourceError
from ..core.ideas import start_project_from_idea
from ..core.llm import LLM, ModelError, ModelUnavailable
from ..core.models import utcnow
from ..core.profile import Profile, build_profile_file, load_profile, profile_project, save_topics
from ..core.providers import (
    PRESETS,
    fetch_models,
    make_llm,
    ollama_models,
    ollama_pull,
    preset,
    runs_locally,
    test_provider,
)
from ..core.safety import check_commands
from ..core.sandbox import Plan, choose_mode
from ..core.scout import ScoutError, run_scout, youtube_key_view, ytdlp_command
from ..core.secrets import (
    SecretStoreUnavailable,
    delete_key,
    get_key,
    key_source,
    keychain_available,
    masked,
    set_key,
)
from ..core.sources import all_tasks, configured_tasks, set_source_enabled

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
STATIC = HERE / "static"
COOKIE = "night_scout_session"
CSRF_HEADER = "x-csrf-token"
NOT_YET = {"error": "not available yet"}
IMPACTS = ("issue", "change", "improvement", "relevant")
VOTES = {"up": 1, "down": -1, "ignore": 0}
PROFILE_LISTS = ("stack", "key_dependencies", "interests", "would_want_to_know")

# Endpoints whose backend lands with the pipeline (B) and providers (C) tracks. The UI is
# built for them already and shows a calm "not wired yet" until they answer.
NOT_YET_ROUTES: list[tuple[str, str]] = []  # every v2 endpoint is wired


class RunBody(BaseModel):
    mode: Literal["all", "digest"] = "all"  # Run now: Digest and Actions, or the Digest only


class ToggleBody(BaseModel):
    key: str
    enabled: bool


class AddSourceBody(BaseModel):
    type: str
    value: str
    # Look back + Scout key terms (feed-style sources only); None = leave as is / default
    look_back_days: int | None = Field(default=None, ge=1, le=LOOK_BACK_MAX)
    scout_terms: list[str] | None = None


class ScoutBody(BaseModel):
    key: str
    look_back_days: int | None = Field(default=None, ge=1, le=LOOK_BACK_MAX)
    scout_terms: list[str] = []


class AddProjectBody(BaseModel):
    path: str


class ScheduleBody(BaseModel):
    time: str
    nightly: bool


class KeyBody(BaseModel):
    key: str
    model: str | None = None


class NewProvider(BaseModel):
    name: str
    base_url: str
    api_key: str | None = None
    model: str | None = None  # Setup's add form sends one model
    models: list[str] | None = None  # the Settings sheet sends the list


class EditProvider(BaseModel):
    name: str
    base_url: str
    api_key: str | None = None  # blank keeps the stored key
    models: list[str] = []


class ProbeBody(BaseModel):
    base_url: str
    api_key: str | None = None
    id: str | None = None  # an existing custom provider: use its stored key if api_key is blank


class DraftBody(BaseModel):
    name: str
    base_url: str
    api_key: str | None = None
    models: list[str] = []
    id: str | None = None


class RolesBody(BaseModel):
    judge: str | None = None
    fallback: str | None = None
    chat: str | None = None  # "" = same as the Judge
    embed: str | None = None


class PullBody(BaseModel):
    name: str


class ModelBody(BaseModel):
    model: str | None = None  # append one (LM Studio, Setup)
    models: list[str] | None = None  # replace the offered list (Models ▾)


class ProviderTestBody(BaseModel):
    model: str | None = None  # test this model rather than the provider's first one


LOOPBACK = ("127.0.0.1", "localhost", "::1")
# Keychain ids used for other keys (web search, YouTube): never a provider id.
RESERVED_IDS = frozenset({"brave-search", "youtube-data-api"})


def model_name_ok(m: str) -> bool:
    return bool(m) and len(m) <= 200 and not any(ch.isspace() for ch in m)


def clean_models(models: list[str]) -> list[str]:
    """Strip, de-duplicate, validate. Raises HTTPException(400) on a bad name."""
    out: list[str] = []
    for raw in models:
        m = str(raw).strip()
        if not m:
            continue
        if not model_name_ok(m):
            raise HTTPException(400, f"That model name doesn't look right: {m[:60]!r}")
        if m not in out:
            out.append(m)
    if len(out) > 300:
        raise HTTPException(400, "That's too many models to offer; tick fewer.")
    return out


def same_origin(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)


def pydantic_msg(e: ValidationError) -> str:
    return "; ".join(str(err["msg"]).removeprefix("Value error, ") for err in e.errors())[:300]


def config_writer(fn: Any) -> Any:
    """Routes that load → change → save config.yaml run one at a time."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with CONFIG_LOCK:
            return fn(*args, **kwargs)

    return wrapper


def probe(url: str) -> httpx.Response | None:
    """One quick GET to a local model server; None when nothing answers."""
    try:
        return httpx.get(url, timeout=1.5)
    except httpx.HTTPError:
        return None


def plain_title(title: str | None) -> str:
    """Posts sometimes carry markdown emphasis in their first line (Bluesky, Mastodon)."""
    return " ".join(re.sub(r"\*\*(.+?)\*\*", r"\1", title or "").split())


def safe_url(url: str | None) -> str | None:
    """Only http(s) links from feeds become hrefs; anything else gets no link at all."""
    u = (url or "").strip()
    if not u.lower().startswith(("https://", "http://")):
        return None
    if any(c in u for c in "\x00\r\n\t ") or not urlsplit(u).netloc:
        return None
    return u


def pretty_path(p: str | Path) -> str:
    """Show ~ instead of the home directory, so screenshots don't leak usernames."""
    s = str(p)
    home = str(Path.home())
    return "~" + s[len(home) :] if s.startswith(home) else s


class Jobs:
    """Tiny in-memory registry for background research and profile rebuilds."""

    def __init__(self) -> None:
        self.state: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def start(self, key: str, fn: Any) -> bool:
        with self.lock:
            if self.state.get(key, {}).get("state") == "running":
                return False
            self.state[key] = {"state": "running"}

        def work() -> None:
            try:
                fn()
                result = {"state": "done"}
            except Exception as e:
                log.exception("job %s failed", key)
                result = {"state": "error", "error": str(e)[:300]}
            with self.lock:
                self.state[key] = result

        threading.Thread(target=work, name=f"job-{key}", daemon=True).start()
        return True

    def get(self, key: str) -> dict[str, Any]:
        with self.lock:
            return dict(self.state.get(key, {"state": "idle"}))


# -- request bodies ------------------------------------------------------------------
Phrase = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class VoteBody(BaseModel):
    value: str


class TopicsBody(BaseModel):
    topics: list[str] = Field(max_length=40)


class SaveBody(BaseModel):
    saved: bool = True


class MuteBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    muted: bool = True


class ConfirmBody(BaseModel):
    confirmed: bool = False


class YamlBody(BaseModel):
    yaml: str = Field(max_length=200_000)


class ProjectEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: Annotated[str, StringConstraints(strip_whitespace=True, max_length=600)]
    stack: list[Phrase] = Field(default_factory=list, max_length=60)
    key_dependencies: list[Phrase] = Field(default_factory=list, max_length=60)
    interests: list[Phrase] = Field(default_factory=list, max_length=60)
    would_want_to_know: list[Phrase] = Field(default_factory=list, max_length=60)


def _json(text: str | None, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def _pid_alive(pid: int) -> bool | None:
    """True/False when we can tell, None when we can't (Windows: os.kill would terminate)."""
    if pid <= 0 or sys.platform == "win32":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


class _Rows:
    """A statement's rows, fetched while the lock was held."""

    def __init__(self, rows: list[Any], rowcount: int, lastrowid: int | None) -> None:
        self._rows = rows
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return self._rows

    def __iter__(self) -> Any:
        return iter(self._rows)


class _SerializedConnection:
    """The server shares one sqlite connection between request threads and background
    jobs. sqlite3 doesn't serialize cursors, so two requests reading at once can get each
    other's rows (or "API misuse" errors); the single page fires several requests at once.
    Run each statement and fetch its rows under the store's (reentrant) lock."""

    def __init__(self, conn: sqlite3.Connection, lock: Any) -> None:
        self._conn = conn
        self._lock = lock

    def execute(self, sql: str, params: Any = ()) -> _Rows:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return _Rows(cur.fetchall(), cur.rowcount, cur.lastrowid)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def load_digest_profile(cfg: Config) -> Profile:
    try:
        return load_profile(cfg.profile_file())
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return Profile(projects=[])


def digest_story(
    store: Store, row: sqlite3.Row, actions_day: set[int], full: bool
) -> dict[str, Any]:
    extra = _json(row["extra"], {})
    judged = row["summary"] is not None
    outline = _json(row["outline"], []) if judged else []
    s: dict[str, Any] = {
        "id": row["id"],
        "source": row["source"],
        "source_name": row["source_name"],
        "title": plain_title(row["title"]),
        "headline": row["headline"] if judged else None,
        "url": safe_url(row["url"]),
        "author": row["author"],
        "published": row["published_at"],
        "also_seen": _json(row["also_seen"], []),
        "video_id": extra.get("video_id") if row["source"] == "youtube" else None,
        "judged": judged,
        "topic": row["digest_topic"] if judged else None,
        "relevance": row["digest_relevance"] if judged else None,
        "match": max(0.0, min(1.0, float(row["prefilter"] or 0.0))),
        "summary": row["summary"] if judged else None,
        "outline": [str(x) for x in outline if isinstance(x, str)][:5],
        "saved": bool(row["saved_at"]),
        "vote": store.item_feedback(row["id"]),
    }
    if full:
        s["text"] = (row["text"] or "")[:900]
    if row["id"] in actions_day and judged and row["impact"] in IMPACTS:
        s["action"] = {
            "kind": row["impact"],
            "project": row["best_project"],
            "score": row["best_score"],
            "next_step": row["next_step"],
        }
    return s


def digest_payload(cfg: Config, store: Store, day: str | None = None) -> dict[str, Any]:
    """The Digest (by topic) for a night, as GET /api/digest sends it; the latest night when
    day is None. Listen reads the same payload. Raises ValueError for a malformed day."""
    days = sorted(set(store.first_seen_days()) | set(store.digest_days()), reverse=True)
    if day is None:
        day = days[0] if days else None
    elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("day must be YYYY-MM-DD")
    prof = load_digest_profile(cfg)
    topics = prof.digest_topics
    out: dict[str, Any] = {
        "day": day,
        "days": days,
        "topics": prof.topics,
        "digest_topics": topics,
        "projects": prof.names,
        "bar": cfg.thresholds.digest_bar,
        "max": cfg.thresholds.digest_max,
        "stories": [],
        "rest": [],
        "saved": [],
        "stats": None,
        "run": None,
    }
    weights = compute_weights(store)
    saved = store.saved_items()
    if day is not None:
        try:
            start, end = day_window(date.fromisoformat(day))
        except ValueError as e:
            raise ValueError("day must be a real date") from e
        rows = store.items_between(start, end)
        digest_row = store.digest(day)
        actions_day = set(_json(digest_row["item_ids"], [])) if digest_row else set()
        hidden = {r["id"] for r in rows if store.item_feedback(r["id"]) == -1}
        stories, rest = split_digest(
            rows, topics, weights, cfg.thresholds.digest_bar, cfg.thresholds.digest_max, hidden
        )
        out["stories"] = [digest_story(store, r, actions_day, True) for r in stories]
        out["rest"] = [digest_story(store, r, actions_day, True) for r in rest]
        run = None
        if digest_row and digest_row["run_id"]:
            run = store.conn.execute(
                "SELECT * FROM runs WHERE id=?", (digest_row["run_id"],)
            ).fetchone()
        elif rows:
            run = store.conn.execute(
                "SELECT * FROM runs WHERE id=?", (rows[-1]["run_id"],)
            ).fetchone()
        out["run"] = run  # the row; GET /api/digest sends run_dict(row)
        out["stats"] = {
            "new": len(rows),
            "judged": sum(1 for r in rows if r["summary"] is not None),
        }
    out["saved"] = [digest_story(store, r, set(), True) for r in saved]
    return out


def create_app(
    cfg: Config,
    config_path: Path,
    port: int = 8765,
    session_key: str | None = None,
    extra_hosts: tuple[str, ...] = (),
) -> FastAPI:
    store = Store(cfg.db_file())
    store.conn = _SerializedConnection(store.conn, store._lock)  # type: ignore[assignment]
    interrupted = store.fail_interrupted_actions()
    if interrupted:
        log.warning("marked %d interrupted Try-it run(s) as failed", interrupted)
    session_key = session_key or secrets.token_urlsafe(24)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        store.close()

    app = FastAPI(
        title="Night Scout", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    csrf = secrets.token_urlsafe(24)
    jobs = Jobs()
    state: dict[str, Any] = {"cfg": cfg}
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", *extra_hosts}
    app.state.session_key = session_key
    app.state.store = store
    app.state.csrf = csrf
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    def asset_version() -> str:
        return str(max(int(p.stat().st_mtime) for p in STATIC.iterdir() if p.is_file()))

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Response:
        host = request.headers.get("host", "")
        if host not in allowed_hosts:
            return Response("Night Scout only answers on localhost.", status_code=400)
        is_api = request.url.path.startswith("/api/")
        mutating = request.method not in ("GET", "HEAD", "OPTIONS")
        if mutating:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc not in allowed_hosts:
                return JSONResponse({"error": "Cross-site request refused."}, status_code=403)
        # Every page needs the per-launch session cookie. `night-scout serve` prints a
        # one-time link that sets it. Other local processes (say, a package being tried
        # in the sandbox) can reach 127.0.0.1 but can't see the key.
        if not request.url.path.startswith("/static/"):
            cookie = request.cookies.get(COOKIE, "")
            key = request.query_params.get("key")
            if not is_api and key is not None and secrets.compare_digest(key, session_key):
                clean = "/" + request.url.path.lstrip("/")  # never //host
                rest = [(k, v) for k, v in request.query_params.multi_items() if k != "key"]
                if rest:
                    clean += "?" + "&".join(f"{quote(k)}={quote(v)}" for k, v in rest)
                redirect = RedirectResponse(clean, status_code=303)
                redirect.set_cookie(COOKIE, session_key, httponly=True, samesite="strict")
                return redirect
            if not secrets.compare_digest(cookie, session_key):
                if is_api:
                    return JSONResponse(
                        {"error": "Open the link printed by `night-scout serve`."},
                        status_code=401,
                    )
                return HTMLResponse(
                    "<h1>Night Scout</h1><p>Open the link printed by "
                    "<code>night-scout serve</code> in your terminal.</p>",
                    status_code=401,
                )
            if is_api and mutating:
                token = request.headers.get(CSRF_HEADER, "")
                if not secrets.compare_digest(token, csrf):
                    return JSONResponse(
                        {"error": "Missing or stale page token. Reload the page."},
                        status_code=403,
                    )
        resp: Response = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = (
            "same-origin"  # no-referrer makes browsers send Origin: null
        )
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: https://i.ytimg.com; style-src 'self'; "
            "frame-src https://www.youtube-nocookie.com; "
            "script-src 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        if is_api:
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def bad_body(_request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [
            f"{'.'.join(str(p) for p in e.get('loc', ())[1:]) or 'body'}: {e.get('msg')}"
            for e in exc.errors()
        ]
        return JSONResponse({"error": "; ".join(problems)[:400]}, status_code=400)

    def llm() -> LLM:
        if "llm" not in state:
            state["llm"] = make_llm(state["cfg"], "judge")
        client: LLM = state["llm"]
        return client

    # -- shared shapes -----------------------------------------------------------------
    def run_active(run: sqlite3.Row | None) -> bool:
        """A run row says 'running' and its process still holds the run lock."""
        if run is None or run["status"] != "running":
            return False
        lock = state["cfg"].data_path() / "run.lock"
        try:
            pid = int(lock.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            pid = 0
        alive = _pid_alive(pid)
        if alive is not None:
            return alive
        try:
            started = datetime.fromisoformat(run["started_at"])
        except ValueError:
            return False
        return utcnow() - started < timedelta(hours=6)

    def run_dict(run: sqlite3.Row | None) -> dict[str, Any] | None:
        if run is None:
            return None
        stats = _json(run["stats"], {})
        return {
            "id": run["id"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "status": run["status"],
            "stats": {k: v for k, v in stats.items() if k != "progress"},
            "running": run_active(run),
        }

    def statuses_for(run_id: int | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        every = store.source_statuses(run_id) if run_id else []
        rows: list[dict[str, Any]] = [
            {
                "key": s.key,
                "label": s.label,
                "ok": s.ok,
                "fetched": s.fetched,
                "new": s.new,
                "message": s.message,
            }
            for s in every
        ]
        feeds = [r for r in rows if not r["key"].startswith("pipeline:")]
        pipeline = [r for r in rows if r["key"].startswith("pipeline:")]
        return feeds, pipeline

    def profile_projects() -> list[dict[str, Any]]:
        try:
            return load_profile(state["cfg"].profile_file()).projects
        except (FileNotFoundError, yaml.YAMLError, OSError):
            return []

    def schedule_info() -> dict[str, Any]:
        installed: bool | None = None
        via = None
        if sys.platform == "darwin":
            installed, via = schedule_mod.launchd_plist_path().exists(), "launchd"
        return {"time": state["cfg"].schedule.time, "installed": installed, "via": via}

    def sandbox_info() -> dict[str, Any]:
        sb = state["cfg"].sandbox
        try:
            mode, engine = choose_mode(sb)
            error = None
        except RuntimeError as e:
            mode, engine, error = None, None, str(e)
        return {
            "mode": mode,
            "engine": engine,
            "error": error,
            "timeout_s": sb.timeout_s,
            "prefer": sb.prefer,
            "container_image": sb.container_image,
            "is_mac": sys.platform == "darwin",
        }

    def action_dict(a: sqlite3.Row, projects: list[dict[str, Any]]) -> dict[str, Any]:
        detail = _json(a["detail"], {})
        result = _json(a["result"], None)
        if isinstance(result, dict) and result.get("path"):
            result["path"] = pretty_path(result["path"])
        out: dict[str, Any] = {
            "id": a["id"],
            "item_id": a["item_id"],
            "kind": a["kind"],
            "project": a["project"],
            "title": a["title"],
            "status": a["status"],
            "updated_at": a["updated_at"],
            "detail": {
                k: detail[k] for k in ("why", "plan", "note_markdown", "refused") if k in detail
            },
            "result": result,
        }
        if a["kind"] == "note_for_project" and a["status"] != "done":
            proj = next((p for p in projects if p.get("name") == a["project"]), None)
            if proj and proj.get("path"):
                try:
                    target = actions.note_path(
                        Path(str(proj["path"])).expanduser(),
                        state["cfg"].notes_subdir,
                        a["title"],
                        date.today(),
                    )
                    out["note_path"] = pretty_path(target)
                except actions.ActionError:
                    pass
        return out

    def card(row: sqlite3.Row, projects: list[dict[str, Any]]) -> dict[str, Any]:
        item_id = row["id"]
        score = store.score(item_id)
        extra = _json(row["extra"], {})
        research = store.research(item_id)
        c: dict[str, Any] = {
            "id": item_id,
            "source": row["source"],
            "source_name": row["source_name"],
            "title": row["title"],
            "url": safe_url(row["url"]),
            "author": row["author"],
            "published": row["published_at"],
            "also_seen": _json(row["also_seen"], []),
            "text": (row["text"] or "")[:1400],
            "transcript": str(extra.get("transcript_excerpt") or "")[:1200],
            "summary": score["summary"] if score else "",
            "best": score["best_project"] if score else None,
            "best_score": score["best_score"] if score else 0.0,
            "per_project": _json(score["per_project"], []) if score else [],
            "topics": _json(score["topics"], []) if score else [],
            "actions": [action_dict(a, projects) for a in store.actions_for(item_id)],
            "research": research["brief"] if research else None,
            "research_model": research["model"] if research else None,
            "research_job": jobs.get(f"research:{item_id}"),
            "vote": store.item_feedback(item_id),
        }
        # Pick labels arrive with the v2 judge (new `scores` columns); older rows are NULL.
        cols = set(score.keys()) if score is not None else set()
        if score is not None and "impact" in cols:
            impact = score["impact"]
            c["impact"] = impact if impact in IMPACTS else None
            matched = _json(score["matched"], []) if "matched" in cols else []
            c["matched"] = [str(m) for m in matched if isinstance(m, str)][:3]
            c["next_step"] = score["next_step"] if "next_step" in cols else None
        return c

    def has_table(name: str) -> bool:
        row = store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def ideas_for(day: str) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
        if not has_table("ideas"):
            return None, {}
        rows = store.conn.execute(
            "SELECT * FROM ideas WHERE day=? AND status!='dismissed' ORDER BY id", (day,)
        ).fetchall()
        ideas: list[dict[str, Any]] = []
        items: dict[str, Any] = {}
        for r in rows:
            inspired = [i for i in _json(r["inspired_by"], []) if isinstance(i, int)]
            ideas.append(
                {
                    "id": r["id"],
                    "title": r["title"],
                    "pitch": r["pitch"],
                    "why_you": r["why_you"],
                    "first_step": r["first_step"],
                    "inspired_by": inspired,
                    "status": r["status"],
                }
            )
            for iid in inspired:
                it = store.item(iid)
                if it is not None:
                    items[str(iid)] = {
                        "id": iid,
                        "title": it["title"],
                        "url": safe_url(it["url"]),
                        "source": it["source"],
                        "source_name": it["source_name"],
                    }
        return ideas, items

    # -- the page shell ----------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def shell(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "app.html", {"csrf": csrf, "v": asset_version()})

    # -- read API ------------------------------------------------------------------------
    def judge_view(c: Config) -> dict[str, Any]:
        ref = c.role_ref("judge") or f"ollama/{c.model.name}"
        pid, model = split_ref(ref)
        try:
            p = c.provider(pid)
            local = runs_locally(p)
            name = p.name or p.id
        except KeyError:
            local, name = False, pid
        return {"ref": ref, "model": model, "provider": name, "local": local}

    @app.get("/api/bootstrap")
    def bootstrap() -> Any:
        c = state["cfg"]
        run = store.last_run()
        feeds, pipeline = statuses_for(run["id"] if run else None)
        score_cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(scores)")}
        try:
            n_sources = len(all_tasks(c))
        except Exception:  # a bad config must not take the page down
            n_sources = len(feeds)
        return {
            "version": __version__,
            "platform": sys.platform,
            "judge": judge_view(c),
            "model": {
                "name": c.model.name,
                "fallback": c.model.fallback,
                "embed": c.model.embed,
                "host": c.model.host,
            },
            "run": run_dict(run),
            "statuses": feeds,
            "pipeline": pipeline,
            "sources_count": n_sources,
            "projects": [p["name"] for p in profile_projects()],
            "topics": digest_profile().topics,
            "schedule": schedule_info(),
            "sandbox": sandbox_info(),
            "thresholds": c.thresholds.model_dump(),
            "notes_subdir": c.notes_subdir,
            "data_dir": pretty_path(c.data_path()),
            "features": {"impact": "impact" in score_cols, "ideas": has_table("ideas")},
            "setup": setup_mod.setup_status(c),
        }

    @app.get("/api/morning")
    def morning(day: str | None = None) -> Any:
        digest = store.digest(day) if day else store.latest_digest()
        if day and digest is None:
            raise HTTPException(404, "No digest for that day")
        projects = profile_projects()
        weights = compute_weights(store)
        cards: list[dict[str, Any]] = []
        ignored = 0
        run = None
        if digest:
            for item_id in _json(digest["item_ids"], []):
                row = store.item(item_id)
                if row is None:
                    continue
                if item_id in weights.ignored_items:
                    ignored += 1
                    continue
                cards.append(card(row, projects))
            if digest["run_id"]:
                run = store.conn.execute(
                    "SELECT * FROM runs WHERE id=?", (digest["run_id"],)
                ).fetchone()
        best = None
        if run is not None:
            row = store.conn.execute(
                "SELECT MAX(s.best_score) FROM scores s JOIN items i ON i.id=s.item_id "
                "WHERE i.run_id=?",
                (run["id"],),
            ).fetchone()
            best = row[0] if row else None
        feeds, pipeline = statuses_for(run["id"] if run else None)
        ideas, idea_items = ideas_for(digest["day"]) if digest else (None, {})
        return {
            "day": digest["day"] if digest else None,
            "days": store.digest_days(),
            "cards": cards,
            "ignored": ignored,
            "run": run_dict(run),
            "statuses": feeds,
            "pipeline": pipeline,
            "best_score": best,
            "bar": state["cfg"].thresholds.relevance,
            "ideas": ideas,
            "idea_items": idea_items,
            "projects": [p["name"] for p in projects],
        }

    # -- the Digest (by topic) ----------------------------------------------------------------
    def digest_profile() -> Profile:
        return load_digest_profile(state["cfg"])

    @app.get("/api/digest")
    def digest_view(day: str | None = None) -> Any:
        try:
            out = digest_payload(state["cfg"], store, day)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        out["run"] = run_dict(out["run"])
        return out

    @app.get("/api/topics")
    def get_topics() -> Any:
        return {"topics": digest_profile().topics}

    @app.put("/api/topics")
    def put_topics(body: TopicsBody) -> Any:
        if jobs.get("profile")["state"] == "running":
            raise HTTPException(409, "A profile rebuild is running; save after it finishes.")
        try:
            topics = save_topics(state["cfg"].profile_file(), body.topics)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "topics": topics}

    @app.post("/api/item/{item_id}/save")
    def save_item(item_id: int, body: SaveBody) -> Any:
        if store.item(item_id) is None:
            raise HTTPException(404, "No such item")
        store.set_saved(item_id, body.saved)
        return {"ok": True, "saved": body.saved}

    @app.post("/api/source/mute")
    def mute_source(body: MuteBody) -> Any:
        known = store.conn.execute(
            "SELECT 1 FROM items WHERE source_name=? LIMIT 1", (body.name,)
        ).fetchone()
        if known is None:
            raise HTTPException(404, "No source by that name")
        with store.tx() as conn:  # mute and unmute both start from a clean slate for the source
            conn.execute(
                "DELETE FROM feedback WHERE target_type='source' AND target=?", (body.name,)
            )
        if body.muted:
            store.add_feedback("source", body.name, MUTE_VOTES, None)
        w = compute_weights(store)
        return {"ok": True, "name": body.name, "muted": w.muted(body.name)}

    def run_estimate() -> dict[str, int | None]:
        """Seconds a run usually takes, from the latest finished runs: `all` (Digest and
        Actions) and `digest` (Digest only: a Digest-only run's own time, else a full run minus
        its Actions planning). None when there's nothing to go on yet."""
        full = digest = None
        for r in store.recent_runs():
            if r["status"] != "done" or not r["finished_at"]:
                continue
            try:
                secs = int(
                    (
                        datetime.fromisoformat(r["finished_at"])
                        - datetime.fromisoformat(r["started_at"])
                    ).total_seconds()
                )
            except (TypeError, ValueError):
                continue
            stats = _json(r["stats"], {})
            if stats.get("mode") == "digest":
                digest = digest if digest is not None else secs
            elif full is None:
                full = secs
                planning = stats.get("planning_secs")
                if digest is None and isinstance(planning, int | float):
                    digest = max(60, secs - int(planning))
            if full is not None and digest is not None:
                break
        return {"all": full, "digest": digest}

    @app.get("/api/run")
    def run_status() -> Any:
        run = store.last_run()
        info = run_dict(run)
        out: dict[str, Any] = {"running": bool(info and info["running"]), "run": info}
        out["estimate"] = run_estimate()
        try:
            total = len(all_tasks(state["cfg"]))
        except Exception:
            total = 0
        out["sources_total"] = total
        out["projects"] = len(profile_projects())
        if not out["running"] or run is None:
            out["progress"] = None
            return out
        progress = _json(run["stats"], {}).get("progress")
        done = store.conn.execute(
            "SELECT COUNT(*) FROM source_status WHERE run_id=? AND key NOT LIKE 'pipeline:%'",
            (run["id"],),
        ).fetchone()[0]
        out["sources_done"] = done
        if not isinstance(progress, dict):
            # Before live progress lands, infer it: fetching until every source reported.
            if done < total:
                progress = {"stage": "fetch", "done": done, "total": total, "stages_done": []}
            else:
                progress = {
                    "stage": "judge",
                    "done": None,
                    "total": None,
                    "stages_done": ["fetch", "dedupe", "prefilter"],
                }
        out["progress"] = progress
        return out

    @app.get("/api/weights")
    def weights_view() -> Any:
        w = compute_weights(store)
        return {
            "source": {k: round(v, 3) for k, v in w.source.items()},
            "topic": {k: round(v, 3) for k, v in w.topic.items()},
            "muted": sorted(k for k, v in w.source.items() if v <= MUTE_AT),
        }

    @app.get("/api/guide")
    def guide() -> Any:
        return {"markdown": (STATIC / "guide.md").read_text(encoding="utf-8")}

    # -- votes ----------------------------------------------------------------------------
    @app.post("/api/item/{item_id}/vote")
    def vote(item_id: int, body: VoteBody) -> Any:
        row = store.item(item_id)
        if row is None:
            raise HTTPException(404, "No such item")
        if body.value == "clear":
            with store.tx() as conn:  # undo: forget every vote on this item
                conn.execute(
                    "DELETE FROM feedback WHERE target_type='item' AND item_id=?", (item_id,)
                )
        elif body.value in VOTES:
            store.add_feedback("item", str(item_id), VOTES[body.value], item_id)
        else:
            raise HTTPException(400, "vote must be up, down, ignore or clear")
        w = compute_weights(store)
        score = store.score(item_id)
        topics = _json(score["topics"], []) if score else []
        return {
            "ok": True,
            "vote": store.item_feedback(item_id),
            "source": {
                "name": row["source_name"],
                "weight": round(w.source_weight(row["source_name"]), 3),
                "muted": w.muted(row["source_name"]),
            },
            "topics": [[str(t), round(w.topic.get(str(t).lower(), 0.0), 3)] for t in topics],
        }

    # -- research -------------------------------------------------------------------------
    def research_view(item_id: int) -> dict[str, Any]:
        job = jobs.get(f"research:{item_id}")
        r = store.research(item_id)
        return {
            **job,
            "brief": r["brief"] if r else None,
            "model": r["model"] if r else None,
            "created_at": r["created_at"] if r else None,
        }

    @app.post("/api/item/{item_id}/research")
    def research(item_id: int) -> Any:
        if store.item(item_id) is None:
            raise HTTPException(404, "No such item")
        started = jobs.start(
            f"research:{item_id}",
            lambda: actions.research_more(state["cfg"], store, llm(), item_id),
        )
        return {"ok": True, "started": started, **research_view(item_id)}

    @app.get("/api/item/{item_id}/research")
    def research_status(item_id: int) -> Any:
        if store.item(item_id) is None:
            raise HTTPException(404, "No such item")
        return research_view(item_id)

    # -- actions --------------------------------------------------------------------------
    def get_action(action_id: int) -> sqlite3.Row:
        a = store.action(action_id)
        if a is None:
            raise HTTPException(404, "No such action")
        return a

    @app.get("/api/action/{action_id}")
    def action_view(action_id: int) -> Any:
        return action_dict(get_action(action_id), profile_projects())

    @app.get("/api/action/{action_id}/plan")
    def action_plan(action_id: int) -> Any:
        a = get_action(action_id)
        if a["kind"] != "try_it":
            raise HTTPException(404, "No such Try-it")
        plan = Plan.from_dict(_json(a["detail"], {}).get("plan") or {})
        verdict = check_commands(plan.commands(), smoke=plan.smoke_test)
        item = store.item(a["item_id"])
        return {
            "id": a["id"],
            "title": a["title"],
            "project": a["project"],
            "status": a["status"],
            "item_title": item["title"] if item else "",
            "plan": {
                "runtime": plan.runtime,
                "setup": plan.setup,
                "smoke_test": plan.smoke_test,
                "expected_output": plan.expected_output,
            },
            "verdict": {"ok": verdict.ok, "problems": verdict.problems},
            "sandbox": sandbox_info(),
        }

    @app.post("/api/action/{action_id}/run")
    def run_action(action_id: int, body: ConfirmBody) -> Any:
        if not body.confirmed:
            raise HTTPException(400, "Confirm first.")
        get_action(action_id)
        try:
            actions.start_try_it(state["cfg"], store, action_id)
        except actions.ActionError as e:
            raise HTTPException(409, str(e)) from e
        return {"ok": True, **action_dict(get_action(action_id), [])}

    @app.post("/api/action/{action_id}/note")
    def note(action_id: int) -> Any:
        a = get_action(action_id)
        try:
            path = actions.write_note(state["cfg"], store, action_id)
        except actions.ActionError as e:
            raise HTTPException(409, f"Couldn't write the note: {e}") from e
        return {"ok": True, "path": pretty_path(path), "project": a["project"]}

    # -- sources --------------------------------------------------------------------------
    def catalog() -> list[dict[str, Any]]:
        c = state["cfg"]
        try:  # Track B's catalog includes disabled sources; fall back to the task list
            from ..core import sources as src_mod

            fn = getattr(src_mod, "source_catalog", None)
            if fn is not None:
                return [dict(x) for x in fn(c)]
            out = []
            for t in all_tasks(c):
                kind = t.key.split(":", 1)[0]
                out.append(
                    {
                        "key": t.key,
                        "label": t.label,
                        "type": kind,
                        "detail": t.key[len(kind) + 1 :],
                        "enabled": True,
                    }
                )
            return out
        except Exception:
            log.exception("source catalog failed")
            return []

    @app.get("/api/sources")
    def sources_view() -> Any:
        c = state["cfg"]
        run = store.last_run()
        feeds, pipeline = statuses_for(run["id"] if run else None)
        text = yaml.safe_dump(
            c.sources.model_dump(mode="json", exclude_none=True),
            sort_keys=False,
            allow_unicode=True,
        )
        return {
            "catalog": catalog(),
            "statuses": feeds,
            "pipeline": pipeline,
            "run": run_dict(run),
            "yaml": text,
            "weights": weights_view(),
            "schedule": schedule_info(),
            "look_back_default": c.thresholds.max_age_days,
            "youtube_api_key": youtube_key_view() is not None,
            "ytdlp": ytdlp_command() is not None,
        }

    @app.post("/api/sources/yaml")
    @config_writer
    def save_sources(body: YamlBody) -> Any:
        try:
            data = yaml.safe_load(body.yaml) or {}
            new_sources = Sources.model_validate(data)
        except yaml.YAMLError as e:
            raise HTTPException(400, f"Not valid YAML: {str(e)[:300]}") from e
        except ValidationError as e:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
            )
            raise HTTPException(400, problems[:400]) from e
        cfg_now = load_config(config_path)
        cfg_now.sources = new_sources
        save_config(cfg_now, config_path)
        state["cfg"] = cfg_now
        return {"ok": True, "count": len(all_tasks(cfg_now))}

    # -- projects -------------------------------------------------------------------------
    @app.get("/api/projects")
    def projects_view() -> Any:
        path = state["cfg"].profile_file()
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as e:
                raise HTTPException(500, f"profile.yaml isn't valid YAML: {e}") from e
        out = []
        for p in profile_projects():
            out.append(
                {
                    "name": p["name"],
                    "path": pretty_path(str(p.get("path") or "")),
                    "purpose": str(p.get("purpose") or ""),
                    **{k: [str(x) for x in (p.get(k) or [])] for k in PROFILE_LISTS},
                    "generated_by": p.get("generated_by"),
                }
            )
        return {
            "exists": path.exists(),
            "generated_at": data.get("generated_at") if isinstance(data, dict) else None,
            "projects": out,
            "job": jobs.get("profile"),
        }

    @app.put("/api/projects/{name}")
    def save_project(name: str, body: ProjectEdit) -> Any:
        path = state["cfg"].profile_file()
        if not path.exists():
            raise HTTPException(404, "No profile yet. Run `night-scout profile`.")
        if jobs.get("profile")["state"] == "running":
            raise HTTPException(409, "A rebuild is running; save after it finishes.")
        text = path.read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            raise HTTPException(500, f"profile.yaml isn't valid YAML: {e}") from e
        projects = data.get("projects") if isinstance(data, dict) else None
        if not isinstance(projects, list):
            raise HTTPException(500, "profile.yaml has no `projects:` list")
        entry = next((p for p in projects if isinstance(p, dict) and p.get("name") == name), None)
        if entry is None:
            raise HTTPException(404, f"No project named {name!r}")
        entry["purpose"] = body.purpose
        for k in PROFILE_LISTS:
            entry[k] = list(dict.fromkeys(getattr(body, k)))  # keep order, drop repeats
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
        saved = next(p for p in projects_view()["projects"] if p["name"] == name)
        return {"ok": True, "project": saved}

    @app.post("/api/projects/rebuild")
    def rebuild() -> Any:
        started = jobs.start("profile", lambda: build_profile_file(state["cfg"], llm()))
        return {"ok": True, "started": started, **jobs.get("profile")}

    @app.get("/api/projects/rebuild")
    def rebuild_status() -> Any:
        return jobs.get("profile")

    # -- sources: toggle and add -------------------------------------------------------------
    def save_cfg(cfg_new: Config) -> None:
        save_config(cfg_new, config_path)
        state["cfg"] = load_config(config_path)
        state.pop("llm", None)  # roles or providers may have changed

    # chat drawer (Track B)
    from .chat_routes import register as register_chat

    register_chat(
        app, store=store, state=state, config_path=config_path, save_cfg=save_cfg, jobs=jobs
    )

    # Listen to your Digest + Settings → Voice (the optional voice add-on)
    from .listen_routes import register as register_listen

    register_listen(app, store=store, state=state, config_path=config_path, save_cfg=save_cfg)

    @app.post("/api/sources/toggle")
    @config_writer
    def toggle_source(body: ToggleBody) -> Any:
        c = load_config(config_path)
        try:
            set_source_enabled(c, body.key, body.enabled)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        save_cfg(c)
        return {"ok": True, "key": body.key, "enabled": body.enabled}

    def scout_now(key: str, terms: list[str], look_back: int) -> dict[str, Any]:
        """The one-off key-term search for a source; never raises (the source is saved)."""
        c = state["cfg"]
        try:
            with PoliteClient(c.contact) as client:
                res = run_scout(c, store, key, terms, client)
            return {"scout": res.to_dict(look_back)}
        except ScoutError as e:
            return {"scout_error": str(e)}
        except Exception as e:  # a search bug must not lose the saved source
            log.exception("key-term search failed for %s", key)
            return {"scout_error": f"The search failed ({type(e).__name__})."}

    def set_scout(c: Config, key: str, look_back: int | None, terms: list[str]) -> list[str]:
        """Store look back + terms for `key`; returns the terms that are new for it."""
        if key.split(":", 1)[0] not in SCOUTABLE_TYPES:
            raise HTTPException(
                400, "Look back and key terms work on YouTube channels, feeds and subreddits."
            )
        try:
            clean = clean_scout_terms(terms)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        old = c.sources.scout.get(key)
        was = {t.casefold() for t in (old.terms if old else [])}
        if look_back is None and not clean:
            c.sources.scout.pop(key, None)
        else:
            c.sources.scout[key] = ScoutSettings(look_back_days=look_back, terms=clean)
        return [t for t in clean if t.casefold() not in was]

    @app.post("/api/sources/add")
    def add_source_route(body: AddSourceBody) -> Any:
        settings = body.look_back_days is not None or body.scout_terms is not None
        existing = False
        with CONFIG_LOCK:
            c = load_config(config_path)
            try:
                with PoliteClient(c.contact) as client:
                    key, label = add_source_keyed(c, body.type, body.value.strip()[:500], client)
            except AlreadyFollowing as e:
                # Adding one you already follow with settings updates its settings.
                if not (settings and e.key and e.key.split(":", 1)[0] in SCOUTABLE_TYPES):
                    raise HTTPException(400, str(e)) from e
                key, label, existing = e.key, e.label or e.key, True
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            except SourceError as e:
                raise HTTPException(400, f"Couldn't reach it: {e}") from e
            terms: list[str] = []
            if settings and key.split(":", 1)[0] in SCOUTABLE_TYPES:
                # search the terms it didn't have (all of them for a new source)
                terms = set_scout(c, key, body.look_back_days, body.scout_terms or [])
            save_cfg(c)
        look_back = body.look_back_days or state["cfg"].thresholds.max_age_days
        out: dict[str, Any] = {"ok": True, "label": label, "key": key, "existing": existing}
        if terms:
            out.update(scout_now(key, terms, look_back))
        out["catalog"] = catalog()
        return out

    @app.post("/api/sources/scout")
    def scout_route(body: ScoutBody) -> Any:
        """Change a source's look back / key terms. New terms on a YouTube channel are searched
        across the whole channel once; on a feed, the feed is checked for them."""
        with CONFIG_LOCK:
            c = load_config(config_path)
            if body.key not in {t.key for t in configured_tasks(c)}:
                raise HTTPException(400, f"No source with key {body.key!r}.")
            added = set_scout(c, body.key, body.look_back_days, body.scout_terms)
            save_cfg(c)
        out: dict[str, Any] = {"ok": True, "key": body.key}
        if added:
            out.update(
                scout_now(
                    body.key, added, body.look_back_days or state["cfg"].thresholds.max_age_days
                )
            )
        out["catalog"] = catalog()
        return out

    # -- ideas ----------------------------------------------------------------------------------
    @app.post("/api/ideas/{idea_id}/dismiss")
    def dismiss_idea(idea_id: int) -> Any:
        if store.idea(idea_id) is None:
            raise HTTPException(404, "No such idea")
        store.set_idea_status(idea_id, "dismissed")
        return {"ok": True}

    @app.post("/api/ideas/{idea_id}/start")
    @config_writer
    def start_idea(idea_id: int) -> Any:
        if jobs.get("profile").get("state") == "running":
            raise HTTPException(409, "A profile rebuild is running. Try again when it's done.")
        try:
            path = start_project_from_idea(
                state["cfg"], store, llm(), idea_id, config_path=config_path
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except ModelError as e:
            raise HTTPException(503, f"The model couldn't profile the new project: {e}") from e
        state["cfg"] = load_config(config_path)
        return {"ok": True, "path": pretty_path(str(path))}

    # -- providers, keys, roles ----------------------------------------------------------------
    is_local = runs_locally

    def quick_status(p: ProviderConfig) -> tuple[str, str | None]:
        """connected/offline for local servers (cheap); 'unknown' for remote ones."""
        if not is_local(p):
            return "unknown", None
        url = p.base_url.rstrip("/") + ("/api/version" if p.kind == "ollama" else "/models")
        r = probe(url)
        if r is None or r.status_code >= 500:
            return "offline", None
        version = None
        if p.kind == "ollama" and r.status_code == 200:
            try:
                version = r.json().get("version")
            except ValueError:
                version = None
        return "connected", version

    def role_labels(c: Config) -> dict[str, str]:
        names = {
            "judge": "Judge · Research",
            "chat": "Chat",
            "fallback": "Fallback",
            "embed": "Embeddings",
        }
        out: dict[str, str] = {}
        for role, label in names.items():
            ref = c.roles.chat if role == "chat" else c.role_ref(role)  # type: ignore[arg-type]
            if ref:
                out[ref] = f"{out[ref]} · {label}" if ref in out else label
        return out

    def key_view(p: ProviderConfig) -> dict[str, Any] | None:
        """Configured? From where? Masked. Never raises: a locked keychain reads as unknown."""
        env_val = os.environ.get(p.api_key_env or "", "").strip() if p.api_key_env else ""
        if env_val:
            return {"configured": True, "source": "env", "masked": masked(env_val)}
        try:
            src = key_source(p.id, None)
            if not src:
                return None
            return {"configured": True, "source": src, "masked": masked(get_key(p.id, None))}
        except Exception:
            return {
                "configured": True,
                "source": "keychain",
                "masked": "••••••••",
                "unreadable": True,
            }

    def providers_view() -> dict[str, Any]:
        c = state["cfg"]
        labels = role_labels(c)
        try:
            have_keychain = keychain_available()
        except Exception:
            have_keychain = False
        out = []
        ids = [p.id for p in c.all_providers()]
        if "lmstudio" not in ids:
            ids.append("lmstudio")  # always offer the LM Studio card
        for pid, pre in PRESETS.items():  # a key in the environment counts before it's saved
            if pid not in ids and pre.api_key_env and os.environ.get(pre.api_key_env, "").strip():
                ids.append(pid)
        for pid in ids:
            p = c.provider(pid) if pid in {x.id for x in c.all_providers()} else preset(pid)
            status, version = quick_status(p)
            models: list[dict[str, Any]] = []
            if p.kind == "ollama" and status == "connected":
                try:
                    for m in ollama_models(c, p.id):
                        models.append(
                            {
                                "name": m.name,
                                "size": m.size,
                                "params": m.parameter_size,
                                "quant": m.quantization,
                                "role": labels.get(f"{p.id}/{m.name}", ""),
                            }
                        )
                except ModelError:
                    status = "offline"
            else:
                models = [{"name": m, "role": labels.get(f"{p.id}/{m}", "")} for m in p.models]
            key = key_view(p) if p.kind != "ollama" else None
            out.append(
                {
                    "id": p.id,
                    "kind": p.kind,
                    "name": p.name or p.id,
                    "base_url": p.base_url,
                    "status": status,
                    "version": version,
                    "models": models,
                    "key": key,
                    "local": is_local(p),
                    "configured": pid in {x.id for x in c.all_providers()},
                }
            )
        return {"providers": out, "roles": roles_view(c), "keychain": have_keychain}

    def roles_view(c: Config) -> dict[str, str | None]:
        """Effective refs; chat is "" while it follows the Judge."""
        roles: dict[str, str | None] = {
            r: c.role_ref(r)  # type: ignore[arg-type]
            for r in ("judge", "fallback", "embed")
        }
        roles["chat"] = c.roles.chat or ""
        return roles

    def reset_roles(c: Config, provider_id: str) -> None:
        """Roles on a removed provider go back to the defaults (Gemma; chat follows the Judge)."""
        for role in ("judge", "fallback", "chat", "embed"):
            ref = getattr(c.roles, role)
            if ref and ref.split("/", 1)[0] == provider_id:
                setattr(c.roles, role, None)

    @app.get("/api/providers")
    def get_providers() -> Any:
        return providers_view()

    def ensure_provider(c: Config, pid: str) -> ProviderConfig:
        for p in c.providers:
            if p.id == pid:
                return p
        if pid in PRESETS:
            p = preset(pid)
            c.providers.append(p)
            return p
        if pid in {x.id for x in c.all_providers()}:  # the built-in Ollama provider
            return c.provider(pid)
        raise HTTPException(404, f"No provider {pid!r}")

    @app.put("/api/keys/{provider_id}")
    @config_writer
    def put_key(provider_id: str, body: KeyBody) -> Any:
        c = load_config(config_path)
        p = ensure_provider(c, provider_id)
        if p.kind == "ollama":
            raise HTTPException(400, "Ollama doesn't use an API key.")
        if body.model:
            m = body.model.strip()
            if not model_name_ok(m):
                raise HTTPException(400, "That model name doesn't look right.")
            if m not in p.models:
                p.models.append(m)
        try:
            set_key(provider_id, body.key)
        except SecretStoreUnavailable as e:
            raise HTTPException(503, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        save_cfg(c)
        return {"ok": True}

    @app.delete("/api/keys/{provider_id}")
    @config_writer
    def del_key(provider_id: str) -> Any:
        c = load_config(config_path)
        try:
            delete_key(provider_id)
        except SecretStoreUnavailable as e:
            raise HTTPException(503, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        reset_roles(c, provider_id)  # roles on it go back to Gemma
        save_cfg(c)
        return {"ok": True}

    @app.post("/api/providers")
    @config_writer
    def add_provider(body: NewProvider) -> Any:
        c = load_config(config_path)
        base = "".join(ch if ch.isalnum() else "-" for ch in body.name.lower()).strip("-")[:24]
        pid, n = base or "custom", 2
        taken = {p.id for p in c.all_providers()} | set(PRESETS) | RESERVED_IDS
        while pid in taken:
            pid, n = f"{base[:20]}-{n}", n + 1
        models = clean_models([*(body.models or []), *([body.model] if body.model else [])])
        try:
            p = ProviderConfig(
                id=pid,
                kind="openai_compatible",
                name=provider_name(body.name),
                base_url=body.base_url.strip(),
                models=models,
            )
        except ValidationError as e:
            raise HTTPException(400, pydantic_msg(e)) from e
        if body.api_key and body.api_key.strip():
            try:
                set_key(pid, body.api_key)
            except SecretStoreUnavailable as e:
                raise HTTPException(503, str(e)) from e
        c.providers.append(p)
        save_cfg(c)
        return {"ok": True, "id": pid}

    def provider_name(raw: str) -> str:
        name = " ".join(raw.split())
        if not name:
            raise HTTPException(400, "Give the provider a name.")
        return name[:60]

    def custom_provider(c: Config, pid: str) -> ProviderConfig:
        """A provider added with "+ Add custom provider" (not a preset, not Ollama)."""
        for p in c.providers:
            if p.id == pid and pid not in PRESETS and p.kind == "openai_compatible":
                return p
        raise HTTPException(404, f"No custom provider {pid!r}")

    def stored_key(c: Config, pid: str | None, base_url: str) -> str | None:
        """The saved key of custom provider `pid`, but only for the address it was saved
        for: a changed host never receives the old key."""
        if not pid:
            return None
        try:
            p = custom_provider(c, pid)
        except HTTPException:
            return None
        if not same_origin(p.base_url, base_url):
            return None
        try:
            return get_key(p.id, p.api_key_env)
        except SecretStoreUnavailable:
            return None

    def draft_provider(name: str, base_url: str, models: list[str], pid: str) -> ProviderConfig:
        try:
            return ProviderConfig(
                id=pid,
                kind="openai_compatible",
                name=name,
                base_url=base_url.strip(),
                models=models,
            )
        except ValidationError as e:
            raise HTTPException(400, pydantic_msg(e)) from e

    @app.get("/api/providers/{provider_id}/models")
    def live_models(provider_id: str) -> Any:
        """What the provider offers right now, ticked where it's offered for roles."""
        c = state["cfg"]
        if provider_id in {p.id for p in c.all_providers()}:
            p = c.provider(provider_id)
        elif provider_id in PRESETS:
            p = preset(provider_id)
        else:
            raise HTTPException(404, f"No provider {provider_id!r}")
        if p.kind == "ollama":
            raise HTTPException(400, "Ollama lists its own models; pull one instead.")
        try:
            key = get_key(p.id, p.api_key_env)
        except SecretStoreUnavailable:
            key = None
        if not key and not runs_locally(p):
            raise HTTPException(400, f"Add a {p.label} API key first.")
        try:
            models = fetch_models(p, key)
        except (ModelUnavailable, ModelError) as e:
            raise HTTPException(502, f"{p.label}: {e}") from None
        offered = list(p.models)
        listed = {m["id"] for m in models}
        out = [{**m, "offered": m["id"] in offered} for m in models]
        out += [{"id": m, "offered": True} for m in offered if m not in listed]
        return {"models": out}

    @app.put("/api/providers/{provider_id}/models")
    @config_writer
    def set_provider_models(provider_id: str, body: ModelBody) -> Any:
        """{models: [...]} replaces the offered list; {model} appends one (LM Studio, Setup)."""
        c = load_config(config_path)
        p = ensure_provider(c, provider_id)
        if p.kind == "ollama":
            raise HTTPException(400, "Ollama lists its own models; pull one instead.")
        if body.models is not None:
            p.models = clean_models(body.models)
        else:
            m = (body.model or "").strip()
            if not model_name_ok(m):
                raise HTTPException(400, "That model name doesn't look right.")
            if m not in p.models:
                p.models.append(m)
        save_cfg(c)
        return {"ok": True, "models": p.models}

    @app.put("/api/providers/{provider_id}")
    @config_writer
    def edit_provider(provider_id: str, body: EditProvider) -> Any:
        c = load_config(config_path)
        old = custom_provider(c, provider_id)
        new = draft_provider(
            provider_name(body.name), body.base_url, clean_models(body.models), provider_id
        )
        new.api_key_env = old.api_key_env
        key = (body.api_key or "").strip()
        dropped = False
        try:
            if key:
                set_key(provider_id, key)
            elif not same_origin(old.base_url, new.base_url):
                dropped = delete_key(provider_id)  # the old key belonged to the old host
        except SecretStoreUnavailable as e:
            if key or keychain_available():
                raise HTTPException(503, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        c.providers = [new if p.id == provider_id else p for p in c.providers]
        save_cfg(c)
        return {"ok": True, "id": provider_id, "key_dropped": dropped}

    @app.delete("/api/providers/{provider_id}")
    @config_writer
    def remove_provider(provider_id: str) -> Any:
        """Remove a provider and its keychain key; roles that used it go back to the defaults."""
        c = load_config(config_path)
        if provider_id == "ollama":
            raise HTTPException(400, "The built-in Ollama provider can't be removed.")
        if provider_id not in {p.id for p in c.providers}:
            raise HTTPException(404, f"No provider {provider_id!r}")
        try:
            delete_key(provider_id)
        except SecretStoreUnavailable as e:
            if keychain_available():  # a key may be there and we couldn't remove it
                raise HTTPException(503, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        c.providers = [p for p in c.providers if p.id != provider_id]
        reset_roles(c, provider_id)
        save_cfg(c)
        return {"ok": True}

    @app.post("/api/providers/probe-models")
    def probe_models(body: ProbeBody) -> Any:
        """The custom-provider sheet's "Fetch models": list a server's models before saving."""
        c = state["cfg"]
        p = draft_provider("draft", body.base_url, [], "draft")
        key = (body.api_key or "").strip() or stored_key(c, body.id, p.base_url)
        try:
            models = fetch_models(p, key)
        except (ModelUnavailable, ModelError) as e:
            raise HTTPException(502, str(e)) from None
        return {"models": [{"id": m["id"]} for m in models]}

    @app.post("/api/providers/test-draft")
    def test_draft(body: DraftBody) -> Any:
        """Health → Auth → Chat for the sheet's values, without saving anything."""
        c = state["cfg"].model_copy(deep=True)
        pid = body.id if body.id and body.id in {p.id for p in c.providers} else "draft-test"
        if pid != "draft-test":
            custom_provider(c, pid)  # 404 unless it's a custom provider
        p = draft_provider(provider_name(body.name), body.base_url, clean_models(body.models), pid)
        key = (body.api_key or "").strip() or stored_key(state["cfg"], body.id, p.base_url)
        c.providers = [x for x in c.providers if x.id != pid] + [p]
        steps = test_provider(c, pid, api_key=key)
        return {"steps": [s.to_dict() for s in steps]}

    @app.post("/api/providers/{provider_id}/test")
    def test_provider_route(provider_id: str, body: ProviderTestBody | None = None) -> Any:
        c = state["cfg"]
        model = (body.model or "").strip() if body else ""
        if model and (len(model) > 200 or any(ch.isspace() for ch in model)):
            raise HTTPException(400, "That model name doesn't look right.")
        known = {p.id for p in c.all_providers()}
        if provider_id not in known and provider_id not in PRESETS:
            raise HTTPException(404, f"No provider {provider_id!r}")
        if provider_id not in known:  # e.g. LM Studio before it's added: test the preset
            c = c.model_copy(deep=True)
            c.providers.append(preset(provider_id))
        steps = test_provider(c, provider_id, model=model or None)
        return {"steps": [s.to_dict() for s in steps]}

    @app.put("/api/roles")
    @config_writer
    def put_roles(body: RolesBody) -> Any:
        c = load_config(config_path)
        for role, ref in body.model_dump(exclude_none=True).items():
            if role == "chat" and not ref.strip():
                c.roles.chat = None  # follow the Judge
                continue
            try:
                pid, _model = split_ref(ref)
                p = c.provider(pid)
            except (ValueError, KeyError) as e:
                raise HTTPException(400, str(e)) from e
            if role == "embed" and not is_local(p):
                raise HTTPException(400, "Embeddings stay on this machine.")
            setattr(c.roles, role, ref)
        save_cfg(c)
        return {"ok": True, "roles": roles_view(state["cfg"])}

    # -- ollama models and pull ---------------------------------------------------------------
    pulls: dict[str, dict[str, Any]] = {}

    @app.get("/api/ollama/models")
    def get_ollama_models() -> Any:
        try:
            ms = ollama_models(state["cfg"])
        except ModelError as e:
            raise HTTPException(503, str(e)) from e
        labels = role_labels(state["cfg"])
        return {
            "models": [
                {
                    "name": m.name,
                    "size": m.size,
                    "params": m.parameter_size,
                    "quant": m.quantization,
                    "role": labels.get(f"ollama/{m.name}", ""),
                }
                for m in ms
            ]
        }

    @app.post("/api/ollama/pull")
    def start_pull(body: PullBody) -> Any:
        name = body.name.strip()
        try:
            stream = ollama_pull(state["cfg"], name)  # validates the name now
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        pulls[name] = {"status": "starting", "percent": None}

        def work() -> None:
            for prog in stream:
                pulls[name] = {"status": prog.status, "percent": prog.percent}
            pulls[name] = {"status": "success", "percent": 100.0}

        started = jobs.start(f"pull:{name}", work)
        return {"ok": True, "started": started, "name": name}

    @app.get("/api/ollama/pull")
    def pull_status(name: str) -> Any:
        job = jobs.get(f"pull:{name}")
        return {**job, **pulls.get(name, {})}

    # -- setup walkthrough ---------------------------------------------------------------------
    def setup_project_rows(c: Config) -> list[dict[str, Any]]:
        profiled = setup_mod.profiled_names(c.profile_file())
        rows = []
        for ref in c.projects:
            name = setup_mod.project_name(ref)
            root = ref.resolved()
            rows.append(
                {
                    "name": name,
                    "path": pretty_path(str(root)),
                    "exists": root.is_dir(),
                    "found": setup_mod.project_files(root) if root.is_dir() else [],
                    "profiled": name in profiled,
                    "read": jobs.get(f"read:{name}"),
                }
            )
        return rows

    @app.get("/api/setup")
    def setup_view() -> Any:
        c = state["cfg"]
        suggestions = [{**s, "path": pretty_path(s["path"])} for s in setup_mod.suggest_projects(c)]
        return {
            **setup_mod.setup_status(c),
            "projects": setup_project_rows(c),
            "suggestions": suggestions,
            "schedule": schedule_info(),
            "platform": sys.platform,
        }

    @app.post("/api/projects/add")
    @config_writer
    def add_project_route(body: AddProjectBody) -> Any:
        c = load_config(config_path)
        try:
            ref, found = setup_mod.add_project(c, body.path)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        save_cfg(c)
        name = setup_mod.project_name(ref)
        return {
            "ok": True,
            "project": {"name": name, "path": pretty_path(str(ref.resolved())), "found": found},
        }

    @app.delete("/api/projects/{name}")
    @config_writer
    def remove_project_route(name: str) -> Any:
        if jobs.get("profile")["state"] == "running" or jobs.get(f"read:{name}")["state"] == (
            "running"
        ):
            raise HTTPException(409, "Gemma is reading projects; try again when it's done.")
        c = load_config(config_path)
        try:
            setup_mod.remove_project(c, name)
        except KeyError as e:
            raise HTTPException(404, f"No project named {name!r}") from e
        try:
            setup_mod.remove_profile_entry(c.profile_file(), name)
        except (ValueError, yaml.YAMLError) as e:
            raise HTTPException(500, str(e)) from e
        save_cfg(c)
        return {"ok": True, "name": name}

    @app.post("/api/projects/{name}/read")
    def read_project(name: str) -> Any:
        if jobs.get("profile")["state"] == "running":
            raise HTTPException(409, "A profile rebuild is running; try again when it's done.")
        c = state["cfg"]
        try:
            ref = setup_mod.find_project(c, name)
        except KeyError as e:
            raise HTTPException(404, f"No project named {name!r}") from e
        if not ref.resolved().is_dir():
            raise HTTPException(400, f"The folder for {name!r} isn't there any more.")

        def work() -> None:
            entry = profile_project(ref, llm())
            entry["name"] = name  # keep the name the config uses
            setup_mod.upsert_profile_entry(state["cfg"].profile_file(), entry)

        started = jobs.start(f"read:{name}", work)
        return {"ok": True, "started": started, **jobs.get(f"read:{name}")}

    @app.get("/api/projects/{name}/read")
    def read_project_status(name: str) -> Any:
        return jobs.get(f"read:{name}")

    @app.get("/api/schedule")
    def schedule_view() -> Any:
        out: dict[str, Any] = schedule_info()
        out["plist"] = None
        if sys.platform == "darwin":
            raw = schedule_mod.launchd_plist(state["cfg"], config_path).decode()
            home = str(Path.home())
            out["plist"] = raw.replace(home, "~")
            out["plist_path"] = pretty_path(schedule_mod.launchd_plist_path())
        return out

    @app.put("/api/schedule")
    @config_writer
    def put_schedule(body: ScheduleBody) -> Any:
        c = load_config(config_path)
        try:
            c.schedule = Schedule(time=body.time)
        except ValidationError as e:
            raise HTTPException(400, "Use 24-hour time, like 04:30.") from e
        save_cfg(c)
        info = schedule_info()
        if body.nightly:
            message = schedule_mod.install(state["cfg"], config_path)
        elif info["installed"]:
            message = schedule_mod.uninstall()
        else:
            message = "Not scheduled. Run it with `night-scout run` whenever you like."
        return {"ok": True, "message": message, "schedule": schedule_info()}

    @app.post("/api/run")
    def run_now(body: RunBody | None = None) -> Any:
        mode = body.mode if body else "all"
        if run_status()["running"]:
            raise HTTPException(409, "The scout is already out.")
        # Same rule as the pipeline: topics alone are enough (the Digest), projects add Actions.
        if not profile_projects() and not digest_profile().topics:
            raise HTTPException(400, "Add a topic or a project first.")
        pid = setup_mod.start_run(state["cfg"], config_path, digest_only=mode == "digest")
        return {"ok": True, "pid": pid, "mode": mode}

    # -- not wired yet --------------------------------------------------------------------
    def not_yet() -> JSONResponse:
        return JSONResponse(NOT_YET, status_code=501)

    for method, route in NOT_YET_ROUTES:
        app.add_api_route(route, not_yet, methods=[method], include_in_schema=False)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"])
    def api_404(rest: str) -> JSONResponse:
        return JSONResponse({"error": "no such endpoint"}, status_code=404)

    return app
