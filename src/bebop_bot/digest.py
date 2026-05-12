import html
import logging
from datetime import UTC, datetime
from typing import Any

from telegram.constants import ParseMode

from bebop_bot.models import ScoredTweet

log = logging.getLogger(__name__)

TELEGRAM_MAX = 4000
POST_TEXT_LIMIT = 280


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
    rel = relative_time(t.created_at, now=now)
    text = t.text
    if len(text) > POST_TEXT_LIMIT:
        text = text[: POST_TEXT_LIMIT - 1] + "…"
    body = html.escape(text)
    reason = ""
    if st.auto_included_reason:
        reason = f" [{html.escape(st.auto_included_reason)}]"
    return (
        f'• <a href="{url}">@{handle}</a> score {composite:.1f}{reason}\n'
        f"  {likes}❤ {replies}\U0001f4ac {rel}\n"
        f"  {body}"
    )


def _format_topic_block(
    topic_name: str, summary: str, posts: list[ScoredTweet], now: datetime
) -> str:
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


async def _send(bot: Any, chat_id: int, text: str) -> None:
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def send_digest(
    bot: Any,
    chat_id: int,
    results: dict[str, tuple[str, list[ScoredTweet]]],
) -> None:
    now = datetime.now(UTC)
    ts = now.strftime("%Y-%m-%d %H:%M UTC")

    if not results:
        await _send(bot, chat_id, f"<b>Roundup - {ts}</b>\nNo posts cleared the threshold.")
        return

    total = sum(len(posts) for _, posts in results.values())
    n_topics = len(results)
    header = f"<b>Roundup - {ts}</b>\n{n_topics} topics, {total} posts"

    blocks = [
        _format_topic_block(name, summary, posts, now)
        for name, (summary, posts) in results.items()
        if posts
    ]

    for msg in _split_messages(header, blocks):
        await _send(bot, chat_id, msg)
