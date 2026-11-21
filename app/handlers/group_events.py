import logging
from aiogram import Router, F
from aiogram.types import Message
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert
from app.database.engine import async_session_factory
from app.database.models import User, ChatLog, utc_now
from app.modules.registry import chat_requires_logging

logger = logging.getLogger(__name__)
router = Router(name="group_logger_router")

SUPPORTED_CONTENT_FILTER = (
    F.text
    | F.caption
    | F.photo
    | F.video
    | F.voice
    | F.video_note
    | F.audio
    | F.document
    | F.sticker
    | F.poll
    | F.animation
    | F.location
)

_SKIPPED_COMMANDS = (
    "/clear",
    "/summary",
    "/tasks",
    "/stats",
    "/start",
    "/help",
    "/dl",
    "/download",
    "/find",
    "/transcribe",
    "/digest",
)


def _is_service_message(message: Message) -> bool:
    """Checks if message is a Telegram service event."""
    service_attrs = (
        "new_chat_members",
        "left_chat_member",
        "new_chat_title",
        "new_chat_photo",
        "delete_chat_photo",
        "group_chat_created",
        "supergroup_chat_created",
        "channel_chat_created",
        "message_auto_delete_timer_changed",
        "pinned_message",
        "forum_topic_created",
        "forum_topic_edited",
        "forum_topic_closed",
        "forum_topic_reopened",
        "general_forum_topic_hidden",
        "general_forum_topic_unhidden",
        "video_chat_scheduled",
        "video_chat_started",
        "video_chat_ended",
        "video_chat_participants_invited",
    )
    return any(getattr(message, attr, None) is not None for attr in service_attrs)


def _extract_media_info(message: Message) -> tuple[str | None, str]:
    """
    Determines concise media type (media_type) and fallback textual label (fallback_label)
    if the message contains no text caption.
    """
    if getattr(message, "photo", None):
        return "photo", "[фото]"
    if getattr(message, "video", None):
        return "video", "[відео]"
    if getattr(message, "voice", None):
        return "voice", "[голосове повідомлення]"
    if getattr(message, "video_note", None):
        return "video_note", "[відеоповідомлення]"
    if getattr(message, "animation", None):
        return "animation", "[анімація]"
    if getattr(message, "audio", None):
        audio = message.audio
        title = getattr(audio, "title", None) or getattr(audio, "file_name", None)
        return "audio", f"[аудіо: {title}]" if title else "[аудіо]"
    if getattr(message, "document", None):
        doc = message.document
        fname = getattr(doc, "file_name", None)
        return "document", f"[документ: {fname}]" if fname else "[документ]"
    if getattr(message, "sticker", None):
        emoji = (getattr(message.sticker, "emoji", None) or "").strip()
        return "sticker", f"[стікер {emoji}]".strip() if emoji else "[стікер]"
    if getattr(message, "poll", None):
        question = (getattr(message.poll, "question", None) or "").strip()
        return "poll", f"[опитування: {question}]" if question else "[опитування]"
    if getattr(message, "location", None):
        return "location", "[геолокація]"
    return None, ""


def _extract_forward_origin(message: Message) -> str | None:
    """Extracts concise description of forwarded message origin (up to 160 characters)."""
    fo = getattr(message, "forward_origin", None)
    if fo is not None:
        fo_type = getattr(fo, "type", "")
        chat_obj = getattr(fo, "chat", None) or getattr(fo, "sender_chat", None)
        if fo_type == "channel" or (chat_obj is not None and getattr(chat_obj, "type", "") == "channel"):
            title = getattr(chat_obj, "title", None) or getattr(chat_obj, "username", None) or "канал"
            return f"канал: {title}"[:160]
        if chat_obj is not None:
            title = getattr(chat_obj, "title", None) or getattr(chat_obj, "username", None) or "чат"
            return f"чат: {title}"[:160]
        sender_user = getattr(fo, "sender_user", None)
        if sender_user is not None:
            name = getattr(sender_user, "full_name", None) or getattr(sender_user, "username", None) or "користувач"
            return f"користувач: {name}"[:160]
        sender_user_name = getattr(fo, "sender_user_name", None)
        if sender_user_name:
            return f"користувач: {sender_user_name}"[:160]

    fwd_chat = getattr(message, "forward_from_chat", None)
    if fwd_chat is not None:
        title = getattr(fwd_chat, "title", None) or getattr(fwd_chat, "username", None) or "канал"
        return f"канал: {title}"[:160]

    fwd_user = getattr(message, "forward_from", None)
    if fwd_user is not None:
        name = getattr(fwd_user, "full_name", None) or getattr(fwd_user, "username", None) or "користувач"
        return f"користувач: {name}"[:160]

    fwd_name = getattr(message, "forward_sender_name", None)
    if fwd_name:
        return f"користувач: {fwd_name}"[:160]

    return None


def _is_forum_topic_root_reply(message: Message, reply: Message) -> bool:
    """
    Checks if reply_to_message is a technical link to forum topic root rather than a user reply.
    """
    if getattr(reply, "forum_topic_created", None) is not None:
        return True
    if getattr(reply, "forum_topic_edited", None) is not None:
        return True
    thread_id = getattr(message, "message_thread_id", None)
    if (
        getattr(message, "is_topic_message", False)
        and thread_id is not None
        and reply.message_id == thread_id
        and not (getattr(reply, "text", None) or getattr(reply, "caption", None))
        and _extract_media_info(reply)[0] is None
    ):
        return True
    return False


def _extract_reply_metadata(
    message: Message,
    explicit_reply: Message | None = None,
) -> tuple[int | None, int | None, str | None, str | None]:
    """
    Returns (reply_to_message_id, reply_to_user_id, reply_to_author, reply_to_snippet).
    For topic root messages, returns (None, None, None, None).
    """
    reply = explicit_reply or getattr(message, "reply_to_message", None)
    if reply is None:
        return None, None, None, None

    if explicit_reply is None and _is_forum_topic_root_reply(message, reply):
        return None, None, None, None

    reply_msg_id = reply.message_id
    reply_user_id: int | None = None
    reply_author: str | None = None

    if getattr(reply, "sender_chat", None) is not None:
        reply_author = (
            getattr(reply.sender_chat, "title", None)
            or getattr(reply.sender_chat, "username", None)
            or "Анонімний адмін"
        )[:128]
    elif getattr(reply, "from_user", None) is not None:
        reply_user_id = reply.from_user.id
        if getattr(reply.from_user, "is_bot", False):
            reply_author = (reply.from_user.full_name or "Бот")[:128]
        else:
            reply_author = (
                reply.from_user.full_name
                or reply.from_user.username
                or "Учасник"
            )[:128]

    raw_reply_text = (getattr(reply, "text", None) or getattr(reply, "caption", None) or "").strip()
    if not raw_reply_text:
        _, fallback_media_tag = _extract_media_info(reply)
        raw_reply_text = fallback_media_tag

    reply_snippet = raw_reply_text[:240] if raw_reply_text else None
    return reply_msg_id, reply_user_id, reply_author, reply_snippet


async def _upsert_message_record(
    message: Message,
    custom_text: str | None = None,
    is_edit: bool = False,
) -> None:
    """Internal helper function to persist or update a message record in ChatLog."""
    if _is_service_message(message):
        return

    sender_chat = getattr(message, "sender_chat", None)
    from_user = getattr(message, "from_user", None)

    # Ignore standard external bots, but allow anonymous admins and connected channel posts (where sender_chat is present)
    if sender_chat is None:
        if from_user is None or getattr(from_user, "is_bot", False):
            return

    media_type, media_fallback_text = _extract_media_info(message)
    raw_msg_text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()

    if custom_text is None:
        if raw_msg_text.startswith("/"):
            lower_cmd = raw_msg_text.split()[0].lower()
            if any(lower_cmd == cmd or lower_cmd.startswith(f"{cmd}@") for cmd in _SKIPPED_COMMANDS):
                return
            return
        content_text = raw_msg_text or media_fallback_text
    else:
        content_text = custom_text.strip()

    if not content_text:
        return

    now_utc = utc_now()

    user_id: int | None = None
    username: str | None = None
    full_name: str | None = None
    sender_name: str | None = None

    if sender_chat is not None:
        sender_name = (
            getattr(sender_chat, "title", None)
            or getattr(sender_chat, "username", None)
            or "Анонімний адмін"
        )[:128]
        # If not system anonymous bot, but real user
        if from_user is not None and not getattr(from_user, "is_bot", False):
            user_id = from_user.id
            username = from_user.username
            full_name = (from_user.full_name or sender_name)[:128]
    elif from_user is not None:
        user_id = from_user.id
        username = from_user.username
        full_name = (from_user.full_name or from_user.username or "Unknown")[:128]
        sender_name = full_name

    reply_msg_id, reply_user_id, reply_author, reply_snippet = _extract_reply_metadata(message)

    quote_obj = getattr(message, "quote", None)
    quote_text: str | None = None
    if quote_obj is not None and getattr(quote_obj, "text", None):
        quote_text = quote_obj.text.strip()[:240] or None

    message_thread_id = getattr(message, "message_thread_id", None)
    forward_origin = _extract_forward_origin(message)

    try:
        async with async_session_factory() as session:
            if user_id is not None and full_name is not None:
                user_stmt = sqlite_upsert(User).values(
                    user_id=user_id,
                    username=username,
                    full_name=full_name,
                    updated_at=now_utc,
                ).on_conflict_do_update(
                    index_elements=[User.user_id],
                    set_={
                        "username": username,
                        "full_name": full_name,
                        "updated_at": now_utc,
                    },
                )
                await session.execute(user_stmt)

            insert_values = {
                "chat_id": message.chat.id,
                "message_id": message.message_id,
                "user_id": user_id,
                "text": content_text,
                "timestamp": now_utc,
                "is_processed": False,
                "reply_to_message_id": reply_msg_id,
                "reply_to_user_id": reply_user_id,
                "reply_to_author": reply_author,
                "reply_to_snippet": reply_snippet,
                "quote_text": quote_text,
                "message_thread_id": message_thread_id,
                "media_type": media_type,
                "forward_origin": forward_origin,
                "sender_name": sender_name,
                "is_bot_message": False,
                "edited_at": now_utc if is_edit else None,
            }

            update_set: dict[str, object] = {
                "text": content_text,
                "reply_to_message_id": reply_msg_id,
                "reply_to_user_id": reply_user_id,
                "reply_to_author": reply_author,
                "reply_to_snippet": reply_snippet,
                "quote_text": quote_text,
                "message_thread_id": message_thread_id,
                "media_type": media_type,
                "forward_origin": forward_origin,
                "sender_name": sender_name,
            }
            if is_edit:
                # When editing message, save edited_at and DO NOT reset is_processed!
                update_set["edited_at"] = now_utc

            log_stmt = sqlite_upsert(ChatLog).values(**insert_values).on_conflict_do_update(
                index_elements=[ChatLog.chat_id, ChatLog.message_id],
                set_=update_set,
            )

            await session.execute(log_stmt)
            await session.commit()
    except Exception as e:
        logger.exception("Failed to save message to DB (chat_id=%s): %s", message.chat.id, e)


@router.message(SUPPORTED_CONTENT_FILTER)
async def log_text_message(
    message: Message,
    custom_text: str | None = None,
    is_edit: bool = False,
) -> None:
    """Asynchronously saves message text, media fallback, replies, quotes, and metadata to DB."""
    if message is None or getattr(message, "chat", None) is None:
        return
    if not await chat_requires_logging(message.chat.id):
        return
    await _upsert_message_record(message, custom_text=custom_text, is_edit=is_edit)


@router.edited_message(SUPPORTED_CONTENT_FILTER)
async def log_edited_message(message: Message) -> None:
    """Updates text and metadata of edited message without resetting is_processed flag."""
    if message is None or getattr(message, "chat", None) is None:
        return
    if not await chat_requires_logging(message.chat.id):
        return
    await _upsert_message_record(message, is_edit=True)


async def log_bot_message(
    bot_message: Message,
    reply_to_message: Message | None = None,
    text_override: str | None = None,
    force: bool = False,
) -> None:
    """
    Saves bot's own response into ChatLog (with is_bot_message=True and sender_name='Bot')
    so subsequent user replies and /summary have full conversational context.
    """
    try:
        if bot_message is None or getattr(bot_message, "chat", None) is None:
            return
        if not force and not await chat_requires_logging(bot_message.chat.id):
            return

        content_text = (
            text_override
            or getattr(bot_message, "text", None)
            or getattr(bot_message, "caption", None)
            or ""
        ).strip()[:4000]
        if not content_text:
            return

        now_utc = utc_now()
        from_user = getattr(bot_message, "from_user", None)
        bot_user_id = from_user.id if from_user is not None else None
        bot_username = getattr(from_user, "username", None) if from_user is not None else None
        bot_full_name = (
            (getattr(from_user, "full_name", None) or "Бот")[:128]
            if from_user is not None
            else "Бот"
        )

        reply_msg_id, reply_user_id, reply_author, reply_snippet = _extract_reply_metadata(
            bot_message,
            explicit_reply=reply_to_message,
        )
        thread_id = getattr(bot_message, "message_thread_id", None) or (
            getattr(reply_to_message, "message_thread_id", None)
            if reply_to_message is not None
            else None
        )

        async with async_session_factory() as session:
            if bot_user_id is not None:
                user_stmt = sqlite_upsert(User).values(
                    user_id=bot_user_id,
                    username=bot_username,
                    full_name=bot_full_name,
                    updated_at=now_utc,
                ).on_conflict_do_update(
                    index_elements=[User.user_id],
                    set_={
                        "username": bot_username,
                        "full_name": bot_full_name,
                        "updated_at": now_utc,
                    },
                )
                await session.execute(user_stmt)

            log_stmt = sqlite_upsert(ChatLog).values(
                chat_id=bot_message.chat.id,
                message_id=bot_message.message_id,
                user_id=bot_user_id,
                text=content_text,
                timestamp=now_utc,
                is_processed=False,
                reply_to_message_id=reply_msg_id,
                reply_to_user_id=reply_user_id,
                reply_to_author=reply_author,
                reply_to_snippet=reply_snippet,
                quote_text=None,
                message_thread_id=thread_id,
                media_type=None,
                forward_origin=None,
                sender_name="Бот",
                is_bot_message=True,
                edited_at=None,
            ).on_conflict_do_update(
                index_elements=[ChatLog.chat_id, ChatLog.message_id],
                set_={
                    "text": content_text,
                    "reply_to_message_id": reply_msg_id,
                    "reply_to_user_id": reply_user_id,
                    "reply_to_author": reply_author,
                    "reply_to_snippet": reply_snippet,
                    "message_thread_id": thread_id,
                    "sender_name": "Бот",
                    "is_bot_message": True,
                },
            )
            await session.execute(log_stmt)
            await session.commit()
    except Exception as e:
        logger.warning(
            "Failed to save bot response to ChatLog (chat_id=%s): %s",
            getattr(getattr(bot_message, "chat", None), "id", None),
            e,
        )
