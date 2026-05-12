"""Tests for the /run and /preview rate-limit gating."""
from datetime import UTC, datetime, timedelta

import pytest

from bebop_bot.db import Db, init_db
from bebop_bot.handlers import (
    RATE_LIMIT_SECONDS,
    _check_and_set_rate_limit,
    _format_rate_limit_remaining,
)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


async def test_first_call_passes_and_stamps(db_path):
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        assert await db.get_setting("last_manual_run_at") is None
        remaining = await _check_and_set_rate_limit(db)
        assert remaining is None
        stamp = await db.get_setting("last_manual_run_at")
        assert stamp is not None
    finally:
        await conn.close()


async def test_second_call_within_window_blocks(db_path):
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        first = await _check_and_set_rate_limit(db)
        assert first is None
        second = await _check_and_set_rate_limit(db)
        assert second is not None
        assert 0 < second <= RATE_LIMIT_SECONDS
    finally:
        await conn.close()


async def test_old_stamp_allows_run(db_path):
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        old = (datetime.now(UTC) - timedelta(seconds=RATE_LIMIT_SECONDS + 100)).isoformat()
        await db.set_setting("last_manual_run_at", old)
        remaining = await _check_and_set_rate_limit(db)
        assert remaining is None
    finally:
        await conn.close()


def test_format_rate_limit_remaining():
    out = _format_rate_limit_remaining(125)
    assert out == "Next /run available in 2m 5s."
