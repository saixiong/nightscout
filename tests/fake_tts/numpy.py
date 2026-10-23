"""Just enough numpy for night_scout/tts/worker.py, over plain lists (tests only)."""

float32 = "float32"


def asarray(x, dtype=None):
    return list(x)


def zeros(n, dtype=None):
    return [0.0] * n


def concatenate(parts):
    out = []
    for p in parts:
        out.extend(p)
    return out
