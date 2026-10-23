"""Fake Kokoro for tests: 0.1 s of silence per word. FAKE_TTS_DELAY slows each chapter;
FAKE_TTS_FAIL=load fails loading the model, and a text containing BOOM fails that chapter."""

import os
import time


class _Result:
    def __init__(self, audio):
        self.audio = audio


class KPipeline:
    def __init__(self, lang_code, repo_id=None, **kw):
        if os.environ.get("FAKE_TTS_FAIL") == "load":
            raise RuntimeError("no model here")
        self.lang_code = lang_code

    def __call__(self, text, voice=None, speed=1.0):
        if "BOOM" in text:
            raise RuntimeError("could not say that")
        time.sleep(float(os.environ.get("FAKE_TTS_DELAY") or 0))
        yield _Result([0.0] * (2400 * len(text.split())))
