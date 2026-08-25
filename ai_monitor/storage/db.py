import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from ai_monitor.storage.models import Item

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "monitor.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    content TEXT DEFAULT '',
    authors TEXT DEFAULT '[]',
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    raw_json TEXT DEFAULT '{}',
    canonical_id INTEGER REFERENCES items(id),
    UNIQUE(source, source_id)
);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    summary TEXT,
    relevance_score REAL,
    matched_areas TEXT DEFAULT '[]',
    justification TEXT,
    model TEXT,
    content_hash TEXT,
    analyzed_at TEXT,
    UNIQUE(item_id)
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    steps_taken INTEGER,
    tool_calls TEXT DEFAULT '[]',
    stop_reason TEXT,
    critique TEXT,
    revised INTEGER DEFAULT 0,
    cost_usd REAL,
    created_at TEXT,
    UNIQUE(item_id)
);

CREATE TABLE IF NOT EXISTS eval_items (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    human_score REAL,
    human_notes TEXT,
    judge_score REAL,
    judge_reasoning TEXT,
    agreement REAL,
    evaluated_at TEXT,
    UNIQUE(item_id)
);

CREATE TABLE IF NOT EXISTS briefs (
    id INTEGER PRIMARY KEY,
    week_of TEXT NOT NULL,
    markdown TEXT NOT NULL,
    item_count INTEGER,
    created_at TEXT
);
"""


def connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def upsert_item(conn: sqlite3.Connection, item: Item) -> int:
    """Insert or refresh an item keyed on (source, source_id); returns its id.

    On conflict, content fields are refreshed but id and canonical_id are
    preserved, so re-running a watcher never duplicates or unlinks anything.
    """
    cur = conn.execute(
        """
        INSERT INTO items
            (source, source_id, title, url, content, authors,
             published_at, fetched_at, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, source_id) DO UPDATE SET
            title = excluded.title,
            url = excluded.url,
            content = excluded.content,
            authors = excluded.authors,
            published_at = excluded.published_at,
            fetched_at = excluded.fetched_at,
            raw_json = excluded.raw_json
        RETURNING id
        """,
        (
            item.source.value,
            item.source_id,
            item.title,
            item.url,
            item.content,
            json.dumps(item.authors),
            _iso(item.published_at),
            _iso(item.fetched_at),
            json.dumps(item.raw, default=str),
        ),
    )
    row = cur.fetchone()
    conn.commit()
    return row["id"]


def get_item(conn: sqlite3.Connection, item_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM items WHERE id = ?", (item_id,)
    ).fetchone()


def get_items(
    conn: sqlite3.Connection,
    source: Optional[str] = None,
    since: Optional[datetime] = None,
    canonical_only: bool = False,
) -> list[sqlite3.Row]:
    query = "SELECT * FROM items WHERE 1=1"
    params: list = []
    if source:
        query += " AND source = ?"
        params.append(source)
    if since:
        query += " AND fetched_at >= ?"
        params.append(since.isoformat())
    if canonical_only:
        query += " AND canonical_id IS NULL"
    query += " ORDER BY fetched_at DESC"
    return conn.execute(query, params).fetchall()


def count_items(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
