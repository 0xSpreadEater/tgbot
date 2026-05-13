"""Tests for /run after rate-limit removal + cron reschedule."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from apscheduler.triggers.cron import CronTrigger

from bebop_bot import handlers
from bebop_bot.db import Db, init_db


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


def _make_update():
    message = MagicMock()
    message.reply_text = AsyncMock()
    update = MagicMock()
    update.effective_message = message
    update.effective_user = SimpleNamespace(id=1)
    update.effective_chat = SimpleNamespace(id=1)
    return update


def _make_context(db, scheduler, telegram_user_id=1, raise_on_reschedule=False):
    if scheduler is None:
        scheduler_obj = None
    elif raise_on_reschedule:
        scheduler_obj = MagicMock()
        scheduler_obj.reschedule_job = MagicMock(
            side_effect=RuntimeError("boom")
        )
    else:
        scheduler_obj = scheduler

    application = MagicMock()
    application.bot_data = {
        "db_wrapper": db,
        "x_client": MagicMock(),
        "claude_client": MagicMock(),
        "settings": SimpleNamespace(telegram_user_id=telegram_user_id),
    }
    if scheduler_obj is not None:
        application.bot_data["scheduler"] = scheduler_obj
    context = MagicMock()
    context.application = application
    context.bot = MagicMock()
    return context, scheduler_obj


async def _run_cmd_run(monkeypatch, db_path, scheduler, raise_on_reschedule=False):
    conn = await init_db(db_path)
    db = Db(conn)

    pipeline_call = AsyncMock(return_value={})
    monkeypatch.setattr(handlers.pipelinem, "run_roundup", pipeline_call)

    update = _make_update()
    context, scheduler_obj = _make_context(
        db, scheduler, raise_on_reschedule=raise_on_reschedule
    )
    # cmd_run is wrapped by @restricted; call the unwrapped function so we
    # don't have to plumb auth in tests.
    inner = handlers.cmd_run.__wrapped__
    await inner(update, context)
    return conn, db, pipeline_call, scheduler_obj


async def test_run_does_not_check_last_manual_run_at(monkeypatch, db_path):
    """A recent 'last_manual_run_at' stamp must not block /run."""
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        # Stamp very recent — old gate would have blocked.
        await db.set_setting(
            "last_manual_run_at",
            (datetime.now(UTC) - timedelta(seconds=5)).isoformat(),
        )
    finally:
        await conn.close()

    scheduler = MagicMock()
    scheduler.reschedule_job = MagicMock()
    conn, db, pipeline_call, scheduler_obj = await _run_cmd_run(
        monkeypatch, db_path, scheduler
    )
    try:
        # The pipeline ran — no early return for rate limit.
        assert pipeline_call.await_count == 1
    finally:
        await conn.close()


async def test_run_reschedules_with_shifted_hours(monkeypatch, db_path):
    """reschedule_job must receive a CronTrigger with shifted hour pattern."""
    fixed_now = datetime(2026, 5, 13, 14, 23, 0, tzinfo=UTC)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    monkeypatch.setattr(handlers, "datetime", _FixedDatetime)

    scheduler = MagicMock()
    scheduler.reschedule_job = MagicMock()

    conn, db, pipeline_call, scheduler_obj = await _run_cmd_run(
        monkeypatch, db_path, scheduler
    )
    try:
        assert scheduler_obj.reschedule_job.call_count == 1
        kwargs = scheduler_obj.reschedule_job.call_args.kwargs
        assert kwargs["job_id"] == "full_cycle"
        trigger = kwargs["trigger"]
        assert isinstance(trigger, CronTrigger)
        # /run at 14:23 UTC → next 18:23, then every 4h
        # Expected hour anchor list: 18,22,2,6,10,14
        fields = {f.name: str(f) for f in trigger.fields}
        assert fields["hour"] == "18,22,2,6,10,14"
        assert fields["minute"] == "23"
        # Pipeline still ran.
        assert pipeline_call.await_count == 1
        # last_manual_run_at stamped at end of handler.
        stamp = await db.get_setting("last_manual_run_at")
        assert stamp == fixed_now.isoformat()
    finally:
        await conn.close()


async def test_run_handles_reschedule_exception(monkeypatch, db_path):
    """If reschedule_job raises, the roundup still runs."""
    scheduler = MagicMock()  # ignored; replaced by raising mock

    conn, db, pipeline_call, scheduler_obj = await _run_cmd_run(
        monkeypatch, db_path, scheduler, raise_on_reschedule=True
    )
    try:
        assert scheduler_obj.reschedule_job.call_count == 1
        # Roundup still ran despite the reschedule failure.
        assert pipeline_call.await_count == 1
        # And the stamp still got written.
        stamp = await db.get_setting("last_manual_run_at")
        assert stamp is not None
    finally:
        await conn.close()
