import asyncio
from contextlib import suppress
import logging
from pathlib import Path
import sys

# Дозволяє запускати бота як через `python -m app.main`, так і напряму з VS Code (`python app/main.py`)
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
    """Вбудований фоновий цикл asyncio (без сторонніх бібліотек apscheduler): щогодини робить дайджест і очищує старі записи."""
    interval_seconds = max(60, settings.digest_interval_minutes * 60)
    logger.info(
        "Фоновий планувальник запущено (погодинний дайджест та очищення БД кожні %d хв).",
        settings.digest_interval_minutes,
    )
    while True:
        await asyncio.sleep(interval_seconds)
        await run_hourly_digest_and_cleanup_job()


async def _setup_bot_commands(bot: Bot) -> None:
    """Реєструє список команд бота в інтерфейсі Telegram (для особистих та групових чатів)."""
    bot_commands = [
        BotCommand(command="start", description="Перелік команд та опис можливостей бота"),
        BotCommand(command="help", description="Довідка по командах бота"),
        BotCommand(command="summary", description="Звіт чату за N годин (напр. /summary 4)"),
        BotCommand(command="ask", description="Пошук в інтернеті або пояснення через Gemini 3"),
        BotCommand(command="stats", description="Статистика повідомлень та топ активних учасників"),
        BotCommand(command="clear", description="Очистити збережену історію чату (адмінам)"),
    ]
    for scope in (
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
    ):
        try:
            await bot.set_my_commands(bot_commands, scope=scope)
        except Exception as e:
            logger.warning("Не вдалося зареєструвати меню команд (%s): %s", type(scope).__name__, e)


async def main() -> None:
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=None),
    )
    dp = Dispatcher()

    # Реєстрація роутерів (спочатку команди та пінги, потім фоновий логер повідомлень)
    dp.include_router(commands.router)
    dp.include_router(group_events.router)

    bg_task: asyncio.Task | None = None

    try:
        logger.info("Ініціалізація бази даних SQLite...")
        await init_db()

        bg_task = asyncio.create_task(_hourly_digest_loop())

        await bot.delete_webhook(drop_pending_updates=True)
        await _setup_bot_commands(bot)
        logger.info("Бот запущений і слухає повідомлення...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.exception("Критична помилка під час запуску або роботи бота: %s", e)
        raise
    finally:
        logger.info("Зупинка сервісів...")
        if bg_task is not None:
            bg_task.cancel()
            with suppress(asyncio.CancelledError):
                await bg_task
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
