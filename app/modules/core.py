import logging
from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete
from app.database.engine import async_session_factory
from app.database.models import ChatDigest, ChatLog
from app.modules.registry import (
    MODULES,
    build_help_text,
    build_modules_keyboard,
    build_start_text,
    get_enabled_modules,
    set_all_modules,
    sync_chat_commands,
    toggle_module,
)

logger = logging.getLogger(__name__)
router = Router(name="core")


async def _reply_text(message: Message, text: str, reply_markup=None) -> Message:
    try:
        return await message.reply(text, reply_markup=reply_markup)
    except Exception:
        return await message.answer(text, reply_markup=reply_markup)


async def is_chat_admin(
    bot: Bot,
    chat_id: int,
    user_id: int | None,
    sender_chat_id: int | None = None,
) -> bool:
    """
    Returns True if the actor is allowed to perform admin actions in the chat:
    - Private chats (positive chat_id) -> True
    - Anonymous group admin (sender_chat_id == chat_id) -> True
    - Group/supergroup member with ADMINISTRATOR or CREATOR status -> True
    """
    if chat_id > 0:
        return True
    if sender_chat_id is not None and sender_chat_id == chat_id:
        return True
    if user_id is None:
        return False
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )
    except Exception as e:
        logger.warning(
            "Failed to verify admin status for user %s in chat %s: %s",
            user_id,
            chat_id,
            e,
        )
        return False


@router.message(Command("start", "modules", "settings", ignore_case=True))
async def cmd_start_modules(message: Message) -> None:
    """Shows interactive per-chat module settings UI and syncs chat commands."""
    chat_id = message.chat.id
    is_group = message.chat.type in ("group", "supergroup")
    enabled = await get_enabled_modules(chat_id)

    if message.bot is not None:
        await sync_chat_commands(message.bot, chat_id, enabled)

    await _reply_text(
        message,
        build_start_text(enabled, is_group=is_group),
        reply_markup=build_modules_keyboard(enabled),
    )


@router.callback_query(F.data.startswith("mod:"))
async def cb_toggle_modules(callback: CallbackQuery) -> None:
    """Handles inline keyboard toggles for per-chat modules."""
    if callback.message is None:
        await callback.answer()
        return

    chat = callback.message.chat
    chat_id = chat.id
    is_group = chat.type in ("group", "supergroup")

    if is_group:
        user_id = callback.from_user.id if callback.from_user else None
        if callback.bot is None or not await is_chat_admin(callback.bot, chat_id, user_id):
            await callback.answer(
                "Змінювати модулі можуть лише адміністратори чату.",
                show_alert=True,
            )
            return

    data = callback.data or ""
    if data.startswith("mod:toggle:"):
        module_id = data.removeprefix("mod:toggle:")
        if module_id not in MODULES:
            await callback.answer("Невідомий модуль.", show_alert=True)
            return
        new_state, new_enabled = await toggle_module(chat_id, module_id)
        spec = MODULES[module_id]
        toast = (
            f"✅ {spec.title}: увімкнено"
            if new_state
            else f"❌ {spec.title}: вимкнено"
        )
    elif data == "mod:all:on":
        new_enabled = await set_all_modules(chat_id, True)
        toast = "✅ Усі модулі увімкнено"
    elif data == "mod:all:off":
        new_enabled = await set_all_modules(chat_id, False)
        toast = "❌ Усі модулі вимкнено"
    else:
        await callback.answer("Невідома дія.")
        return

    if callback.bot is not None:
        await sync_chat_commands(callback.bot, chat_id, new_enabled)

    try:
        await callback.message.edit_text(
            build_start_text(new_enabled, is_group=is_group),
            reply_markup=build_modules_keyboard(new_enabled),
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.warning("Failed to edit module settings message: %s", e)
    except Exception as e:
        logger.warning("Unexpected error editing module settings message: %s", e)

    await callback.answer(toast)


@router.message(Command("help", ignore_case=True))
async def cmd_help(message: Message) -> None:
    """Shows help listing only core commands and currently enabled modules."""
    enabled = await get_enabled_modules(message.chat.id)
    await _reply_text(message, build_help_text(enabled))


@router.message(Command("clear", "clear_memory", ignore_case=True))
async def cmd_clear_memory(message: Message) -> None:
    """Clears stored chat message history and digests (admins only in groups)."""
    if message.chat.type in ("group", "supergroup"):
        user_id = message.from_user.id if message.from_user else None
        sender_chat_id = message.sender_chat.id if message.sender_chat else None
        if message.bot is None or not await is_chat_admin(
            message.bot, message.chat.id, user_id, sender_chat_id=sender_chat_id
        ):
            await _reply_text(
                message,
                "⛔ Очищувати історію чату можуть лише адміністратори групи.",
            )
            return

    try:
        async with async_session_factory() as session:
            await session.execute(
                delete(ChatLog).where(ChatLog.chat_id == message.chat.id)
            )
            await session.execute(
                delete(ChatDigest).where(ChatDigest.chat_id == message.chat.id)
            )
            await session.commit()
    except Exception as e:
        logger.exception("Error clearing chat history %s: %s", message.chat.id, e)
        await _reply_text(
            message,
            "⚠️ Виникла помилка при очищенні історії чату з бази даних.",
        )
        return

    await _reply_text(
        message,
        "🧹 Історію повідомлень та дайджестів цього чату повністю очищено!",
    )
