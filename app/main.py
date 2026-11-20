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
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
)

from app.config import settings
from app.database.engine import init_db, engine
from app.handlers import commands, group_events
from app.services.gemini_client import run_hourly_digest_and_cleanup_job

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


async def _setup_bot_commands(bot: Bot) -> None:
    """Registers bot command list in Telegram UI (for private and group chats)."""
    bot_commands = [
        BotCommand(command="start", description="List commands and bot capabilities"),
        BotCommand(command="help", description="Show bot command help and usage"),
        BotCommand(command="summary", description="Generate chat summary for N hours (e.g. /summary 4)"),
        BotCommand(command="ask", description="Web search or AI explanation via Gemini 3"),
        BotCommand(command="stats", description="Chat message statistics and most active participants"),
        BotCommand(command="clear", description="Clear stored chat history (admins only)"),
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

    # Register routers (commands and pings first, then background message logger)
    dp.include_router(commands.router)
    dp.include_router(group_events.router)

    bg_task: asyncio.Task | None = None

    try:
        logger.info("Initializing SQLite database...")
        await init_db()

        bg_task = asyncio.create_task(_hourly_digest_loop())

        await bot.delete_webhook(drop_pending_updates=True)
        await _setup_bot_commands(bot)
        logger.info("Bot is running and listening for messages...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.exception("Critical error during bot startup or execution: %s", e)
        raise
    finally:
        logger.info("Shutting down services...")
        if bg_task is not None:
            bg_task.cancel()
            with suppress(asyncio.CancelledError):
                await bg_task
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
