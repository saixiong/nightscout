"""Fake soundfile: writes a small file standing in for the MP3 (tests only)."""

import json


def write(path, data, rate, format=None):
    with open(path, "wb") as fh:
        fh.write(
            b"ID3FAKE" + json.dumps({"samples": len(data), "rate": rate, "format": format}).encode()
        )
        fh.write(b"\0" * 2048)
