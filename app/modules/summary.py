from __future__ import annotations

from datetime import timedelta
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from app.config import settings
from app.database.engine import async_session_factory
from app.database.models import ChatLog, utc_now
from app.handlers.commands import (
    DEFAULT_SUMMARY_HOURS,
    _check_rate_limit,
    _format_hours_label,
    _parse_summary_hours,
    _safe_edit_text,
    _safe_reply,
)
from app.modules.registry import is_module_enabled
from app.services.context_builder import build_summary_context
from app.services.gemini_client import gemini_service
from app.services.summary_verifier import (
    ChatSummarySchema,
    _clean_plain_text,
    format_citation_badge,
)

logger = logging.getLogger(__name__)
router = Router(name="summary")

DEFAULT_TASKS_HOURS = 24.0
SUMMARY_DISABLED_MSG = "Модуль підсумків вимкнено в цьому чаті. Увімкнути: /start"
NO_TASKS_MSG = "За цей період не зафіксовано конкретних задач, рішень чи відкритих питань."


def _build_summary_keyboard(hours: float) -> InlineKeyboardMarkup:
    hours_token = int(hours) if float(hours).is_integer() else round(hours, 1)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="За 24 год", callback_data="sum:24"),
                InlineKeyboardButton(
                    text="Тільки задачі/рішення",
                    callback_data=f"sum:tasks:{hours_token}",
                ),
            ]
        ]
    )


def _render_tasks_only(
    verified: ChatSummarySchema | None,
    chat_id: int | None = None,
    chat_username: str | None = None,
    show_citations: bool = True,
    header_note: str | None = None,
) -> str:
    sections: list[str] = []
    if header_note and header_note.strip():
        sections.append(_clean_plain_text(header_note))

    if verified is None or not verified.decisions_and_outcomes:
        sections.append(NO_TASKS_MSG)
        return "\n\n".join(sections)

    status_map = {
        "confirmed": "[Підтверджено]",
        "proposed": "[Пропозиція]",
        "open_question": "[Відкрите питання]",
    }
    lines: list[str] = []
    for decision in verified.decisions_and_outcomes:
        status_prefix = status_map.get(decision.status, "[Пропозиція]")
        text_clean = _clean_plain_text(decision.text)
        participants_clean = [
            _clean_plain_text(p) for p in decision.participants if _clean_plain_text(p)
        ]
        participants_str = f" ({', '.join(participants_clean)})" if participants_clean else ""
        citation_badge = (
            format_citation_badge(
                decision.evidence_msg_ids,
                chat_id=chat_id,
                chat_username=chat_username,
            )
            if show_citations
            else ""
        )
        lines.append(f"- {status_prefix} {text_clean}{participants_str}{citation_badge}")

    sections.append("\n".join(lines))
    return "\n\n".join(sections).strip()


async def execute_summary_for_hours(
    message: Message,
    hours: float,
    tasks_only: bool = False,
    user_id: int | None = None,
) -> None:
    """
    Generates verified summary (or tasks/decisions-only view) for the last `hours` hours
    with message ID citations, reply-chain context, and source links.
    """
    effective_user_id = (
        user_id
        if user_id is not None
        else (message.from_user.id if message.from_user else None)
    )
    wait_sec = _check_rate_limit(effective_user_id)
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

    wait_prefix = (
        f"⏳ Шукаю задачі, рішення та відкриті питання за останні {hours_label} ({total_items_desc})..."
        if tasks_only
        else f"⏳ Формую звіт за останні {hours_label} ({total_items_desc})..."
    )
    wait_msg = await _safe_reply(message, wait_prefix)

    header_note = None
    if bundle.was_truncated:
        header_note = (
            f"Враховано останні {bundle.message_count} із "
            f"{bundle.total_available_messages} повідомлень."
        )

    show_citations = getattr(settings, "summary_show_citations", True)
    reply_markup: InlineKeyboardMarkup | None = None

    try:
        rendered_summary, verified = await gemini_service.generate_verified_summary(
            bundle=bundle,
            chat_id=chat_id,
            chat_username=message.chat.username,
            show_citations=show_citations,
            header_note=header_note,
        )
        if tasks_only:
            tasks_body = _render_tasks_only(
                verified=verified,
                chat_id=chat_id,
                chat_username=message.chat.username,
                show_citations=show_citations,
                header_note=header_note,
            )
            report = (
                f"✅ Задачі, рішення та відкриті питання за останні {hours_label} ({total_items_desc}):\n\n"
                f"{tasks_body}"
            )
        else:
            report = (
                f"📋 Підсумок чату за останні {hours_label} ({total_items_desc}):\n\n"
                f"{rendered_summary}"
            )
            reply_markup = _build_summary_keyboard(hours)
    except Exception as e:
        logger.error("Error generating verified summary for chat %s: %s", chat_id, e)
        fallback_body = "\n".join(bundle.raw_fallback_lines)[:3500]
        report = (
            f"⚠️ Не вдалося об'єднати звіт через ліміт API. "
            f"Збережені дані за останні {hours_label}:\n\n{fallback_body}"
        )

    await _safe_edit_text(wait_msg, report, reply_markup=reply_markup)


@router.message(Command("summary", ignore_case=True))
async def cmd_summary(message: Message, command: CommandObject) -> None:
    """Generates summary for last N hours (e.g. /summary 4 or /summary)."""
    if not await is_module_enabled(message.chat.id, "summary"):
        await _safe_reply(message, SUMMARY_DISABLED_MSG)
        return
    hours = _parse_summary_hours(command.args, default=DEFAULT_SUMMARY_HOURS)
    await execute_summary_for_hours(message, hours, tasks_only=False)


@router.message(Command("tasks", ignore_case=True))
async def cmd_tasks(message: Message, command: CommandObject) -> None:
    """Lists confirmed decisions, proposals, and open questions for last N hours (default 24h)."""
    if not await is_module_enabled(message.chat.id, "summary"):
        await _safe_reply(message, SUMMARY_DISABLED_MSG)
        return
    hours = _parse_summary_hours(command.args, default=DEFAULT_TASKS_HOURS)
    await execute_summary_for_hours(message, hours, tasks_only=True)


@router.callback_query(F.data.startswith("sum:"))
async def cb_summary_action(callback: CallbackQuery) -> None:
    """Handles inline summary buttons: sum:24 and sum:tasks:<hours>."""
    if callback.message is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    chat_id = callback.message.chat.id
    if not await is_module_enabled(chat_id, "summary"):
        await callback.answer(SUMMARY_DISABLED_MSG, show_alert=True)
        return

    data = callback.data or ""
    parts = data.split(":")
    tasks_only = False
    hours = DEFAULT_SUMMARY_HOURS

    if len(parts) >= 2 and parts[1] == "tasks":
        tasks_only = True
        hours_arg = parts[2] if len(parts) >= 3 else None
        hours = _parse_summary_hours(hours_arg, default=DEFAULT_SUMMARY_HOURS)
    elif len(parts) >= 2:
        hours = _parse_summary_hours(parts[1], default=24.0)

    await callback.answer()
    caller_id = callback.from_user.id if callback.from_user else None
    await execute_summary_for_hours(
        callback.message,
        hours,
        tasks_only=tasks_only,
        user_id=caller_id,
    )
