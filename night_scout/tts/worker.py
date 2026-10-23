"""Records a Digest (or a voice preview) with Kokoro. Runs OUT of the web server's process:

    <voice python> worker.py <job.json>

It's a standalone script on purpose: the voice add-on (kokoro, PyTorch) can live in another
Python than Night Scout, and torch never loads into the web server. Only the standard library,
kokoro, soundfile and numpy are imported here; nothing from night_scout.

job.json: {"voice": "af_heart", "out": "<dir>", "manifest": "manifest.json" | null,
           "chapters": [{"id": ..., "text": ..., "file": "01-....mp3", "gap": 0.8}, ...]}

Each chapter becomes its own MP3 in <out>, written under a temporary name and renamed when
complete, so a reader never sees half a file. With a manifest, the worker updates it after
every chapter (seconds, done, status), which is what lets playback start before the whole
Digest is recorded. A chapter id written to <out>/want.json is recorded next (someone pressed
Listen on that story).
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

RATE = 24000  # Kokoro's sample rate


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


REPO = "hexgrad/Kokoro-82M"
MODEL_FILES = ("config.json", "kokoro-v1_0.pth")


def hub_cache() -> Path:
    """Where huggingface_hub keeps downloads ($HF_HUB_CACHE, $HF_HOME/hub, ~/.cache/...)."""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"]).expanduser()
    home = os.environ.get("HF_HOME") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "huggingface"
    )
    return Path(home).expanduser() / "hub"


def cached(voice: str) -> bool:
    """Kokoro's model and this voice are already downloaded (in some snapshot)."""
    snaps = hub_cache() / f"models--{REPO.replace('/', '--')}" / "snapshots"
    need = (*MODEL_FILES, f"voices/{voice}.pt")
    return any(all((s / f).is_file() for f in need) for s in snaps.glob("*"))


def next_chapter(chapters: list[dict], done: set[int], out: Path) -> int:
    """The wanted chapter if it's still to do, else the first one not done."""
    want = read_json(out / "want.json").get("id")
    for i, c in enumerate(chapters):
        if i not in done and want is not None and c.get("id") == want:
            return i
    return min(i for i in range(len(chapters)) if i not in done)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: worker.py <job.json>", file=sys.stderr)
        return 2
    job = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    out = Path(job["out"])
    voice = str(job["voice"])
    chapters: list[dict] = job["chapters"]
    mpath = out / job["manifest"] if job.get("manifest") else None
    t0 = time.time()

    def update(**fields: object) -> None:
        if mpath is None:
            return
        m = read_json(mpath)
        m.update(fields)
        write_json(mpath, m)

    update(pid=os.getpid(), status="recording")
    # Once the model and voice are downloaded, don't ask the Hub again: each load would
    # otherwise check online first, and a slow network stalls the recording. (Read by
    # huggingface_hub when it's imported, so it's set before kokoro is.)
    if "HF_HUB_OFFLINE" not in os.environ and cached(voice):
        os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        import numpy as np
        import soundfile as sf
        from kokoro import KPipeline

        pipe = KPipeline(lang_code="b" if voice.startswith("b") else "a", repo_id=REPO)
        done: set[int] = set()
        while len(done) < len(chapters):
            i = next_chapter(chapters, done, out)
            c = chapters[i]
            parts = [
                np.asarray(r.audio, dtype=np.float32)
                for r in pipe(c["text"], voice=voice)
                if r.audio is not None
            ]
            gap = np.zeros(int(RATE * float(c.get("gap", 0.0))), dtype=np.float32)
            audio = np.concatenate([*parts, gap]) if parts else gap
            target = out / c["file"]
            tmp = target.with_name(f".{target.stem}.part.mp3")
            sf.write(str(tmp), audio, RATE, format="MP3")
            os.replace(tmp, target)
            done.add(i)
            if mpath is not None:
                m = read_json(mpath)
                for mc in m.get("chapters", []):
                    if mc.get("file") == c["file"]:
                        mc["seconds"] = round(len(audio) / RATE, 2)
                m["done"] = len(done)
                write_json(mpath, m)
        update(
            status="ready",
            render_s=round(time.time() - t0, 1),
            bytes=sum((out / c["file"]).stat().st_size for c in chapters),
            finished=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        return 0
    except Exception as e:  # recorded in the manifest; the caller decides what to show
        traceback.print_exc()
        update(status="failed", error=f"{type(e).__name__}: {e}"[:300])
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
