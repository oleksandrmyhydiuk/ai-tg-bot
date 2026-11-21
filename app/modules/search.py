from __future__ import annotations

import logging
import re

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import select

from app.database.engine import async_session_factory
from app.database.models import ChatLog, User
from app.modules.registry import is_module_enabled
from app.services.context_builder import format_kyiv_datetime
from app.services.summary_verifier import format_message_link

logger = logging.getLogger(__name__)
router = Router(name="search")

MAX_SEARCH_RESULTS = 10
MAX_QUERY_TOKENS = 8
SNIPPET_MAX_LEN = 140


async def _reply_safely(message: Message, text: str) -> Message:
    try:
        return await message.reply(text)
    except Exception:
        return await message.answer(text)


def _escape_like(token: str) -> str:
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _format_snippet(text: str, max_len: int = SNIPPET_MAX_LEN) -> str:
    clean = re.sub(r"\s+", " ", (text or "")).strip()
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 3].rstrip() + "..."


def _resolve_author(log: ChatLog, user: User | None) -> str:
    if getattr(log, "is_bot_message", False):
        return "Бот"
    if user is not None and user.full_name:
        name = user.full_name.strip()
        if name:
            return name
    if log.sender_name and log.sender_name.strip():
        return log.sender_name.strip()
    return f"User#{log.user_id or 0}"


@router.message(Command("find", ignore_case=True))
async def cmd_find(message: Message, command: CommandObject) -> None:
    """Searches chat history for messages matching all query tokens (/find <query>)."""
    chat_id = message.chat.id
    if not await is_module_enabled(chat_id, "search"):
        await _reply_safely(
            message,
            "Модуль пошуку по історії чату вимкнено в цьому чаті. Увімкнути: /start",
        )
        return

    raw_query = (command.args or "").strip()
    if not raw_query:
        await _reply_safely(message, "💡 Використання: /find <пошуковий запит>")
        return

    tokens = [tok for tok in re.split(r"\s+", raw_query) if len(tok) >= 2][:MAX_QUERY_TOKENS]
    if not tokens:
        await _reply_safely(
            message,
            "⚠️ Запит занадто короткий. Введіть слово довжиною від 2 символів.",
        )
        return

    try:
        async with async_session_factory() as session:
            stmt = (
                select(ChatLog, User)
                .outerjoin(User, ChatLog.user_id == User.user_id)
                .where(ChatLog.chat_id == chat_id)
            )
            for token in tokens:
                stmt = stmt.where(
                    ChatLog.text.ilike(f"%{_escape_like(token)}%", escape="\\")
                )
            stmt = (
                stmt.order_by(ChatLog.timestamp.desc(), ChatLog.message_id.desc())
                .limit(MAX_SEARCH_RESULTS)
            )
            rows = (await session.execute(stmt)).all()
    except Exception as exc:
        logger.exception("Database error in /find for chat %s: %s", chat_id, exc)
        await _reply_safely(
            message,
            "⚠️ Виникла помилка під час пошуку по історії чату.",
        )
        return

    if not rows:
        await _reply_safely(message, f"🔍 Нічого не знайдено за запитом «{raw_query}».")
        return

    lines: list[str] = [f"🔍 Результати пошуку за запитом «{raw_query}» (знайдено {len(rows)}):"]
    for idx, (log, user) in enumerate(rows, start=1):
        ts_str = format_kyiv_datetime(log.timestamp, include_date=True)
        author = _resolve_author(log, user)
        snippet = _format_snippet(log.text, SNIPPET_MAX_LEN)
        link = format_message_link(chat_id, log.message_id, message.chat.username)
        lines.append(f"{idx}. [{ts_str}] {author}: {snippet}\n   {link}")

    await _reply_safely(message, "\n".join(lines))
