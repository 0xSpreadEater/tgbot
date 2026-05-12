import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS topics (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        query TEXT NOT NULL,
        last_seen_id TEXT,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS allowlist (
        handle TEXT PRIMARY KEY
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feedback (
        tweet_id TEXT PRIMARY KEY,
        topic_name TEXT,
        author_handle TEXT NOT NULL,
        label TEXT NOT NULL,
        tweet_text TEXT NOT NULL,
        tweet_metrics_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS author_scores (
        handle TEXT PRIMARY KEY,
        ups INTEGER NOT NULL DEFAULT 0,
        downs INTEGER NOT NULL DEFAULT 0,
        last_up_at TIMESTAMP,
        last_down_at TIMESTAMP,
        muted_until TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS suggestion_blocks (
        handle TEXT PRIMARY KEY,
        blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pending_feedback (
        tweet_id TEXT PRIMARY KEY,
        author_handle TEXT NOT NULL,
        tweet_text TEXT NOT NULL,
        topic_name TEXT,
        metrics_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS token_mentions (
        token TEXT NOT NULL,
        chain TEXT NOT NULL,
        cycle_ts TIMESTAMP NOT NULL,
        weighted_count REAL NOT NULL,
        raw_count INTEGER NOT NULL,
        unique_authors_count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (token, chain, cycle_ts)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS token_metadata (
        token TEXT NOT NULL,
        chain TEXT NOT NULL,
        contract_address TEXT,
        first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        user_dismissed BOOLEAN NOT NULL DEFAULT 0,
        PRIMARY KEY (token, chain)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sector_dictionary (
        term TEXT PRIMARY KEY,
        display_name TEXT,
        weight REAL NOT NULL DEFAULT 1.0,
        source TEXT NOT NULL,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        promoted_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS venue_dictionary (
        term TEXT PRIMARY KEY,
        display_name TEXT,
        weight REAL NOT NULL DEFAULT 1.0,
        source TEXT NOT NULL,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        promoted_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS entity_mentions (
        entity_type TEXT NOT NULL,
        entity_term TEXT NOT NULL,
        cycle_ts TIMESTAMP NOT NULL,
        weighted_count REAL NOT NULL,
        raw_count INTEGER NOT NULL,
        unique_authors INTEGER NOT NULL,
        PRIMARY KEY (entity_type, entity_term, cycle_ts)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dictionary_feedback (
        entity_type TEXT NOT NULL,
        entity_term TEXT NOT NULL,
        label TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (entity_type, entity_term, created_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS airdrop_opportunities (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_feedback_label_created ON feedback(label, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_feedback_author ON feedback(author_handle)",
    "CREATE INDEX IF NOT EXISTS idx_token_mentions_cycle ON token_mentions(cycle_ts)",
    "CREATE INDEX IF NOT EXISTS idx_token_mentions_token_chain ON token_mentions(token, chain, cycle_ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_entity_mentions_lookup ON entity_mentions(entity_type, entity_term, cycle_ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sector_dict_weight ON sector_dictionary(weight DESC)",
    "CREATE INDEX IF NOT EXISTS idx_venue_dict_weight ON venue_dictionary(weight DESC)",
)


async def connect(db_path: str) -> aiosqlite.Connection:
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = aiosqlite.Row
    return conn


async def apply_schema(conn: aiosqlite.Connection) -> None:
    for stmt in SCHEMA_STATEMENTS:
        await conn.execute(stmt)
    await conn.commit()
    log.info("schema_applied", extra={"statements": len(SCHEMA_STATEMENTS)})


async def init_db(db_path: str) -> aiosqlite.Connection:
    from bebop_bot.seed import seed_all

    log.info("db_init_start", extra={"db_path": db_path})
    conn = await connect(db_path)
    await apply_schema(conn)
    await seed_all(conn)
    log.info("db_init_done", extra={"db_path": db_path})
    return conn


async def get_setting(conn: aiosqlite.Connection, key: str, default: str | None = None) -> str | None:
    async with conn.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    return row["value"] if row else default


async def set_setting(conn: aiosqlite.Connection, key: str, value: str) -> None:
    await conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await conn.commit()


async def count_rows(conn: aiosqlite.Connection, table: str) -> int:
    async with conn.execute(f"SELECT COUNT(*) AS n FROM {table}") as cur:
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def fetch_all(conn: aiosqlite.Connection, sql: str, params: Iterable = ()) -> list[aiosqlite.Row]:
    async with conn.execute(sql, tuple(params)) as cur:
        return list(await cur.fetchall())


@dataclass(frozen=True, slots=True)
class TopicRow:
    name: str
    query: str
    last_seen_id: str | None


@dataclass(frozen=True, slots=True)
class FeedbackRow:
    tweet_id: str
    author_handle: str
    label: str
    tweet_text: str


class Db:
    """Thin async wrapper exposing the operations pipeline code needs.

    Holds a reference to an open aiosqlite connection. Existing handlers may
    keep using the raw connection; this class is the surface the pipeline,
    filter, and new commands use.
    """

    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        return await get_setting(self.conn, key, default)

    async def set_setting(self, key: str, value: str) -> None:
        await set_setting(self.conn, key, value)

    async def is_paused(self) -> bool:
        return (await self.get_setting("paused", "0")) == "1"

    async def get_topics(self) -> list[TopicRow]:
        rows = await fetch_all(
            self.conn,
            "SELECT name, query, last_seen_id FROM topics ORDER BY name",
        )
        return [TopicRow(r["name"], r["query"], r["last_seen_id"]) for r in rows]

    async def update_topic_since_id(self, name: str, since_id: str) -> None:
        await self.conn.execute(
            "UPDATE topics SET last_seen_id = ? WHERE name = ?",
            (since_id, name),
        )
        await self.conn.commit()

    async def get_allowlist(self) -> set[str]:
        rows = await fetch_all(self.conn, "SELECT handle FROM allowlist")
        return {r["handle"].lower() for r in rows}

    async def get_recent_feedback(self, label: str, limit: int) -> list[FeedbackRow]:
        rows = await fetch_all(
            self.conn,
            "SELECT tweet_id, author_handle, label, tweet_text FROM feedback "
            "WHERE label = ? ORDER BY created_at DESC LIMIT ?",
            (label, int(limit)),
        )
        return [
            FeedbackRow(r["tweet_id"], r["author_handle"], r["label"], r["tweet_text"])
            for r in rows
        ]

    async def get_author_feedback_counts(self, handle: str, days: int = 60) -> tuple[int, int]:
        sql = (
            "SELECT "
            "  SUM(CASE WHEN label='up' THEN 1 ELSE 0 END) AS ups, "
            "  SUM(CASE WHEN label='down' THEN 1 ELSE 0 END) AS downs "
            "FROM feedback "
            "WHERE author_handle = ? AND created_at >= datetime('now', ?)"
        )
        days_clause = f"-{int(days)} days"
        async with self.conn.execute(sql, (handle.lower(), days_clause)) as cur:
            row = await cur.fetchone()
        if not row:
            return 0, 0
        return int(row["ups"] or 0), int(row["downs"] or 0)

    async def get_muted_until(self, handle: str) -> str | None:
        async with self.conn.execute(
            "SELECT muted_until FROM author_scores WHERE handle = ?",
            (handle.lower(),),
        ) as cur:
            row = await cur.fetchone()
        return row["muted_until"] if row and row["muted_until"] else None

    async def add_feedback(
        self,
        *,
        tweet_id: str,
        topic_name: str | None,
        author_handle: str,
        label: str,
        tweet_text: str,
        metrics: dict | None = None,
    ) -> None:
        metrics_json = json.dumps(metrics) if metrics else None
        await self.conn.execute(
            "INSERT OR REPLACE INTO feedback("
            "tweet_id, topic_name, author_handle, label, tweet_text, tweet_metrics_json"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (tweet_id, topic_name, author_handle.lower(), label, tweet_text, metrics_json),
        )
        await self.conn.commit()

    async def upsert_pending_feedback(
        self,
        *,
        tweet_id: str,
        author_handle: str,
        tweet_text: str,
        topic_name: str | None,
        metrics_json: str,
    ) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO pending_feedback("
            "tweet_id, author_handle, tweet_text, topic_name, metrics_json"
            ") VALUES (?, ?, ?, ?, ?)",
            (tweet_id, author_handle.lower(), tweet_text, topic_name, metrics_json),
        )
        await self.conn.commit()

    async def get_pending_feedback(self, tweet_id: str) -> dict | None:
        async with self.conn.execute(
            "SELECT tweet_id, author_handle, tweet_text, topic_name, metrics_json "
            "FROM pending_feedback WHERE tweet_id = ?",
            (tweet_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "tweet_id": row["tweet_id"],
            "author_handle": row["author_handle"],
            "tweet_text": row["tweet_text"],
            "topic_name": row["topic_name"],
            "metrics_json": row["metrics_json"],
        }

    async def get_feedback(self, tweet_id: str) -> dict | None:
        async with self.conn.execute(
            "SELECT tweet_id, topic_name, author_handle, label, tweet_text, tweet_metrics_json "
            "FROM feedback WHERE tweet_id = ?",
            (tweet_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "tweet_id": row["tweet_id"],
            "topic_name": row["topic_name"],
            "author_handle": row["author_handle"],
            "label": row["label"],
            "tweet_text": row["tweet_text"],
            "tweet_metrics_json": row["tweet_metrics_json"],
        }

    async def upsert_feedback(
        self,
        *,
        tweet_id: str,
        topic_name: str | None,
        author_handle: str,
        label: str,
        tweet_text: str,
        tweet_metrics_json: str | None = None,
    ) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO feedback("
            "tweet_id, topic_name, author_handle, label, tweet_text, tweet_metrics_json"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (tweet_id, topic_name, author_handle.lower(), label, tweet_text, tweet_metrics_json),
        )
        await self.conn.commit()

    async def adjust_author_score(
        self,
        handle: str,
        prior_label: str | None,
        new_label: str,
    ) -> None:
        """Diff the author_scores counters between prior and new label.

        Only up/down affect counters; mute is tracked via muted_until separately.
        """
        h = handle.lower()
        d_up = 0
        d_down = 0
        if prior_label == "up":
            d_up -= 1
        elif prior_label == "down":
            d_down -= 1
        if new_label == "up":
            d_up += 1
        elif new_label == "down":
            d_down += 1

        now_iso = datetime.utcnow().isoformat()
        await self.conn.execute(
            "INSERT INTO author_scores(handle, ups, downs, last_up_at, last_down_at) "
            "VALUES(?, 0, 0, NULL, NULL) "
            "ON CONFLICT(handle) DO NOTHING",
            (h,),
        )
        await self.conn.execute(
            "UPDATE author_scores SET "
            "ups = MAX(0, ups + ?), "
            "downs = MAX(0, downs + ?), "
            "last_up_at = CASE WHEN ? = 'up' THEN ? ELSE last_up_at END, "
            "last_down_at = CASE WHEN ? = 'down' THEN ? ELSE last_down_at END "
            "WHERE handle = ?",
            (d_up, d_down, new_label, now_iso, new_label, now_iso, h),
        )
        await self.conn.commit()

    async def set_author_muted(self, handle: str, until: datetime) -> None:
        h = handle.lower()
        until_iso = until.isoformat()
        await self.conn.execute(
            "INSERT INTO author_scores(handle, ups, downs, muted_until) VALUES(?, 0, 0, ?) "
            "ON CONFLICT(handle) DO UPDATE SET muted_until = excluded.muted_until",
            (h, until_iso),
        )
        await self.conn.commit()

    async def clear_author_muted(self, handle: str) -> bool:
        h = handle.lower()
        cur = await self.conn.execute(
            "UPDATE author_scores SET muted_until = NULL WHERE handle = ?",
            (h,),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_recent_author_score(self, handle: str, days: int) -> tuple[int, int]:
        return await self.get_author_feedback_counts(handle, days=days)

    async def is_allowlisted(self, handle: str) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM allowlist WHERE handle = ?",
            (handle.lower(),),
        ) as cur:
            row = await cur.fetchone()
        return row is not None

    async def add_to_allowlist(self, handle: str) -> bool:
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO allowlist(handle) VALUES(?)",
            (handle.lower(),),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def is_suggestion_blocked(self, handle: str) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM suggestion_blocks WHERE handle = ?",
            (handle.lower(),),
        ) as cur:
            row = await cur.fetchone()
        return row is not None

    async def add_suggestion_block(self, handle: str) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO suggestion_blocks(handle) VALUES(?)",
            (handle.lower(),),
        )
        await self.conn.commit()

    async def top_authors_by_ups(self, days: int = 60, limit: int = 10) -> list[tuple[str, int, int]]:
        days_clause = f"-{int(days)} days"
        sql = (
            "SELECT author_handle, "
            "  SUM(CASE WHEN label='up' THEN 1 ELSE 0 END) AS ups, "
            "  SUM(CASE WHEN label='down' THEN 1 ELSE 0 END) AS downs "
            "FROM feedback WHERE created_at >= datetime('now', ?) "
            "GROUP BY author_handle HAVING ups > 0 "
            "ORDER BY ups DESC, downs ASC LIMIT ?"
        )
        async with self.conn.execute(sql, (days_clause, int(limit))) as cur:
            rows = await cur.fetchall()
        return [(r["author_handle"], int(r["ups"] or 0), int(r["downs"] or 0)) for r in rows]

    async def bottom_authors_by_downs(
        self, days: int = 60, limit: int = 10
    ) -> list[tuple[str, int, int]]:
        days_clause = f"-{int(days)} days"
        sql = (
            "SELECT author_handle, "
            "  SUM(CASE WHEN label='up' THEN 1 ELSE 0 END) AS ups, "
            "  SUM(CASE WHEN label='down' THEN 1 ELSE 0 END) AS downs "
            "FROM feedback WHERE created_at >= datetime('now', ?) "
            "GROUP BY author_handle HAVING downs > 0 "
            "ORDER BY downs DESC, ups ASC LIMIT ?"
        )
        async with self.conn.execute(sql, (days_clause, int(limit))) as cur:
            rows = await cur.fetchall()
        return [(r["author_handle"], int(r["ups"] or 0), int(r["downs"] or 0)) for r in rows]
