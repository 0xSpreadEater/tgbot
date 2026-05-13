import logging
import traceback
from datetime import UTC, datetime
from typing import Any

from bebop_bot import digest
from bebop_bot import emerging as emerging_mod
from bebop_bot import patterns as patterns_mod
from bebop_bot.filter import filter_topic
from bebop_bot.models import ScoredTweet

log = logging.getLogger(__name__)


async def run_roundup(
    db: Any,
    x: Any,
    claude: Any,
    bot: Any,
    chat_id: int,
    advance_since_id: bool = True,
    force: bool = False,
    manual_scan: bool = False,
    skip_topics: bool = False,
) -> dict[str, tuple[str, list[ScoredTweet]]]:
    results: dict[str, tuple[str, list[ScoredTweet]]] = {}
    try:
        if not force and await db.is_paused():
            log.info("roundup_skipped_paused")
            return {}

        allowlist = await db.get_allowlist()
        threshold_raw = await db.get_setting("threshold", "2")
        try:
            threshold = float(threshold_raw)
        except (TypeError, ValueError):
            threshold = 2.0
        taste_rubric = await db.get_setting("taste_rubric", "") or ""
        ups = await db.get_recent_feedback("up", 15)
        downs = await db.get_recent_feedback("down", 15)

        topics = [] if skip_topics else await db.get_topics()

        log.info(
            "roundup_start",
            extra={
                "topics": len(topics),
                "allowlist": len(allowlist),
                "threshold": threshold,
                "advance_since_id": advance_since_id,
                "scan_mode": "emerging_only" if skip_topics else "full",
            },
        )

        for topic in topics:
            try:
                raw = await x.search_recent(
                    topic.query,
                    since_id=topic.last_seen_id,
                    max_results=100,
                )
            except Exception as e:  # noqa: BLE001
                log.exception(
                    "topic_fetch_error",
                    extra={"topic": topic.name, "error": str(e)},
                )
                continue

            if advance_since_id and raw:
                new_max = max(t.id for t in raw)
                await db.update_topic_since_id(topic.name, new_max)

            scored = await filter_topic(
                db,
                topic,
                raw,
                allowlist,
                threshold,
                taste_rubric,
                ups,
                downs,
                claude,
            )
            log.info(
                "topic_processed",
                extra={
                    "topic": topic.name,
                    "raw": len(raw),
                    "after_filter": len(scored),
                },
            )
            if not scored:
                continue

            summary = await claude.summarize_topic(
                topic.name, [st.tweet for st in scored]
            )
            results[topic.name] = (summary, scored)

        emerging_result: dict | None = None
        try:
            emerging_result = await emerging_mod.run_emerging(
                db=db, x=x, claude=claude, bot=bot, chat_id=chat_id,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("emerging_run_failed", extra={"error": str(e)})

        await digest.send_digest(
            bot, chat_id, results, db=db, manual_scan=manual_scan,
            emerging=emerging_result,
        )
        # Pattern of the week: only on full cycles (not /scan). Silent
        # when there's no clear weekly winner.
        if not manual_scan:
            try:
                await patterns_mod.maybe_send_pattern_of_the_week(
                    bot=bot, chat_id=chat_id, db=db,
                )
            except Exception:  # noqa: BLE001
                log.exception("pattern_of_week_pipeline_failed")
        await db.set_setting("last_run_at", datetime.now(UTC).isoformat())
        log.info(
            "roundup_done",
            extra={"topics_with_results": len(results)},
        )
        return results
    except Exception as e:  # noqa: BLE001
        log.error(
            "roundup_failed",
            extra={"error": str(e), "trace": traceback.format_exc()},
        )
        try:
            await bot.send_message(chat_id=chat_id, text="Roundup failed - check logs")
        except Exception:  # noqa: BLE001
            log.exception("roundup_failure_notify_failed")
        return results
