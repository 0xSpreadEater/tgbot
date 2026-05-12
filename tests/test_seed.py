import pytest

from bebop_bot.db import apply_phase4_migration, apply_schema, connect, count_rows
from bebop_bot.seed import (
    DEFAULT_SETTINGS,
    SEED_ALLOWLIST,
    SEED_MECHANISMS,
    SEED_SECTORS,
    SEED_TOPICS,
    SEED_VENUES,
    SEED_VIRAL_EXAMPLES,
    SEED_VIRAL_HANDLES,
    seed_all,
)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


def test_seed_topics_unique_names():
    names = [n for n, _ in SEED_TOPICS]
    assert len(names) == len(set(names))


def test_seed_allowlist_lowercase_no_at():
    for h in SEED_ALLOWLIST:
        assert h == h.lower()
        assert not h.startswith("@")


def test_seed_sectors_and_venues_nonempty():
    assert len(SEED_SECTORS) > 0
    assert len(SEED_VENUES) > 0


def test_default_settings_keys_present():
    keys = {k for k, _ in DEFAULT_SETTINGS}
    for required in (
        "paused", "threshold", "emerging_entity_threshold",
        "chain_evm_enabled", "chain_solana_enabled",
        "evm_sweep_query", "solana_sweep_query",
    ):
        assert required in keys


async def test_seed_all_inserts_and_is_idempotent(db_path):
    conn = await connect(db_path)
    try:
        await apply_schema(conn)
        await apply_phase4_migration(conn)
        first = await seed_all(conn)
        assert first["topics"] == len(SEED_TOPICS)
        assert first["allowlist"] == len(SEED_ALLOWLIST)
        assert first["sectors"] == len(SEED_SECTORS)
        assert first["venues"] == len(SEED_VENUES)
        assert first["mechanisms"] == len(SEED_MECHANISMS)
        assert first["viral_examples"] == len(SEED_VIRAL_EXAMPLES)
        assert first["viral_handles"] == len(set(h.lower() for h in SEED_VIRAL_HANDLES))

        second = await seed_all(conn)
        for v in second.values():
            assert v == 0

        assert await count_rows(conn, "topics") == len(SEED_TOPICS)
        assert await count_rows(conn, "allowlist") == len(SEED_ALLOWLIST)
        assert await count_rows(conn, "sector_dictionary") == len(SEED_SECTORS)
        assert await count_rows(conn, "venue_dictionary") == len(SEED_VENUES)
        assert await count_rows(conn, "mechanism_dictionary") == len(SEED_MECHANISMS)
        assert await count_rows(conn, "viral_seed_examples") == len(SEED_VIRAL_EXAMPLES)
    finally:
        await conn.close()
