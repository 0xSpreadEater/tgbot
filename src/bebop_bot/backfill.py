"""Historical backfill for emerging detection.

On a fresh install the four-track emerging signals (tokens, sectors,
venues, mechanisms) and the co-occurrence graph have no baseline. A
single 7-day backfill walks the X recent-search window in 4h chunks
and writes per-cycle aggregates so the very next /run produces
meaningful momentum / coherence scores.

Backfill intentionally skips convergence detection — convergence is a
real-time signal and replaying it across ~42 historical cycles would
generate notification noise without value.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from bebop_bot import cooccurrence, extractors
from bebop_bot.auth import restricted

log = logging.getLogger(__name__)

# X API Basic / pay-as-you-go tier only supports ~7 days of recent search.
BACKFILL_DAYS = 7


def now_utc() -> datetime:
    return datetime.now(UTC)


async def author_weight(
    db: Any,
    handle: str | None,
    allowlist: set[str],
    author_created_at: datetime | None,
) -> float:
    """Backfill-time author weight, mirroring cooccurrence._author_weight.

    Drops weight slightly for very young accounts (<30 days) which are
    over-represented in older windows because their early tweets are now
    captured alongside their peak activity.
    """
    h = (handle or "").lower()
    base = 1.5 if h in allowlist else 1.0
    if author_created_at is not None:
        try:
            age_days = (now_utc() - author_created_at).days
            if age_days < 30:
                base *= 0.8
        except (TypeError, ValueError):
            pass
    _ = db  # reserved for future per-handle reputation lookups
    return base


@restricted
async def cmd_backfill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = list(context.args or [])
    force = "--force" in args
    days: int | None = None
    if "--days" in args:
        idx = args.index("--days")
        if idx + 1 < len(args):
            try:
                days = int(args[idx + 1])
            except ValueError:
                await update.message.reply_text("--days requires an integer.")
                return
        else:
            await update.message.reply_text("--days requires an integer.")
            return

    db = context.application.bot_data.get("db_wrapper")
    x = context.application.bot_data.get("x_client")
    claude = context.application.bot_data.get("claude_client")
    if db is None or x is None:
        await update.message.reply_text(
            "Backfill needs the DB and X client wired up. Set X_BEARER_TOKEN."
        )
        return

    if days is None:
        raw = await db.get_setting("backfill_days", str(BACKFILL_DAYS))
        try:
            days = int(raw or str(BACKFILL_DAYS))
        except ValueError:
            days = BACKFILL_DAYS
    if days < 1 or days > 7:
        await update.message.reply_text(
            "Backfill window must be 1-7 days. The X API Basic / "
            "pay-as-you-go tier only supports ~7 days of recent search; "
            "7 is the default."
        )
        return

    last_run = await db.get_setting("backfilled_at")
    if last_run and not force:
        try:
            last_dt = datetime.fromisoformat(last_run)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=UTC)
            if (now_utc() - last_dt).days < 30:
                await update.message.reply_text(
                    f"Backfill last ran {last_run}. Use --force to override."
                )
                return
        except ValueError:
            pass

    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        await update.message.reply_text("No chat context; cannot run backfill.")
        return

    await update.message.reply_text(
        f"Starting backfill ({days} days). Expect "
        f"{int(2 + days * 0.3)}-{int(2 + days * 0.5)} minutes. "
        f"Will DM progress."
    )
    asyncio.create_task(
        run_backfill(db, x, claude, context.bot, chat_id=chat_id, days=days)
    )


async def run_backfill(
    db: Any,
    x: Any,
    claude: Any,
    bot: Any,
    chat_id: int,
    days: int,
) -> None:
    start = now_utc()
    total_tweets = 0
    pause_count = 0
    max_tweets = 4500
    max_pauses = 3
    log.info(
        "backfill_started",
        extra={
            "backfill_days": days,
            "max_tweets_cap": max_tweets,
            "start_iso": start.isoformat(),
        },
    )
    await bot.send_message(
        chat_id, f"Backfill: sweeping X for past {days} days..."
    )

    cycles_to_simulate: list[tuple[datetime, datetime]] = []
    for hours_ago in range(0, days * 24, 4):
        cycle_end = start - timedelta(hours=hours_ago)
        cycle_start = cycle_end - timedelta(hours=4)
        cycles_to_simulate.append((cycle_start, cycle_end))
    cycles_to_simulate.reverse()

    try:
        for idx, (cycle_start, cycle_end) in enumerate(cycles_to_simulate):
            if total_tweets >= max_tweets:
                await bot.send_message(
                    chat_id,
                    f"Backfill: hit {max_tweets}-tweet cap; stopping early.",
                )
                log.info(
                    "backfill_capped",
                    extra={
                        "max_tweets": max_tweets,
                        "tweets_total": total_tweets,
                        "cycles_done": idx,
                    },
                )
                break

            cycle_ts = cycle_end
            sweep_pool: list[Any] = []
            per_chain: dict[str, list[Any]] = {"evm": [], "solana": []}
            seen_ids: set[str] = set()

            for chain in ("evm", "solana"):
                if not await db.get_setting_bool(f"chain_{chain}_enabled"):
                    continue
                sweep_query = await db.get_setting(f"{chain}_sweep_query")
                if not sweep_query:
                    continue
                start_iso = cycle_start.isoformat().replace("+00:00", "") + "Z"
                end_iso = cycle_end.isoformat().replace("+00:00", "") + "Z"
                try:
                    raw = await x.search_recent(
                        sweep_query,
                        max_results=100,
                        start_time=start_iso,
                        end_time=end_iso,
                    )
                except Exception as e:  # noqa: BLE001
                    if "429" in str(e) and pause_count < max_pauses:
                        pause_count += 1
                        await bot.send_message(
                            chat_id,
                            f"Rate limited, pausing 60s "
                            f"({pause_count}/{max_pauses})",
                        )
                        log.warning(
                            "backfill_rate_limited",
                            extra={
                                "pause_index": pause_count,
                                "max_pauses": max_pauses,
                                "cycle_iso": cycle_ts.isoformat(),
                            },
                        )
                        await asyncio.sleep(60)
                        raw = await x.search_recent(
                            sweep_query,
                            max_results=100,
                            start_time=start_iso,
                            end_time=end_iso,
                        )
                    else:
                        raise

                total_tweets += len(raw)
                per_chain[chain] = raw
                for t in raw:
                    if t.id not in seen_ids:
                        sweep_pool.append(t)
                        seen_ids.add(t.id)

            allowlist = set(await db.get_allowlist())

            # ---- Tokens (per chain) ----------------------------------
            for chain in ("evm", "solana"):
                tweets = per_chain[chain]
                if not tweets:
                    continue
                token_obs: dict[tuple[str, str], list[tuple[str, float]]] = {}
                for t in tweets:
                    cashtags = extractors.extract_cashtags(t.text)
                    if chain == "evm":
                        addrs = extractors.extract_evm_addresses(t.text)
                    else:
                        addrs = extractors.extract_solana_addresses(t.text)
                    this_tweet: set[tuple[str, str]] = set()
                    for sym in cashtags:
                        ch = extractors.classify_chain_for_cashtag(t.text, sym)
                        if ch == "unknown":
                            ch = chain
                        if ch != chain:
                            continue
                        this_tweet.add((sym, ch))
                    for addr in addrs:
                        this_tweet.add((addr, chain))
                    w = await author_weight(
                        db, t.author_handle, allowlist, t.author_created_at,
                    )
                    for tok, ch in this_tweet:
                        token_obs.setdefault((tok, ch), []).append(
                            (t.author_handle, w)
                        )
                for (tok, ch), obs in token_obs.items():
                    unique = len({o[0] for o in obs})
                    weighted = sum(o[1] for o in obs)
                    await db.upsert_token_mention(
                        token=tok, chain=ch, cycle_ts=cycle_ts,
                        weighted_count=weighted, raw_count=len(obs),
                        unique_authors_count=unique,
                    )

            # ---- Sectors, venues, mechanisms -------------------------
            sector_dict = await db.get_dictionary("sector")
            venue_dict = await db.get_dictionary("venue")
            mechanism_dict = (
                await db.get_dictionary("mechanism")
                if await db.get_setting_bool("mechanism_track_enabled")
                else []
            )

            dictionary_loops = (
                ("sector", sector_dict),
                ("venue", venue_dict),
                ("mechanism", mechanism_dict),
            )
            for kind, dictionary in dictionary_loops:
                if not dictionary:
                    continue
                entity_obs: dict[str, list[tuple[str, float]]] = {}
                for t in sweep_pool:
                    matches = extractors.extract_dictionary_phrases(
                        t.text, dictionary,
                    )
                    if not matches:
                        continue
                    w = await author_weight(
                        db, t.author_handle, allowlist, t.author_created_at,
                    )
                    seen: set[str] = set()
                    for term, term_w in matches:
                        if term in seen:
                            continue
                        seen.add(term)
                        entity_obs.setdefault(term, []).append(
                            (t.author_handle, w * term_w)
                        )
                for term, obs in entity_obs.items():
                    unique = len({o[0] for o in obs})
                    weighted = sum(o[1] for o in obs)
                    await db.upsert_entity_mention(
                        entity_type=kind, entity_term=term, cycle_ts=cycle_ts,
                        weighted_count=weighted, raw_count=len(obs),
                        unique_authors=unique,
                    )

            # ---- Co-occurrence graph ---------------------------------
            # Same builder as live cycle so coherence scoring has a
            # full-window history (not just the last 24h).
            if sweep_pool:
                await cooccurrence.build_cooccurrence_graph(
                    db, sweep_pool, allowlist,
                    sector_dict, venue_dict, mechanism_dict, cycle_ts,
                )

            # Intentionally skip convergence detection during backfill.

            if idx % 10 == 0:
                await bot.send_message(
                    chat_id,
                    f"Backfill progress: {idx}/{len(cycles_to_simulate)} "
                    f"cycles, {total_tweets} tweets so far",
                )
                log.info(
                    "backfill_progress",
                    extra={
                        "cycles_done": idx,
                        "cycles_total": len(cycles_to_simulate),
                        "tweets_total": total_tweets,
                        "cycle_iso": cycle_ts.isoformat(),
                    },
                )

        await db.set_setting("backfilled_at", now_utc().isoformat())
        await db.set_setting("backfill_days_last", str(days))
        duration = (now_utc() - start).total_seconds() / 60
        await bot.send_message(
            chat_id,
            f"Backfill complete in {duration:.1f}min. "
            f"Fetched {total_tweets} tweets over {days} days. "
            f"Next /run uses full baselines.",
        )
        log.info(
            "backfill_done",
            extra={
                "backfill_days": days,
                "tweets_total": total_tweets,
                "duration_min": round(duration, 2),
                "cycles_total": len(cycles_to_simulate),
            },
        )
        _ = claude  # claude not used during backfill; reserved for future
    except Exception as e:  # noqa: BLE001
        log.exception(
            "backfill_failed",
            extra={
                "backfill_days": days,
                "tweets_total": total_tweets,
                "cycles_done": locals().get("idx", 0),
            },
        )
        await bot.send_message(
            chat_id,
            f"Backfill failed: {str(e)[:200]}. Partial state written; "
            f"re-run with --force after fixing.",
        )
