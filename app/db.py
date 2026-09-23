"""SQLite persistence.

Two jobs only:

1. **History** — a narrow numeric series table so the power panel can show
   "today vs typical" instead of just an instantaneous watt reading. Homepage
   can't do this because it holds no state.
2. **Bookmarks** — the local store, and the mirror of Nextcloud when that's
   configured.

Deliberately not an ORM and deliberately not Postgres: the whole dataset is a
few MB a year, and the homelab convention is that a JSON/SQLite file beats a
database server at this scale.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    key        TEXT    NOT NULL,
    ts         INTEGER NOT NULL,
    value      REAL,
    PRIMARY KEY (key, ts)
);
CREATE INDEX IF NOT EXISTS series_key_ts ON series (key, ts DESC);

CREATE TABLE IF NOT EXISTS snapshot (
    name       TEXT PRIMARY KEY,
    ts         INTEGER NOT NULL,
    payload    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS bookmark (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT    NOT NULL UNIQUE,
    title      TEXT    NOT NULL,
    folder     TEXT    NOT NULL DEFAULT '',
    notes      TEXT    NOT NULL DEFAULT '',
    source     TEXT    NOT NULL DEFAULT 'manual',
    tags       TEXT    NOT NULL DEFAULT '',
    added_ts   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS bookmark_folder ON bookmark (folder, title);

CREATE TABLE IF NOT EXISTS event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    detail     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS event_ts ON event (ts DESC);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """Additive column migrations.

        `CREATE TABLE IF NOT EXISTS` is a no-op on a database that already exists, so a
        new column in SCHEMA reaches a fresh install and silently misses every running
        one. Each step is idempotent and checked against the live table.
        """
        cols = {r[1] for r in c.execute("PRAGMA table_info(bookmark)")}
        if "tags" not in cols:
            c.execute("ALTER TABLE bookmark ADD COLUMN tags TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ----- series -----------------------------------------------------------
    def record(self, points: dict[str, float], ts: int | None = None) -> None:
        ts = ts or int(time.time())
        rows = [(k, ts, v) for k, v in points.items() if v is not None]
        if not rows:
            return
        with self.conn() as c:
            c.executemany("INSERT OR REPLACE INTO series (key, ts, value) VALUES (?,?,?)", rows)

    def series_since(self, key: str, since_ts: int) -> list[tuple[int, float]]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT ts, value FROM series WHERE key=? AND ts>=? ORDER BY ts", (key, since_ts)
            ).fetchall()
        return [(r["ts"], r["value"]) for r in rows]

    def window_stats(self, key: str, start_ts: int, end_ts: int) -> dict | None:
        """Mean value of one key over [start_ts, end_ts), plus how well covered it is.

        The MEAN is deliberate. Power is duty-cycled: a dishwasher sits at 0 W
        most of the day and spikes during a cycle, so its median is 0 and any
        comparison against it is meaningless. A mean over a fixed window is
        proportional to the energy used in that window, which is what "using
        more than usual" actually means, and it behaves correctly for both
        always-on loads (a desk) and intermittent ones (a fridge compressor).

        `span` lets the caller refuse to judge a baseline it does not have
        enough history to trust.
        """
        with self.conn() as c:
            r = c.execute(
                "SELECT AVG(value) AS avg, COUNT(*) AS n, MIN(ts) AS lo, MAX(ts) AS hi "
                "FROM series WHERE key=? AND ts>=? AND ts<?",
                (key, start_ts, end_ts),
            ).fetchone()
        if not r or not r["n"]:
            return None
        return {"avg": r["avg"], "n": r["n"], "span": (r["hi"] or 0) - (r["lo"] or 0)}

    def latest(self, key: str) -> tuple[int, float] | None:
        with self.conn() as c:
            r = c.execute(
                "SELECT ts, value FROM series WHERE key=? ORDER BY ts DESC LIMIT 1", (key,)
            ).fetchone()
        return (r["ts"], r["value"]) if r else None

    def prune(self, older_than_days: int = 400) -> int:
        cutoff = int(time.time()) - older_than_days * 86400
        with self.conn() as c:
            cur = c.execute("DELETE FROM series WHERE ts < ?", (cutoff,))
            return cur.rowcount

    # ----- snapshot ---------------------------------------------------------
    def put_snapshot(self, name: str, payload: Any) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO snapshot (name, ts, payload) VALUES (?,?,?)",
                (name, int(time.time()), json.dumps(payload, default=str)),
            )

    def get_snapshot(self, name: str) -> tuple[int, Any] | None:
        with self.conn() as c:
            r = c.execute("SELECT ts, payload FROM snapshot WHERE name=?", (name,)).fetchone()
        if not r:
            return None
        return r["ts"], json.loads(r["payload"])

    # ----- bookmarks --------------------------------------------------------
    def upsert_bookmark(
        self, url: str, title: str, folder: str = "", notes: str = "",
        source: str = "manual", tags: str = ""
    ) -> None:
        with self.conn() as c:
            c.execute(
                """INSERT INTO bookmark (url,title,folder,notes,source,tags,added_ts)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(url) DO UPDATE SET
                     title=excluded.title, folder=excluded.folder,
                     notes=excluded.notes, source=excluded.source,
                     tags=excluded.tags""",
                (url, title, folder, notes, source, tags, int(time.time())),
            )

    def bookmarks(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM bookmark ORDER BY folder, title COLLATE NOCASE"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_bookmark(self, bid: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM bookmark WHERE id=?", (bid,))

    def replace_source(self, source: str) -> None:
        """Drop every bookmark from one source, ahead of a fresh sync."""
        with self.conn() as c:
            c.execute("DELETE FROM bookmark WHERE source=?", (source,))

    # ----- events -----------------------------------------------------------
    def log_event(self, kind: str, detail: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO event (ts, kind, detail) VALUES (?,?,?)",
                (int(time.time()), kind, detail),
            )

    def recent_events(self, limit: int = 20) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM event ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
