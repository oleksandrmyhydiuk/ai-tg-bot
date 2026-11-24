from __future__ import annotations

import asyncio
from collections import Counter
from datetime import timedelta, UTC
from io import BytesIO
import logging
import re

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message
from sqlalchemy import func, select

from app.database.engine import async_session_factory
from app.database.models import ChatDigest, ChatLog, User, utc_now
from app.modules.registry import is_module_enabled
from app.services.context_builder import KYIV_TZ
from app.services.stats_renderer import (
    ParticipantStat,
    StatsCardData,
    render_stats_leaderboard,
)
from app.services.summary_verifier import format_message_link

logger = logging.getLogger(__name__)
router = Router(name="stats")

DEFAULT_STATS_HOURS = 24
MAX_STATS_HOURS = 720  # up to 30 days
TOP_PARTICIPANTS_LIMIT = 8
AVATAR_FETCH_TIMEOUT_SECONDS = 3.0
MAX_CAPTION_LEN = 1024


async def _reply_safely(message: Message, text: str) -> Message:
    try:
        return await message.reply(text)
    except Exception:
        return await message.answer(text)


def _parse_stats_period(raw_arg: str | None) -> tuple[int, str]:
    """
    Parses period argument such as '24h', '7d', '168h', '48', or None.
    Returns (hours, human_label).
    """
    if not raw_arg or not raw_arg.strip():
        return DEFAULT_STATS_HOURS, "24 год."

    token = raw_arg.strip().lower().split()[0]
    match = re.fullmatch(r"(\d+)\s*([hdдг])?", token)
    if not match:
        return DEFAULT_STATS_HOURS, "24 год."

    value = int(match.group(1))
    unit = match.group(2)

    if unit in ("d", "д"):
        hours = value * 24
    else:
        hours = value

    hours = max(1, min(hours, MAX_STATS_HOURS))
    if hours % 24 == 0 and hours >= 48:
        days = hours // 24
        return hours, f"{days} дн."
    return hours, f"{hours} год."


def _truncate_snippet(text: str | None, max_len: int = 80) -> str:
    clean = re.sub(r"\s+", " ", (text or "")).strip()
    if not clean:
        return "(без тексту)"
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 3].rstrip() + "..."


async def _fetch_user_avatar(bot: Bot | None, user_id: int) -> bytes | None:
    """Safely fetches a user's smallest profile photo bytes with a short timeout."""
    if bot is None or not user_id:
        return None
    try:
        photos_result = await asyncio.wait_for(
            bot.get_user_profile_photos(user_id, limit=1),
            timeout=AVATAR_FETCH_TIMEOUT_SECONDS,
        )
        photos = getattr(photos_result, "photos", None)
        if not photos or not photos[0]:
            return None
        smallest_photo = photos[0][0]
        file_id = getattr(smallest_photo, "file_id", None)
        if not file_id:
            return None

        buf = BytesIO()
        downloaded = await asyncio.wait_for(
            bot.download(file_id, destination=buf),
            timeout=AVATAR_FETCH_TIMEOUT_SECONDS,
        )
        data = buf.getvalue()
        if not data and isinstance(downloaded, BytesIO):
            data = downloaded.getvalue()
        return data or None
    except Exception as exc:
        logger.debug("Could not fetch avatar for user %s: %s", user_id, exc)
        return None


def _build_compact_caption(
    *,
    period_label: str,
    msgs_in_period: int,
    total_msgs: int,
    total_digests: int,
    peak_hour_text: str,
    replied_section: str,
) -> str:
    caption = (
        f"📊 Статистика чату за останні {period_label}\n"
        f"• Повідомлень: {msgs_in_period} | У базі: {total_msgs} | Дайджестів: {total_digests}\n"
        f"• Пік активності: {peak_hour_text}"
        f"{replied_section}"
    )
    if len(caption) > MAX_CAPTION_LEN:
        caption = caption[: MAX_CAPTION_LEN - 3].rstrip() + "..."
    return caption


@router.message(Command("stats", ignore_case=True))
async def cmd_stats(message: Message, command: CommandObject) -> None:
    """Displays enhanced chat statistics for 24h, 7d, or custom hours (/stats [24h|7d])."""
    chat_id = message.chat.id
    if not await is_module_enabled(chat_id, "stats"):
        await _reply_safely(
            message,
            "Модуль статистики вимкнено в цьому чаті. Увімкнути: /start",
        )
        return

    hours, period_label = _parse_stats_period(command.args)
    cutoff_utc = utc_now() - timedelta(hours=hours)

    try:
        async with async_session_factory() as session:
            msgs_in_period = (
                await session.execute(
                    select(func.count(ChatLog.message_id)).where(
                        ChatLog.chat_id == chat_id,
                        ChatLog.timestamp >= cutoff_utc,
                    )
                )
            ).scalar_one() or 0

            total_msgs = (
                await session.execute(
                    select(func.count(ChatLog.message_id)).where(
                        ChatLog.chat_id == chat_id
                    )
                )
            ).scalar_one() or 0

            total_digests = (
                await session.execute(
                    select(func.count(ChatDigest.id)).where(
                        ChatDigest.chat_id == chat_id
                    )
                )
            ).scalar_one() or 0

            top_users_stmt = (
                select(
                    User.user_id,
                    User.full_name,
                    User.username,
                    func.count(ChatLog.message_id).label("msg_count"),
                )
                .join(ChatLog, ChatLog.user_id == User.user_id)
                .where(
                    ChatLog.chat_id == chat_id,
                    ChatLog.timestamp >= cutoff_utc,
                    ChatLog.is_bot_message.is_(False),
                )
                .group_by(User.user_id)
                .order_by(func.count(ChatLog.message_id).desc())
                .limit(TOP_PARTICIPANTS_LIMIT)
            )
            top_users = (await session.execute(top_users_stmt)).all()

            ts_stmt = select(ChatLog.timestamp).where(
                ChatLog.chat_id == chat_id,
                ChatLog.timestamp >= cutoff_utc,
            )
            timestamps = (await session.execute(ts_stmt)).scalars().all()

            top_replied_stmt = (
                select(
                    ChatLog.reply_to_message_id,
                    func.count(ChatLog.message_id).label("reply_count"),
                )
                .where(
                    ChatLog.chat_id == chat_id,
                    ChatLog.timestamp >= cutoff_utc,
                    ChatLog.reply_to_message_id.is_not(None),
                )
                .group_by(ChatLog.reply_to_message_id)
                .order_by(func.count(ChatLog.message_id).desc())
                .limit(3)
            )
            top_replied = (await session.execute(top_replied_stmt)).all()

            target_msg_map: dict[int, tuple[ChatLog, User | None]] = {}
            replied_ids = [
                int(row.reply_to_message_id)
                for row in top_replied
                if row.reply_to_message_id is not None
            ]
            if replied_ids:
                target_stmt = (
                    select(ChatLog, User)
                    .outerjoin(User, ChatLog.user_id == User.user_id)
                    .where(
                        ChatLog.chat_id == chat_id,
                        ChatLog.message_id.in_(replied_ids),
                    )
                )
                for target_log, target_user in (await session.execute(target_stmt)).all():
                    target_msg_map[target_log.message_id] = (target_log, target_user)
    except Exception as exc:
        logger.exception("Error retrieving statistics for chat %s: %s", chat_id, exc)
        await _reply_safely(
            message,
            "⚠️ Виникла помилка при отриманні статистики з бази даних.",
        )
        return

    # Peak activity hour in Europe/Kyiv
    peak_hour_text = "немає даних"
    peak_hour_card_label: str | None = None
    if timestamps:
        hour_counts: Counter[int] = Counter()
        for dt in timestamps:
            if dt is None:
                continue
            aware = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
            kyiv_hour = aware.astimezone(KYIV_TZ).hour
            hour_counts[kyiv_hour] += 1
        if hour_counts:
            peak_hour, peak_cnt = hour_counts.most_common(1)[0]
            next_hour = (peak_hour + 1) % 24
            peak_hour_text = f"{peak_hour:02d}:00–{next_hour:02d}:00 Київ ({peak_cnt} повід.)"
            peak_hour_card_label = f"{peak_hour:02d}:00–{next_hour:02d}:00 ({peak_cnt})"

    if top_users:
        top_users_lines = [
            f"{idx}. {row.full_name}"
            + (f" (@{row.username})" if row.username else "")
            + f" — {row.msg_count} повід."
            for idx, row in enumerate(top_users, start=1)
        ]
        top_users_text = "\n".join(top_users_lines)
    else:
        top_users_text = "Повідомлень за цей період ще немає."

    replied_section = ""
    if top_replied:
        replied_lines = ["💬 Топ-3 найбільш обговорюваних повідомлень:"]
        for idx, row in enumerate(top_replied, start=1):
            target_id = int(row.reply_to_message_id)
            link = format_message_link(chat_id, target_id, message.chat.username)
            target_pair = target_msg_map.get(target_id)
            if target_pair is not None:
                t_log, t_user = target_pair
                author = (
                    t_user.full_name.strip()
                    if t_user and t_user.full_name
                    else (t_log.sender_name or f"User#{t_log.user_id or 0}")
                )
                snippet = _truncate_snippet(t_log.text, 70)
                replied_lines.append(
                    f"{idx}. {author}: «{snippet}» ({row.reply_count} відп.) — {link}"
                )
            else:
                replied_lines.append(
                    f"{idx}. Повідомлення #{target_id} ({row.reply_count} відп.) — {link}"
                )
        replied_section = "\n\n" + "\n".join(replied_lines)

    stats_text = (
        f"📊 Статистика чату за останні {period_label}:\n"
        f"• Повідомлень за період: {msgs_in_period}\n"
        f"• Усього збережено в базі: {total_msgs}\n"
        f"• Погодинних дайджестів у кеші: {total_digests}\n"
        f"• Пік активності: {peak_hour_text}\n\n"
        f"🏆 Топ-{len(top_users) if top_users else 8} активних учасників:\n{top_users_text}"
        f"{replied_section}"
    )

    if top_users and callable(getattr(message, "reply_photo", None)):
        try:
            bot = getattr(message, "bot", None)
            if bot is not None:
                avatars = list(
                    await asyncio.gather(
                        *(_fetch_user_avatar(bot, int(row.user_id)) for row in top_users),
                    )
                )
            else:
                avatars = [None] * len(top_users)

            participants = [
                ParticipantStat(
                    user_id=int(row.user_id),
                    display_name=(row.full_name or "").strip()
                    or (f"@{row.username}" if row.username else f"User#{row.user_id}"),
                    message_count=int(row.msg_count),
                    avatar_bytes=avatar_bytes,
                )
                for row, avatar_bytes in zip(top_users, avatars, strict=False)
            ]

            chat_title = (
                getattr(message.chat, "title", None)
                or (f"@{message.chat.username}" if getattr(message.chat, "username", None) else None)
                or "Чат"
            )
            card_data = StatsCardData(
                chat_title=chat_title,
                period_label=period_label,
                total_period_messages=int(msgs_in_period),
                total_retained_messages=int(total_msgs),
                digest_count=int(total_digests),
                peak_hour_label=peak_hour_card_label,
                participants=participants,
            )

            png_bytes = await asyncio.to_thread(render_stats_leaderboard, card_data)
            compact_caption = _build_compact_caption(
                period_label=period_label,
                msgs_in_period=int(msgs_in_period),
                total_msgs=int(total_msgs),
                total_digests=int(total_digests),
                peak_hour_text=peak_hour_text,
                replied_section=replied_section,
            )
            photo_file = BufferedInputFile(png_bytes, filename="chat_stats.png")
            await message.reply_photo(photo_file, caption=compact_caption)
            return
        except Exception as exc:
            logger.warning(
                "Visual stats card unavailable for chat %s, falling back to text: %s",
                chat_id,
                exc,
            )

    await _reply_safely(message, stats_text)
