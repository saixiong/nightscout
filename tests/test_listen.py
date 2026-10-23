"""Listen: the voice add-on check, recordings on disk, the render queue, previews, pruning.

The real worker script (night_scout/tts/worker.py) runs in a subprocess here, with fake
kokoro/soundfile/numpy modules (tests/fake_tts) instead of the model: no torch, no network."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from night_scout.core import listen
from night_scout.core.config import Config, VoiceConfig

DIGEST = {
    "day": "2026-10-22",
    "digest_topics": ["Local LLMs", "Security"],
    "projects": [],
    "stories": [
        {"id": 11, "topic": "Local LLMs", "headline": "One", "source_name": "Blog",
         "summary": "First summary here."},
        {"id": 12, "topic": "Local LLMs", "headline": "Two", "source_name": "Blog",
         "summary": "Second summary, a little longer than the first one."},
        {"id": 13, "topic": "Security", "headline": "Three", "source_name": "r/netsec",
         "summary": "Third."},
    ],
}  # fmt: skip


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(data_dir=str(tmp_path / "data"))


def wait_for(fn, timeout: float = 20.0):  # type: ignore[no-untyped-def]
    end = time.time() + timeout
    while time.time() < end:
        got = fn()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError("timed out")


# -- the add-on -----------------------------------------------------------------------------
def test_not_installed_by_default_in_tests() -> None:
    assert listen.check_available() == (False, "not in tests")
    assert listen.cached_available() is False


def test_available_when_the_voice_python_imports_kokoro(fake_voice: Path) -> None:
    assert listen.cached_available() is None  # not asked yet
    assert listen.check_available() == (True, None)
    assert listen.cached_available() is True


def test_missing_kokoro_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NIGHT_SCOUT_TTS_PYTHON", sys.executable)  # this venv: no kokoro
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(listen, "_avail", {})
    ok, why = listen.check_available()
    assert not ok and why and "kokoro" in why
    monkeypatch.setenv("NIGHT_SCOUT_TTS_PYTHON", "/nonexistent/python3")
    ok, why = listen.check_available()
    assert not ok and why and "FileNotFoundError" in why


def test_voice_python_defaults_to_this_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NIGHT_SCOUT_TTS_PYTHON")
    assert listen.voice_python() == sys.executable


def test_voice_catalog() -> None:
    assert len(listen.VOICES) == 28
    assert [g for g, _ in listen.VOICE_GROUPS] == [
        "American · female", "American · male", "British · female", "British · male",
    ]  # fmt: skip
    assert listen.DEFAULT_VOICE == "af_heart" == VoiceConfig().voice
    assert all(v in listen.VOICES for v in listen.SUGGESTED)
    assert [listen.voice_name(v) for v in listen.SUGGESTED] == [
        "Heart", "Bella", "Emma", "George", "Michael",
    ]  # fmt: skip


# -- plans ----------------------------------------------------------------------------------
def test_plan_key_follows_voice_mode_and_text(cfg: Config) -> None:
    a = listen.plan_for(cfg, DIGEST)
    assert a == listen.plan_for(cfg, DIGEST)
    assert a.out == cfg.data_path() / "listen" / "2026-10-22" / a.key
    assert a.stories_count == 3 and [c.id for c in a.chapters][:2] == ["intro", "s11"]
    cfg.voice.voice = "bf_emma"
    b = listen.plan_for(cfg, DIGEST)
    cfg.voice.reads = "headlines"
    c = listen.plan_for(cfg, DIGEST)
    changed = {**DIGEST, "stories": [*DIGEST["stories"][:2]]}  # type: ignore[list-item]
    d = listen.plan_for(cfg, changed)
    assert len({a.key, b.key, c.key, d.key}) == 4
    assert sum(len(x.text.split()) for x in c.chapters) < sum(
        len(x.text.split()) for x in b.chapters
    )


def test_plan_refuses_a_bad_day(cfg: Config) -> None:
    with pytest.raises(listen.ListenError):
        listen.plan_for(cfg, {**DIGEST, "day": "../../etc"})


# -- recording ------------------------------------------------------------------------------
def test_record_writes_one_mp3_per_chapter_and_a_manifest(cfg: Config, fake_voice: Path) -> None:
    plan = listen.plan_for(cfg, DIGEST)
    m = listen.record(cfg, plan)
    assert m["status"] == "ready" and m["done"] == m["total"] == 5
    files = sorted(p.name for p in plan.out.glob("*.mp3"))
    assert files == ["00-intro.mp3", "01-s11.mp3", "02-s12.mp3", "03-s13.mp3", "04-outro.mp3"]
    assert (plan.out / "01-s11.mp3").read_bytes().startswith(b"ID3FAKE")
    ch = {c["id"]: c for c in m["chapters"]}
    # the fake says 0.1 s a word; the worker adds the chapter's gap after it
    words = len(plan.chapters[1].text.split())
    assert ch["s11"]["seconds"] == pytest.approx(words * 0.1 + 0.8, abs=0.01)
    assert ch["s11"]["story_id"] == 11 and ch["s11"]["title"] == "One"
    assert m["render_s"] is not None and m["bytes"] > 0 and m["voice"] == "af_heart"
    v = listen.view(plan)
    assert v["status"] == "ready" and not v["estimated"] and v["stories_done"] == 3
    assert v["chapters"][1]["url"] == f"/api/listen/audio/2026-10-22/{plan.key}/01-s11.mp3"
    assert v["seconds"] == pytest.approx(sum(c["seconds"] for c in m["chapters"]), abs=0.2)
    # recording the same plan again is a no-op
    before = (plan.out / "manifest.json").stat().st_mtime_ns
    assert listen.record(cfg, plan)["status"] == "ready"
    assert (plan.out / "manifest.json").stat().st_mtime_ns == before


def test_a_failed_chapter_fails_the_recording(cfg: Config, fake_voice: Path) -> None:
    bad = {**DIGEST, "stories": [{**DIGEST["stories"][0], "summary": "It went BOOM."}]}  # type: ignore[index]
    plan = listen.plan_for(cfg, bad)
    m = listen.record(cfg, plan)
    assert m["status"] == "failed" and "could not say that" in m["error"]
    assert listen.view(plan)["status"] == "failed"


def test_a_model_that_wont_load_fails_cleanly(
    cfg: Config, fake_voice: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TTS_FAIL", "load")
    m = listen.record(cfg, listen.plan_for(cfg, DIGEST))
    assert m["status"] == "failed" and "no model here" in m["error"]


def test_a_dead_worker_is_not_recording_forever(cfg: Config) -> None:
    plan = listen.plan_for(cfg, DIGEST)
    m = listen.new_manifest(plan)
    m["pid"] = 999_999_99  # no such process
    listen.write_json(plan.manifest, m)
    assert listen.manifest_status(listen.read_json(plan.manifest)) == "failed"
    m["pid"] = os.getpid()
    listen.write_json(plan.manifest, m)
    assert listen.manifest_status(listen.read_json(plan.manifest)) == "recording"


def test_recorder_plays_as_it_goes_and_records_a_wanted_story_next(
    cfg: Config, fake_voice: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TTS_DELAY", "0.4")
    rec = listen.Recorder(lambda: cfg)
    plan = listen.plan_for(cfg, DIGEST)
    rec.request(plan, story=13)
    rec.request(plan, story=13)  # a second request joins the first
    assert rec.waiting(plan) or listen.view(plan)["status"] == "recording"
    # progressive: the intro and the wanted story are ready before the whole thing is
    first = wait_for(lambda: (v := listen.view(plan))["done"] >= 2 and v)
    assert first["status"] == "recording"
    assert {c["id"] for c in first["chapters"] if c["url"]} <= {"intro", "s13", "s11"}
    assert "s13" in {c["id"] for c in first["chapters"] if c["url"]}
    done = wait_for(lambda: (v := listen.view(plan))["status"] == "ready" and v)
    assert done["done"] == 5
    log = (plan.out / "worker.log").read_text()
    assert log.count("Traceback") == 0
    assert len(list(plan.out.parent.iterdir())) == 1  # one recording, not two


def test_a_new_voice_stops_the_old_recording_of_that_night(
    cfg: Config, fake_voice: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TTS_DELAY", "0.5")
    rec = listen.Recorder(lambda: cfg)
    old = listen.plan_for(cfg, DIGEST)
    rec.request(old)
    wait_for(lambda: listen.view(old)["status"] == "recording")
    cfg.voice.voice = "bm_george"
    new = listen.plan_for(cfg, DIGEST)
    rec.request(new)
    done = wait_for(lambda: (v := listen.view(new))["status"] == "ready" and v, timeout=30)
    assert done["voice_name"] == "George"
    wait_for(lambda: not old.out.exists())
    assert [p.name for p in new.out.parent.iterdir()] == [new.key]


def test_one_recording_at_a_time_across_processes(cfg: Config, fake_voice: Path) -> None:
    # someone else (a nightly run) holds the render lock: we wait, then record
    held = listen.RunLock(listen.listen_dir(cfg) / "render.lock")
    assert held.acquire()
    rec = listen.Recorder(lambda: cfg)
    plan = listen.plan_for(cfg, DIGEST)
    rec.request(plan)
    time.sleep(1.0)
    assert rec.waiting(plan) and listen.view(plan, waiting=True)["status"] == "waiting"
    held.release()
    wait_for(lambda: listen.view(plan)["status"] == "ready")


# -- serving --------------------------------------------------------------------------------
def test_audio_path_only_serves_recorded_chapters(cfg: Config, fake_voice: Path) -> None:
    plan = listen.plan_for(cfg, DIGEST)
    listen.record(cfg, plan)
    ok = listen.audio_path(cfg, plan.day, plan.key, "01-s11.mp3")
    assert ok is not None and ok.name == "01-s11.mp3"
    for day, key, name in [
        (plan.day, plan.key, "manifest.json"),
        (plan.day, plan.key, "job.json"),
        (plan.day, plan.key, "../01-s11.mp3"),
        (plan.day, plan.key, "99-s11.mp3"),
        (plan.day, "../" + plan.key, "01-s11.mp3"),
        ("..", plan.key, "01-s11.mp3"),
        (plan.day, "0" * 16, "01-s11.mp3"),
    ]:
        assert listen.audio_path(cfg, day, key, name) is None, (day, key, name)
    (plan.out / "05-sneaky.mp3").write_bytes(b"x")  # on disk but not in the manifest
    assert listen.audio_path(cfg, plan.day, plan.key, "05-sneaky.mp3") is None


# -- previews -------------------------------------------------------------------------------
def test_preview_is_recorded_once_then_cached(cfg: Config, fake_voice: Path) -> None:
    p = listen.preview(cfg, "bf_emma")
    assert p == cfg.data_path() / "listen" / "previews" / "bf_emma.mp3" and p.is_file()
    stamp = p.stat().st_mtime_ns
    assert listen.preview(cfg, "bf_emma").stat().st_mtime_ns == stamp
    assert "Emma" in listen.preview_text("bf_emma")
    with pytest.raises(listen.ListenError):
        listen.preview(cfg, "xx_nobody")


def test_preview_failure_is_an_error(
    cfg: Config, fake_voice: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TTS_FAIL", "load")
    with pytest.raises(listen.ListenError, match="Couldn't record a sample of George"):
        listen.preview(cfg, "bm_george")


# -- housekeeping ---------------------------------------------------------------------------
def _fake_recording(
    cfg: Config, day: str, key: str, age_days: float, status: str = "ready"
) -> Path:
    d = listen.listen_dir(cfg) / day / key
    d.mkdir(parents=True)
    listen.write_json(d / "manifest.json", {"status": status, "pid": os.getpid()})
    (d / "00-intro.mp3").write_bytes(b"x")
    t = time.time() - age_days * 86400
    os.utime(d / "manifest.json", (t, t))
    return d


def test_prune_deletes_recordings_older_than_seven_days(cfg: Config) -> None:
    old = _fake_recording(cfg, "2026-10-10", "a" * 16, age_days=8)
    new = _fake_recording(cfg, "2026-10-21", "b" * 16, age_days=1)
    busy = _fake_recording(cfg, "2026-10-09", "c" * 16, age_days=9, status="recording")
    assert listen.prune(cfg) == 1
    assert not old.exists() and not old.parent.exists()
    assert new.exists() and busy.exists()


def test_a_new_recording_replaces_older_ones_of_that_night(cfg: Config, fake_voice: Path) -> None:
    stale = _fake_recording(cfg, "2026-10-22", "d" * 16, age_days=0)
    plan = listen.plan_for(cfg, DIGEST)
    listen.record(cfg, plan)
    assert not stale.exists() and plan.out.exists()


def test_last_recording(cfg: Config, fake_voice: Path) -> None:
    assert listen.last_recording(cfg) is None
    listen.record(cfg, listen.plan_for(cfg, DIGEST))
    last = listen.last_recording(cfg)
    assert last and last["day"] == "2026-10-22" and last["bytes"] > 0
