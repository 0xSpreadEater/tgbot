"""4-hour cron scheduler.

Single cron job, anchored to UTC midnight, fires at 00/04/08/12/16/20.
Always runs the FULL pipeline (topics + emerging + patterns). Manual
trigger paths (/run and /scan) bypass this entirely.
"""
from __future__ import annotations

import logging
import traceback
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bebop_bot import pipeline as pipeline_mod

log = logging.getLogger(__name__)

CRON_HOURS = "0,4,8,12,16,20"


async def run_full_cycle(
    db: Any,
    x: Any,
    claude: Any,
    bot: Any,
    chat_id: int,
) -> None:
    """Thin wrapper used by the scheduler ONLY.

    Runs the full pipeline (same shape as /run, but cron-triggered),
    respects /pause via an early return, advances since_id, never sets
    manual_scan, never skips topics. Wraps everything in try/except so
    a single broken cycle doesn't take down the scheduler.
    """
    try:
        paused = (await db.get_setting("paused", "0")) == "1"
        if paused:
            log.info("scheduled_cycle_skipped_paused")
            return
        log.info("scheduled_cycle_start")
        await pipeline_mod.run_roundup(
            db=db,
            x=x,
            claude=claude,
            bot=bot,
            chat_id=chat_id,
            advance_since_id=True,
            force=False,
            manual_scan=False,
            skip_topics=False,
        )
        await db.set_setting("last_run_at", datetime.now(UTC).isoformat())
        log.info("scheduled_cycle_done")
    except Exception as e:  # noqa: BLE001
        log.error(
            "scheduled_cycle_failed",
            extra={"error": str(e), "trace": traceback.format_exc()},
        )
        try:
            ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            await bot.send_message(
                chat_id=chat_id,
                text=f"Scheduled cycle failed at {ts} - check logs",
            )
        except Exception:  # noqa: BLE001
            log.exception("scheduled_cycle_failure_notify_failed")


def start_scheduler(
    db: Any,
    x: Any,
    claude: Any,
    bot: Any,
    chat_id: int,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    trigger = CronTrigger(hour=CRON_HOURS, minute=0, second=0, timezone="UTC")
    scheduler.add_job(
        run_full_cycle,
        trigger=trigger,
        kwargs={
            "db": db, "x": x, "claude": claude, "bot": bot, "chat_id": chat_id,
        },
        id="full_cycle",
        name="full_cycle_every_4h_utc",
        misfire_grace_time=60 * 30,
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )
    scheduler.start()
    log.info("scheduler_started", extra={"cron": f"hour={CRON_HOURS} minute=0 tz=UTC"})
    return scheduler


def next_run_at(scheduler: AsyncIOScheduler | None) -> datetime | None:
    if scheduler is None:
        return None
    try:
        job = scheduler.get_job("full_cycle")
    except Exception:  # noqa: BLE001
        return None
    if job is None:
        return None
    return job.next_run_time
