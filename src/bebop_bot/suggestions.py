import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

log = logging.getLogger(__name__)


async def maybe_suggest_allowlist(
    *,
    bot: Any,
    chat_id: int,
    db: Any,
    handle: str,
) -> bool:
    handle = handle.lower()
    if await db.is_allowlisted(handle):
        return False
    if await db.is_suggestion_blocked(handle):
        return False
    ups, downs = await db.get_recent_author_score(handle, 60)
    if ups < 3 or downs != 0:
        return False
    text = (
        f"<b>@{handle}</b> has earned {ups} upvotes with no downvotes "
        f"in the last 60 days. Add to allowlist?"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, allow", callback_data=f"s:y:{handle}"),
        InlineKeyboardButton("Not now",   callback_data=f"s:n:{handle}"),
        InlineKeyboardButton("Never",     callback_data=f"s:b:{handle}"),
    ]])
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    return True
