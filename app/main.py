import asyncio
from contextlib import suppress
import logging
from pathlib import Path
import sys

# Allows running the bot either as `python -m app.main` or directly from VS Code (`python app/main.py`)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
)

from app.config import settings
from app.database.engine import engine, init_db
from app.handlers import group_events
from app.modules import (
    ask,
    core,
    daily_digest,
    media_dl,
    search,
    stats,
    summary,
    voice,
)
from app.modules.registry import CORE_COMMANDS
from app.services.gemini_client import (
    run_daily_digest_tick,
    run_hourly_digest_and_cleanup_job,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


async def _hourly_digest_loop() -> None:
    """Built-in asyncio background loop: runs hourly digests and cleans up outdated records."""
    interval_seconds = max(60, settings.digest_interval_minutes * 60)
    logger.info(
        "Background scheduler started (hourly digest and DB cleanup every %d min).",
        settings.digest_interval_minutes,
    )
    while True:
        await asyncio.sleep(interval_seconds)
        await run_hourly_digest_and_cleanup_job()


async def _daily_digest_loop(bot: Bot) -> None:
    """Built-in asyncio background loop: checks scheduled daily digests every 60 seconds."""
    logger.info("Daily digest scheduler started (tick every 60s).")
    while True:
        await asyncio.sleep(60)
        try:
            await run_daily_digest_tick(bot)
        except Exception as e:
            logger.exception("Error in daily digest loop: %s", e)


async def _setup_bot_commands(bot: Bot) -> None:
    """Registers minimal global fallback command list; per-chat BotCommandScopeChat overrides dynamically."""
    bot_commands = [
        BotCommand(command=cmd, description=desc)
        for cmd, desc in CORE_COMMANDS
    ]
    for scope in (
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
    ):
        try:
            await bot.set_my_commands(bot_commands, scope=scope)
        except Exception as e:
            logger.warning("Failed to register command menu (%s): %s", type(scope).__name__, e)


async def main() -> None:
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=None),
    )
    dp = Dispatcher()

    # Register modular routers in priority order, followed by passive group message logger
    dp.include_router(core.router)
    dp.include_router(summary.router)
    dp.include_router(ask.router)
    dp.include_router(media_dl.router)
    dp.include_router(search.router)
    dp.include_router(stats.router)
    dp.include_router(voice.router)
    dp.include_router(daily_digest.router)
    dp.include_router(group_events.router)

    hourly_task: asyncio.Task | None = None
    daily_task: asyncio.Task | None = None

    try:
        logger.info("Initializing SQLite database...")
        await init_db()

        hourly_task = asyncio.create_task(_hourly_digest_loop())
        daily_task = asyncio.create_task(_daily_digest_loop(bot))

        await bot.delete_webhook(drop_pending_updates=True)
        await _setup_bot_commands(bot)
        logger.info("Bot is running and listening for messages...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.exception("Critical error during bot startup or execution: %s", e)
        raise
    finally:
        logger.info("Shutting down services...")
        for task in (hourly_task, daily_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
