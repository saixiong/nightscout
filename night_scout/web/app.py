"""Local web UI: Morning digest, item detail, Try-it, Research, Sources and Profile editors.

Binds to 127.0.0.1 only. Every POST needs the per-launch CSRF token, and requests
with a foreign Host or Origin are refused, so other websites can't drive it.
"""

from __future__ import annotations

import json
import logging
import secrets
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import yaml
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from pydantic import ValidationError

from ..core import actions
from ..core.config import Config, Sources, load_config, save_config
from ..core.db import Store
from ..core.digest import ACTION_LABEL
from ..core.feedback import compute_weights
from ..core.llm import OllamaClient
from ..core.profile import build_profile_file, load_profile
from ..core.safety import check_commands
from ..core.sandbox import Plan, choose_mode

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
SOURCE_ICON = {
    "youtube": "▶",
    "rss": "◉",
    "hackernews": "Y",
    "reddit": "r/",
    "bluesky": "🦋",
    "mastodon": "🐘",
    "github": "⌥",
}
# No raw HTML and no images: a remote image URL written by the model (possibly steered by
# a hostile feed item) would leak data the moment the page renders.
_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table").disable("image")
COOKIE = "night_scout_session"


def safe_url(url: str | None) -> str:
    """Only http(s) links from feeds become hrefs."""
    u = (url or "").strip()
    return u if u.lower().startswith(("https://", "http://")) else "#"


def nice_day(d: date) -> str:
    return f"{d:%A, %B} {d.day}"  # %-d isn't portable to Windows


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
    templates.env.filters["md"] = lambda text: _md.render(text or "")
    templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else {}
    templates.env.globals.update(icon=SOURCE_ICON, action_label=ACTION_LABEL)
    templates.env.filters["safe_url"] = safe_url
    csrf = secrets.token_urlsafe(24)
    jobs = Jobs()
    state: dict[str, Any] = {"cfg": cfg}
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", *extra_hosts}
    app.state.session_key = session_key
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Response:
        host = request.headers.get("host", "")
        if host not in allowed_hosts:
            return Response("Night Scout only answers on localhost.", status_code=400)
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc not in allowed_hosts:
                return Response("Cross-site request refused.", status_code=403)
        # Every page needs the per-launch session cookie. `night-scout serve` prints a
        # one-time link that sets it. Other local processes (say, a package being tried
        # in the sandbox) can reach 127.0.0.1 but can't see the key.
        if not request.url.path.startswith("/static/"):
            cookie = request.cookies.get(COOKIE, "")
            key = request.query_params.get("key")
            if key is not None and secrets.compare_digest(key, session_key):
                clean = request.url.path
                rest = [(k, v) for k, v in request.query_params.multi_items() if k != "key"]
                if rest:
                    clean += "?" + "&".join(f"{quote(k)}={quote(v)}" for k, v in rest)
                redirect = RedirectResponse(clean, status_code=303)
                redirect.set_cookie(COOKIE, session_key, httponly=True, samesite="strict")
                return redirect
            if not secrets.compare_digest(cookie, session_key):
                return HTMLResponse(
                    "<h1>Night Scout</h1><p>Open the link printed by "
                    "<code>night-scout serve</code> in your terminal.</p>",
                    status_code=401,
                )
        resp: Response = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        return resp

    def check_csrf(token: str) -> None:
        if not secrets.compare_digest(token or "", csrf):
            raise HTTPException(403, "Missing or stale form token. Reload the page.")

    def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        run = store.last_run()
        statuses = store.source_statuses(run["id"]) if run else []
        return templates.TemplateResponse(
            request,
            name,
            {
                "csrf": csrf,
                "cfg": state["cfg"],
                "last_run": run,
                "statuses": statuses,
                "failed_sources": [s for s in statuses if not s.ok],
                "flash": request.query_params.get("msg"),
                **ctx,
            },
        )

    def go(path: str, msg: str | None = None, anchor: str = "") -> RedirectResponse:
        url = path + (f"?msg={quote(msg[:400])}" if msg else "") + (f"#{anchor}" if anchor else "")
        return RedirectResponse(url, status_code=303)

    def llm() -> OllamaClient:
        if "llm" not in state:
            state["llm"] = OllamaClient(state["cfg"].model)
        client: OllamaClient = state["llm"]
        return client

    def card(row: Any) -> dict[str, Any]:
        score = store.score(row["id"])
        per = json.loads(score["per_project"]) if score else []
        best = next((p for p in per if p["project"] == (score and score["best_project"])), None)
        return {
            "row": row,
            "extra": json.loads(row["extra"] or "{}"),
            "score": score,
            "best": best,
            "others": [p for p in per if p is not best and p["relevance"] >= 0.3],
            "topics": json.loads(score["topics"]) if score else [],
            "actions": store.actions_for(row["id"]),
            "vote": store.item_feedback(row["id"]),
            "also_seen": json.loads(row["also_seen"] or "[]"),
        }

    # -- pages ----------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def morning(request: Request, day: str | None = None) -> HTMLResponse:
        digest = store.digest(day) if day else store.latest_digest()
        cards: list[dict[str, Any]] = []
        weights = compute_weights(store)
        if digest:
            for item_id in json.loads(digest["item_ids"]):
                row = store.item(item_id)
                if row is not None and item_id not in weights.ignored_items:
                    cards.append(card(row))
        run = store.last_run()
        stats = json.loads(run["stats"]) if run else {}
        day_label = nice_day(
            datetime.strptime(digest["day"], "%Y-%m-%d").date() if digest else date.today()
        )
        return render(
            request,
            "morning.html",
            digest=digest,
            cards=cards,
            stats=stats,
            day_label=day_label,
            days=store.digest_days(),
        )

    @app.get("/item/{item_id}", response_class=HTMLResponse)
    def item(request: Request, item_id: int) -> HTMLResponse:
        row = store.item(item_id)
        if row is None:
            raise HTTPException(404, "No such item")
        research = store.research(item_id)
        c = card(row)
        mode = None
        if any(a["kind"] == "try_it" for a in c["actions"]):
            try:
                mode = choose_mode(state["cfg"].sandbox)[0]
            except RuntimeError as e:
                mode = f"unavailable: {e}"
        return render(
            request,
            "item.html",
            c=c,
            research=research,
            research_job=jobs.get(f"research:{item_id}"),
            sandbox_mode=mode,
            pretty_path=pretty_path,
        )

    @app.get("/action/{action_id}/confirm", response_class=HTMLResponse)
    def confirm(request: Request, action_id: int) -> HTMLResponse:
        a = store.action(action_id)
        if a is None or a["kind"] != "try_it":
            raise HTTPException(404, "No such Try-it")
        plan = Plan.from_dict(json.loads(a["detail"]).get("plan") or {})
        verdict = check_commands(plan.commands())
        try:
            mode, _ = choose_mode(state["cfg"].sandbox)
        except RuntimeError as e:
            mode = f"unavailable: {e}"
        return render(
            request,
            "confirm.html",
            a=a,
            plan=plan,
            verdict=verdict,
            mode=mode,
            item=store.item(a["item_id"]),
            is_mac=sys.platform == "darwin",
        )

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request) -> HTMLResponse:
        text = yaml.safe_dump(
            state["cfg"].sources.model_dump(mode="json", exclude_none=True),
            sort_keys=False,
            allow_unicode=True,
        )
        w = compute_weights(store)
        return render(request, "sources.html", sources_yaml=text, weights=w)

    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request) -> HTMLResponse:
        path = state["cfg"].profile_file()
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        try:
            projects = load_profile(path).projects
        except (FileNotFoundError, yaml.YAMLError):
            projects = []
        return render(request, "profile.html", raw=raw, projects=projects, job=jobs.get("profile"))

    # -- feedback ---------------------------------------------------------------
    VOTES = {"up": 1, "down": -1, "ignore": 0}

    @app.post("/item/{item_id}/feedback")
    def item_feedback(item_id: int, value: str = Form(...), csrf_token: str = Form("")) -> Any:
        check_csrf(csrf_token)
        if value not in VOTES or store.item(item_id) is None:
            raise HTTPException(400, "bad vote")
        store.add_feedback("item", str(item_id), VOTES[value], item_id)
        return JSONResponse({"ok": True, "value": value})

    @app.post("/feedback/{kind}")
    def other_feedback(
        kind: str, target: str = Form(...), value: str = Form(...), csrf_token: str = Form("")
    ) -> Any:
        check_csrf(csrf_token)
        if kind not in ("source", "topic") or value not in ("up", "down") or not target.strip():
            raise HTTPException(400, "bad feedback")
        store.add_feedback(kind, target.strip()[:200], VOTES[value])
        w = compute_weights(store)
        weight = w.source_weight(target) if kind == "source" else w.topic.get(target.lower(), 0)
        return JSONResponse(
            {"ok": True, "weight": round(weight, 2), "muted": kind == "source" and w.muted(target)}
        )

    # -- actions ------------------------------------------------------------------
    @app.post("/item/{item_id}/research")
    def research(item_id: int, csrf_token: str = Form("")) -> Any:
        check_csrf(csrf_token)
        if store.item(item_id) is None:
            raise HTTPException(404, "No such item")
        started = jobs.start(
            f"research:{item_id}",
            lambda: actions.research_more(state["cfg"], store, llm(), item_id),
        )
        return JSONResponse({"ok": True, "started": started})

    @app.get("/item/{item_id}/research/status")
    def research_status(item_id: int) -> Any:
        job = jobs.get(f"research:{item_id}")
        r = store.research(item_id)
        html = _md.render(r["brief"]) if r and job["state"] != "running" else None
        return JSONResponse({**job, "html": html})

    @app.post("/action/{action_id}/run")
    def run_action(action_id: int, csrf_token: str = Form(""), confirmed: str = Form("")) -> Any:
        check_csrf(csrf_token)
        if confirmed != "yes":
            raise HTTPException(400, "Confirm first.")
        a = store.action(action_id)
        if a is None:
            raise HTTPException(404, "No such action")
        try:
            actions.start_try_it(state["cfg"], store, action_id)
        except actions.ActionError as e:
            return go(f"/item/{a['item_id']}", str(e), f"action-{action_id}")
        return go(f"/item/{a['item_id']}", None, f"action-{action_id}")

    @app.get("/action/{action_id}/status")
    def action_status(action_id: int) -> Any:
        a = store.action(action_id)
        if a is None:
            raise HTTPException(404, "No such action")
        result = json.loads(a["result"]) if a["result"] else {}
        return JSONResponse({"status": a["status"], **result})

    @app.post("/action/{action_id}/note")
    def note(action_id: int, csrf_token: str = Form("")) -> Any:
        check_csrf(csrf_token)
        a = store.action(action_id)
        if a is None:
            raise HTTPException(404, "No such action")
        try:
            path = actions.write_note(state["cfg"], store, action_id)
            msg = f"Note saved to {path.parent.name}/{path.name} in {a['project']}"
        except actions.ActionError as e:
            msg = f"Couldn't write the note: {e}"
        return go(f"/item/{a['item_id']}", msg, f"action-{action_id}")

    @app.post("/action/{action_id}/done")
    def done(action_id: int, csrf_token: str = Form("")) -> Any:
        check_csrf(csrf_token)
        a = store.action(action_id)
        if a is None or a["kind"] not in ("watch_later", "ignore", "research_more"):
            raise HTTPException(400, "Can't mark that done")
        store.set_action(action_id, "done", {})
        return JSONResponse({"ok": True})

    # -- editors --------------------------------------------------------------------
    @app.post("/sources")
    def save_sources(sources_yaml: str = Form(...), csrf_token: str = Form("")) -> Any:
        check_csrf(csrf_token)
        try:
            data = yaml.safe_load(sources_yaml) or {}
            new_sources = Sources.model_validate(data)
        except (yaml.YAMLError, ValidationError) as e:
            return go("/sources", f"Not saved: {str(e)[:300]}")
        cfg_now = load_config(config_path)
        cfg_now.sources = new_sources
        save_config(cfg_now, config_path)
        state["cfg"] = cfg_now
        return go("/sources", "Saved. The next run uses these sources.")

    @app.post("/profile")
    def save_profile(profile_yaml: str = Form(...), csrf_token: str = Form("")) -> Any:
        check_csrf(csrf_token)
        try:
            data = yaml.safe_load(profile_yaml) or {}
            projects = data.get("projects") if isinstance(data, dict) else None
            if not isinstance(projects, list) or not all(
                isinstance(p, dict) and p.get("name") for p in projects
            ):
                raise ValueError("expected `projects:` as a list, each with a `name`")
        except (yaml.YAMLError, ValueError) as e:
            return go("/profile", f"Not saved: {str(e)[:300]}")
        state["cfg"].profile_file().write_text(profile_yaml, encoding="utf-8")
        return go("/profile", "Saved.")

    @app.post("/profile/regenerate")
    def regenerate(csrf_token: str = Form("")) -> Any:
        check_csrf(csrf_token)
        jobs.start("profile", lambda: build_profile_file(state["cfg"], llm()))
        return go("/profile", "Rebuilding the profile with Gemma…")

    @app.get("/profile/status")
    def profile_status() -> Any:
        return JSONResponse(jobs.get("profile"))

    return app
