"""Local web app: a single page (static JS) over a small JSON API.

Binds to 127.0.0.1 only. Every request needs the per-launch session cookie, requests with
a foreign Host or Origin are refused, and every mutating API call needs the per-launch
CSRF token in the X-CSRF-Token header, so other websites can't drive it.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

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
from ..core.config import Config, Sources, load_config, save_config
from ..core.db import Store
from ..core.feedback import MUTE_AT, compute_weights
from ..core.llm import OllamaClient
from ..core.models import utcnow
from ..core.profile import build_profile_file, load_profile
from ..core.safety import check_commands
from ..core.sandbox import Plan, choose_mode
from ..core.sources import all_tasks

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
NOT_YET_ROUTES: list[tuple[str, str]] = [
    ("POST", "/api/sources/toggle"),
    ("POST", "/api/sources/add"),
    ("POST", "/api/ideas/{idea_id}/dismiss"),
    ("POST", "/api/ideas/{idea_id}/start"),
    ("GET", "/api/providers"),
    ("POST", "/api/providers"),
    ("POST", "/api/providers/{provider_id}/test"),
    ("PUT", "/api/keys/{provider_id}"),
    ("DELETE", "/api/keys/{provider_id}"),
    ("PUT", "/api/roles"),
    ("GET", "/api/ollama/models"),
    ("POST", "/api/ollama/pull"),
]


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


def create_app(
    cfg: Config,
    config_path: Path,
    port: int = 8765,
    session_key: str | None = None,
    extra_hosts: tuple[str, ...] = (),
) -> FastAPI:
    store = Store(cfg.db_file())
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
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
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

    def llm() -> OllamaClient:
        if "llm" not in state:
            state["llm"] = OllamaClient(state["cfg"].model)
        client: OllamaClient = state["llm"]
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
            from ..core.schedule import launchd_plist_path

            installed, via = launchd_plist_path().exists(), "launchd"
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
            "schedule": schedule_info(),
            "sandbox": sandbox_info(),
            "thresholds": c.thresholds.model_dump(),
            "notes_subdir": c.notes_subdir,
            "data_dir": pretty_path(c.data_path()),
            "features": {"impact": "impact" in score_cols, "ideas": has_table("ideas")},
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

    @app.get("/api/run")
    def run_status() -> Any:
        run = store.last_run()
        info = run_dict(run)
        out: dict[str, Any] = {"running": bool(info and info["running"]), "run": info}
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
        }

    @app.post("/api/sources/yaml")
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

    # -- not wired yet --------------------------------------------------------------------
    def not_yet() -> JSONResponse:
        return JSONResponse(NOT_YET, status_code=501)

    for method, route in NOT_YET_ROUTES:
        app.add_api_route(route, not_yet, methods=[method], include_in_schema=False)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"])
    def api_404(rest: str) -> JSONResponse:
        return JSONResponse({"error": "no such endpoint"}, status_code=404)

    return app
