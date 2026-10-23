"""Listen to your Digest and Settings → Voice routes (the optional voice add-on).

Wired into create_app with one call, like chat_routes. Everything is under /api/, so the guard
middleware in app.py already demands the session cookie (and, for PUT/POST, a same-site
Origin and the CSRF token). Recording runs in the voice worker, never in a request.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from ..core import listen
from ..core.config import VOICE_SPEEDS, Config, VoiceConfig, load_config
from ..core.db import Store
from .app import config_writer, digest_payload, pretty_path


class VoiceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    voice: str | None = Field(default=None, max_length=40)
    speed: float | None = None
    reads: Literal["full", "headlines"] | None = None
    ready_by_morning: bool | None = None


class ListenBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    day: str | None = Field(default=None, max_length=10)
    story: int | None = Field(default=None, ge=1)


def register(
    app: FastAPI,
    *,
    store: Store,
    state: dict[str, Any],
    config_path: Path,
    save_cfg: Callable[[Config], None],
) -> None:
    recorder = listen.Recorder(lambda: state["cfg"])
    # Ask the voice Python once, in the background: importing torch takes a few seconds.
    threading.Thread(target=listen.check_available, name="voice-check", daemon=True).start()
    threading.Thread(
        target=lambda: listen.prune(state["cfg"]), name="listen-prune", daemon=True
    ).start()

    def voice_view(refresh: bool = False) -> dict[str, Any]:
        c: Config = state["cfg"]
        ok, why = listen.check_available(refresh=refresh)
        return {
            "available": ok,
            "error": why,
            "python": pretty_path(listen.voice_python()),
            "engine": listen.ENGINE,
            "install": listen.INSTALL,
            "voices": list(listen.VOICES.values()),
            "groups": [[g, [v for v, _, _ in rows]] for g, rows in listen.VOICE_GROUPS],
            "suggested": list(listen.SUGGESTED),
            "default": listen.DEFAULT_VOICE,
            "speeds": list(VOICE_SPEEDS),
            "settings": c.voice.model_dump(),
            "last": listen.last_recording(c),
            "keep_days": listen.KEEP_DAYS,
            "folder": pretty_path(listen.listen_dir(c)),
        }

    @app.get("/api/voice")
    def get_voice(refresh: bool = False) -> Any:
        return voice_view(refresh)

    @app.put("/api/voice")
    @config_writer
    def put_voice(body: VoiceBody) -> Any:
        if body.voice is not None and body.voice not in listen.VOICES:
            raise HTTPException(400, "That isn't one of Kokoro's English voices.")
        c = load_config(config_path)
        merged = {**c.voice.model_dump(), **body.model_dump(exclude_none=True)}
        try:
            c.voice = VoiceConfig.model_validate(merged)
        except ValueError as e:
            raise HTTPException(400, str(e).splitlines()[-1][:200]) from e
        save_cfg(c)
        return voice_view()

    def need_addon() -> None:
        ok, _ = listen.check_available()
        if not ok:
            raise HTTPException(409, f"The voice add-on isn't installed: {listen.INSTALL}")

    @app.get("/api/voice/preview/{voice}")
    def voice_preview(voice: str) -> Any:
        if voice not in listen.VOICES:
            raise HTTPException(404, "No such voice")
        need_addon()
        try:
            path = listen.preview(state["cfg"], voice)
        except listen.ListenError as e:
            raise HTTPException(500, str(e)) from e
        return FileResponse(path, media_type="audio/mpeg")

    def plan(day: str | None) -> listen.Plan | None:
        try:
            d = digest_payload(state["cfg"], store, day)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if not d["day"]:
            return None
        if day is not None and not d["stories"]:
            return None
        return listen.plan_for(state["cfg"], d)

    def listen_view(p: listen.Plan | None) -> dict[str, Any]:
        out: dict[str, Any] = {"available": True, "speed": state["cfg"].voice.speed}
        if p is None:
            return {**out, "status": "none", "day": None, "chapters": [], "stories": 0}
        return {**out, **listen.view(p, waiting=recorder.waiting(p))}

    @app.get("/api/listen")
    def listen_state(day: str | None = None) -> Any:
        ok, _ = listen.check_available()
        if not ok:
            return {"available": False, "install": listen.INSTALL}
        return listen_view(plan(day))

    @app.post("/api/listen")
    def listen_start(body: ListenBody | None = None) -> Any:
        body = body or ListenBody()
        need_addon()
        p = plan(body.day)
        if p is None or p.stories_count == 0:
            raise HTTPException(404, "There's no Digest to read for that night.")
        if body.story is not None and not any(c.story_id == body.story for c in p.chapters):
            raise HTTPException(404, "That story isn't in this Digest.")
        recorder.request(p, story=body.story)
        return listen_view(p)

    @app.get("/api/listen/audio/{day}/{key}/{name}")
    def listen_audio(day: str, key: str, name: str) -> Any:
        path = listen.audio_path(state["cfg"], day, key, name)
        if path is None:
            raise HTTPException(404, "No such recording")
        return FileResponse(path, media_type="audio/mpeg")
