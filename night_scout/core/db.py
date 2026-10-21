"""SQLite store. One file, local only."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .dedupe import canonical_url
from .models import Item, SourceStatus, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    url_key TEXT NOT NULL,
    source TEXT NOT NULL,
    source_name TEXT NOT NULL,
    title TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    author TEXT,
    published_at TEXT,
    first_seen TEXT NOT NULL,
    run_id INTEGER,
    extra TEXT NOT NULL DEFAULT '{}',
    also_seen TEXT NOT NULL DEFAULT '[]',
    dup_of INTEGER,
    embedding TEXT,
    prefilter REAL,
    status TEXT NOT NULL DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS idx_items_url_key ON items(url_key);
CREATE INDEX IF NOT EXISTS idx_items_run ON items(run_id);

CREATE TABLE IF NOT EXISTS scores (
    item_id INTEGER PRIMARY KEY REFERENCES items(id),
    model TEXT NOT NULL,
    summary TEXT NOT NULL,
    best_project TEXT,
    best_score REAL NOT NULL,
    per_project TEXT NOT NULL,
    topics TEXT NOT NULL DEFAULT '[]',
    scored_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    kind TEXT NOT NULL,
    project TEXT,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'proposed',
    result TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_item ON actions(item_id);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    target_type TEXT NOT NULL,
    target TEXT NOT NULL,
    value INTEGER NOT NULL,
    item_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research (
    item_id INTEGER PRIMARY KEY REFERENCES items(id),
    model TEXT NOT NULL,
    brief TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    stats TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS source_status (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    key TEXT NOT NULL,
    label TEXT NOT NULL,
    ok INTEGER NOT NULL,
    fetched INTEGER NOT NULL,
    new INTEGER NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, key)
);

CREATE TABLE IF NOT EXISTS digests (
    day TEXT PRIMARY KEY,
    run_id INTEGER,
    path TEXT,
    item_ids TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


@dataclass
class InsertResult:
    item_id: int
    new: bool


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # The web UI serves requests from a threadpool; each call is short, so one
        # connection with check_same_thread=False plus SQLite's own locking is enough.
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        # The web UI writes from several threads; serialize transactions so one thread's
        # commit or rollback can't land in the middle of another's.
        self._lock = threading.RLock()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def fail_interrupted_actions(self) -> int:
        """Try-its left 'running' by a stopped server can never finish; mark them failed."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE actions SET status='failed', result=?, updated_at=? WHERE status='running'",
                (
                    json.dumps({"reason": "interrupted: Night Scout stopped while it ran"}),
                    _iso(utcnow()),
                ),
            )
            return cur.rowcount

    # -- runs ---------------------------------------------------------------
    def start_run(self) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO runs(started_at) VALUES (?)", (_iso(utcnow()),))
            return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, status: str, stats: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, status=?, stats=? WHERE id=?",
                (_iso(utcnow()), status, json.dumps(stats), run_id),
            )

    def record_source_status(self, run_id: int, st: SourceStatus) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO source_status VALUES (?,?,?,?,?,?,?)",
                (run_id, st.key, st.label, int(st.ok), st.fetched, st.new, st.message),
            )

    def last_run(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()

    def source_statuses(self, run_id: int) -> list[SourceStatus]:
        rows = self.conn.execute(
            "SELECT * FROM source_status WHERE run_id=? ORDER BY ok, key", (run_id,)
        ).fetchall()
        return [
            SourceStatus(
                key=r["key"],
                label=r["label"],
                ok=bool(r["ok"]),
                fetched=r["fetched"],
                new=r["new"],
                message=r["message"],
            )
            for r in rows
        ]

    # -- items --------------------------------------------------------------
    def upsert_item(self, item: Item, run_id: int | None) -> InsertResult:
        """Insert a new item, or note a sighting if the uid or URL is already known."""
        url_key = canonical_url(item.url) or item.uid
        row = self.conn.execute("SELECT id FROM items WHERE uid=?", (item.uid,)).fetchone()
        if row:
            return InsertResult(row["id"], False)
        row = self.conn.execute(
            "SELECT id, also_seen FROM items WHERE url_key=? ORDER BY id LIMIT 1", (url_key,)
        ).fetchone()
        if row:
            seen = json.loads(row["also_seen"])
            tag = f"{item.source}:{item.source_name}"
            if tag not in seen:
                seen.append(tag)
                with self.tx() as c:
                    c.execute(
                        "UPDATE items SET also_seen=? WHERE id=?", (json.dumps(seen), row["id"])
                    )
            # Remember this uid too, so the next run doesn't re-check it. It points at the
            # original via dup_of and is never scored.
            with self.tx() as c:
                c.execute(
                    "INSERT OR IGNORE INTO items(uid,url,url_key,source,source_name,title,text,"
                    "author,published_at,first_seen,run_id,extra,dup_of,status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'duplicate')",
                    (
                        item.uid,
                        item.url,
                        url_key,
                        item.source,
                        item.source_name,
                        item.title,
                        "",
                        item.author,
                        _iso(item.published_at),
                        _iso(utcnow()),
                        run_id,
                        "{}",
                        row["id"],
                    ),
                )
            return InsertResult(row["id"], False)
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO items(uid,url,url_key,source,source_name,title,text,author,"
                "published_at,first_seen,run_id,extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item.uid,
                    item.url,
                    url_key,
                    item.source,
                    item.source_name,
                    item.title,
                    item.text,
                    item.author,
                    _iso(item.published_at),
                    _iso(utcnow()),
                    run_id,
                    json.dumps(item.extra),
                ),
            )
            return InsertResult(int(cur.lastrowid or 0), True)

    def item(self, item_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()

    def items_with_status(self, status: str, run_id: int | None = None) -> list[sqlite3.Row]:
        if run_id is None:
            return self.conn.execute(
                "SELECT * FROM items WHERE status=? ORDER BY id", (status,)
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM items WHERE status=? AND run_id=? ORDER BY id", (status, run_id)
        ).fetchall()

    def set_item_fields(self, item_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE items SET {cols} WHERE id=?", (*fields.values(), item_id))

    def update_extra(self, item_id: int, **updates: Any) -> None:
        row = self.item(item_id)
        if row is None:
            return
        extra = json.loads(row["extra"])
        extra.update(updates)
        self.set_item_fields(item_id, extra=json.dumps(extra))

    def count_items(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])

    # -- scores -------------------------------------------------------------
    def save_score(
        self,
        item_id: int,
        model: str,
        summary: str,
        per_project: list[dict[str, Any]],
        topics: Iterable[str],
    ) -> None:
        best = max(per_project, key=lambda p: p["relevance"], default=None)
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO scores VALUES (?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    model,
                    summary,
                    best["project"] if best else None,
                    float(best["relevance"]) if best else 0.0,
                    json.dumps(per_project),
                    json.dumps(list(topics)),
                    _iso(utcnow()),
                ),
            )
            c.execute("UPDATE items SET status='scored' WHERE id=?", (item_id,))

    def score(self, item_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM scores WHERE item_id=?", (item_id,)).fetchone()

    def scored_since(self, iso: str) -> list[sqlite3.Row]:
        """Items scored at or after `iso` (i.e. during one run), best first."""
        return self.conn.execute(
            "SELECT i.*, s.summary, s.best_project, s.best_score, s.per_project, s.topics, "
            "s.model AS score_model FROM items i JOIN scores s ON s.item_id=i.id "
            "WHERE s.scored_at>=? ORDER BY s.best_score DESC, i.id",
            (iso,),
        ).fetchall()

    def scored_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT i.*, s.summary, s.best_project, s.best_score, s.per_project, s.topics, "
            "s.model AS score_model FROM items i JOIN scores s ON s.item_id=i.id "
            "WHERE i.run_id=? ORDER BY s.best_score DESC, i.id",
            (run_id,),
        ).fetchall()

    # -- actions ------------------------------------------------------------
    def add_action(
        self, item_id: int, kind: str, project: str | None, title: str, detail: dict[str, Any]
    ) -> int:
        now = _iso(utcnow())
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO actions(item_id,kind,project,title,detail,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (item_id, kind, project, title, json.dumps(detail), now, now),
            )
            return int(cur.lastrowid or 0)

    def actions_for(self, item_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM actions WHERE item_id=? ORDER BY id", (item_id,)
        ).fetchall()

    def action(self, action_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()

    def set_action(self, action_id: int, status: str, result: dict[str, Any] | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE actions SET status=?, result=?, updated_at=? WHERE id=?",
                (
                    status,
                    json.dumps(result) if result is not None else None,
                    _iso(utcnow()),
                    action_id,
                ),
            )

    # -- research -----------------------------------------------------------
    def save_research(self, item_id: int, model: str, brief: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO research VALUES (?,?,?,?)",
                (item_id, model, brief, _iso(utcnow())),
            )

    def research(self, item_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM research WHERE item_id=?", (item_id,)).fetchone()

    # -- feedback -----------------------------------------------------------
    def add_feedback(
        self, target_type: str, target: str, value: int, item_id: int | None = None
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO feedback(target_type,target,value,item_id,created_at) "
                "VALUES (?,?,?,?,?)",
                (target_type, target, value, item_id, _iso(utcnow())),
            )

    def feedback_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM feedback ORDER BY id").fetchall()

    def item_feedback(self, item_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT value FROM feedback WHERE target_type='item' AND item_id=? "
            "ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return int(row["value"]) if row else None

    # -- digests ------------------------------------------------------------
    def save_digest(self, day: str, run_id: int, path: str, item_ids: list[int]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO digests VALUES (?,?,?,?,?)",
                (day, run_id, path, json.dumps(item_ids), _iso(utcnow())),
            )

    def latest_digest(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM digests ORDER BY day DESC LIMIT 1").fetchone()

    def digest(self, day: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM digests WHERE day=?", (day,)).fetchone()

    def digest_days(self, limit: int = 14) -> list[str]:
        rows = self.conn.execute(
            "SELECT day FROM digests ORDER BY day DESC LIMIT ?", (limit,)
        ).fetchall()
        return [r["day"] for r in rows]
