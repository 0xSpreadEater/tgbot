import os

import pytest

from bebop_bot.db import (
    apply_phase4_migration,
    apply_schema,
    connect,
    count_rows,
    get_setting,
    init_db,
    set_setting,
)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


async def test_schema_creates_all_tables(db_path):
    conn = await connect(db_path)
    try:
        await apply_schema(conn)
        await apply_phase4_migration(conn)
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) as cur:
            tables = [r["name"] for r in await cur.fetchall()]
    finally:
        await conn.close()
    expected = {
        "topics", "allowlist", "feedback", "author_scores", "suggestion_blocks",
        "pending_feedback", "token_mentions", "token_metadata",
        "sector_dictionary", "venue_dictionary", "entity_mentions",
        "dictionary_feedback", "airdrop_opportunities", "settings",
        "mechanism_dictionary", "entity_cooccurrences",
        "convergence_signals", "convergence_events",
        "viral_seed_examples", "venue_suggestion_state", "viral_handles",
    }
    assert expected.issubset(set(tables))


async def test_phase4_migration_is_idempotent(db_path):
    conn = await connect(db_path)
    try:
        await apply_schema(conn)
        await apply_phase4_migration(conn)
        await apply_phase4_migration(conn)
        async with conn.execute(
            "SELECT COUNT(*) AS n FROM settings WHERE key = 'convergence_signal_threshold'"
        ) as cur:
            row = await cur.fetchone()
        assert row["n"] == 1
    finally:
        await conn.close()


async def test_seed_mechanisms_and_viral_examples(db_path):
    conn = await init_db(db_path)
    try:
        assert await count_rows(conn, "mechanism_dictionary") >= 45
        assert await count_rows(conn, "viral_seed_examples") == 11
        assert await count_rows(conn, "viral_handles") >= 10
    finally:
        await conn.close()


async def test_init_db_seeds_and_is_idempotent(db_path):
    conn = await init_db(db_path)
    try:
        assert await count_rows(conn, "topics") == 8
        assert await count_rows(conn, "allowlist") == 14
        assert await count_rows(conn, "sector_dictionary") >= 30
        assert await count_rows(conn, "venue_dictionary") >= 20
        assert await get_setting(conn, "paused") == "0"
        assert await get_setting(conn, "threshold") == "2"
    finally:
        await conn.close()

    conn2 = await init_db(db_path)
    try:
        assert await count_rows(conn2, "topics") == 8
        assert await count_rows(conn2, "allowlist") == 14
    finally:
        await conn2.close()


async def test_settings_get_and_set(db_path):
    conn = await init_db(db_path)
    try:
        assert await get_setting(conn, "threshold") == "2"
        await set_setting(conn, "threshold", "4")
        assert await get_setting(conn, "threshold") == "4"
        assert await get_setting(conn, "missing_key", "fallback") == "fallback"
    finally:
        await conn.close()
    assert os.path.exists(db_path)
