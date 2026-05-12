import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)

HandlerFn = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]]


def restricted(handler: HandlerFn) -> HandlerFn:
    """Restrict a handler to the single authorized Telegram user ID."""

    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        settings = context.application.bot_data.get("settings")
        allowed_id = settings.telegram_user_id if settings else None
        user = update.effective_user
        if user is None or allowed_id is None or user.id != allowed_id:
            log.info(
                "blocked_user",
                extra={
                    "user_id": getattr(user, "id", None),
                    "username": getattr(user, "username", None),
                    "command": (update.effective_message.text if update.effective_message else None),
                },
            )
            return None
        return await handler(update, context)

    return wrapper
