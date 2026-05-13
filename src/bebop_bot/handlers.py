import hashlib
import html
import json
import logging
import os
import re
from datetime import UTC, date, datetime, timedelta

import aiosqlite
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from bebop_bot import db as dbm
from bebop_bot import dictionary as dictm
from bebop_bot import pipeline as pipelinem
from bebop_bot.auth import restricted
from bebop_bot.backfill import cmd_backfill
from bebop_bot.digest import relative_time
from bebop_bot.feedback import (
    on_feedback_callback,
    on_suggestion_callback,
    on_venue_suggestion_callback,
)
from bebop_bot.query_parser import normalize

RATE_LIMIT_SECONDS = 60 * 60

log = logging.getLogger(__name__)

HELP_TEXT = (
    "<b>bebop-bot commands</b>\n"
    "\n"
    "<b>Topics</b>\n"
    "/add &lt;name&gt; | &lt;query&gt; — add a topic\n"
    "/remove &lt;name&gt; — remove a topic\n"
    "/list — list topics\n"
    "/show &lt;name&gt; — full query for a topic\n"
    "/edit &lt;name&gt; | &lt;query&gt; — replace a topic's query\n"
    "/test &lt;query&gt; — fetch 5 most recent (no scoring, no save)\n"
    "\n"
    "<b>Roundup</b>\n"
    "/scan — manual scan of emerging coins and trends ONLY (no topic "
    "roundup, no rate limit, ignores /pause)\n"
    "/run — full manual scan including topics (60-min rate limit, "
    "respects /pause)\n"
    "/pause — stop scheduled cycles (Phase 5)\n"
    "/resume — resume scheduled cycles\n"
    "/status — bot health\n"
    "\n"
    "<b>Filtering</b>\n"
    "/threshold &lt;n&gt; — set roundup threshold (1..5)\n"
    "/rubric — show, /rubric set &lt;text&gt;, /rubric clear\n"
    "/learnings — summarize patterns from recent feedback\n"
    "/calibrate &lt;score&gt; | &lt;tweet text&gt; — label a manual example\n"
    "/calibration — list calibration examples\n"
    "/calibration show &lt;name&gt; — full rationale for one\n"
    "/calibration add NAME | CHAIN | DATE | SIGNALS | RATIONALE\n"
    "/calibration remove &lt;name&gt; — delete (asks for confirmation)\n"
    "\n"
    "<b>Authors</b>\n"
    "/allow &lt;handle&gt; — add to allowlist\n"
    "/disallow &lt;handle&gt; — remove from allowlist\n"
    "/allowlist — show allowlist\n"
    "/authors — top/bottom authors (60d)\n"
    "/mute &lt;handle&gt; [days] — mute author (default 30)\n"
    "/unmute &lt;handle&gt; — clear muted_until\n"
    "\n"
    "<b>Emerging</b>\n"
    "/emerging — show last cycle's emerging entities\n"
    "/convergence — show last cycle's convergence events\n"
    "/convergence_threshold [weak|medium &lt;n&gt;] — set tier floors\n"
    "/strong_convergence on|off — toggle Claude tier\n"
    "/strong_threshold &lt;n&gt; — set Claude confidence floor\n"
    "/patterns — list / curate Claude-proposed patterns\n"
    "/dismiss — (coming soon)\n"
    "/chains — (coming soon)\n"
    "/sol_config — (coming soon)\n"
    "/evm_config — (coming soon)\n"
    "\n"
    "<b>Dictionary</b>\n"
    "/sectors — list sector dictionary\n"
    "/sector add &lt;term&gt; — add sector term\n"
    "/sector remove &lt;term&gt; — remove sector term\n"
    "/venues — list venue dictionary\n"
    "/venue add|remove &lt;term&gt;\n"
    "/mechanisms — list mechanism dictionary\n"
    "/mechanism add|remove &lt;term&gt;\n"
    "\n"
    "<b>Viral seeds</b>\n"
    "/viral_handles — list known builder handles\n"
    "/viral_handle add|remove &lt;handle&gt;\n"
    "/seed_viral_example — show last 3 viral seed examples\n"
    "/seed_viral_example add &lt;json&gt; — append a viral seed\n"
    "\n"
    "<b>Backfill</b>\n"
    "/backfill — historical sweep to seed baselines (default 14d)\n"
    "/backfill --force — re-run within the 30-day cooldown\n"
    "/backfill --days N — override the window (1..30)\n"
    "\n"
    "<b>Meta</b>\n"
    "/help — this message\n"
    "/start — same as /help\n"
)

COMING_SOON = "Coming in a later phase."


def _conn(context: ContextTypes.DEFAULT_TYPE) -> aiosqlite.Connection:
    return context.application.bot_data["db"]


def _split_name_query(raw: str) -> tuple[str, str]:
    if "|" not in raw:
        raise ValueError("missing '|' separator. Usage: /add <name> | <query>")
    name, query = raw.split("|", 1)
    name = name.strip()
    query = query.strip()
    if not name:
        raise ValueError("topic name is empty")
    if not query:
        raise ValueError("query is empty")
    return name, query


def _humanize_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GB"


async def _reply(update: Update, text: str, parse_mode: str | None = None) -> None:
    if update.effective_message is not None:
        await update.effective_message.reply_text(text, parse_mode=parse_mode)


@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, HELP_TEXT, parse_mode=ParseMode.HTML)


@restricted
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, HELP_TEXT, parse_mode=ParseMode.HTML)


@restricted
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = " ".join(context.args or [])
    if not raw.strip():
        await _reply(update, "Usage: /add <name> | <query>")
        return
    try:
        name, query = _split_name_query(raw)
        normalized, warnings = normalize(query)
    except ValueError as e:
        await _reply(update, f"Rejected: {e}")
        return
    conn = _conn(context)
    try:
        await conn.execute("INSERT INTO topics(name, query) VALUES(?, ?)", (name, normalized))
        await conn.commit()
    except aiosqlite.IntegrityError:
        await _reply(update, f"Topic '{name}' already exists. Use /edit to update.")
        return
    msg = f"Added topic '{name}':\n{normalized}"
    if warnings:
        msg += "\n\nWarnings:\n" + "\n".join(f"- {w}" for w in warnings)
    await _reply(update, msg)


@restricted
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await _reply(update, "Usage: /remove <name>")
        return
    name = args[0].strip()
    conn = _conn(context)
    cur = await conn.execute("DELETE FROM topics WHERE name = ?", (name,))
    await conn.commit()
    if cur.rowcount:
        await _reply(update, f"Removed topic '{name}'.")
    else:
        await _reply(update, f"No topic named '{name}'.")


@restricted
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = _conn(context)
    rows = await dbm.fetch_all(conn, "SELECT name, query FROM topics ORDER BY name")
    if not rows:
        await _reply(update, "No topics. Use /add to create one.")
        return
    lines = ["Topics:"]
    for i, row in enumerate(rows, start=1):
        q = row["query"]
        trimmed = q if len(q) <= 80 else q[:77] + "..."
        lines.append(f"{i}. {row['name']} — {trimmed}")
    await _reply(update, "\n".join(lines))


@restricted
async def cmd_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await _reply(update, "Usage: /show <name>")
        return
    name = args[0].strip()
    conn = _conn(context)
    rows = await dbm.fetch_all(conn, "SELECT query FROM topics WHERE name = ?", (name,))
    if not rows:
        await _reply(update, f"No topic named '{name}'.")
        return
    await _reply(update, f"{name}:\n{rows[0]['query']}")


@restricted
async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = " ".join(context.args or [])
    if not raw.strip():
        await _reply(update, "Usage: /edit <name> | <query>")
        return
    try:
        name, query = _split_name_query(raw)
        normalized, warnings = normalize(query)
    except ValueError as e:
        await _reply(update, f"Rejected: {e}")
        return
    conn = _conn(context)
    cur = await conn.execute("UPDATE topics SET query = ? WHERE name = ?", (normalized, name))
    await conn.commit()
    if not cur.rowcount:
        await _reply(update, f"No topic named '{name}'. Use /add to create it.")
        return
    msg = f"Updated topic '{name}':\n{normalized}"
    if warnings:
        msg += "\n\nWarnings:\n" + "\n".join(f"- {w}" for w in warnings)
    await _reply(update, msg)


@restricted
async def cmd_allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await _reply(update, "Usage: /allow <handle>")
        return
    handle = args[0].lstrip("@").lower().strip()
    if not handle:
        await _reply(update, "Handle is empty.")
        return
    conn = _conn(context)
    cur = await conn.execute("INSERT OR IGNORE INTO allowlist(handle) VALUES(?)", (handle,))
    await conn.commit()
    if cur.rowcount:
        await _reply(update, f"Allowed @{handle}.")
    else:
        await _reply(update, f"@{handle} already on allowlist.")


@restricted
async def cmd_disallow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await _reply(update, "Usage: /disallow <handle>")
        return
    handle = args[0].lstrip("@").lower().strip()
    conn = _conn(context)
    cur = await conn.execute("DELETE FROM allowlist WHERE handle = ?", (handle,))
    await conn.commit()
    if cur.rowcount:
        await _reply(update, f"Removed @{handle} from allowlist.")
    else:
        await _reply(update, f"@{handle} was not on the allowlist.")


@restricted
async def cmd_allowlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = _conn(context)
    rows = await dbm.fetch_all(conn, "SELECT handle FROM allowlist ORDER BY handle")
    handles = [r["handle"] for r in rows]
    header = f"Allowlist ({len(handles)}):"
    body = "\n".join(f"- @{h}" for h in handles) if handles else "(empty)"
    await _reply(update, f"{header}\n{body}")


@restricted
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = _conn(context)
    await dbm.set_setting(conn, "paused", "1")
    await _reply(update, "Paused.")


@restricted
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = _conn(context)
    await dbm.set_setting(conn, "paused", "0")
    await _reply(update, "Resumed.")


@restricted
async def cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await _reply(update, "Usage: /threshold <n>  (1..5)")
        return
    try:
        n = float(args[0])
    except ValueError:
        await _reply(update, "Threshold must be a number 1..5.")
        return
    if not 1.0 <= n <= 5.0:
        await _reply(update, "Threshold must be between 1 and 5.")
        return
    conn = _conn(context)
    formatted = f"{n:g}"
    await dbm.set_setting(conn, "threshold", formatted)
    await _reply(update, f"Threshold set to {formatted}.")


@restricted
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = _conn(context)
    paused = await dbm.get_setting(conn, "paused", "0")
    threshold = await dbm.get_setting(conn, "threshold", "2")
    evm_on = await dbm.get_setting(conn, "chain_evm_enabled", "1")
    sol_on = await dbm.get_setting(conn, "chain_solana_enabled", "1")
    conv_weak = await dbm.get_setting(conn, "convergence_weak_threshold", "2")
    conv_medium = await dbm.get_setting(conn, "convergence_medium_threshold", "3")
    strong_on = await dbm.get_setting(conn, "strong_convergence_enabled", "1")
    strong_threshold = await dbm.get_setting(
        conn, "convergence_strong_claude_min", "4",
    )
    topics_count = await dbm.count_rows(conn, "topics")
    allow_count = await dbm.count_rows(conn, "allowlist")
    mech_total = await dbm.count_rows(conn, "mechanism_dictionary")
    async with conn.execute(
        "SELECT COUNT(*) AS n FROM mechanism_dictionary WHERE source = 'claude_proposed'"
    ) as cur:
        row = await cur.fetchone()
    mech_proposed = int(row["n"]) if row else 0
    viral_seeds_count = await dbm.count_rows(conn, "viral_seed_examples")
    settings = context.application.bot_data["settings"]
    db_path = settings.db_path
    try:
        size = os.path.getsize(db_path)
        size_str = _humanize_bytes(size)
    except OSError:
        size_str = "unknown"
    text = (
        "<b>Bebop bot status</b>\n"
        f"Paused: {'yes' if paused == '1' else 'no'}\n"
        f"Topics: {topics_count}\n"
        f"Allowlist: {allow_count}\n"
        f"Threshold: {threshold}\n"
        f"Chains: EVM={'on' if evm_on == '1' else 'off'}, "
        f"Solana={'on' if sol_on == '1' else 'off'}\n"
        f"Mechanisms tracked: {mech_total} ({mech_proposed} proposed)\n"
        f"Convergence thresholds: weak={conv_weak}/7, medium={conv_medium}/7\n"
        f"Strong convergence: {'on' if strong_on == '1' else 'off'} "
        f"(threshold {strong_threshold}/5)\n"
        f"Viral seed examples: {viral_seeds_count}\n"
        "Scheduler: not yet implemented\n"
        f"DB path: {html.escape(db_path)}, size: {size_str}"
    )
    await _reply(update, text, parse_mode=ParseMode.HTML)


def _make_coming_soon(name: str):
    @restricted
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _reply(update, COMING_SOON)
    handler.__name__ = f"cmd_{name}_coming_soon"
    return handler


COMING_SOON_COMMANDS: tuple[str, ...] = (
    "chains", "sol_config", "evm_config", "dismiss",
)


def _db_wrapper(context: ContextTypes.DEFAULT_TYPE) -> dbm.Db:
    db = context.application.bot_data.get("db_wrapper")
    if db is None:
        db = dbm.Db(_conn(context))
        context.application.bot_data["db_wrapper"] = db
    return db


async def _check_and_set_rate_limit(db: dbm.Db) -> int | None:
    """Return remaining seconds if rate-limited, otherwise None and stamp now."""
    last = await db.get_setting("last_manual_run_at")
    now = datetime.now(UTC)
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=UTC)
            elapsed = (now - last_dt).total_seconds()
            if elapsed < RATE_LIMIT_SECONDS:
                return int(RATE_LIMIT_SECONDS - elapsed)
        except ValueError:
            pass
    await db.set_setting("last_manual_run_at", now.isoformat())
    return None


def _format_rate_limit_remaining(seconds: int) -> str:
    m, s = divmod(max(0, seconds), 60)
    return f"Next /run available in {m}m {s}s."


async def _ensure_pipeline_clients(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> tuple[object, object, dbm.Db] | None:
    x_client = context.application.bot_data.get("x_client")
    claude_client = context.application.bot_data.get("claude_client")
    if x_client is None or claude_client is None:
        await _reply(update, "Roundup is not configured. Set X_BEARER_TOKEN and ANTHROPIC_API_KEY.")
        return None
    return x_client, claude_client, _db_wrapper(context)


@restricted
async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clients = await _ensure_pipeline_clients(update, context)
    if clients is None:
        return
    x_client, claude_client, db = clients
    remaining = await _check_and_set_rate_limit(db)
    if remaining is not None:
        await _reply(update, _format_rate_limit_remaining(remaining))
        return
    settings = context.application.bot_data["settings"]
    await _reply(update, "Running roundup…")
    await pipelinem.run_roundup(
        db=db,
        x=x_client,
        claude=claude_client,
        bot=context.bot,
        chat_id=settings.telegram_user_id,
        advance_since_id=True,
        force=False,
        manual_scan=False,
    )


@restricted
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clients = await _ensure_pipeline_clients(update, context)
    if clients is None:
        return
    x_client, claude_client, db = clients
    settings = context.application.bot_data["settings"]
    chat_id = update.effective_chat.id if update.effective_chat else settings.telegram_user_id
    await _reply(update, "Running manual emerging scan (no topic roundup)...")
    try:
        await pipelinem.run_roundup(
            db=db,
            x=x_client,
            claude=claude_client,
            bot=context.bot,
            chat_id=chat_id,
            advance_since_id=False,
            force=True,
            manual_scan=True,
            skip_topics=True,
        )
    except Exception:  # noqa: BLE001
        log.exception("scan_failed")
        await _reply(update, "Scan failed - check logs.")


@restricted
async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = " ".join(context.args or []).strip()
    if not raw:
        await _reply(update, "Usage: /test <query>")
        return
    try:
        normalized, _ = normalize(raw)
    except ValueError as e:
        await _reply(update, f"Rejected: {e}")
        return
    x_client = context.application.bot_data.get("x_client")
    if x_client is None:
        await _reply(update, "X client not configured. Set X_BEARER_TOKEN.")
        return
    try:
        tweets = await x_client.search_recent(normalized, max_results=5)
    except Exception as e:  # noqa: BLE001
        log.exception("test_fetch_error")
        await _reply(update, f"Fetch failed: {e}")
        return
    if not tweets:
        await _reply(update, "No results.")
        return
    lines = [f"<b>/test</b> ({len(tweets)} of 5)"]
    for t in tweets:
        url = html.escape(t.url, quote=True)
        handle = html.escape(t.author_handle)
        text = t.text if len(t.text) <= 280 else t.text[:279] + "…"
        body = html.escape(text)
        rel = relative_time(t.created_at)
        lines.append(
            f'• <a href="{url}">@{handle}</a> {rel}\n  {body}'
        )
    await _reply(update, "\n".join(lines), parse_mode=ParseMode.HTML)


@restricted
async def cmd_rubric(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db_wrapper(context)
    args = context.args or []
    if not args:
        current = await db.get_setting("taste_rubric", "") or ""
        if not current.strip():
            await _reply(update, "Taste rubric is empty. Use /rubric set <text>.")
        else:
            await _reply(update, f"Taste rubric:\n{current}")
        return
    sub = args[0].lower()
    if sub == "clear":
        await db.set_setting("taste_rubric", "")
        await _reply(update, "Taste rubric cleared.")
        return
    if sub == "set":
        text = " ".join(args[1:]).strip()
        if not text:
            await _reply(update, "Usage: /rubric set <text>")
            return
        await db.set_setting("taste_rubric", text)
        await _reply(update, "Taste rubric updated.")
        return
    await _reply(update, "Usage: /rubric | /rubric set <text> | /rubric clear")


@restricted
async def cmd_calibrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = " ".join(context.args or []).strip()
    if "|" not in raw:
        await _reply(update, "Usage: /calibrate <score> | <tweet text>")
        return
    score_str, text = raw.split("|", 1)
    score_str = score_str.strip()
    text = text.strip()
    if not text:
        await _reply(update, "Tweet text is empty.")
        return
    try:
        score = int(score_str)
    except ValueError:
        await _reply(update, "Score must be an integer 1..5.")
        return
    if score == 3:
        await _reply(update, "score 3 is ambiguous, use 1-2 or 4-5")
        return
    if score >= 4:
        label = "up"
    elif score <= 2:
        label = "down"
    else:
        await _reply(update, "Score must be 1..5.")
        return
    digest_hex = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    tweet_id = f"manual:{digest_hex}"
    db = _db_wrapper(context)
    await db.add_feedback(
        tweet_id=tweet_id,
        topic_name=None,
        author_handle="__manual__",
        label=label,
        tweet_text=text,
    )
    await _reply(update, f"Recorded as '{label}'.")


@restricted
async def cmd_authors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db_wrapper(context)
    top = await db.top_authors_by_ups(days=60, limit=10)
    bottom = await db.bottom_authors_by_downs(days=60, limit=10)
    allowlist = await db.get_allowlist()

    lines = ["<b>Top authors (60d)</b>"]
    if not top:
        lines.append("(no upvotes yet)")
    else:
        for handle, ups, downs in top:
            trusted = handle in allowlist or (ups >= 3 and downs == 0)
            marker = "  * (auto-include trusted)" if trusted else ""
            lines.append(f"@{handle}   {ups}/{downs}{marker}")

    lines.append("")
    lines.append("<b>Bottom authors (60d)</b>")
    if not bottom:
        lines.append("(no downvotes yet)")
    else:
        for handle, ups, downs in bottom:
            net = ups - downs
            marker = "  (auto-hidden)" if net <= -3 else ""
            lines.append(f"@{handle}   {ups}/{downs}{marker}")

    await _reply(update, "\n".join(lines), parse_mode=ParseMode.HTML)


@restricted
async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await _reply(update, "Usage: /mute <handle> [days]")
        return
    handle = args[0].lstrip("@").lower().strip()
    if not handle:
        await _reply(update, "Handle is empty.")
        return
    days = 30
    if len(args) >= 2:
        try:
            days = int(args[1])
        except ValueError:
            await _reply(update, "Days must be an integer.")
            return
        if days <= 0:
            await _reply(update, "Days must be positive.")
            return
    until = datetime.now(UTC) + timedelta(days=days)
    db = _db_wrapper(context)
    await db.set_author_muted(handle, until)
    await _reply(update, f"Muted @{handle} for {days} days.")


@restricted
async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await _reply(update, "Usage: /unmute <handle>")
        return
    handle = args[0].lstrip("@").lower().strip()
    if not handle:
        await _reply(update, "Handle is empty.")
        return
    db = _db_wrapper(context)
    changed = await db.clear_author_muted(handle)
    if changed:
        await _reply(update, f"Unmuted @{handle}.")
    else:
        await _reply(update, f"@{handle} was not muted.")


@restricted
async def cmd_learnings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    claude_client = context.application.bot_data.get("claude_client")
    if claude_client is None:
        await _reply(update, "Claude not configured. Set ANTHROPIC_API_KEY.")
        return
    db = _db_wrapper(context)
    ups = await db.get_recent_feedback("up", 50)
    downs = await db.get_recent_feedback("down", 50)
    rubric = await db.get_setting("taste_rubric", "") or ""
    if not ups and not downs:
        await _reply(update, "No feedback yet. Use the reaction buttons or /calibrate first.")
        return
    await _reply(update, "Summarizing learnings…")
    try:
        text = await claude_client.summarize_learnings(ups, downs, rubric)
    except Exception:  # noqa: BLE001
        log.exception("learnings_failed")
        await _reply(update, "Could not produce learnings - check logs.")
        return
    if not text.strip():
        await _reply(update, "Got an empty response from Claude.")
        return
    await _reply(update, text)


# ---------------------------------------------------------------------------
# Phase 4: dictionary commands
# ---------------------------------------------------------------------------


def _entity_type_for_singular(cmd: str) -> str | None:
    return {"sector": "sector", "venue": "venue", "mechanism": "mechanism"}.get(cmd)


def _entity_type_for_plural(cmd: str) -> str | None:
    return {
        "sectors": "sector",
        "venues": "venue",
        "mechanisms": "mechanism",
    }.get(cmd)


async def _cmd_list_dictionary(
    update: Update, context: ContextTypes.DEFAULT_TYPE, entity_type: str,
) -> None:
    db = _db_wrapper(context)
    rows = await db.get_dictionary(entity_type)
    if not rows:
        await _reply(update, f"No {entity_type} terms yet.")
        return
    lines = [f"<b>{entity_type.capitalize()} dictionary ({len(rows)})</b>"]
    for r in rows[:80]:
        weight = f"{r['weight']:.1f}"
        marker = ""
        if entity_type == "mechanism" and r.get("is_novelty_marker"):
            marker = " *novelty"
        lines.append(
            f"- {html.escape(r['term'])} (w={weight}, {r['source']}){marker}"
        )
    if len(rows) > 80:
        lines.append(f"... and {len(rows) - 80} more")
    await _reply(update, "\n".join(lines), parse_mode=ParseMode.HTML)


@restricted
async def cmd_sectors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_list_dictionary(update, context, "sector")


@restricted
async def cmd_venues(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_list_dictionary(update, context, "venue")


@restricted
async def cmd_mechanisms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_list_dictionary(update, context, "mechanism")


async def _cmd_modify_dictionary(
    update: Update, context: ContextTypes.DEFAULT_TYPE, entity_type: str,
) -> None:
    args = context.args or []
    if len(args) < 2 or args[0].lower() not in ("add", "remove"):
        await _reply(
            update,
            f"Usage: /{entity_type} add <term>  |  /{entity_type} remove <term>",
        )
        return
    action = args[0].lower()
    term = " ".join(args[1:]).strip()
    if not term:
        await _reply(update, "Term is empty.")
        return
    db = _db_wrapper(context)
    if action == "add":
        ok = await dictm.add_term(
            db, entity_type, term, weight=1.0, source="user_added",
        )
        await _reply(
            update,
            f"Added {entity_type} '{term}'." if ok else f"'{term}' already in dictionary.",
        )
    else:
        ok = await dictm.remove_term(db, entity_type, term)
        await _reply(
            update,
            f"Removed {entity_type} '{term}'." if ok else f"No {entity_type} named '{term}'.",
        )


@restricted
async def cmd_sector(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_modify_dictionary(update, context, "sector")


@restricted
async def cmd_venue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_modify_dictionary(update, context, "venue")


@restricted
async def cmd_mechanism(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_modify_dictionary(update, context, "mechanism")


# ---------------------------------------------------------------------------
# Phase 4: convergence commands
# ---------------------------------------------------------------------------


@restricted
async def cmd_convergence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db_wrapper(context)
    events = await db.get_last_cycle_convergence_events()
    if not events:
        await _reply(update, "No convergence events in the most recent cycle.")
        return
    ts = events[0]["cycle_ts"]
    strong = [e for e in events if e["tier"] in ("strong", "strong_convergence")]
    medium = [
        e for e in events
        if e["tier"] in ("medium", "convergence", "structural")
    ]
    weak = [e for e in events if e["tier"] == "weak"]
    lines = [f"<b>Convergence — cycle {html.escape(str(ts))}</b>"]
    lines.append(f"STRONG ({len(strong)}):")
    for e in strong:
        conf = e.get("claude_confidence")
        conf_str = f"Claude {conf}/5, " if conf is not None else ""
        lines.append(
            f"  - {html.escape(e['entity_type'])}: "
            f"{html.escape(e['entity_term'])} "
            f"({conf_str}{e['signal_count']}/7 sigs)"
        )
        if e.get("summary"):
            lines.append(f"    {html.escape(e['summary'])[:300]}")
    lines.append(f"MEDIUM ({len(medium)}):")
    for e in medium:
        lines.append(
            f"  - {html.escape(e['entity_type'])}: "
            f"{html.escape(e['entity_term'])} "
            f"({e['signal_count']}/7 sigs)"
        )
    lines.append(f"WEAK ({len(weak)}):")
    for e in weak:
        lines.append(
            f"  - {html.escape(e['entity_type'])}: "
            f"{html.escape(e['entity_term'])} "
            f"({e['signal_count']}/7 sigs)"
        )
    await _reply(update, "\n".join(lines), parse_mode=ParseMode.HTML)


@restricted
async def cmd_convergence_threshold(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Three-tier (Phase 4.7) convergence threshold control.

    Usage:
      /convergence_threshold                — show current weak+medium
      /convergence_threshold weak <n>       — set weak floor   (1..7)
      /convergence_threshold medium <n>     — set medium floor (1..7)
      /convergence_threshold <n>            — legacy: sets the medium floor

    Validation: medium must remain strictly greater than weak.
    """
    args = context.args or []
    db = _db_wrapper(context)
    if not args:
        weak = await db.get_setting("convergence_weak_threshold", "2")
        med = await db.get_setting("convergence_medium_threshold", "3")
        await _reply(
            update,
            f"Convergence thresholds: weak={weak}/7, medium={med}/7.",
        )
        return

    first = args[0].lower()
    # Legacy: a single integer maps to the medium threshold.
    if first.isdigit() or (first.startswith("-") and first[1:].isdigit()):
        try:
            n = int(first)
        except ValueError:
            await _reply(update, "Threshold must be an integer 1..7.")
            return
        if not 1 <= n <= 7:
            await _reply(update, "Threshold must be between 1 and 7.")
            return
        weak = int(await db.get_setting("convergence_weak_threshold", "2") or 2)
        if n <= weak:
            await _reply(
                update,
                f"Rejected: medium threshold must be greater than weak "
                f"(currently weak={weak}).",
            )
            return
        await db.set_setting("convergence_medium_threshold", str(n))
        # Phase 4 alias, kept up-to-date for backwards compat.
        await db.set_setting("convergence_signal_threshold", str(n))
        await _reply(update, f"Medium convergence threshold set to {n}/7.")
        return

    if first in ("weak", "medium") and len(args) >= 2:
        try:
            n = int(args[1])
        except ValueError:
            await _reply(update, "Threshold must be an integer 1..7.")
            return
        if not 1 <= n <= 7:
            await _reply(update, "Threshold must be between 1 and 7.")
            return
        if first == "weak":
            med = int(await db.get_setting("convergence_medium_threshold", "3") or 3)
            if n >= med:
                await _reply(
                    update,
                    f"Rejected: weak threshold must be less than medium "
                    f"(currently medium={med}).",
                )
                return
            await db.set_setting("convergence_weak_threshold", str(n))
            await _reply(update, f"Weak convergence threshold set to {n}/7.")
        else:
            weak = int(await db.get_setting("convergence_weak_threshold", "2") or 2)
            if n <= weak:
                await _reply(
                    update,
                    f"Rejected: medium threshold must be greater than weak "
                    f"(currently weak={weak}).",
                )
                return
            await db.set_setting("convergence_medium_threshold", str(n))
            await db.set_setting("convergence_signal_threshold", str(n))
            await _reply(update, f"Medium convergence threshold set to {n}/7.")
        return

    await _reply(
        update,
        "Usage: /convergence_threshold [weak|medium <n>] (1..7)",
    )


@restricted
async def cmd_strong_convergence(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    args = context.args or []
    if not args:
        await _reply(update, "Usage: /strong_convergence on|off")
        return
    val = args[0].lower()
    if val not in ("on", "off"):
        await _reply(update, "Usage: /strong_convergence on|off")
        return
    db = _db_wrapper(context)
    await db.set_setting("strong_convergence_enabled", "1" if val == "on" else "0")
    await _reply(update, f"Strong convergence: {val}.")


@restricted
async def cmd_strong_threshold(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Phase 4.7: thin alias over convergence_strong_claude_min.

    Updates both the Phase 4 key and the new Phase 4.7 key so older
    callers keep reading the right value.
    """
    args = context.args or []
    db = _db_wrapper(context)
    if not args:
        cur = await db.get_setting("convergence_strong_claude_min", "4")
        await _reply(update, f"Strong convergence Claude threshold: {cur}/5")
        return
    try:
        n = int(args[0])
    except ValueError:
        await _reply(update, "Threshold must be an integer 1..5.")
        return
    if not 1 <= n <= 5:
        await _reply(update, "Threshold must be between 1 and 5.")
        return
    await db.set_setting("convergence_strong_claude_min", str(n))
    await db.set_setting("strong_convergence_claude_threshold", str(n))
    await _reply(update, f"Strong convergence Claude threshold set to {n}/5.")


@restricted
async def cmd_emerging(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db_wrapper(context)
    # Show the most recent cycle's entities from entity_mentions
    conn = _conn(context)
    async with conn.execute(
        "SELECT MAX(cycle_ts) AS m FROM entity_mentions"
    ) as cur:
        row = await cur.fetchone()
    if not row or not row["m"]:
        await _reply(update, "No emerging entities recorded yet. Run /scan first.")
        return
    last_ts = row["m"]
    rows = await dbm.fetch_all(
        conn,
        "SELECT entity_type, entity_term, weighted_count, raw_count, unique_authors "
        "FROM entity_mentions WHERE cycle_ts = ? "
        "ORDER BY entity_type, weighted_count DESC",
        (last_ts,),
    )
    if not rows:
        await _reply(update, "No emerging entities in the most recent cycle.")
        return
    lines = [f"<b>Emerging — cycle {html.escape(str(last_ts))}</b>"]
    current_type = None
    for r in rows[:80]:
        et = r["entity_type"]
        if et != current_type:
            lines.append("")
            lines.append(f"<b>{et}s</b>")
            current_type = et
        lines.append(
            f"  - {html.escape(r['entity_term'])} "
            f"(w={r['weighted_count']:.1f}, raw={r['raw_count']}, "
            f"authors={r['unique_authors']})"
        )
    if len(rows) > 80:
        lines.append(f"... and {len(rows) - 80} more")
    _ = db  # keep wrapper warm
    await _reply(update, "\n".join(lines), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Phase 4: viral handle / viral seed example commands
# ---------------------------------------------------------------------------


@restricted
async def cmd_viral_handles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db_wrapper(context)
    handles = sorted(await db.get_viral_handles())
    if not handles:
        await _reply(update, "No viral handles yet.")
        return
    body = ", ".join(f"@{h}" for h in handles)
    await _reply(update, f"Known builder handles ({len(handles)}): {body}")


@restricted
async def cmd_viral_handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if len(args) < 2 or args[0].lower() not in ("add", "remove"):
        await _reply(update, "Usage: /viral_handle add|remove <handle>")
        return
    action = args[0].lower()
    handle = args[1].lstrip("@").lower().strip()
    if not handle:
        await _reply(update, "Handle is empty.")
        return
    db = _db_wrapper(context)
    if action == "add":
        ok = await db.add_viral_handle(handle, source="user_added")
        await _reply(
            update,
            f"Added @{handle}." if ok else f"@{handle} already on list.",
        )
    else:
        ok = await db.remove_viral_handle(handle)
        await _reply(
            update,
            f"Removed @{handle}." if ok else f"@{handle} was not on the list.",
        )


@restricted
async def cmd_seed_viral_example(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    args = context.args or []
    db = _db_wrapper(context)
    if not args or args[0].lower() != "add":
        examples = await db.get_viral_seed_examples()
        if not examples:
            await _reply(update, "No viral seed examples yet.")
            return
        recent = examples[-3:]
        lines = [f"<b>Viral seed examples (last {len(recent)} of {len(examples)})</b>"]
        for e in recent:
            lines.append("")
            lines.append(f"<b>{html.escape(e['name'])}</b> ({html.escape(e['chain'])})")
            sigs = ", ".join(e.get("signals", []))
            lines.append(f"signals: {html.escape(sigs)}")
            lines.append(f"<i>{html.escape(e.get('rationale','')[:300])}</i>")
        await _reply(update, "\n".join(lines), parse_mode=ParseMode.HTML)
        return
    payload = " ".join(args[1:]).strip()
    if not payload:
        await _reply(
            update,
            'Usage: /seed_viral_example add {"name": "...", "chain": "...", '
            '"signals": [...], "phrases": [...], "handles": [...], '
            '"rationale": "..."}',
        )
        return
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        await _reply(update, f"Invalid JSON: {e}")
        return
    name = data.get("name")
    chain = data.get("chain")
    if not name or not chain:
        await _reply(update, "Required fields: name, chain.")
        return
    added = await db.add_viral_seed_example(
        name=name, chain=chain,
        window_start=data.get("window_start"),
        window_end=data.get("window_end"),
        signals=data.get("signals", []),
        phrases=data.get("phrases", []),
        handles=data.get("handles", []),
        rationale=data.get("rationale", ""),
    )
    await _reply(
        update,
        f"Added viral seed example '{name}'." if added else f"'{name}' already exists.",
    )


# ---------------------------------------------------------------------------
# /calibration: manage viral_seed_examples from Telegram
# ---------------------------------------------------------------------------

_KNOWN_CHAINS: frozenset[str] = frozenset({
    "evm", "solana", "base", "abstract", "megaeth", "hyperliquid",
    "plasma", "monad", "sonic", "berachain", "tempo", "ethereum",
    "arbitrum", "optimism", "polygon", "bnb",
})

_VALID_SIGNALS: frozenset[str] = frozenset({
    "novel_mechanism", "new_venue_context", "known_builder",
    "recursive_lang", "backing_event", "builder_ape_overlap",
    "fair_launch_lang",
})

_CHAIN_RE = re.compile(r"^[a-z][a-z0-9-]{1,20}$")

_CALIBRATION_USAGE = (
    "<b>/calibration</b> — manage historical viral-token examples\n\n"
    "<b>Subcommands</b>\n"
    "/calibration list — list all examples\n"
    "/calibration show &lt;name&gt; — show one in full\n"
    "/calibration add NAME | CHAIN | YYYY-MM-DD | SIGNALS | RATIONALE\n"
    "/calibration remove &lt;name&gt; — delete one (asks for confirmation)\n\n"
    "<b>Valid signals</b>\n"
    f"{', '.join(sorted(_VALID_SIGNALS))}\n\n"
    "Date is the window center; bot derives a 14-day window ending on it."
)


def _calibration_add_example_line() -> str:
    return (
        "Example:\n"
        "<code>/calibration add Plasma launch | plasma | 2026-09-15 | "
        "new_venue_context, novel_mechanism, known_builder | "
        "Plasma launched as Bitcoin sidechain optimized for USDT transfers. "
        "Brand-new-chain + Tether backing + cross-chain bridging team.</code>"
    )


@restricted
async def cmd_calibration_root(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    args = context.args or []
    if not args:
        await _reply(update, _CALIBRATION_USAGE, parse_mode=ParseMode.HTML)
        return
    sub = args[0].lower()
    rest_args = args[1:]
    if sub == "list":
        await _cmd_calibration_list(update, context)
    elif sub == "add":
        await _cmd_calibration_add(update, context, rest_args)
    elif sub == "remove":
        await _cmd_calibration_remove(update, context, rest_args)
    elif sub == "show":
        await _cmd_calibration_show(update, context, rest_args)
    else:
        await _reply(update, _CALIBRATION_USAGE, parse_mode=ParseMode.HTML)


async def _cmd_calibration_list(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    db = _db_wrapper(context)
    examples = await db.get_viral_seed_examples()
    if not examples:
        await _reply(update, "No calibration examples yet. Use /calibration add to create one.")
        return
    examples_sorted = sorted(examples, key=lambda e: (e.get("name") or "").lower())
    lines = [f"<b>Calibration examples</b> ({len(examples_sorted)} total)", ""]
    for e in examples_sorted:
        name = html.escape(str(e.get("name") or ""))
        chain = html.escape(str(e.get("chain") or ""))
        ws = e.get("window_start") or ""
        we = e.get("window_end") or ""
        marker = " [user-added]" if e.get("source") == "user_added" else ""
        lines.append(f"• <b>{name}</b> — {chain}, {html.escape(ws)} to {html.escape(we)}{marker}")
    lines.append("")
    lines.append("<i>Use /calibration show NAME to see the full rationale.</i>")
    await _reply(update, "\n".join(lines), parse_mode=ParseMode.HTML)


async def _cmd_calibration_show(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    rest_args: list[str],
) -> None:
    if not rest_args:
        await _reply(update, "Usage: /calibration show <name>")
        return
    name = " ".join(rest_args).strip()
    db = _db_wrapper(context)
    example = await db.get_viral_seed_example_by_name(name)
    if not example:
        await _reply(
            update,
            f"No example named '{name}'. Use /calibration list to see what's there.",
        )
        return
    signals = ", ".join(example.get("signals") or [])
    rationale = example.get("rationale") or ""
    body = (
        f"<b>{html.escape(example['name'])}</b>\n"
        f"Source: {html.escape(example.get('source') or 'seed')}\n"
        f"Chain: {html.escape(example.get('chain') or '')}\n"
        f"Window: {html.escape(example.get('window_start') or '')} to "
        f"{html.escape(example.get('window_end') or '')}\n"
        f"Signals: {html.escape(signals)}\n\n"
        f"<b>Rationale</b>\n"
        f"<i>{html.escape(rationale)}</i>"
    )
    await _reply(update, body, parse_mode=ParseMode.HTML)


def _parse_calibration_add_payload(payload: str) -> tuple[str, str, str, list[str], str]:
    """Parse the pipe-separated add payload. Raises ValueError on failure."""
    parts = [p.strip() for p in payload.split("|")]
    if len(parts) != 5:
        raise ValueError(
            f"expected 5 pipe-separated fields, got {len(parts)}. "
            "Format: NAME | CHAIN | YYYY-MM-DD | SIGNALS | RATIONALE"
        )
    name, chain, date_str, signals_str, rationale = parts

    if not (3 <= len(name) <= 80):
        raise ValueError("NAME must be 3-80 characters.")

    chain_l = chain.lower()
    if not _CHAIN_RE.match(chain_l):
        raise ValueError(
            "CHAIN must be lowercase, 2-21 chars, match [a-z][a-z0-9-]+. "
            f"Got: '{chain}'"
        )

    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError as e:
        raise ValueError(f"DATE must be YYYY-MM-DD: {e}") from e
    today = date.today()
    if d > today:
        raise ValueError(f"DATE must be on or before today ({today.isoformat()}).")
    if d < date(2020, 1, 1):
        raise ValueError("DATE must be on or after 2020-01-01.")

    raw_signals = [s.strip() for s in signals_str.split(",") if s.strip()]
    if not raw_signals:
        raise ValueError(
            "SIGNALS required. Valid: " + ", ".join(sorted(_VALID_SIGNALS))
        )
    # Dedupe preserving order
    seen: set[str] = set()
    signals: list[str] = []
    for s in raw_signals:
        if s in seen:
            continue
        if s not in _VALID_SIGNALS:
            raise ValueError(
                f"Invalid signal '{s}'. Valid: " + ", ".join(sorted(_VALID_SIGNALS))
            )
        seen.add(s)
        signals.append(s)

    rationale = rationale.strip()
    if not (20 <= len(rationale) <= 2000):
        raise ValueError("RATIONALE must be 20-2000 characters.")

    return name, chain_l, d.isoformat(), signals, rationale


async def _cmd_calibration_add(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    rest_args: list[str],
) -> None:
    payload = " ".join(rest_args).strip()
    if not payload:
        await _reply(
            update,
            "Usage: /calibration add NAME | CHAIN | YYYY-MM-DD | "
            "SIGNALS | RATIONALE\n\n" + _calibration_add_example_line(),
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        name, chain, date_iso, signals, rationale = _parse_calibration_add_payload(payload)
    except ValueError as e:
        await _reply(
            update,
            f"Rejected: {e}\n\n{_calibration_add_example_line()}",
            parse_mode=ParseMode.HTML,
        )
        return

    db = _db_wrapper(context)
    existing = await db.get_viral_seed_example_by_name(name)
    if existing:
        await _reply(
            update,
            f"An example named '{html.escape(name)}' already exists. "
            f"Use /calibration show {html.escape(name)} to see it, or "
            f"/calibration remove {html.escape(name)} first.",
            parse_mode=ParseMode.HTML,
        )
        return

    d = datetime.strptime(date_iso, "%Y-%m-%d").date()
    window_start = (d - timedelta(days=14)).isoformat()
    window_end = d.isoformat()

    if chain not in _KNOWN_CHAINS:
        log.warning(
            "calibration_unknown_chain",
            extra={"chain_name": chain, "example_name": name},
        )

    ok = await db.add_viral_seed_example(
        name=name,
        chain=chain,
        window_start=window_start,
        window_end=window_end,
        signals=signals,
        phrases=[],
        handles=[],
        rationale=rationale,
        source="user_added",
    )
    if not ok:
        await _reply(
            update,
            f"Could not add '{html.escape(name)}' (name conflict).",
            parse_mode=ParseMode.HTML,
        )
        return
    total = await db.count_viral_seed_examples()
    await _reply(
        update,
        f"Added calibration example: {html.escape(name)} "
        f"({html.escape(chain)}, {window_start} to {window_end}). "
        "It'll appear in the next convergence Claude judge call's "
        f"few-shot pool. {total} examples total.",
        parse_mode=ParseMode.HTML,
    )


async def _cmd_calibration_remove(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    rest_args: list[str],
) -> None:
    if not rest_args:
        await _reply(update, "Usage: /calibration remove <name> [confirm]")
        return

    confirm = False
    name_parts = list(rest_args)
    if name_parts and name_parts[-1].lower() == "confirm":
        confirm = True
        name_parts = name_parts[:-1]
    name = " ".join(name_parts).strip()
    if not name:
        await _reply(update, "Usage: /calibration remove <name> [confirm]")
        return

    db = _db_wrapper(context)
    example = await db.get_viral_seed_example_by_name(name)
    if not example:
        await _reply(
            update,
            f"No example named '{html.escape(name)}'.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not confirm:
        body = (
            "<b>Remove calibration example?</b>\n"
            f"Name: {html.escape(example['name'])}\n"
            f"Source: {html.escape(example.get('source') or 'seed')}\n"
            f"Chain: {html.escape(example.get('chain') or '')}\n\n"
            f"Reply with <code>/calibration remove {html.escape(example['name'])} "
            "confirm</code> to delete."
        )
        await _reply(update, body, parse_mode=ParseMode.HTML)
        return

    deleted = await db.delete_viral_seed_example(name)
    if deleted:
        await _reply(
            update,
            f"Removed '{html.escape(example['name'])}'.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await _reply(
            update,
            f"No example named '{html.escape(name)}'.",
            parse_mode=ParseMode.HTML,
        )


# ---------------------------------------------------------------------------
# Phase 4.7: Claude-proposed pattern commands & callbacks
# ---------------------------------------------------------------------------


_PATTERNS_USAGE = (
    "<b>/patterns</b> — manage Claude-proposed patterns\n\n"
    "/patterns — list active patterns (excludes hidden)\n"
    "/patterns down — list hidden patterns\n"
    "/patterns show &lt;name&gt; — full details + supporting tweets\n"
    "/patterns up &lt;name&gt; — up-vote (weight 2.0)\n"
    "/patterns down &lt;name&gt; — hide from future prompts\n"
    "/patterns unhide &lt;name&gt; — restore a hidden pattern"
)


def _format_pattern_summary_line(p: dict) -> str:
    name = html.escape(str(p.get("name") or ""))
    desc = html.escape(str(p.get("description") or ""))
    weight = float(p.get("weight", 1.0) or 1.0)
    pc = int(p.get("propose_count", 0) or 0)
    label = p.get("user_label")
    tag = ""
    if label == "up":
        tag = " [up]"
    elif label == "down":
        tag = " [hidden]"
    elif weight >= 1.5 and not label:
        tag = " [auto-bumped]"
    return (
        f"<b>{name}</b> (weight {weight:.1f}, proposed {pc}x){tag}\n"
        f"<i>{desc}</i>"
    )


@restricted
async def cmd_patterns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    db = _db_wrapper(context)
    if not args:
        rows = await db.get_patterns_active(exclude_down=True, limit=200)
        if not rows:
            await _reply(
                update,
                "No Claude-proposed patterns yet. They show up after a cycle "
                "with /scan or /run.",
            )
            return
        n_total = len(rows)
        n_up = sum(1 for r in rows if r.get("user_label") == "up")
        n_bumped = sum(
            1 for r in rows
            if not r.get("user_label") and float(r.get("weight", 1.0) or 1.0) >= 1.5
        )
        lines = [
            f"<b>Active patterns</b> ({n_total} total, {n_up} up-voted, "
            f"{n_bumped} naturally-bumped)",
            "",
        ]
        for r in rows[:50]:
            lines.append(_format_pattern_summary_line(r))
            lines.append("")
        lines.append("<i>Use /patterns show NAME for full details.</i>")
        await _reply(update, "\n".join(lines), parse_mode=ParseMode.HTML)
        return

    sub = args[0].lower()
    rest = " ".join(args[1:]).strip()
    if sub == "down" and not rest:
        rows = await db.get_patterns_hidden(limit=200)
        if not rows:
            await _reply(update, "No hidden patterns.")
            return
        lines = [f"<b>Hidden patterns</b> ({len(rows)})", ""]
        for r in rows:
            lines.append(_format_pattern_summary_line(r))
            lines.append("")
        await _reply(update, "\n".join(lines), parse_mode=ParseMode.HTML)
        return

    if not rest:
        await _reply(update, _PATTERNS_USAGE, parse_mode=ParseMode.HTML)
        return

    name = rest
    pattern = await db.get_pattern_by_name(name)
    if not pattern:
        await _reply(
            update,
            f"No pattern named '{html.escape(name)}'. Use /patterns to list "
            "active ones.",
            parse_mode=ParseMode.HTML,
        )
        return

    if sub == "show":
        observations = await db.get_pattern_observations(int(pattern["id"]))
        from bebop_bot.patterns import format_pattern_detail
        body = format_pattern_detail(pattern, observations)
        await _reply(update, body, parse_mode=ParseMode.HTML)
        return
    if sub == "up":
        await db.update_pattern_label(int(pattern["id"]), "up")
        await db.set_pattern_weight(int(pattern["id"]), 2.0)
        await _reply(
            update,
            f"Up-voted '{html.escape(pattern['name'])}'. Weight 2.0.",
            parse_mode=ParseMode.HTML,
        )
        return
    if sub == "down":
        await db.update_pattern_label(int(pattern["id"]), "down")
        await _reply(
            update,
            f"Hidden '{html.escape(pattern['name'])}'. Claude won't see "
            "it in future few-shot context.",
            parse_mode=ParseMode.HTML,
        )
        return
    if sub == "unhide":
        await db.update_pattern_label(int(pattern["id"]), None)
        await db.set_pattern_weight(int(pattern["id"]), 1.0)
        await _reply(
            update,
            f"Unhid '{html.escape(pattern['name'])}'. Weight reset to 1.0.",
            parse_mode=ParseMode.HTML,
        )
        return

    await _reply(update, _PATTERNS_USAGE, parse_mode=ParseMode.HTML)


async def on_pattern_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    data = query.data
    if not data.startswith("pat:"):
        return
    parts = data.split(":", 2)
    if len(parts) < 3:
        await query.answer("Malformed pattern action.")
        return
    action, name = parts[1], parts[2]
    db = _db_wrapper(context)
    pattern = await db.get_pattern_by_name(name)
    if pattern is None:
        await query.answer("Pattern not found (may have aged out).")
        return

    if action == "up":
        await db.update_pattern_label(int(pattern["id"]), "up")
        await db.set_pattern_weight(int(pattern["id"]), 2.0)
        await query.answer(
            "Kept. This pattern will rank higher in future Claude prompts.",
        )
        try:
            if query.message is not None:
                await query.message.reply_text(
                    f"👍 keeping pattern '{pattern['name']}' (weight 2.0)",
                )
        except Exception:  # noqa: BLE001
            log.exception(
                "pattern_callback_followup_failed",
                extra={"pattern_name": pattern["name"], "action_name": action},
            )
        return

    if action == "down":
        await db.update_pattern_label(int(pattern["id"]), "down")
        await query.answer(
            "Hidden. Claude won't see this in future prompts.",
        )
        try:
            if query.message is not None:
                await query.message.reply_text(
                    f"👎 hidden pattern '{pattern['name']}'",
                )
        except Exception:  # noqa: BLE001
            log.exception(
                "pattern_callback_followup_failed",
                extra={"pattern_name": pattern["name"], "action_name": action},
            )
        return

    if action == "show":
        observations = await db.get_pattern_observations(int(pattern["id"]))
        from bebop_bot.patterns import format_pattern_detail
        body = format_pattern_detail(pattern, observations)
        await query.answer()
        try:
            if query.message is not None:
                await query.message.reply_text(
                    body, parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
        except Exception:  # noqa: BLE001
            log.exception(
                "pattern_show_followup_failed",
                extra={"pattern_name": pattern["name"]},
            )
        return

    await query.answer("Unknown pattern action.")


async def on_convergence_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Convergence-event keyboard callback (cv:<tier>:<etype>:<eterm>:<action>).

    Legacy 'cv:structural:*' is treated as 'cv:medium:*' so older Telegram
    history keeps working. Convergence callbacks currently just acknowledge
    the user's vote — they don't yet write into a separate feedback table.
    """
    query = update.callback_query
    if query is None or not query.data:
        return
    data = query.data
    if not data.startswith("cv:"):
        return
    parts = data.split(":", 4)
    if len(parts) < 2:
        await query.answer("Malformed convergence action.")
        return
    tier = parts[1]
    if tier == "structural":
        tier = "medium"
    if tier not in ("weak", "medium", "strong"):
        await query.answer("Unknown convergence tier.")
        return
    action = parts[4] if len(parts) >= 5 else ""
    if action == "up":
        await query.answer("Marked as insight.")
    elif action == "down":
        await query.answer("Marked as noise.")
    else:
        await query.answer("Recorded.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("handler_error", exc_info=context.error)


def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("show", cmd_show))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("allow", cmd_allow))
    app.add_handler(CommandHandler("disallow", cmd_disallow))
    app.add_handler(CommandHandler("allowlist", cmd_allowlist))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("threshold", cmd_threshold))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("rubric", cmd_rubric))
    app.add_handler(CommandHandler("calibrate", cmd_calibrate))
    app.add_handler(CommandHandler("authors", cmd_authors))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("learnings", cmd_learnings))
    app.add_handler(CommandHandler("sectors", cmd_sectors))
    app.add_handler(CommandHandler("venues", cmd_venues))
    app.add_handler(CommandHandler("mechanisms", cmd_mechanisms))
    app.add_handler(CommandHandler("sector", cmd_sector))
    app.add_handler(CommandHandler("venue", cmd_venue))
    app.add_handler(CommandHandler("mechanism", cmd_mechanism))
    app.add_handler(CommandHandler("convergence", cmd_convergence))
    app.add_handler(CommandHandler("convergence_threshold", cmd_convergence_threshold))
    app.add_handler(CommandHandler("strong_convergence", cmd_strong_convergence))
    app.add_handler(CommandHandler("strong_threshold", cmd_strong_threshold))
    app.add_handler(CommandHandler("emerging", cmd_emerging))
    app.add_handler(CommandHandler("viral_handles", cmd_viral_handles))
    app.add_handler(CommandHandler("viral_handle", cmd_viral_handle))
    app.add_handler(CommandHandler("seed_viral_example", cmd_seed_viral_example))
    app.add_handler(CommandHandler("backfill", cmd_backfill))
    app.add_handler(CommandHandler("calibration", cmd_calibration_root))
    app.add_handler(CommandHandler("patterns", cmd_patterns))
    app.add_handler(CallbackQueryHandler(on_venue_suggestion_callback, pattern=r"^s_v:"))
    app.add_handler(CallbackQueryHandler(on_suggestion_callback, pattern=r"^s:"))
    app.add_handler(CallbackQueryHandler(on_pattern_callback, pattern=r"^pat:"))
    app.add_handler(CallbackQueryHandler(on_convergence_callback, pattern=r"^cv:"))
    app.add_handler(CallbackQueryHandler(on_feedback_callback, pattern=r"^[udm]:"))
    for name in COMING_SOON_COMMANDS:
        app.add_handler(CommandHandler(name, _make_coming_soon(name)))
    app.add_error_handler(error_handler)
