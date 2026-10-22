"""Chat drawer and web-search settings routes (Track B).

Wired into create_app with one call. Every non-GET route here is under /api/, so the guard
middleware in app.py already demands the session cookie, a same-site Origin and the CSRF
token before any of this runs.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from datetime import date
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from ..core import attachments as att
from ..core import chat
from ..core.config import Config, SearchConfig, check_searxng_url, load_config, split_ref
from ..core.db import Store
from ..core.discover import add_source
from ..core.http import PoliteClient, SourceError
from ..core.llm import ToolChat
from ..core.profile import load_profile, save_topics
from ..core.providers import close_llm, make_llm
from ..core.secrets import SecretStoreUnavailable, delete_key, get_key, masked, set_key
from ..core.tools import BRAVE_KEY_ID, TOOLS, ToolContext, offered
from ..core.tools.search import web_search
from ..core.tools.tonight import latest_day
from .app import LOOPBACK, config_writer

log = logging.getLogger(__name__)

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ChatContext(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["digest", "item", "app"]
    day: str | None = Field(default=None, max_length=10)
    id: int | None = Field(default=None, ge=1)


class AttachmentIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(default="", max_length=300)
    kind: Literal["image", "text", "pdf"]
    data: str = Field(max_length=14_000_000)  # a 10 MB PDF as base64, or ≤200 KB of text


class AskBody(BaseModel):
    context: ChatContext | None = None
    question: str = Field(min_length=1, max_length=2000)
    thread: int | None = Field(default=None, ge=1)
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=att.MAX_ATTACHMENTS)


class ProposalBody(BaseModel):
    action: Literal["add", "skip"]


class SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["none", "searxng", "brave"]
    searxng_url: str | None = Field(default=None, max_length=300)


class BraveKeyBody(BaseModel):
    key: str = Field(min_length=1, max_length=4096)


class SearchTestBody(BaseModel):
    query: str | None = Field(default=None, max_length=200)


# Seams for tests: the chat model and the tools' network access.
def make_chat_llm(cfg: Config) -> Any:
    return make_llm(cfg, "chat")


def tool_context(cfg: Config, store: Store) -> ToolContext:
    return ToolContext(cfg=cfg, store=store, contact=cfg.contact)


def register(
    app: FastAPI,
    *,
    store: Store,
    state: dict[str, Any],
    config_path: Path,
    save_cfg: Any,
    jobs: Any,
) -> None:
    chat.ensure_tables(store)
    live: dict[int, dict[str, Any]] = {}  # thread id → what the running turn is doing
    live_lock = threading.Lock()
    ask_lock = threading.Lock()

    def model_view(c: Config) -> dict[str, Any]:
        ref = c.role_ref("chat") or f"ollama/{c.model.name}"
        pid, model = split_ref(ref)
        try:
            p = c.provider(pid)
            local = p.kind == "ollama" or urlsplit(p.base_url).hostname in LOOPBACK
            name = p.name or p.id
        except KeyError:
            local, name = False, pid
        return {"ref": ref, "model": model, "provider": name, "local": local}

    def tools_view(c: Config) -> list[dict[str, Any]]:
        on = {t.name for t in offered(c)}
        out = []
        for t in TOOLS.values():
            row: dict[str, Any] = {"name": t.name, "label": t.label, "icon": t.icon}
            row["enabled"] = t.name in on
            if t.name not in on:
                row["why"] = "Off: pick a web search provider in Settings → Chat & search"
            out.append(row)
        return out

    def clean_context(ctx: ChatContext | None) -> dict[str, Any]:
        if ctx is None:
            raise HTTPException(400, "Say what the question is about (context).")
        if ctx.kind == "app":
            return {"kind": "app"}
        if ctx.kind == "item":
            if ctx.id is None:
                raise HTTPException(400, "context.id is required for a story.")
            if store.item(ctx.id) is None:
                raise HTTPException(404, "No such item")
            return {"kind": "item", "id": ctx.id}
        day = ctx.day or latest_day(store) or date.today().isoformat()
        try:
            if not _DAY.match(day):
                raise ValueError(day)
            date.fromisoformat(day)
        except ValueError as e:
            raise HTTPException(400, "context.day must look like YYYY-MM-DD.") from e
        return {"kind": "digest", "day": day}

    # -- chat ----------------------------------------------------------------------------
    @app.get("/api/chat/meta")
    def chat_meta(kind: str | None = None, day: str | None = None, id: int | None = None) -> Any:
        c = state["cfg"]
        out: dict[str, Any] = {
            "model": model_view(c),
            "tools": tools_view(c),
            "search": c.search.provider,
            "platform": sys.platform,
            "max_steps": chat.MAX_STEPS,
        }
        if kind == "app":
            out["context"] = {"kind": "app", "label": "Night Scout"}
        if kind in ("digest", "item"):
            ctx = clean_context(ChatContext(kind=kind, day=day, id=id))  # type: ignore[arg-type]
            _about, label, _block = chat.build_context(c, store, ctx)
            out["context"] = {**ctx, "label": label}
        return out

    @app.post("/api/chat")
    def ask(body: AskBody) -> Any:
        question = body.question.strip()
        if not question:
            raise HTTPException(400, "Ask a question first.")
        try:
            files = att.parse([a.model_dump() for a in body.attachments])
        except att.AttachmentError as e:
            raise HTTPException(400, str(e)) from e
        extra_text, images = att.for_model(files)
        if body.thread is not None:
            thread = chat.get_thread(store, body.thread)
            if thread is None:
                raise HTTPException(404, "No such conversation")
            tid = int(thread["id"])
        else:
            context = clean_context(body.context)
            tid = chat.create_thread(store, context, question)
        key = f"chat:{tid}"
        cfg = state["cfg"]
        gate = threading.Event()  # the job waits until its question is stored
        slot: dict[str, int] = {}

        def progress(event: dict[str, Any]) -> None:
            with live_lock:
                live[tid] = event

        def work() -> None:
            gate.wait(10)
            turn = slot.get("turn")
            if turn is None:  # storing the question failed; nothing to answer
                return
            try:
                llm = make_chat_llm(cfg)
            except Exception as e:  # e.g. no model configured
                chat.add_message(store, tid, turn, "error", f"No chat model available: {e}"[:300])
                return
            try:
                if not callable(getattr(llm, "chat_tools", None)):
                    chat.add_message(
                        store, tid, turn, "error", "This model client can't use research tools."
                    )
                    return
                tool_llm: ToolChat = llm
                chat.answer(
                    tool_llm,
                    cfg,
                    store,
                    tid,
                    turn,
                    question,
                    tool_ctx=tool_context(cfg, store),
                    on_event=progress,
                    extra_text=extra_text,
                    images=images,
                )
            finally:
                close_llm(llm)
                with live_lock:
                    live.pop(tid, None)

        # Claim the thread first (Jobs.start is atomic), then number and store the question:
        # two quick submits can't both store a question for the same turn.
        try:
            with ask_lock:
                if not jobs.start(key, work):
                    raise HTTPException(409, "Gemma is still answering the last question.")
                progress({"stage": "thinking"})
                turn = chat.next_turn(store, tid)
                metas = [f.meta(n) for n, f in enumerate(files)]
                mid = chat.add_message(store, tid, turn, "user", question, attachments=metas)
                if files:
                    att.save(cfg.data_path(), tid, mid, files)
                slot["turn"] = turn
        finally:
            gate.set()
        return {"ok": True, "thread": tid, "turn": turn, "status": "running"}

    def user_attachment(thread_id: int, message_id: int, n: int) -> dict[str, Any]:
        row = store.conn.execute(
            "SELECT attachments FROM chat_messages WHERE id=? AND thread_id=? AND role='user'",
            (message_id, thread_id),
        ).fetchone()
        meta = next(
            (m for m in att.metas(row["attachments"] if row else None) if m["n"] == n), None
        )
        if meta is None:
            raise HTTPException(404, "No such attachment")
        return meta

    @app.get("/api/chat/{thread_id}/att/{message_id}/{n}")
    def chat_attachment(thread_id: int, message_id: int, n: int) -> Any:
        meta = user_attachment(thread_id, message_id, n)
        path = att.file_path(state["cfg"].data_path(), thread_id, message_id, meta)
        if not path.is_file():
            raise HTTPException(404, "That attachment is no longer on this machine.")
        kind = meta.get("kind")
        mime = {"image": str(meta.get("mime") or ""), "pdf": "application/pdf"}.get(
            str(kind), "text/plain; charset=utf-8"
        )
        if kind == "image" and mime not in ("image/jpeg", "image/png"):
            raise HTTPException(404, "No such attachment")
        disposition = "attachment" if kind == "pdf" else "inline"
        name = att.clean_name(str(meta.get("name") or ""), "attachment").replace('"', "")
        return FileResponse(
            path,
            media_type=mime,
            headers={
                "Content-Disposition": f'{disposition}; filename="{quote(name)}"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=3600",
            },
        )

    @app.get("/api/chat/{thread_id}/att/{message_id}/{n}/text")
    def chat_attachment_text(thread_id: int, message_id: int, n: int) -> Any:
        meta = user_attachment(thread_id, message_id, n)
        if meta.get("kind") == "image":
            raise HTTPException(404, "Images have no text.")
        path = att.text_path(state["cfg"].data_path(), thread_id, message_id, n)
        if not path.is_file():
            raise HTTPException(404, "That attachment is no longer on this machine.")
        return PlainTextResponse(
            path.read_text(encoding="utf-8"), headers={"X-Content-Type-Options": "nosniff"}
        )

    @app.post("/api/chat/{thread_id}/proposal/{message_id}")
    @config_writer
    def chat_proposal(thread_id: int, message_id: int, body: ProposalBody) -> Any:
        found = chat.proposal(store, thread_id, message_id)
        if found is None:
            raise HTTPException(404, "No such suggestion")
        prop, decided = found
        if decided:
            raise HTTPException(409, "That suggestion was already answered.")
        if body.action == "skip":
            decision = {"state": "skipped", "note": "Not added."}
        elif prop.get("kind") == "source":
            c = load_config(config_path)
            try:
                with PoliteClient(c.contact) as client:
                    label = add_source(
                        c, str(prop.get("type")), str(prop.get("value"))[:500], client
                    )
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            except SourceError as e:
                raise HTTPException(400, f"Couldn't reach it: {e}") from e
            save_cfg(c)
            decision = {"state": "added", "note": f"Added: {label}. It's read from the next run."}
        elif prop.get("kind") == "topic":
            topic = str(prop.get("topic") or "")
            path = state["cfg"].profile_file()
            try:
                have = load_profile(path).topics if path.exists() else []
                save_topics(path, [*have, topic])
            except (ValueError, OSError, yaml.YAMLError) as e:
                raise HTTPException(400, f"Couldn't save the topic: {e}") from e
            decision = {
                "state": "added",
                "note": f"Added the topic {topic}. It fills from the next run.",
            }
        else:
            raise HTTPException(404, "No such suggestion")
        chat.set_decision(store, message_id, decision)
        return {"ok": True, "decision": decision}

    @app.get("/api/chat/{thread_id}")
    def chat_thread(thread_id: int) -> Any:
        thread = chat.get_thread(store, thread_id)
        if thread is None:
            raise HTTPException(404, "No such conversation")
        job = jobs.get(f"chat:{thread_id}")
        messages = chat.get_messages(store, thread_id)
        error = None
        if job.get("state") == "running":
            status = "running"
        else:
            status = chat.turn_status(store, thread_id)
            if job.get("state") == "error":
                status, error = "error", "Something went wrong while answering."
            elif status == "pending":  # the server restarted mid-answer
                status, error = "error", "This answer was interrupted. Ask again."
        with live_lock:
            progress = live.get(thread_id) if status == "running" else None
        return {
            "thread": thread,
            "messages": messages,
            "status": status,
            "error": error,
            "progress": progress,
        }

    # -- web search settings --------------------------------------------------------------
    def brave_view() -> dict[str, Any] | None:
        try:
            key = get_key(BRAVE_KEY_ID)
        except SecretStoreUnavailable:
            return {"configured": None, "unreadable": True, "masked": ""}
        if not key:
            return None
        return {"configured": True, "source": "keychain", "masked": masked(key)}

    def search_view() -> dict[str, Any]:
        c = state["cfg"]
        return {
            "provider": c.search.provider,
            "searxng_url": c.search.searxng_url,
            "brave": brave_view(),
            "chat_model": model_view(c),
        }

    @app.get("/api/search")
    def get_search() -> Any:
        return search_view()

    @app.put("/api/search")
    @config_writer
    def put_search(body: SearchBody) -> Any:
        c = load_config(config_path)
        url = (body.searxng_url or "").strip() or None
        if url:
            try:
                url = check_searxng_url(url)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
        if body.provider == "searxng" and not url:
            raise HTTPException(400, "Enter your SearXNG address, like http://127.0.0.1:8888.")
        if body.provider == "brave" and not (brave_view() or {}).get("configured"):
            raise HTTPException(400, "Add a Brave Search API key first.")
        c.search = SearchConfig(provider=body.provider, searxng_url=url)
        save_cfg(c)
        return {"ok": True, **search_view()}

    @app.put("/api/search/brave-key")
    def put_brave_key(body: BraveKeyBody) -> Any:
        if BRAVE_KEY_ID in {p.id for p in state["cfg"].all_providers()}:
            # One keychain namespace: the provider would send this key as its bearer token.
            raise HTTPException(
                409,
                f"A model provider uses the id {BRAVE_KEY_ID!r}; rename it in config.yaml "
                "before saving a Brave Search key.",
            )
        try:
            set_key(BRAVE_KEY_ID, body.key)
        except SecretStoreUnavailable as e:
            raise HTTPException(503, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, **search_view()}

    @app.delete("/api/search/brave-key")
    @config_writer
    def del_brave_key() -> Any:
        try:
            delete_key(BRAVE_KEY_ID)
        except SecretStoreUnavailable as e:
            raise HTTPException(503, str(e)) from e
        c = load_config(config_path)
        if c.search.provider == "brave":  # nothing to search with any more
            c.search = SearchConfig(provider="none", searxng_url=c.search.searxng_url)
            save_cfg(c)
        return {"ok": True, **search_view()}

    @app.post("/api/search/test")
    def test_search(body: SearchTestBody | None = None) -> Any:
        c = state["cfg"]
        if c.search.provider == "none":
            raise HTTPException(400, "Web search is off. Pick SearXNG or Brave Search first.")
        query = ((body.query if body else None) or "").strip() or "ollama gemma"
        out = web_search(tool_context(c, store), query)
        if "error" in out:
            return {"ok": False, "provider": c.search.provider, "error": out["error"]}
        results = out.get("results") or []
        return {
            "ok": True,
            "provider": c.search.provider,
            "query": query,
            "count": len(results),
            "results": [{"title": r["title"], "url": r["url"]} for r in results[:3]],
        }
