from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC
import logging
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message
from sqlalchemy import select

from app.database.engine import async_session_factory
from app.database.models import ChatDigest, utc_now
from app.modules.core import is_chat_admin
from app.modules.registry import is_module_enabled, set_user_opted_out

logger = logging.getLogger(__name__)
router = Router(name="privacy_export")

try:
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except Exception:
    KYIV_TZ = timezone(timedelta(hours=2))


async def _reply_safely(message: Message, text: str) -> Message:
    try:
        return await message.reply(text)
    except Exception:
        return await message.answer(text)


def _format_kyiv_full(dt: datetime | None) -> str:
    if dt is None:
        return "невідомо"
    aware = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
    return aware.astimezone(KYIV_TZ).strftime("%Y-%m-%d %H:%M")


def _format_export_markdown(
    chat_title: str,
    chat_id: int,
    digests: list[ChatDigest],
    exported_at: datetime,
) -> str:
    lines: list[str] = [
        f"# Архів дайджестів чату: {chat_title}",
        "",
        f"- **Chat ID:** `{chat_id}`",
        f"- **Експортовано (Київ):** {_format_kyiv_full(exported_at)}",
        f"- **Кількість дайджестів:** {len(digests)}",
        "",
        "---",
        "",
    ]

    for idx, digest in enumerate(digests, start=1):
        period_start = _format_kyiv_full(digest.period_start) if digest.period_start else None
        period_end = _format_kyiv_full(digest.period_end) if digest.period_end else None
        created = _format_kyiv_full(digest.created_at)

        if period_start and period_end:
            heading = f"## {idx}. Період: {period_start} — {period_end} (Київ)"
        else:
            heading = f"## {idx}. Дайджест від {created} (Київ)"

        lines.append(heading)
        meta_parts = [f"Створено: {created}"]
        if digest.message_count is not None:
            meta_parts.append(f"Повідомлень: {digest.message_count}")
        lines.append(f"*{' | '.join(meta_parts)}*")
        lines.append("")
        lines.append((digest.digest_text or "").strip() or "(порожній дайджест)")
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _format_export_txt(
    chat_title: str,
    chat_id: int,
    digests: list[ChatDigest],
    exported_at: datetime,
) -> str:
    sep = "=" * 60
    sub_sep = "-" * 60
    lines: list[str] = [
        sep,
        f"АРХІВ ДАЙДЖЕСТІВ ЧАТУ: {chat_title}",
        f"Chat ID: {chat_id}",
        f"Експортовано (Київ): {_format_kyiv_full(exported_at)}",
        f"Кількість дайджестів: {len(digests)}",
        sep,
        "",
    ]

    for idx, digest in enumerate(digests, start=1):
        period_start = _format_kyiv_full(digest.period_start) if digest.period_start else None
        period_end = _format_kyiv_full(digest.period_end) if digest.period_end else None
        created = _format_kyiv_full(digest.created_at)

        if period_start and period_end:
            lines.append(f"[{idx}] Період: {period_start} — {period_end} (Київ)")
        else:
            lines.append(f"[{idx}] Дайджест від {created} (Київ)")

        meta_parts = [f"Створено: {created}"]
        if digest.message_count is not None:
            meta_parts.append(f"Повідомлень: {digest.message_count}")
        lines.append(" | ".join(meta_parts))
        lines.append(sub_sep)
        lines.append((digest.digest_text or "").strip() or "(порожній дайджест)")
        lines.append("")
        lines.append(sep)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


@router.message(Command("optout", ignore_case=True))
async def cmd_optout(message: Message) -> None:
    """Opts the user out of future ChatLog message storage."""
    if not await is_module_enabled(message.chat.id, "privacy_export"):
        await _reply_safely(
            message,
            "Модуль приватності та експорту вимкнено в цьому чаті. Увімкнути: /start",
        )
        return

    if message.from_user is None:
        await _reply_safely(
            message,
            "⚠️ Не вдалося визначити ваш обліковий запис користувача.",
        )
        return

    try:
        await set_user_opted_out(message.from_user.id, True)
    except Exception as exc:
        logger.exception("Failed to set opt-out for user %s: %s", message.from_user.id, exc)
        await _reply_safely(
            message,
            "⚠️ Не вдалося оновити налаштування приватності. Спробуйте пізніше.",
        )
        return

    await _reply_safely(
        message,
        "🔒 Готово! Ваші майбутні повідомлення не будуть зберігатися в історії чату (ChatLog).\n"
        "Щоб знову увімкнути збереження, виконайте /optin.",
    )


@router.message(Command("optin", ignore_case=True))
async def cmd_optin(message: Message) -> None:
    """Re-enables ChatLog message storage for the user."""
    if not await is_module_enabled(message.chat.id, "privacy_export"):
        await _reply_safely(
            message,
            "Модуль приватності та експорту вимкнено в цьому чаті. Увімкнути: /start",
        )
        return

    if message.from_user is None:
        await _reply_safely(
            message,
            "⚠️ Не вдалося визначити ваш обліковий запис користувача.",
        )
        return

    try:
        await set_user_opted_out(message.from_user.id, False)
    except Exception as exc:
        logger.exception("Failed to set opt-in for user %s: %s", message.from_user.id, exc)
        await _reply_safely(
            message,
            "⚠️ Не вдалося оновити налаштування приватності. Спробуйте пізніше.",
        )
        return

    await _reply_safely(
        message,
        "🔓 Готово! Збереження ваших майбутніх повідомлень для підсумків чату знову увімкнено.",
    )


@router.message(Command("export", ignore_case=True))
async def cmd_export(message: Message, bot: Bot, command: CommandObject) -> None:
    """Exports chronological ChatDigest archive for this chat as .md or .txt (admin-only in groups)."""
    if not await is_module_enabled(message.chat.id, "privacy_export"):
        await _reply_safely(
            message,
            "Модуль приватності та експорту вимкнено в цьому чаті. Увімкнути: /start",
        )
        return

    chat_id = message.chat.id
    if message.chat.type in ("group", "supergroup"):
        user_id = message.from_user.id if message.from_user else None
        sender_chat_id = message.sender_chat.id if message.sender_chat else None
        if not await is_chat_admin(bot, chat_id, user_id, sender_chat_id=sender_chat_id):
            await _reply_safely(
                message,
                "⛔ Експортувати архів дайджестів можуть лише адміністратори групи.",
            )
            return

    raw_fmt = (command.args or "").strip().lower()
    if raw_fmt in ("", "md", "markdown"):
        ext = "md"
    elif raw_fmt in ("txt", "text"):
        ext = "txt"
    else:
        await _reply_safely(
            message,
            "💡 Невідомий формат експорту. Використовуйте: /export md або /export txt",
        )
        return

    try:
        async with async_session_factory() as session:
            stmt = (
                select(ChatDigest)
                .where(ChatDigest.chat_id == chat_id)
                .order_by(
                    ChatDigest.period_start.asc().nullslast(),
                    ChatDigest.created_at.asc(),
                    ChatDigest.id.asc(),
                )
            )
            digests = list((await session.execute(stmt)).scalars().all())
    except Exception as exc:
        logger.exception("Database error during /export for chat %s: %s", chat_id, exc)
        await _reply_safely(
            message,
            "⚠️ Виникла помилка при читанні дайджестів з бази даних.",
        )
        return

    if not digests:
        await _reply_safely(
            message,
            "📭 Для цього чату ще немає збережених дайджестів для експорту.",
        )
        return

    chat_title = (
        message.chat.title
        or message.chat.full_name
        or message.chat.username
        or str(chat_id)
    )
    exported_at = utc_now()
    if ext == "md":
        content_text = _format_export_markdown(chat_title, chat_id, digests, exported_at)
    else:
        content_text = _format_export_txt(chat_title, chat_id, digests, exported_at)

    content_bytes = content_text.encode("utf-8")
    filename = f"chat_export_{chat_id}.{ext}"
    caption = f"📦 Експорт дайджестів чату ({len(digests)} шт., формат .{ext})"
    document = BufferedInputFile(content_bytes, filename=filename)

    try:
        await message.reply_document(document, caption=caption)
    except Exception:
        await message.answer_document(document, caption=caption)
