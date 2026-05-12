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


PHASE4_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS mechanism_dictionary(
        term TEXT PRIMARY KEY,
        display_name TEXT,
        weight REAL NOT NULL DEFAULT 1.0,
        source TEXT NOT NULL,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        promoted_at TIMESTAMP,
        is_novelty_marker BOOLEAN NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_mech_dict_weight ON mechanism_dictionary(weight DESC)",
    """
    CREATE TABLE IF NOT EXISTS entity_cooccurrences(
        cycle_ts TIMESTAMP NOT NULL,
        entity_a_type TEXT NOT NULL,
        entity_a_term TEXT NOT NULL,
        entity_b_type TEXT NOT NULL,
        entity_b_term TEXT NOT NULL,
        raw_count INTEGER NOT NULL,
        weighted_count REAL NOT NULL,
        unique_authors INTEGER NOT NULL,
        PRIMARY KEY (cycle_ts, entity_a_type, entity_a_term,
                     entity_b_type, entity_b_term)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cooccur_entity_a ON entity_cooccurrences(entity_a_type, entity_a_term, cycle_ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_cooccur_entity_b ON entity_cooccurrences(entity_b_type, entity_b_term, cycle_ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS convergence_signals(
        cycle_ts TIMESTAMP NOT NULL,
        entity_type TEXT NOT NULL,
        entity_term TEXT NOT NULL,
        signal_name TEXT NOT NULL,
        evidence_json TEXT,
        PRIMARY KEY (cycle_ts, entity_type, entity_term, signal_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS convergence_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_ts TIMESTAMP NOT NULL,
        entity_type TEXT NOT NULL,
        entity_term TEXT NOT NULL,
        tier TEXT NOT NULL,
        signal_count INTEGER NOT NULL,
        claude_confidence INTEGER,
        claude_rationale TEXT,
        summary TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_conv_events_entity ON convergence_events(entity_type, entity_term, cycle_ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS viral_seed_examples(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        chain TEXT NOT NULL,
        window_start TIMESTAMP,
        window_end TIMESTAMP,
        signals_json TEXT NOT NULL,
        rationale TEXT NOT NULL,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS venue_suggestion_state(
        venue_term TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        last_suggested_at TIMESTAMP,
        decision_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS viral_handles(
        handle TEXT PRIMARY KEY,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        source TEXT NOT NULL DEFAULT 'seed'
    )
    """,
)


PHASE4_NEW_SETTINGS: tuple[tuple[str, str], ...] = (
    ("convergence_signal_threshold", "3"),
    ("strong_convergence_claude_threshold", "4"),
    ("strong_convergence_enabled", "1"),
    ("venue_suggest_min_cycles", "3"),
    ("venue_suggest_min_unique_authors", "5"),
    ("mechanism_track_enabled", "1"),
    ("backfill_days", "14"),
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


async def apply_phase4_migration(conn: aiosqlite.Connection) -> None:
    """Idempotent Phase 4 schema additions. Safe to re-run."""
    for stmt in PHASE4_MIGRATION_STATEMENTS:
        await conn.execute(stmt)
    for key, value in PHASE4_NEW_SETTINGS:
        await conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
            (key, value),
        )
    await conn.commit()
    log.info(
        "phase4_migration_applied",
        extra={"statements": len(PHASE4_MIGRATION_STATEMENTS)},
    )


async def init_db(db_path: str) -> aiosqlite.Connection:
    from bebop_bot.seed import seed_all

    log.info("db_init_start", extra={"db_path": db_path})
    conn = await connect(db_path)
    await apply_schema(conn)
    await apply_phase4_migration(conn)
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

    async def get_setting_bool(self, key: str, default: bool = False) -> bool:
        v = await self.get_setting(key, "1" if default else "0")
        return v == "1"

    async def get_dictionary(self, entity_type: str) -> list[dict]:
        table = {
            "sector": "sector_dictionary",
            "venue": "venue_dictionary",
            "mechanism": "mechanism_dictionary",
        }.get(entity_type)
        if table is None:
            return []
        if entity_type == "mechanism":
            sql = (
                f"SELECT term, display_name, weight, source, added_at, "
                f"promoted_at, is_novelty_marker "
                f"FROM {table} ORDER BY weight DESC, term"
            )
        else:
            sql = (
                f"SELECT term, display_name, weight, source, added_at, promoted_at "
                f"FROM {table} ORDER BY weight DESC, term"
            )
        rows = await fetch_all(self.conn, sql)
        out = []
        for r in rows:
            d = {
                "term": r["term"],
                "display_name": r["display_name"],
                "weight": float(r["weight"]),
                "source": r["source"],
                "added_at": r["added_at"],
                "promoted_at": r["promoted_at"],
            }
            if entity_type == "mechanism":
                d["is_novelty_marker"] = bool(r["is_novelty_marker"])
            out.append(d)
        return out

    async def add_dictionary_term(
        self,
        entity_type: str,
        term: str,
        display_name: str | None = None,
        weight: float = 1.0,
        source: str = "user_added",
        is_novelty_marker: bool = False,
    ) -> bool:
        if entity_type == "sector":
            cur = await self.conn.execute(
                "INSERT OR IGNORE INTO sector_dictionary(term, display_name, weight, source) "
                "VALUES(?, ?, ?, ?)",
                (term, display_name or term, weight, source),
            )
        elif entity_type == "venue":
            cur = await self.conn.execute(
                "INSERT OR IGNORE INTO venue_dictionary(term, display_name, weight, source) "
                "VALUES(?, ?, ?, ?)",
                (term, display_name or term, weight, source),
            )
        elif entity_type == "mechanism":
            cur = await self.conn.execute(
                "INSERT OR IGNORE INTO mechanism_dictionary("
                "term, display_name, weight, source, is_novelty_marker"
                ") VALUES(?, ?, ?, ?, ?)",
                (term, display_name, weight, source, 1 if is_novelty_marker else 0),
            )
        else:
            return False
        await self.conn.commit()
        return cur.rowcount > 0

    async def remove_dictionary_term(self, entity_type: str, term: str) -> bool:
        table = {
            "sector": "sector_dictionary",
            "venue": "venue_dictionary",
            "mechanism": "mechanism_dictionary",
        }.get(entity_type)
        if table is None:
            return False
        cur = await self.conn.execute(
            f"DELETE FROM {table} WHERE term = ?", (term,)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def insert_cooccurrence(
        self,
        cycle_ts: datetime,
        entity_a_type: str,
        entity_a_term: str,
        entity_b_type: str,
        entity_b_term: str,
        raw_count: int,
        weighted_count: float,
        unique_authors: int,
    ) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO entity_cooccurrences("
            "cycle_ts, entity_a_type, entity_a_term, entity_b_type, entity_b_term, "
            "raw_count, weighted_count, unique_authors) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cycle_ts.isoformat() if isinstance(cycle_ts, datetime) else cycle_ts,
                entity_a_type, entity_a_term, entity_b_type, entity_b_term,
                raw_count, weighted_count, unique_authors,
            ),
        )

    async def insert_convergence_signal(
        self,
        cycle_ts: datetime,
        entity_type: str,
        entity_term: str,
        signal_name: str,
        evidence_json: str,
    ) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO convergence_signals("
            "cycle_ts, entity_type, entity_term, signal_name, evidence_json) "
            "VALUES(?, ?, ?, ?, ?)",
            (
                cycle_ts.isoformat() if isinstance(cycle_ts, datetime) else cycle_ts,
                entity_type, entity_term, signal_name, evidence_json,
            ),
        )
        await self.conn.commit()

    async def insert_convergence_event(
        self,
        cycle_ts: datetime,
        entity_type: str,
        entity_term: str,
        tier: str,
        signal_count: int,
        claude_confidence: int | None,
        claude_rationale: str | None,
        summary: str,
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO convergence_events("
            "cycle_ts, entity_type, entity_term, tier, signal_count, "
            "claude_confidence, claude_rationale, summary) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cycle_ts.isoformat() if isinstance(cycle_ts, datetime) else cycle_ts,
                entity_type, entity_term, tier, signal_count,
                claude_confidence, claude_rationale, summary,
            ),
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    async def get_recent_convergence_events(self, limit: int = 50) -> list[dict]:
        rows = await fetch_all(
            self.conn,
            "SELECT cycle_ts, entity_type, entity_term, tier, signal_count, "
            "claude_confidence, claude_rationale, summary FROM convergence_events "
            "ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )
        return [dict(r) for r in rows]

    async def get_last_cycle_convergence_events(self) -> list[dict]:
        async with self.conn.execute(
            "SELECT MAX(cycle_ts) AS m FROM convergence_events"
        ) as cur:
            row = await cur.fetchone()
        if not row or not row["m"]:
            return []
        last_ts = row["m"]
        rows = await fetch_all(
            self.conn,
            "SELECT cycle_ts, entity_type, entity_term, tier, signal_count, "
            "claude_confidence, claude_rationale, summary FROM convergence_events "
            "WHERE cycle_ts = ? ORDER BY tier DESC, signal_count DESC",
            (last_ts,),
        )
        return [dict(r) for r in rows]

    async def get_viral_seed_examples(self) -> list[dict]:
        rows = await fetch_all(
            self.conn,
            "SELECT name, chain, window_start, window_end, signals_json, "
            "rationale, added_at FROM viral_seed_examples ORDER BY id",
        )
        out = []
        for r in rows:
            try:
                sigs = json.loads(r["signals_json"])
            except (json.JSONDecodeError, TypeError):
                sigs = {}
            out.append({
                "name": r["name"],
                "chain": r["chain"],
                "window_start": r["window_start"],
                "window_end": r["window_end"],
                "signals": sigs.get("signals", []),
                "phrases": sigs.get("phrases", []),
                "handles": sigs.get("handles", []),
                "rationale": r["rationale"],
                "added_at": r["added_at"],
            })
        return out

    async def add_viral_seed_example(
        self,
        name: str,
        chain: str,
        window_start: str | None,
        window_end: str | None,
        signals: list[str],
        phrases: list[str],
        handles: list[str],
        rationale: str,
    ) -> bool:
        signals_json = json.dumps({
            "signals": signals, "phrases": phrases, "handles": handles,
        })
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO viral_seed_examples("
            "name, chain, window_start, window_end, signals_json, rationale) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (name, chain, window_start, window_end, signals_json, rationale),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_viral_handles(self) -> set[str]:
        rows = await fetch_all(self.conn, "SELECT handle FROM viral_handles")
        return {r["handle"].lower() for r in rows}

    async def add_viral_handle(self, handle: str, source: str = "user_added") -> bool:
        h = handle.lstrip("@").lower().strip()
        if not h:
            return False
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO viral_handles(handle, source) VALUES(?, ?)",
            (h, source),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def remove_viral_handle(self, handle: str) -> bool:
        h = handle.lstrip("@").lower().strip()
        cur = await self.conn.execute(
            "DELETE FROM viral_handles WHERE handle = ?", (h,),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_venue_suggestion_state(self, venue_term: str) -> dict | None:
        async with self.conn.execute(
            "SELECT venue_term, status, last_suggested_at, decision_at "
            "FROM venue_suggestion_state WHERE venue_term = ?",
            (venue_term,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return dict(row)

    async def set_venue_suggestion(
        self, venue_term: str, status: str, last_suggested_at: str | None = None,
    ) -> None:
        now_iso = datetime.utcnow().isoformat()
        decision_at = now_iso if status in ("accepted", "blocked") else None
        await self.conn.execute(
            "INSERT INTO venue_suggestion_state("
            "venue_term, status, last_suggested_at, decision_at) "
            "VALUES(?, ?, ?, ?) "
            "ON CONFLICT(venue_term) DO UPDATE SET "
            "status = excluded.status, "
            "last_suggested_at = COALESCE(excluded.last_suggested_at, "
            "venue_suggestion_state.last_suggested_at), "
            "decision_at = COALESCE(excluded.decision_at, "
            "venue_suggestion_state.decision_at)",
            (venue_term, status, last_suggested_at or now_iso, decision_at),
        )
        await self.conn.commit()

    async def count_venue_recent_cycles(
        self, venue_term: str, min_unique_authors: int,
    ) -> int:
        """Number of distinct cycle_ts in entity_mentions where this venue
        showed up with at least min_unique_authors unique authors."""
        sql = (
            "SELECT COUNT(DISTINCT cycle_ts) AS n FROM entity_mentions "
            "WHERE entity_type = 'venue' AND entity_term = ? "
            "AND unique_authors >= ?"
        )
        async with self.conn.execute(sql, (venue_term, int(min_unique_authors))) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def insert_entity_mention(
        self,
        entity_type: str,
        entity_term: str,
        cycle_ts: datetime,
        weighted_count: float,
        raw_count: int,
        unique_authors: int,
    ) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO entity_mentions("
            "entity_type, entity_term, cycle_ts, weighted_count, raw_count, unique_authors) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (
                entity_type, entity_term,
                cycle_ts.isoformat() if isinstance(cycle_ts, datetime) else cycle_ts,
                weighted_count, raw_count, unique_authors,
            ),
        )
        await self.conn.commit()

    async def get_entity_first_seen(self, entity_type: str, entity_term: str) -> str | None:
        async with self.conn.execute(
            "SELECT MIN(cycle_ts) AS m FROM entity_mentions "
            "WHERE entity_type = ? AND entity_term = ?",
            (entity_type, entity_term),
        ) as cur:
            row = await cur.fetchone()
        return row["m"] if row and row["m"] else None

    async def commit(self) -> None:
        await self.conn.commit()

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
