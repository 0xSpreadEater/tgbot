import asyncio
import contextlib
import logging

from dotenv import load_dotenv
from telegram.ext import AIORateLimiter, Application

from bebop_bot.config import get_settings
from bebop_bot.db import init_db
from bebop_bot.handlers import register_handlers
from bebop_bot.logging_setup import setup_logging

log = logging.getLogger(__name__)


async def main() -> None:
    load_dotenv()
    settings = get_settings()
    setup_logging(settings.log_level)
    log.info("startup", extra={"db_path": settings.db_path, "log_level": settings.log_level})

    conn = await init_db(settings.db_path)

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .rate_limiter(AIORateLimiter())
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["db"] = conn

    register_handlers(app)

    log.info("bot_ready")
    try:
        # run_polling is async-aware; use the underscore variant to avoid
        # nested event-loop management when we're already in asyncio.run().
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        # Block forever until cancelled.
        stop_event = asyncio.Event()
        await stop_event.wait()
    finally:
        with contextlib.suppress(Exception):
            await app.updater.stop()
        with contextlib.suppress(Exception):
            await app.stop()
            await app.shutdown()
        await conn.close()
        log.info("shutdown_complete")


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
