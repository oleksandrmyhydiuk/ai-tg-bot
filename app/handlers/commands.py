from datetime import timedelta
from io import BytesIO
import logging
import re
import time
from aiogram import Router, Bot, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import Message
from sqlalchemy import select, func, delete
from app.config import settings
from app.database.engine import async_session_factory
from app.database.models import User, ChatLog, ChatDigest, utc_now
from app.handlers.group_events import log_text_message, log_bot_message
from app.services.context_builder import build_summary_context
from app.services.gemini_client import (
    gemini_service,
    strip_markdown,
)

logger = logging.getLogger(__name__)
router = Router(name="commands_router")
MAX_TELEGRAM_TEXT_LEN = 4000
MAX_TG_LENGTH = MAX_TELEGRAM_TEXT_LEN
MAX_PHOTO_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
DEFAULT_SUMMARY_HOURS = 6.0
MAX_SUMMARY_HOURS = 168.0  # Up to 7 days
USER_COOLDOWN_SECONDS = 8.0  # Gemini free tier rate limit protection against spam

_cached_bot_username: str | None = None
_user_last_request: dict[int, float] = {}

# Regex for invocation via bot mention: "@bot summary 4", "@bot summary 12"
MENTION_SUMMARY_REGEX = re.compile(
    r"^\s*(?:сум+арі|сам+арі|summary)(?:\s+(\d+(?:[.,]\d+)?))?\s*$",
    re.IGNORECASE,
)


def _check_rate_limit(user_id: int | None) -> float:
    """
    Checks user request frequency limit.
    Returns 0.0 if request is permitted, or remaining seconds to wait.
    """
    if user_id is None:
        return 0.0
    now = time.monotonic()
    last_time = _user_last_request.get(user_id, 0.0)
    elapsed = now - last_time
    if elapsed < USER_COOLDOWN_SECONDS:
        return USER_COOLDOWN_SECONDS - elapsed
    _user_last_request[user_id] = now
    # Periodic cleanup of cache to prevent memory growth
    if len(_user_last_request) > 1000:
        cutoff = now - 60.0
        for uid in [k for k, v in _user_last_request.items() if v < cutoff]:
            _user_last_request.pop(uid, None)
    return 0.0


async def _get_bot_username(bot: Bot) -> str:
    """Returns and caches bot username to avoid redundant get_me() requests."""
    global _cached_bot_username
    if _cached_bot_username is None:
        try:
            bot_info = await bot.get_me()
            _cached_bot_username = bot_info.username or ""
        except Exception as e:
            logger.warning("Failed to fetch get_me(): %s", e)
            return ""
    return _cached_bot_username


def _split_text_chunks(text: str, max_len: int = MAX_TELEGRAM_TEXT_LEN) -> list[str]:
    """
    Splits long text into chunks <= max_len characters along paragraph or line boundaries,
    preventing bot report truncation.
    """
    if not text:
        return ["..."]
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text.strip()

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # Look for paragraph break, then newline, then space within max_len
        split_at = remaining.rfind("\n\n", 0, max_len)
        if split_at < max_len // 3:
            split_at = remaining.rfind("\n", 0, max_len)
        if split_at < max_len // 3:
            split_at = remaining.rfind(" ", 0, max_len)
        if split_at <= 0:
            split_at = max_len

        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    return chunks or [text[:max_len]]


async def _safe_answer_text(message: Message, text: str) -> Message:
    """
    Safely responds to message (falling back to answer if reply is unavailable).
    If text exceeds MAX_TELEGRAM_TEXT_LEN, splits into multiple sequential messages.
    """
    clean_text = strip_markdown(text) or "..."
    chunks = _split_text_chunks(clean_text, MAX_TELEGRAM_TEXT_LEN)

    try:
        first_msg = await message.reply(chunks[0])
    except Exception:
        first_msg = await message.answer(chunks[0])

    for extra_chunk in chunks[1:]:
        try:
            await message.answer(extra_chunk)
        except (TelegramBadRequest, Exception) as e:
            logger.warning("Failed to send extra message chunk: %s", e)
            break

    return first_msg


_safe_reply = _safe_answer_text


async def _safe_edit_text(target_message: Message, text: str) -> None:
    """
    Safely strips Markdown and edits message.
    If text exceeds MAX_TELEGRAM_TEXT_LEN, splits into parts:
    edits target_message with the first chunk and sends remaining parts as new messages.
    """
    clean_text = strip_markdown(text) or "..."
    chunks = _split_text_chunks(clean_text, MAX_TELEGRAM_TEXT_LEN)

    try:
        await target_message.edit_text(chunks[0])
    except Exception as e:
        logger.warning("Failed to edit message, sending new message: %s", e)
        try:
            await target_message.answer(chunks[0])
        except (TelegramBadRequest, Exception) as fallback_err:
            logger.error("Failed to fallback send message: %s", fallback_err)
            return

    for extra_chunk in chunks[1:]:
        try:
            await target_message.answer(extra_chunk)
        except (TelegramBadRequest, Exception) as e:
            logger.warning("Failed to send extra chunk of reply: %s", e)
            break


class BotMentionFilter(Filter):
    """Filter triggered when the bot is pinged via @username in text or media caption."""

    async def __call__(self, message: Message, bot: Bot) -> bool:
        raw_text = (message.text or message.caption or "").strip()
        if not raw_text or raw_text.startswith("/"):
            return False
        bot_username = await _get_bot_username(bot)
        if not bot_username:
            return False
        return bool(re.search(rf"(?i)@{re.escape(bot_username)}\b", raw_text))


def _parse_hours(arg_str: str | None) -> float:
    """Parses number of hours from command argument (e.g. '4', '0.5', '12h')."""
    if not arg_str:
        return DEFAULT_SUMMARY_HOURS
    match = re.search(r"(\d+(?:[.,]\d+)?)", arg_str)
    if not match:
        return DEFAULT_SUMMARY_HOURS
    try:
        val = float(match.group(1).replace(",", "."))
        return max(0.1, min(val, MAX_SUMMARY_HOURS))
    except ValueError:
        return DEFAULT_SUMMARY_HOURS


def _format_hours_label(hours: float) -> str:
    if hours.is_integer():
        return f"{int(hours)} год."
    return f"{hours:.1f} год."


def _build_query_with_reply(raw_query: str, message: Message) -> str:
    """Constructs final Gemini prompt incorporating the text of the replied message (Reply)."""
    replied_text = ""
    if message.reply_to_message:
        replied_text = (
            message.reply_to_message.text or message.reply_to_message.caption or ""
        ).strip()

    if raw_query and replied_text:
        return (
            f"Повідомлення, на яке відповіли:\n«{replied_text}»\n\n"
            f"Запит користувача:\n{raw_query}"
        )
    return raw_query or replied_text


def _get_target_photo_message(message: Message) -> Message | None:
    """Returns message containing photo (current message or replied-to message)."""
    if message.photo:
        return message
    if message.reply_to_message and message.reply_to_message.photo:
        return message.reply_to_message
    return None


def _is_photo_too_large(message: Message) -> bool:
    """Checks if photo size exceeds 10 MB limit before downloading."""
    target_msg = _get_target_photo_message(message)
    if not target_msg or not target_msg.photo:
        return False
    largest_photo = target_msg.photo[-1]
    return bool(largest_photo.file_size and largest_photo.file_size > MAX_PHOTO_SIZE_BYTES)


async def _extract_photo_bytes(message: Message, bot: Bot | None = None) -> bytes | None:
    """
    Extracts photo bytes from current or replied-to message.
    Rejects files > 10 MB and safely handles download exceptions.
    """
    target_msg = _get_target_photo_message(message)
    if not target_msg or not target_msg.photo:
        return None

    largest_photo = target_msg.photo[-1]
    if largest_photo.file_size and largest_photo.file_size > MAX_PHOTO_SIZE_BYTES:
        logger.warning(
            "Rejected download of oversized photo (%d bytes > %d bytes).",
            largest_photo.file_size,
            MAX_PHOTO_SIZE_BYTES,
        )
        return None

    active_bot = bot or message.bot
    if active_bot is None:
        return None

    try:
        buffer = BytesIO()
        await active_bot.download(largest_photo, destination=buffer)
        return buffer.getvalue()
    except Exception as e:
        logger.warning("Failed to download photo from Telegram: %s", e)
        return None


async def _log_user_query_safely(message: Message, custom_text: str) -> None:
    """Safely logs user query to chat history."""
    try:
        await log_text_message(message, custom_text=custom_text)
    except TypeError:
        await log_text_message(message)
    except Exception as e:
        logger.warning("Failed to log user query: %s", e)


async def _process_ask_request(
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
    4) Invokes Gemini/Gemma and logs bot response in ChatLog for chat context.
    """
    raw_query = (query or "").strip()
    full_query = _build_query_with_reply(raw_query, message)
    has_photo = _get_target_photo_message(message) is not None

    if not full_query and not has_photo:
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


async def _execute_summary_for_hours(message: Message, hours: float) -> None:
    """
    Generates verified summary for last `hours` hours via build_summary_context
    with message ID citations, reply-chain context, and source links.
    """
    wait_sec = _check_rate_limit(message.from_user.id if message.from_user else None)
    if wait_sec > 0:
        await _safe_reply(
            message,
            f"⏳ Зачекайте {int(wait_sec) + 1} сек. перед наступним запитом до ШІ.",
        )
        return

    hours_label = _format_hours_label(hours)
    cutoff_utc = utc_now() - timedelta(hours=hours)
    chat_id = message.chat.id

    try:
        async with async_session_factory() as session:
            bundle = await build_summary_context(session, chat_id, cutoff_utc, hours)
            total_in_db = 0
            if bundle.message_count == 0 and bundle.digest_count == 0:
                total_stmt = select(func.count(ChatLog.message_id)).where(
                    ChatLog.chat_id == chat_id
                )
                total_in_db = (await session.execute(total_stmt)).scalar_one() or 0
    except Exception as e:
        logger.exception("Database error during /summary: %s", e)
        await _safe_reply(
            message,
            "⚠️ Виникла помилка при читанні історії повідомлень з бази даних.",
        )
        return

    if bundle.message_count == 0 and bundle.digest_count == 0:
        if total_in_db == 0:
            await _safe_reply(
                message,
                "📭 У базі ще немає жодного збереженого повідомлення для цього чату.\n\n"
                "💡 Щоб бот міг читати повідомлення у групі:\n"
                "1. Зробіть бота адміністратором групи.\n"
                "2. У @BotFather виконайте /setprivacy -> оберіть бота -> натисніть Disable.",
            )
        else:
            await _safe_reply(
                message,
                f"📭 За останні {hours_label} у чаті не було нових повідомлень.\n"
                "Спробуйте вказати більший період, наприклад: /summary 24",
            )
        return

    desc_parts = []
    if bundle.digest_count:
        desc_parts.append(f"погодинних дайджестів: {bundle.digest_count}")
    if bundle.message_count:
        desc_parts.append(f"повідомлень: {bundle.message_count}")
    total_items_desc = ", ".join(desc_parts)

    wait_msg = await _safe_reply(
        message,
        f"⏳ Формую звіт за останні {hours_label} ({total_items_desc})...",
    )

    header_note = None
    if bundle.was_truncated:
        header_note = (
            f"Враховано останні {bundle.message_count} із "
            f"{bundle.total_available_messages} повідомлень."
        )

    try:
        rendered_summary, _ = await gemini_service.generate_verified_summary(
            bundle=bundle,
            chat_id=chat_id,
            chat_username=message.chat.username,
            show_citations=getattr(settings, "summary_show_citations", True),
            header_note=header_note,
        )
        report = (
            f"📋 Підсумок чату за останні {hours_label} ({total_items_desc}):\n\n"
            f"{rendered_summary}"
        )
    except Exception as e:
        logger.error("Error generating verified summary for chat %s: %s", chat_id, e)
        fallback_body = "\n".join(bundle.raw_fallback_lines)[:3500]
        report = (
            f"⚠️ Не вдалося об'єднати звіт через ліміт API. "
            f"Збережені дані за останні {hours_label}:\n\n{fallback_body}"
        )

    await _safe_edit_text(wait_msg, report)


@router.message(Command("start", "help", ignore_case=True))
async def cmd_start_or_help(message: Message, bot: Bot) -> None:
    """Displays help message with full list of commands and bot capabilities."""
    bot_username = await _get_bot_username(bot)
    mention_example = f"@{bot_username}" if bot_username else "@bot"
    help_text = (
        "🤖 Вітаю! Я ШІ-асистент групового чату на базі Gemini 3 та Gemma 4 API.\n\n"
        "📌 ПЕРЕЛІК КОМАНД:\n"
        "• /start або /help — Показати це меню з описом команд та функцій\n"
        "• /summary — Підсумок обговорень у чаті за останні 6 годин (із джерелами)\n"
        "• /summary [години] — Підсумок чату за вказану кількість годин (наприклад: /summary 4 або /summary 24)\n"
        "• /ask [запитання] — Пошук інформації в інтернеті або пояснення через модель Gemini 3\n"
        "• /stats — Статистика повідомлень чату та топ активних учасників\n"
        "• /clear — Очистити збережену історію чату (доступно адміністраторам)\n\n"
        "💡 РОБОТА ЧЕРЕЗ ПІНГ (@тег) ТА РЕПЛАЇ:\n"
        f"• Пінг із питанням: напишіть «{mention_example} що таке VLAN?» — я знайду відповідь в інтернеті або поясню.\n"
        f"• Пінг для звіту: напишіть «{mention_example} суммарі 4» — згенерую звіт за останні 4 години.\n"
        f"• Пінг у відповідь (Reply): зробіть Reply на будь-яке чуже повідомлення чи фото і напишіть «{mention_example}» "
        f"(або «{mention_example} поясни простіше»).\n"
        "• В особистих повідомленнях: пишіть запитання або надсилайте фото напряму без команд."
    )
    await _safe_reply(message, help_text)


@router.message(Command("summary", ignore_case=True))
async def cmd_summary(message: Message, command: CommandObject) -> None:
    """Generates summary for last N hours (e.g. /summary 4 or /summary)."""
    hours = _parse_hours(command.args)
    await _execute_summary_for_hours(message, hours)


@router.message(Command("stats", ignore_case=True))
async def cmd_stats(message: Message) -> None:
    """Displays chat statistics for last 24 hours and all time."""
    cutoff_24h = utc_now() - timedelta(hours=24)

    try:
        async with async_session_factory() as session:
            total_msgs = (
                await session.execute(
                    select(func.count(ChatLog.message_id)).where(ChatLog.chat_id == message.chat.id)
                )
            ).scalar_one() or 0

            msgs_24h = (
                await session.execute(
                    select(func.count(ChatLog.message_id)).where(
                        ChatLog.chat_id == message.chat.id,
                        ChatLog.timestamp >= cutoff_24h,
                    )
                )
            ).scalar_one() or 0

            total_digests = (
                await session.execute(
                    select(func.count(ChatDigest.id)).where(ChatDigest.chat_id == message.chat.id)
                )
            ).scalar_one() or 0

            top_users_stmt = (
                select(User.full_name, User.username, func.count(ChatLog.message_id).label("msg_count"))
                .join(ChatLog, ChatLog.user_id == User.user_id)
                .where(ChatLog.chat_id == message.chat.id)
                .group_by(User.user_id)
                .order_by(func.count(ChatLog.message_id).desc())
                .limit(5)
            )
            top_users = (await session.execute(top_users_stmt)).all()
    except Exception as e:
        logger.exception("Error retrieving statistics for chat %s: %s", message.chat.id, e)
        await _safe_reply(message, "⚠️ Виникла помилка при отриманні статистики з бази даних.")
        return

    top_users_text = (
        "\n".join(
            f"{idx}. {u.full_name} (@{u.username or 'без_ніку'}) — {u.msg_count} повід."
            for idx, u in enumerate(top_users, start=1)
        )
        if top_users
        else "Повідомлень ще немає."
    )

    stats_text = (
        "📊 Статистика цього чату:\n"
        f"• Повідомлень за останні 24 години: {msgs_24h}\n"
        f"• Усього повідомлень у базі: {total_msgs}\n"
        f"• Погодинних дайджестів у кеші: {total_digests}\n\n"
        f"🏆 Топ-5 активних учасників:\n{top_users_text}"
    )
    await _safe_reply(message, stats_text)


@router.message(Command("clear", "clear_memory", ignore_case=True))
async def cmd_clear_memory(message: Message, bot: Bot) -> None:
    """Clears stored chat message history (in groups: administrators only)."""
    if message.chat.type in ("group", "supergroup"):
        is_anonymous_group_admin = bool(
            message.sender_chat and message.sender_chat.id == message.chat.id
        )
        if not is_anonymous_group_admin:
            if not message.from_user:
                await _safe_reply(
                    message,
                    "⛔ Очищувати історію чату можуть лише адміністратори групи.",
                )
                return
            try:
                member = await bot.get_chat_member(message.chat.id, message.from_user.id)
                if member.status not in ("administrator", "creator"):
                    await _safe_reply(
                        message,
                        "⛔ Очищувати історію чату можуть лише адміністратори групи.",
                    )
                    return
            except Exception as e:
                logger.warning("Failed to verify administrator status: %s", e)
                await _safe_reply(message, "⚠️ Не вдалося перевірити права адміністратора.")
                return

    try:
        async with async_session_factory() as session:
            await session.execute(delete(ChatLog).where(ChatLog.chat_id == message.chat.id))
            await session.execute(delete(ChatDigest).where(ChatDigest.chat_id == message.chat.id))
            await session.commit()
    except Exception as e:
        logger.exception("Error clearing chat history %s: %s", message.chat.id, e)
        await _safe_reply(message, "⚠️ Виникла помилка при очищенні історії чату з бази даних.")
        return

    await _safe_reply(message, "🧹 Історію повідомлень та дайджестів цього чату повністю очищено!")


@router.message(Command("ask", ignore_case=True))
@router.message(F.photo & F.caption.regexp(r"^/ask(?:@\S+)?(?:\s|$)", mode="search"))
async def cmd_ask(message: Message, bot: Bot, command: CommandObject | None = None) -> None:
    """Web search or AI explanation via Gemini 3 model (/ask)."""
    if command is not None:
        raw_query = (command.args or "").strip()
    else:
        caption = (message.caption or "").strip()
        bot_username = await _get_bot_username(bot)
        # Verify /ask@OtherBot in photo caption is not intercepted
        m = re.match(r"^/ask(?:@(\S+))?(?:\s+(.*))?$", caption, flags=re.IGNORECASE | re.DOTALL)
        if not m:
            return
        target_bot = m.group(1)
        if target_bot and bot_username and target_bot.lower() != bot_username.lower():
            return
        raw_query = (m.group(2) or "").strip()

    await _process_ask_request(
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
    - if mention contains 'summary 4' -> generates 4-hour summary;
    - if contains a question or reply to message/photo -> searches web or explains via Gemini.
    """
    bot_username = await _get_bot_username(bot)
    raw_text = message.text or message.caption or ""

    cleaned_query = re.sub(rf"(?i)@{re.escape(bot_username)}\b", "", raw_text).strip()

    # If user pinged the bot with summary intent: "@bot summary 4"
    summary_match = MENTION_SUMMARY_REGEX.match(cleaned_query)
    if summary_match and not message.reply_to_message:
        hours = _parse_hours(summary_match.group(1))
        await _execute_summary_for_hours(message, hours)
        return

    await _process_ask_request(
        message=message,
        query=cleaned_query,
        bot=bot,
        empty_prompt_hint=(
            "👋 Слухаю! Напишіть запитання після тегу бота або пінганіть мене у відповідь (Reply) "
            "на будь-яке повідомлення чи фото."
        ),
        log_custom_text=cleaned_query if cleaned_query else None,
    )


@router.message(F.chat.type == "private", F.text | F.photo)
async def handle_private_direct_message(message: Message, bot: Bot) -> None:
    """
    In private chat with bot: if user sends message or photo without commands,
    bot immediately answers via Gemini (and logs text).
    """
    raw_text = (message.text or message.caption or "").strip()
    if raw_text.startswith("/"):
        return

    # If private chat received 'summary 4'
    summary_match = MENTION_SUMMARY_REGEX.match(raw_text)
    if summary_match and not message.reply_to_message:
        hours = _parse_hours(summary_match.group(1))
        await _execute_summary_for_hours(message, hours)
        return

    await _process_ask_request(
        message=message,
        query=raw_text,
        bot=bot,
        empty_prompt_hint=None,
        log_custom_text=raw_text if raw_text else None,
    )
