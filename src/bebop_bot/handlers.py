import hashlib
import html
import logging
import os
from datetime import UTC, datetime, timedelta

import aiosqlite
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from bebop_bot import db as dbm
from bebop_bot import pipeline as pipelinem
from bebop_bot.auth import restricted
from bebop_bot.digest import relative_time
from bebop_bot.feedback import on_feedback_callback, on_suggestion_callback
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
    "/scan — manual force scan (no rate limit, ignores /pause)\n"
    "/run — manual scan (60-min rate limit, respects /pause)\n"
    "/pause — stop scheduled cycles (Phase 5)\n"
    "/resume — resume scheduled cycles\n"
    "/status — bot health\n"
    "\n"
    "<b>Filtering</b>\n"
    "/threshold &lt;n&gt; — set roundup threshold (1..5)\n"
    "/rubric — show, /rubric set &lt;text&gt;, /rubric clear\n"
    "/learnings — summarize patterns from recent feedback\n"
    "/calibrate &lt;score&gt; | &lt;tweet text&gt; — label a manual example\n"
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
    "/chains — (coming soon)\n"
    "/sol_config — (coming soon)\n"
    "/evm_config — (coming soon)\n"
    "/emerging — (coming soon)\n"
    "/dismiss — (coming soon)\n"
    "\n"
    "<b>Dictionary</b>\n"
    "/sectors — (coming soon)\n"
    "/venues — (coming soon)\n"
    "/sector — (coming soon)\n"
    "/venue — (coming soon)\n"
    "\n"
    "<b>Backfill</b>\n"
    "/backfill — (coming soon)\n"
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
    topics_count = await dbm.count_rows(conn, "topics")
    allow_count = await dbm.count_rows(conn, "allowlist")
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
    "chains", "sol_config", "evm_config",
    "emerging", "dismiss", "sectors", "venues", "sector", "venue",
    "backfill",
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
    await _reply(update, "Running manual scan...")
    try:
        await pipelinem.run_roundup(
            db=db,
            x=x_client,
            claude=claude_client,
            bot=context.bot,
            chat_id=chat_id,
            advance_since_id=True,
            force=True,
            manual_scan=True,
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
    app.add_handler(CallbackQueryHandler(on_suggestion_callback, pattern=r"^s:"))
    app.add_handler(CallbackQueryHandler(on_feedback_callback, pattern=r"^[udm]:"))
    for name in COMING_SOON_COMMANDS:
        app.add_handler(CommandHandler(name, _make_coming_soon(name)))
    app.add_error_handler(error_handler)
