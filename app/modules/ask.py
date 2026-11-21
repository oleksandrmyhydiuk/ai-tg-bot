from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import Message
from sqlalchemy import select

from app.database.engine import async_session_factory
from app.database.models import ChatLog, User
from app.handlers.commands import (
    MENTION_SUMMARY_REGEX,
    BotMentionFilter,
    _build_query_with_reply,
    _check_rate_limit,
    _extract_photo_bytes,
    _get_bot_username,
    _get_target_photo_message,
    _is_photo_too_large,
    _log_user_query_safely,
    _parse_summary_hours,
    _safe_edit_text,
    _safe_reply,
)
from app.handlers.group_events import log_bot_message
from app.modules.registry import is_module_enabled
from app.modules.summary import SUMMARY_DISABLED_MSG, execute_summary_for_hours
from app.services.gemini_client import gemini_service

logger = logging.getLogger(__name__)
router = Router(name="ask")

ASK_DISABLED_MSG = "Модуль AI-помічника вимкнено в цьому чаті. Увімкнути: /start"
ASK_DISABLED_DM_HINT = "Модуль AI-помічника вимкнено. Увімкнути: /start"
MAX_REPLY_CHAIN_DEPTH = 4
MAX_TURN_CHARS = 600


def _is_bare_auto_download_url(text: str) -> bool:
    """Returns True if text is a bare supported video URL handled by media_dl auto-detect."""
    stripped = text.strip()
    if not stripped or " " in stripped or "\n" in stripped:
        return False
    try:
        from app.services.media_downloader import extract_supported_urls
    except Exception:
        return False
    urls = extract_supported_urls(stripped)
    return bool(urls and urls[0].strip() == stripped)


class PrivateAskFilter(Filter):
    """
    Matches private DM text/photo messages for AI Q&A, while ignoring slash commands
    and letting media_dl auto-detect handle bare supported video URLs when enabled.
    """

    async def __call__(self, message: Message) -> bool:
        if message.chat.type != "private":
            return False
        if not (message.text or message.photo):
            return False
        raw_text = (message.text or message.caption or "").strip()
        if raw_text.startswith("/"):
            return False
        if (
            raw_text
            and not message.photo
            and _is_bare_auto_download_url(raw_text)
            and await is_module_enabled(message.chat.id, "media_dl")
        ):
            return False
        return True


async def _collect_reply_chain_turns(
    message: Message,
    max_depth: int = MAX_REPLY_CHAIN_DEPTH,
) -> list[tuple[str, str]]:
    """
    Walks up to `max_depth` ancestor messages in ChatLog starting from `message.reply_to_message`
    and returns chronological (role_label, text) pairs.
    """
    reply = message.reply_to_message
    if reply is None:
        return []

    chat_id = message.chat.id
    turns_rev: list[tuple[str, str]] = []
    visited: set[int] = set()
    current_msg_id: int | None = reply.message_id

    try:
        async with async_session_factory() as session:
            for _ in range(max_depth):
                if current_msg_id is None or current_msg_id in visited:
                    break
                visited.add(current_msg_id)

                stmt = (
                    select(ChatLog, User)
                    .outerjoin(User, ChatLog.user_id == User.user_id)
                    .where(
                        ChatLog.chat_id == chat_id,
                        ChatLog.message_id == current_msg_id,
                    )
                )
                row = (await session.execute(stmt)).first()
                if row is None:
                    break

                log_entry: ChatLog = row[0]
                user_entry: User | None = row[1]

                if log_entry.is_bot_message:
                    label = "Бот"
                else:
                    author = (
                        log_entry.sender_name
                        or (user_entry.full_name if user_entry else None)
                        or (user_entry.username if user_entry else None)
                        or "Користувач"
                    )
                    label = f"Користувач ({author})"

                text_val = (log_entry.text or "").strip()
                if text_val:
                    turns_rev.append((label, text_val[:MAX_TURN_CHARS]))

                current_msg_id = log_entry.reply_to_message_id
    except Exception as e:
        logger.debug("Failed to load multi-turn reply chain for chat %s: %s", chat_id, e)

    if not turns_rev:
        # Fallback to immediate Telegram reply object if not present in ChatLog
        replied_text = (reply.text or reply.caption or "").strip()
        if replied_text:
            from_user = getattr(reply, "from_user", None)
            if from_user is not None and getattr(from_user, "is_bot", False):
                label = "Бот"
            elif from_user is not None:
                author = from_user.full_name or from_user.username or "Користувач"
                label = f"Користувач ({author})"
            else:
                label = "Повідомлення"
            turns_rev.append((label, replied_text[:MAX_TURN_CHARS]))

    turns_rev.reverse()
    return turns_rev


async def _build_query_with_reply_chain(raw_query: str, message: Message) -> str:
    """
    Constructs the Gemini prompt with multi-turn reply chain context (up to 4 ancestors)
    when replying to a message, falling back cleanly to single-reply context.
    """
    if message.reply_to_message is None:
        return raw_query

    turns = await _collect_reply_chain_turns(message, max_depth=MAX_REPLY_CHAIN_DEPTH)
    if len(turns) <= 1:
        return _build_query_with_reply(raw_query, message)

    history_lines = [f"• {label}: {text}" for label, text in turns]
    history_block = "\n".join(history_lines)
    if raw_query:
        return (
            f"Контекст попереднього діалогу:\n{history_block}\n\n"
            f"Поточний запит користувача:\n{raw_query}"
        )
    return f"Контекст попереднього діалогу:\n{history_block}"


async def process_ask_request(
    message: Message,
    query: str,
    bot: Bot | None = None,
    empty_prompt_hint: str | None = None,
    log_custom_text: str | None = None,
) -> None:
    """
    Unified AI request processing pipeline (/ask, @bot mention, direct message):
    1) Checks prompt and photo existence.
    2) Verifies rate limit BEFORE photo download.
    3) Safely downloads photo (up to 10 MB).
    4) Walks multi-turn reply chain context from ChatLog (up to 4 ancestors).
    5) Invokes Gemini/Gemma and logs bot response in ChatLog for conversational continuity.
    """
    raw_query = (query or "").strip()
    has_photo = _get_target_photo_message(message) is not None
    replied_text_exists = bool(
        message.reply_to_message
        and (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
    )

    if not raw_query and not replied_text_exists and not has_photo:
        if empty_prompt_hint:
            await _safe_reply(message, empty_prompt_hint)
        return

    # Check rate-limit BEFORE downloading photo and accessing DB/API
    user_id = message.from_user.id if message.from_user else None
    wait_sec = _check_rate_limit(user_id)
    if wait_sec > 0:
        await _safe_reply(
            message,
            f"⏳ Зачекайте {int(wait_sec) + 1} сек. перед наступним запитом до ШІ.",
        )
        return

    if has_photo and _is_photo_too_large(message):
        await _safe_reply(
            message,
            "⚠️ Зображення занадто велике (максимальний розмір — 10 МБ).",
        )
        return

    full_query = await _build_query_with_reply_chain(raw_query, message)

    if raw_query:
        await _log_user_query_safely(message, log_custom_text or raw_query)

    image_bytes = await _extract_photo_bytes(message, bot)

    if not full_query and image_bytes:
        full_query = "Проаналізуй це зображення та поясни, що на ньому зображено."
    elif not full_query and not image_bytes:
        await _safe_reply(
            message,
            "⚠️ Не вдалося завантажити зображення для аналізу. Спробуйте надіслати його ще раз.",
        )
        return

    wait_text = (
        "🖼️ Аналізую зображення у Gemini Vision..."
        if image_bytes
        else "🤔 Шукаю та аналізую інформацію через Gemini..."
    )
    wait_msg = await _safe_reply(message, wait_text)

    answer = await gemini_service.ask(
        query=full_query,
        image_bytes=image_bytes,
    )
    await _safe_edit_text(wait_msg, answer)
    await log_bot_message(wait_msg, reply_to_message=message, text_override=answer)


@router.message(Command("ask", ignore_case=True))
@router.message(F.photo & F.caption.regexp(r"^/ask(?:@\S+)?(?:\s|$)", mode="search"))
async def cmd_ask(message: Message, bot: Bot, command: CommandObject | None = None) -> None:
    """Web search or AI explanation via Gemini model (/ask)."""
    if command is not None:
        raw_query = (command.args or "").strip()
    else:
        caption = (message.caption or "").strip()
        bot_username = await _get_bot_username(bot)
        m = re.match(r"^/ask(?:@(\S+))?(?:\s+(.*))?$", caption, flags=re.IGNORECASE | re.DOTALL)
        if not m:
            return
        target_bot = m.group(1)
        if target_bot and bot_username and target_bot.lower() != bot_username.lower():
            return
        raw_query = (m.group(2) or "").strip()

    if not await is_module_enabled(message.chat.id, "ask"):
        await _safe_reply(message, ASK_DISABLED_MSG)
        return

    await process_ask_request(
        message=message,
        query=raw_query,
        bot=bot,
        empty_prompt_hint=(
            "💡 Використання: /ask [запитання], або пінганіть бота через @тег у тексті "
            "чи у відповідь (Reply) на повідомлення."
        ),
        log_custom_text=f"(запитання до бота): {raw_query}" if raw_query else None,
    )


@router.message(BotMentionFilter())
async def handle_bot_mention(message: Message, bot: Bot) -> None:
    """
    Handles bot mention via @username:
    - if mention contains 'summary 4' -> delegates to summary module (if enabled);
    - otherwise -> AI Q&A via ask module (if enabled).
    """
    bot_username = await _get_bot_username(bot)
    raw_text = message.text or message.caption or ""
    cleaned_query = re.sub(rf"(?i)@{re.escape(bot_username)}\b", "", raw_text).strip()

    summary_match = MENTION_SUMMARY_REGEX.match(cleaned_query)
    if summary_match and not message.reply_to_message:
        if not await is_module_enabled(message.chat.id, "summary"):
            await _safe_reply(message, SUMMARY_DISABLED_MSG)
            return
        hours = _parse_summary_hours(summary_match.group(1))
        await execute_summary_for_hours(message, hours)
        return

    if not await is_module_enabled(message.chat.id, "ask"):
        await _safe_reply(message, ASK_DISABLED_MSG)
        return

    await process_ask_request(
        message=message,
        query=cleaned_query,
        bot=bot,
        empty_prompt_hint=(
            "👋 Слухаю! Напишіть запитання після тегу бота або пінганіть мене у відповідь (Reply) "
            "на будь-яке повідомлення чи фото."
        ),
        log_custom_text=cleaned_query if cleaned_query else None,
    )


@router.message(PrivateAskFilter())
async def handle_private_direct_message(message: Message, bot: Bot) -> None:
    """
    In private chat with bot: answers text/photo messages via Gemini
    (or triggers summary on 'суммарі [hours]'), while letting media_dl handle bare video URLs.
    """
    raw_text = (message.text or message.caption or "").strip()

    summary_match = MENTION_SUMMARY_REGEX.match(raw_text)
    if summary_match and not message.reply_to_message:
        if not await is_module_enabled(message.chat.id, "summary"):
            await _safe_reply(message, SUMMARY_DISABLED_MSG)
            return
        hours = _parse_summary_hours(summary_match.group(1))
        await execute_summary_for_hours(message, hours)
        return

    if not await is_module_enabled(message.chat.id, "ask"):
        await _safe_reply(message, ASK_DISABLED_DM_HINT)
        return

    await process_ask_request(
        message=message,
        query=raw_text,
        bot=bot,
        empty_prompt_hint=None,
        log_custom_text=raw_text if raw_text else None,
    )
