import asyncio
from contextlib import suppress
import logging
from pathlib import Path
import sys

# Allows running the bot either as `python -m app.main` or directly from VS Code (`python app/main.py`)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    CallbackQuery,
    Message,
    TelegramObject,
)

from app.config import settings
from app.database.engine import engine, init_db
from app.handlers import group_events
from app.modules import (
    admin_ops,
    ask,
    core,
    daily_digest,
    link_tldr,
    media_dl,
    privacy_export,
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


class AllowlistMiddleware(BaseMiddleware):
    """
    Restricts bot interaction to chats in ALLOWED_CHAT_IDS (plus global ADMIN_USER_IDS).
    When ALLOWED_CHAT_IDS is empty, all chats are allowed.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        allowed_chats = settings.parsed_allowed_chat_ids
        if not allowed_chats:
            return await handler(event, data)

        chat_id: int | None = None
        user_id: int | None = None

        if isinstance(event, Message):
            chat_id = event.chat.id if event.chat else None
            user_id = event.from_user.id if event.from_user else None
        elif isinstance(event, CallbackQuery):
            chat_id = event.message.chat.id if (event.message and event.message.chat) else None
            user_id = event.from_user.id if event.from_user else None

        if (chat_id is not None and chat_id in allowed_chats) or (
            user_id is not None and user_id in settings.parsed_admin_user_ids
        ):
            return await handler(event, data)

        return None


async def main() -> None:
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=None),
    )
    dp = Dispatcher()

    allowlist_mw = AllowlistMiddleware()
    dp.message.middleware(allowlist_mw)
    dp.callback_query.middleware(allowlist_mw)

    # Register modular routers in priority order, followed by passive group message logger
    dp.include_router(core.router)
    dp.include_router(summary.router)
    dp.include_router(ask.router)
    dp.include_router(media_dl.router)
    dp.include_router(search.router)
    dp.include_router(stats.router)
    dp.include_router(voice.router)
    dp.include_router(daily_digest.router)
    dp.include_router(link_tldr.router)
    dp.include_router(privacy_export.router)
    dp.include_router(admin_ops.router)
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
