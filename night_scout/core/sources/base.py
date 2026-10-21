"""Shared pieces for source adapters."""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from ..http import PoliteClient
from ..models import Item


@dataclass
class SourceTask:
    """One fetchable unit (a channel, a subreddit, a hashtag) with its own status line."""

    key: str
    label: str
    fetch: Callable[[PoliteClient], list[Item]]


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_html(text: str | None, limit: int = 4000) -> str:
    if not text:
        return ""
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", text)
    text = html.unescape(_TAG.sub(" ", text))
    text = "\n".join(_WS.sub(" ", line).strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def parse_time(value: Any) -> datetime | None:
    """Accept ISO 8601 strings, RFC 2822 strings, unix seconds or feedparser struct_time."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=UTC)
        if hasattr(value, "tm_year"):
            y, mo, d, h, mi, sec = value[:6]
            return datetime(y, mo, d, h, mi, sec, tzinfo=UTC)
        s = str(value).strip()
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            dt = parsedate_to_datetime(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (ValueError, TypeError, OverflowError, IndexError):
        return None


def first_line(text: str, limit: int = 140) -> str:
    line = text.strip().split("\n", 1)[0]
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
