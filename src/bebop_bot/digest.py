import html
import json
import logging
from datetime import UTC, datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from bebop_bot.models import ScoredTweet

log = logging.getLogger(__name__)

TELEGRAM_MAX = 4000
POST_TEXT_LIMIT = 320


def relative_time(dt: datetime, now: datetime | None = None) -> str:
    if dt is None:
        return ""
    now = now or datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{max(seconds, 0)}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _format_post(st: ScoredTweet, now: datetime) -> str:
    t = st.tweet
    handle = html.escape(t.author_handle)
    url = html.escape(t.url, quote=True)
    composite = st.score.composite
    likes = t.like_count
    replies = t.reply_count
    retweets = t.retweet_count
    rel = relative_time(t.created_at, now=now)
    text = t.text
    if len(text) > POST_TEXT_LIMIT:
        text = text[: POST_TEXT_LIMIT - 1] + "…"
    body = html.escape(text)
    auto_marker = " *" if st.auto_included_reason else ""
    return (
        f'<a href="{url}">@{handle}</a> score {composite:.1f}{auto_marker}\n'
        f"{likes}❤ {replies}\U0001f4ac {retweets}\U0001f501 {rel}\n\n"
        f"{body}"
    )


def _format_topic_block(
    topic_name: str, summary: str, posts: list[ScoredTweet], now: datetime
) -> str:
    """Kept for backwards compat with tests. Phase 3 sends per-post messages."""
    header = f"<b>{html.escape(topic_name)}</b> ({len(posts)})"
    parts = [header]
    if summary:
        parts.append(f"<i>{html.escape(summary)}</i>")
    for post in posts:
        parts.append(_format_post(post, now))
    return "\n".join(parts)


def _split_messages(header: str, blocks: list[str]) -> list[str]:
    messages: list[str] = []
    current = header
    for block in blocks:
        candidate = current + "\n\n" + block if current else block
        if len(candidate) > TELEGRAM_MAX and current:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def _post_keyboard(tweet_id: str, handle: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Insight", callback_data=f"u:{tweet_id}"),
                InlineKeyboardButton("Noise", callback_data=f"d:{tweet_id}"),
                InlineKeyboardButton(f"Mute @{handle} 30d", callback_data=f"m:{tweet_id}"),
            ]
        ]
    )


async def _send_plain(bot: Any, chat_id: int, text: str) -> None:
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def _send_post(
    bot: Any,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=reply_markup,
    )


async def send_digest(
    bot: Any,
    chat_id: int,
    results: dict[str, tuple[str, list[ScoredTweet]]],
    db: Any = None,
    manual_scan: bool = False,
) -> None:
    now = datetime.now(UTC)
    ts = now.strftime("%Y-%m-%d %H:%M UTC")
    header_title = "Manual scan" if manual_scan else "Roundup"

    if not results:
        await _send_plain(
            bot, chat_id,
            f"<b>{header_title} - {ts}</b>\nNo posts cleared the threshold.",
        )
        return

    total = sum(len(posts) for _, posts in results.values())
    n_topics = len(results)
    header = (
        f"<b>{header_title} - {ts}</b>\n"
        f"{n_topics} topics, {total} posts surfaced"
    )
    await _send_plain(bot, chat_id, header)

    for topic_name, (summary, posts) in results.items():
        if not posts:
            continue
        topic_header = f"<b>=== {html.escape(topic_name)} ===</b>"
        if summary:
            topic_header += f"\n<i>{html.escape(summary)}</i>"
        await _send_plain(bot, chat_id, topic_header)

        for st in posts:
            t = st.tweet
            body = _format_post(st, now)
            metrics = {
                "likes": t.like_count,
                "replies": t.reply_count,
                "retweets": t.retweet_count,
                "quotes": t.quote_count,
            }
            if db is not None:
                try:
                    await db.upsert_pending_feedback(
                        tweet_id=t.id,
                        author_handle=t.author_handle,
                        tweet_text=t.text,
                        topic_name=topic_name,
                        metrics_json=json.dumps(metrics),
                    )
                except Exception:  # noqa: BLE001
                    log.exception("pending_feedback_insert_failed", extra={"tweet_id": t.id})
            kb = _post_keyboard(t.id, t.author_handle)
            await _send_post(bot, chat_id, body, kb)
