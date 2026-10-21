"""Canonical URLs for exact dedupe, and cosine similarity for near-duplicates."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PREFIXES = ("utm_", "mc_", "pk_")
_TRACKING_KEYS = {"fbclid", "gclid", "ref", "ref_src", "ref_url", "si"}
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def canonical_url(url: str) -> str:
    """Normalize a URL so the same story found via two sources collides.

    Lowercases scheme and host, drops "www." / "m." / "old.", tracking params,
    fragments and trailing slashes, and folds YouTube URL forms to watch?v=ID.
    """
    url = url.strip()
    if not url:
        return ""
    parts = urlsplit(url)
    scheme = (parts.scheme or "https").lower()
    if scheme == "http":
        scheme = "https"
    host = (parts.hostname or "").lower()
    for prefix in ("www.", "m.", "old.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    path = parts.path or "/"

    # YouTube: youtu.be/ID, /shorts/ID, /embed/ID and /watch?v=ID all mean one video.
    if host in ("youtube.com", "youtu.be", "youtube-nocookie.com"):
        vid = None
        if host == "youtu.be":
            vid = path.strip("/").split("/")[0]
        elif path.startswith(("/shorts/", "/embed/", "/live/")):
            vid = path.split("/")[2] if len(path.split("/")) > 2 else None
        elif path == "/watch":
            vid = dict(parse_qsl(parts.query)).get("v")
        if vid and _YT_ID.match(vid):
            return f"https://youtube.com/watch?v={vid}"

    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PREFIXES) and k.lower() not in _TRACKING_KEYS
    ]
    query.sort()
    if len(path) > 1:
        path = path.rstrip("/")
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    return urlunsplit((scheme, host + port, path, urlencode(query), ""))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def near_duplicate_groups(
    vectors: Mapping[int, Sequence[float]], threshold: float
) -> dict[int, int]:
    """Map each item id to the id it duplicates (the earliest id in its group).

    Items that duplicate nothing are absent from the result. O(n^2), fine for a
    nightly batch of a few hundred items.
    """
    ids = sorted(vectors)
    dup_of: dict[int, int] = {}
    for i, a in enumerate(ids):
        if a in dup_of:
            continue
        for b in ids[i + 1 :]:
            if b in dup_of:
                continue
            if cosine(vectors[a], vectors[b]) >= threshold:
                dup_of[b] = a
    return dup_of
