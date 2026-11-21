from __future__ import annotations

import logging
import re

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.modules.core import is_chat_admin
from app.modules.registry import (
    get_daily_digest_time,
    is_module_enabled,
    set_daily_digest_time,
)

logger = logging.getLogger(__name__)
router = Router(name="daily_digest")

DEFAULT_DIGEST_TIME = "21:00"
_HHMM_REGEX = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

_USAGE_TEXT = (
    "💡 Використання команди /digest:\n"
    "• /digest або /digest status — поточний розклад\n"
    "• /digest on [HH:MM] — увімкнути щоденний дайджест (за замовчуванням 21:00 за Києвом)\n"
    "• /digest off — вимкнути щоденний дайджест"
)


async def _reply_safely(message: Message, text: str) -> Message:
    try:
        return await message.reply(text)
    except Exception:
        return await message.answer(text)


def _parse_hhmm(raw: str) -> str | None:
    match = _HHMM_REGEX.match(raw.strip())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    return f"{hour:02d}:{minute:02d}"


async def _require_admin_in_group(message: Message, bot: Bot) -> bool:
    if message.chat.type not in ("group", "supergroup"):
        return True
    user_id = message.from_user.id if message.from_user else None
    sender_chat_id = message.sender_chat.id if message.sender_chat else None
    allowed = await is_chat_admin(bot, message.chat.id, user_id, sender_chat_id)
    if not allowed:
        await _reply_safely(
            message,
            "⛔ Змінювати розклад щоденного дайджесту можуть лише адміністратори групи.",
        )
    return allowed


@router.message(Command("digest", ignore_case=True))
async def cmd_digest(message: Message, command: CommandObject, bot: Bot) -> None:
    """Manages daily scheduled digest (/digest [on HH:MM | off | status])."""
    chat_id = message.chat.id
    if not await is_module_enabled(chat_id, "daily_digest"):
        await _reply_safely(
            message,
            "Модуль щоденного дайджесту вимкнено в цьому чаті. Увімкнути: /start",
        )
        return

    raw_args = (command.args or "").strip()
    parts = raw_args.split()
    action = parts[0].lower() if parts else "status"

    if action == "status":
        current_time = await get_daily_digest_time(chat_id)
        if current_time:
            await _reply_safely(
                message,
                f"📅 Щоденний дайджест увімкнено: щодня о {current_time} (Київ).\n"
                "Вимкнути: /digest off",
            )
        else:
            await _reply_safely(
                message,
                "📅 Щоденний дайджест наразі вимкнено.\n"
                f"Увімкнути: /digest on {DEFAULT_DIGEST_TIME}",
            )
        return

    if action == "on":
        if not await _require_admin_in_group(message, bot):
            return

        if len(parts) >= 2:
            normalized = _parse_hhmm(parts[1])
            if normalized is None:
                await _reply_safely(
                    message,
                    "⚠️ Некоректний формат часу. Вкажіть час від 00:00 до 23:59, наприклад: /digest on 21:00",
                )
                return
        else:
            normalized = DEFAULT_DIGEST_TIME

        await set_daily_digest_time(chat_id, normalized)
        await _reply_safely(
            message,
            f"✅ Щоденний дайджест увімкнено! Бот надсилатиме підсумок щодня о {normalized} (за київським часом).",
        )
        return

    if action == "off":
        if not await _require_admin_in_group(message, bot):
            return

        await set_daily_digest_time(chat_id, None)
        await _reply_safely(message, "🔕 Щоденний дайджест вимкнено.")
        return

    await _reply_safely(message, _USAGE_TEXT)
