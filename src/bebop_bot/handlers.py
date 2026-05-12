import html
import logging
import os

import aiosqlite
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from bebop_bot import db as dbm
from bebop_bot.auth import restricted
from bebop_bot.query_parser import normalize

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
    "/test &lt;name&gt; — (coming soon)\n"
    "\n"
    "<b>Roundup</b>\n"
    "/run — (coming soon)\n"
    "/preview — (coming soon)\n"
    "/pause — pause the roundup\n"
    "/resume — resume the roundup\n"
    "/status — show bot status\n"
    "\n"
    "<b>Filtering</b>\n"
    "/threshold &lt;n&gt; — set roundup threshold (1..5)\n"
    "/rubric — (coming soon)\n"
    "/learnings — (coming soon)\n"
    "/calibrate — (coming soon)\n"
    "\n"
    "<b>Authors</b>\n"
    "/allow &lt;handle&gt; — add to allowlist\n"
    "/disallow &lt;handle&gt; — remove from allowlist\n"
    "/allowlist — show allowlist\n"
    "/authors — (coming soon)\n"
    "/mute &lt;handle&gt; — (coming soon)\n"
    "/unmute &lt;handle&gt; — (coming soon)\n"
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
        n = int(args[0])
    except ValueError:
        await _reply(update, "Threshold must be an integer 1..5.")
        return
    if not 1 <= n <= 5:
        await _reply(update, "Threshold must be between 1 and 5.")
        return
    conn = _conn(context)
    await dbm.set_setting(conn, "threshold", str(n))
    await _reply(update, f"Threshold set to {n}.")


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
    "test", "run", "preview", "rubric", "learnings", "calibrate",
    "authors", "mute", "unmute", "chains", "sol_config", "evm_config",
    "emerging", "dismiss", "sectors", "venues", "sector", "venue",
    "backfill",
)


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
    for name in COMING_SOON_COMMANDS:
        app.add_handler(CommandHandler(name, _make_coming_soon(name)))
    app.add_error_handler(error_handler)
