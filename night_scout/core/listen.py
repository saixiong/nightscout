"""Listen to your Digest: the voice add-on, recordings on disk, and one render at a time.

The voice is Kokoro, recorded OUT of process by night_scout/tts/worker.py, run with the voice
Python ($NIGHT_SCOUT_TTS_PYTHON, else this Python). The add-on is available when that Python
can import kokoro and soundfile; without it nothing here runs and the app shows no Listen.

On disk, under the data folder:
    listen/<day>/<key>/manifest.json   chapters (id, story_id, title, file, seconds), status
    listen/<day>/<key>/NN-<id>.mp3     one MP3 per chapter: intro, each story, outro
    listen/previews/<voice>.mp3        the short Settings → Voice samples
key = hash(voice, what it reads, every chapter's text): a changed Digest or voice re-records.
Recordings older than KEEP_DAYS are deleted. Everything stays on this machine.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .lock import RunLock
from .speak import Chapter, script

log = logging.getLogger(__name__)

ENGINE = "Kokoro-82M"
INSTALL = 'pip install "night-scout[voice]"'
WORKER = Path(__file__).resolve().parents[1] / "tts" / "worker.py"
KEEP_DAYS = 7
WORDS_PER_S = 2.2  # Kokoro at 1×, measured on the demo Digest (724 words → 5:52 with gaps)
RENDER_TIMEOUT_S = 30 * 60
PREVIEW_TIMEOUT_S = 180
LOCK_WAIT_S = 20 * 60

# Kokoro-82M's English voices (hexgrad/Kokoro-82M/voices), in the groups Settings shows.
VOICE_GROUPS: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("American · female", [
        ("af_heart", "Heart", "warm, natural"), ("af_bella", "Bella", "bright, confident"),
        ("af_nicole", "Nicole", "soft, close-mic"), ("af_aoede", "Aoede", ""),
        ("af_kore", "Kore", ""), ("af_sarah", "Sarah", ""), ("af_nova", "Nova", ""),
        ("af_sky", "Sky", ""), ("af_alloy", "Alloy", ""), ("af_jessica", "Jessica", ""),
        ("af_river", "River", ""),
    ]),
    ("American · male", [
        ("am_michael", "Michael", "steady, measured"), ("am_fenrir", "Fenrir", "fuller"),
        ("am_puck", "Puck", "lively"), ("am_onyx", "Onyx", "low"), ("am_adam", "Adam", ""),
        ("am_echo", "Echo", ""), ("am_eric", "Eric", ""), ("am_liam", "Liam", ""),
        ("am_santa", "Santa", ""),
    ]),
    ("British · female", [
        ("bf_emma", "Emma", "clear, composed"), ("bf_isabella", "Isabella", ""),
        ("bf_alice", "Alice", ""), ("bf_lily", "Lily", ""),
    ]),
    ("British · male", [
        ("bm_george", "George", "calm, documentary"), ("bm_fable", "Fable", "lighter"),
        ("bm_lewis", "Lewis", ""), ("bm_daniel", "Daniel", ""),
    ]),
]  # fmt: skip
SUGGESTED = ("af_heart", "af_bella", "bf_emma", "bm_george", "am_michael")
VOICES: dict[str, dict[str, str]] = {
    vid: {"id": vid, "name": name, "desc": desc, "group": group}
    for group, rows in VOICE_GROUPS
    for vid, name, desc in rows
}
DEFAULT_VOICE = "af_heart"

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KEY = re.compile(r"^[0-9a-f]{16}$")
_FILE = re.compile(r"^\d{2,3}-[a-z0-9]+\.mp3$")


class ListenError(Exception):
    """Something to show the user (the add-on is missing, a recording failed)."""


def voice_name(voice: str) -> str:
    return VOICES.get(voice, {}).get("name") or voice


# -- is the add-on there? --------------------------------------------------------------------
def voice_python() -> str:
    return os.environ.get("NIGHT_SCOUT_TTS_PYTHON") or sys.executable


_avail: dict[str, tuple[bool, str | None]] = {}
_avail_lock = threading.Lock()


def check_available(refresh: bool = False) -> tuple[bool, str | None]:
    """(available, why not). Asks the voice Python to import kokoro and soundfile, in a
    subprocess so torch never loads here. Cached per Python; refresh re-asks."""
    py = voice_python()
    if not refresh:  # a cached answer never waits for a re-check that's running
        got = _avail.get(py)
        if got is not None:
            return got
    with _avail_lock:
        if not refresh and py in _avail:
            return _avail[py]
        try:
            r = subprocess.run(
                [py, "-c", "import kokoro, soundfile"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            ok = r.returncode == 0
            lines = [ln for ln in (r.stderr or "").strip().splitlines() if ln.strip()]
            why = None if ok else (lines[-1] if lines else f"exit code {r.returncode}")[:300]
        except (OSError, subprocess.SubprocessError) as e:
            ok, why = False, f"{type(e).__name__}: {e}"[:300]
        _avail[py] = (ok, why)
        return ok, why


def cached_available() -> bool | None:
    """The last answer for the current voice Python, without asking (None: not asked yet)."""
    with _avail_lock:
        got = _avail.get(voice_python())
    return None if got is None else got[0]


# -- what gets recorded --------------------------------------------------------------------
def listen_dir(cfg: Config) -> Path:
    return cfg.data_path() / "listen"


def chapter_file(i: int, c: Chapter) -> str:
    return f"{i:02d}-{re.sub(r'[^a-z0-9]', '', c.id.lower()) or 'part'}.mp3"


def estimate_s(c: Chapter) -> float:
    return round(len(c.text.split()) / WORDS_PER_S + c.gap, 1)


@dataclass(frozen=True)
class Plan:
    """One Digest night, read in one voice and mode: what a recording must contain."""

    day: str
    voice: str
    mode: str  # "full" | "headlines"
    chapters: tuple[Chapter, ...]
    key: str
    out: Path

    @property
    def manifest(self) -> Path:
        return self.out / "manifest.json"

    @property
    def stories_count(self) -> int:
        return sum(1 for c in self.chapters if c.story_id is not None)


def plan_for(cfg: Config, digest: dict[str, Any]) -> Plan:
    """The recording for /api/digest's payload under the current voice settings."""
    day = str(digest["day"])
    if not _DAY.match(day):
        raise ListenError("No Digest for that day")
    voice = cfg.voice.voice if cfg.voice.voice in VOICES else DEFAULT_VOICE
    mode = cfg.voice.reads
    chapters = tuple(script(digest, headlines_only=mode == "headlines"))
    blob = json.dumps([voice, mode, [[c.id, c.text] for c in chapters]], ensure_ascii=False)
    key = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return Plan(day, voice, mode, chapters, key, listen_dir(cfg) / day / key)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _pid_alive(pid: Any) -> bool | None:
    if not isinstance(pid, int) or pid <= 0 or sys.platform == "win32":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def manifest_status(m: dict[str, Any] | None) -> str:
    """none | recording | ready | failed. A 'recording' whose worker is gone has failed."""
    if not m:
        return "none"
    st = str(m.get("status") or "none")
    if st != "recording":
        return st if st in ("ready", "failed") else "none"
    alive = _pid_alive(m.get("pid"))
    if alive is False:
        return "failed"
    if alive is None and time.time() - float(m.get("created_ts") or 0) > RENDER_TIMEOUT_S:
        return "failed"
    return "recording"


def new_manifest(plan: Plan) -> dict[str, Any]:
    return {
        "version": 1,
        "day": plan.day,
        "key": plan.key,
        "voice": plan.voice,
        "mode": plan.mode,
        "status": "recording",
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "created_ts": time.time(),
        "pid": None,
        "done": 0,
        "total": len(plan.chapters),
        "chapters": [
            {
                "id": c.id,
                "story_id": c.story_id,
                "title": c.title,
                "file": chapter_file(i, c),
                "seconds": None,
                "words": len(c.text.split()),
            }
            for i, c in enumerate(plan.chapters)
        ],
    }


def audio_path(cfg: Config, day: str, key: str, name: str) -> Path | None:
    """A recorded chapter's file, or None for anything that isn't one (no path tricks)."""
    if not (_DAY.match(day) and _KEY.match(key) and _FILE.match(name)):
        return None
    root = listen_dir(cfg).resolve()
    p = (root / day / key / name).resolve()
    if p.parent.parent.parent != root or not p.is_file():
        return None
    m = read_json(p.parent / "manifest.json") or {}
    if not any(c.get("file") == name for c in m.get("chapters") or []):
        return None
    return p


# -- recording ------------------------------------------------------------------------------
def _job(plan: Plan) -> dict[str, Any]:
    return {
        "voice": plan.voice,
        "out": str(plan.out),
        "manifest": "manifest.json",
        "chapters": [
            {"id": c.id, "text": c.text, "file": chapter_file(i, c), "gap": c.gap}
            for i, c in enumerate(plan.chapters)
        ],
    }


def _spawn(job_file: Path, log_file: Path) -> subprocess.Popen[bytes]:
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "TOKENIZERS_PARALLELISM": "false"}
    with open(log_file, "ab") as fh:
        return subprocess.Popen(
            [voice_python(), str(WORKER), str(job_file)],
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(job_file.parent),
            start_new_session=True,  # a Ctrl-C in the terminal doesn't cut a file in half
        )


def _lock(cfg: Config, cancelled: Callable[[], bool]) -> RunLock | None:
    """The machine-wide 'one recording at a time' lock (the web app and a nightly run share
    it). Waits for it; None if this render was cancelled or the wait ran too long."""
    lock = RunLock(listen_dir(cfg) / "render.lock")
    end = time.time() + LOCK_WAIT_S
    while not lock.acquire():
        if cancelled() or time.time() > end:
            return None
        time.sleep(0.5)
    return lock


def record(
    cfg: Config,
    plan: Plan,
    on_start: Callable[[subprocess.Popen[bytes]], None] | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Record `plan` now, in this thread (blocks until the worker is done). Returns the final
    manifest. Joins instead when another process is already recording the same plan."""
    while manifest_status(read_json(plan.manifest)) == "recording":  # someone else's: wait
        if cancelled():
            return read_json(plan.manifest) or {}
        time.sleep(1)
    m = read_json(plan.manifest)
    if manifest_status(m) == "ready" and m and _complete(plan, m):
        return m
    lock = _lock(cfg, cancelled)
    if lock is None:
        return read_json(plan.manifest) or {"status": "none"}
    try:
        m = read_json(plan.manifest)
        if manifest_status(m) == "ready" and m and _complete(plan, m):
            return m  # recorded while we waited for the lock
        if plan.out.exists():
            shutil.rmtree(plan.out, ignore_errors=True)
        plan.out.mkdir(parents=True, exist_ok=True)
        job = plan.out / "job.json"
        write_json(job, _job(plan))
        write_json(plan.manifest, new_manifest(plan))
        t0 = time.time()
        proc = _spawn(job, plan.out / "worker.log")
        m = read_json(plan.manifest) or new_manifest(plan)
        if m.get("pid") is None:
            m["pid"] = proc.pid
            write_json(plan.manifest, m)
        if on_start:
            on_start(proc)
        try:
            rc = proc.wait(timeout=RENDER_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
        m = read_json(plan.manifest) or {}
        if m.get("status") != "ready":
            m["status"] = "failed"
            m.setdefault("error", f"the voice worker stopped (exit code {rc})")
            write_json(plan.manifest, m)
            log.warning("listen: recording %s/%s failed: %s", plan.day, plan.key, m.get("error"))
        else:
            log.info(
                "listen: recorded %s (%s, %s) in %.0f s",
                plan.day,
                plan.voice,
                plan.mode,
                time.time() - t0,
            )
        return m
    finally:
        lock.release()
        prune(cfg)


def _complete(plan: Plan, m: dict[str, Any]) -> bool:
    return all((plan.out / str(c.get("file"))).is_file() for c in m.get("chapters") or [])


class Recorder:
    """The web app's renders: one at a time in a background thread. A request for the plan
    being recorded joins it; a new plan for the same night replaces one that's waiting, and
    stops one this app is recording (you changed the voice; the old one is no use)."""

    def __init__(self, cfg_fn: Callable[[], Config]) -> None:
        self.cfg_fn = cfg_fn
        self.lock = threading.Lock()
        self.queue: list[Plan] = []
        self.current: Plan | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self.stopped: set[str] = set()
        self.wanted: dict[str, str] = {}  # plan key → chapter id to record next
        self.thread: threading.Thread | None = None

    def waiting(self, plan: Plan) -> bool:
        """Queued, or waiting for another recording to finish."""
        with self.lock:
            return any(p.key == plan.key for p in self.queue) or (
                self.current is not None and self.current.key == plan.key and self.proc is None
            )

    def _want(self, plan: Plan) -> None:
        cid = self.wanted.get(plan.key)
        if cid is not None and plan.out.is_dir():
            write_json(plan.out / "want.json", {"id": cid})

    def request(self, plan: Plan, story: int | None = None) -> None:
        """Start (or join) recording `plan`; `story`'s chapter is recorded next."""
        cid = next((c.id for c in plan.chapters if story is not None and c.story_id == story), None)
        m = read_json(plan.manifest)
        status = manifest_status(m)
        if status == "ready" and m and _complete(plan, m):
            return
        with self.lock:
            if cid is not None:
                self.wanted[plan.key] = cid
            if status == "recording":
                self._want(plan)
                return
            if self.current is not None and self.current.key == plan.key:
                return
            if any(p.key == plan.key for p in self.queue):
                return
            self.queue = [p for p in self.queue if p.day != plan.day]
            self.queue.append(plan)
            self.stopped.discard(plan.key)
            cur = self.current
            if cur is not None and cur.day == plan.day and cur.key != plan.key:
                self.stopped.add(cur.key)
                if self.proc is not None and self.proc.poll() is None:
                    log.info(
                        "listen: stopping the %s recording of %s (settings changed)",
                        cur.voice,
                        cur.day,
                    )
                    self.proc.terminate()
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, name="listen", daemon=True)
                self.thread.start()

    def _loop(self) -> None:
        while True:
            with self.lock:
                if not self.queue:
                    self.current = None
                    return
                plan = self.queue.pop(0)
                self.current, self.proc = plan, None

            def started(proc: subprocess.Popen[bytes], plan: Plan = plan) -> None:
                with self.lock:
                    self.proc = proc
                    self._want(plan)

            def cancelled(plan: Plan = plan) -> bool:
                return plan.key in self.stopped

            try:
                record(self.cfg_fn(), plan, on_start=started, cancelled=cancelled)
            except Exception:
                log.exception("listen: recording %s failed", plan.day)
            with self.lock:
                if plan.key in self.stopped:
                    shutil.rmtree(plan.out, ignore_errors=True)
                self.wanted.pop(plan.key, None)
                self.current, self.proc = None, None


# -- previews --------------------------------------------------------------------------------
_preview_lock = threading.Lock()


def preview_text(voice: str) -> str:
    return f"Hi, I'm {voice_name(voice)}. This is how your Digest will sound in the morning."


def preview(cfg: Config, voice: str) -> Path:
    """A short sample in this voice: recorded once, then served from listen/previews/."""
    if voice not in VOICES:
        raise ListenError("No such voice")
    out = listen_dir(cfg) / "previews"
    target = out / f"{voice}.mp3"
    with _preview_lock:
        if target.is_file():
            return target
        out.mkdir(parents=True, exist_ok=True)
        job = out / f".{voice}.job.json"
        write_json(job, {
            "voice": voice, "out": str(out), "manifest": None,
            "chapters": [{"id": "preview", "text": preview_text(voice), "file": target.name,
                          "gap": 0.2}],
        })  # fmt: skip
        proc = _spawn(job, out / "worker.log")
        try:
            proc.wait(timeout=PREVIEW_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        job.unlink(missing_ok=True)
        if not target.is_file():
            raise ListenError(
                f"Couldn't record a sample of {voice_name(voice)}; see {out}/worker.log"
            )
        return target


# -- housekeeping ----------------------------------------------------------------------------
def prune(cfg: Config, keep_days: int = KEEP_DAYS, now: float | None = None) -> int:
    """Delete recordings older than keep_days (by when they were made). Returns how many."""
    root = listen_dir(cfg)
    cutoff = (now or time.time()) - keep_days * 86400
    n = 0
    for day in root.iterdir() if root.is_dir() else []:
        if not (day.is_dir() and _DAY.match(day.name)):
            continue
        for k in list(day.iterdir()):
            m = k / "manifest.json"
            made = m.stat().st_mtime if m.is_file() else k.stat().st_mtime
            if k.is_dir() and made < cutoff and manifest_status(read_json(m)) != "recording":
                shutil.rmtree(k, ignore_errors=True)
                n += 1
        if not any(day.iterdir()):
            day.rmdir()
    return n


# -- what the player and Settings see -----------------------------------------------------------
def view(plan: Plan, waiting: bool = False) -> dict[str, Any]:
    m = read_json(plan.manifest)
    status = manifest_status(m)
    if waiting and status not in ("recording", "ready"):
        status = "waiting"
    rec = {c.get("file"): c for c in (m or {}).get("chapters") or []} if status != "none" else {}
    chapters: list[dict[str, Any]] = []
    for i, c in enumerate(plan.chapters):
        f = chapter_file(i, c)
        secs = rec.get(f, {}).get("seconds")
        ready = secs is not None and (plan.out / f).is_file()
        chapters.append({
            "id": c.id, "story_id": c.story_id, "title": c.title,
            "seconds": secs if ready else None, "est": estimate_s(c),
            "url": f"/api/listen/audio/{plan.day}/{plan.key}/{f}" if ready else None,
        })  # fmt: skip
    total = sum(c["seconds"] if c["seconds"] is not None else c["est"] for c in chapters)
    return {
        "status": status,
        "day": plan.day,
        "key": plan.key,
        "voice": plan.voice,
        "voice_name": voice_name(plan.voice),
        "mode": plan.mode,
        "stories": plan.stories_count,
        "chapters": chapters,
        "done": sum(1 for c in chapters if c["url"]),
        "stories_done": sum(1 for c in chapters if c["url"] and c["story_id"] is not None),
        "total": len(chapters),
        "seconds": round(total, 1),
        "estimated": any(c["seconds"] is None for c in chapters),
        "error": (m or {}).get("error") if status == "failed" else None,
        "render_s": (m or {}).get("render_s"),
        "bytes": (m or {}).get("bytes"),
    }


def last_recording(cfg: Config) -> dict[str, Any] | None:
    """The newest finished recording: for 'Last night's took 46 s and 2.7 MB'."""
    root = listen_dir(cfg)
    best: dict[str, Any] | None = None
    for m in root.glob("*/*/manifest.json") if root.is_dir() else []:
        d = read_json(m)
        if (
            d
            and d.get("status") == "ready"
            and d.get("render_s") is not None
            and (best is None or str(d.get("finished", "")) > str(best.get("finished", "")))
        ):
            best = d
    if best is None:
        return None
    return {k: best.get(k) for k in ("day", "voice", "mode", "render_s", "bytes", "finished")}
