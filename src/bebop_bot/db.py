import logging
import os
from collections.abc import Iterable

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
