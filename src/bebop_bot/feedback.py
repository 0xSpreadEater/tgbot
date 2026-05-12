import contextlib
import logging
from datetime import UTC, datetime, timedelta

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bebop_bot import suggestions
from bebop_bot.auth import restricted
from bebop_bot.db import Db

log = logging.getLogger(__name__)

LABEL_BY_PREFIX = {"u": "up", "d": "down", "m": "mute"}


def _db(context: ContextTypes.DEFAULT_TYPE) -> Db:
    db = context.application.bot_data.get("db_wrapper")
    if db is None:
        db = Db(context.application.bot_data["db"])
        context.application.bot_data["db_wrapper"] = db
    return db


def _parse_callback(data: str | None) -> tuple[str, str] | None:
    if not data or ":" not in data:
        return None
    prefix, rest = data.split(":", 1)
    if not rest:
        return None
    return prefix, rest


async def update_message_marker(q, new_label, pf, prior_change, db: Db) -> None:
    ups, downs = await db.get_recent_author_score(pf["author_handle"], 60)
    text = {
        "up":   f"Marked up - @{pf['author_handle']} now {ups}/{downs} (60d)",
        "down": f"Marked down - @{pf['author_handle']} now {ups}/{downs} (60d)",
        "mute": f"Muted @{pf['author_handle']} for 30 days",
    }[new_label]
    if prior_change:
        text = f"Changed from {prior_change} to {new_label}. " + text

    existing = q.message.text_html if q.message and q.message.text_html else ""
    new_text = existing + f"\n\n<i>{text}</i>"
    try:
        await q.edit_message_text(
            new_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=None,
        )
    except Exception:  # noqa: BLE001
        log.exception("edit_marker_failed")


@restricted
async def on_feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None:
        return
    await q.answer()
    parsed = _parse_callback(q.data)
    if not parsed:
        return
    prefix, tweet_id = parsed
    if prefix == "s":
        # suggestion callbacks routed elsewhere
        return
    new_label = LABEL_BY_PREFIX.get(prefix)
    if not new_label:
        return

    db = _db(context)

    pf = await db.get_pending_feedback(tweet_id)
    if pf is None:
        with contextlib.suppress(Exception):
            await q.edit_message_reply_markup(reply_markup=None)
        if q.message is not None:
            await q.message.reply_text("This post's context expired.")
        return

    prior = await db.get_feedback(tweet_id)
    prior_label = prior["label"] if prior else None

    if prior_label == new_label:
        # Idempotent: leave DB alone, still surface marker.
        await update_message_marker(q, new_label, pf, prior_change=None, db=db)
        return

    await db.upsert_feedback(
        tweet_id=tweet_id,
        topic_name=pf["topic_name"],
        author_handle=pf["author_handle"],
        label=new_label,
        tweet_text=pf["tweet_text"],
        tweet_metrics_json=pf["metrics_json"],
    )
    # Only diff ups/downs counters; mute doesn't affect those counters.
    if new_label in ("up", "down") or prior_label in ("up", "down"):
        await db.adjust_author_score(pf["author_handle"], prior_label, new_label)

    if new_label == "mute":
        until = datetime.now(UTC) + timedelta(days=30)
        await db.set_author_muted(pf["author_handle"], until)

    await update_message_marker(q, new_label, pf, prior_change=prior_label, db=db)

    if new_label == "up" and q.message is not None:
        try:
            await suggestions.maybe_suggest_allowlist(
                bot=context.bot,
                chat_id=q.message.chat_id,
                db=db,
                handle=pf["author_handle"],
            )
        except Exception:  # noqa: BLE001
            log.exception("suggestion_failed")


@restricted
async def on_venue_suggestion_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    q = update.callback_query
    if q is None:
        return
    await q.answer()
    data = q.data or ""
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "s_v":
        return
    action, venue_term = parts[1], parts[2]
    db = _db(context)

    if action == "accept":
        await db.set_venue_suggestion(venue_term, "accepted")
        msg = f"Accepted {venue_term}. Will be added to active sweep set."
    elif action == "later":
        await db.set_venue_suggestion(
            venue_term, "pending",
            last_suggested_at=datetime.now(UTC).isoformat(),
        )
        msg = f"Will suggest {venue_term} again later."
    elif action == "never":
        await db.set_venue_suggestion(venue_term, "blocked")
        msg = f"Won't suggest {venue_term} again."
    else:
        return

    try:
        await q.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=None)
    except Exception:  # noqa: BLE001
        log.exception("venue_suggestion_edit_failed", extra={"entity_term": venue_term})


@restricted
async def on_suggestion_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None:
        return
    await q.answer()
    data = q.data or ""
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "s":
        return
    action, handle = parts[1], parts[2].lower()
    db = _db(context)

    if action == "y":
        added = await db.add_to_allowlist(handle)
        msg = f"Added @{handle}." if added else f"@{handle} already on allowlist."
    elif action == "n":
        msg = "Skipped - will suggest again if more upvotes accumulate."
    elif action == "b":
        await db.add_suggestion_block(handle)
        msg = "Won't suggest again."
    else:
        return

    try:
        await q.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=None)
    except Exception:  # noqa: BLE001
        log.exception("suggestion_edit_failed")
