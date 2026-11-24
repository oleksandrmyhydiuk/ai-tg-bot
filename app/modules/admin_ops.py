import logging
import time

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select

from app.config import settings
from app.database.engine import async_session_factory
from app.database.models import ChatDigest, ChatLog, User
from app.modules.core import is_chat_admin
from app.modules.registry import (
    MODULE_ORDER,
    get_enabled_modules,
    is_module_enabled,
)
from app.services.gemini_client import gemini_service

logger = logging.getLogger(__name__)
router = Router(name="admin_ops")
BOT_STARTED_AT = time.monotonic()


async def _reply_safely(message: Message, text: str) -> Message:
    try:
        return await message.reply(text)
    except Exception:
        return await message.answer(text)


def _format_uptime(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}д")
    if hours or days:
        parts.append(f"{hours}год")
    if minutes or hours or days:
        parts.append(f"{minutes}хв")
    parts.append(f"{secs}с")
    return " ".join(parts)


async def _is_ops_authorized(message: Message) -> bool:
    user_id = message.from_user.id if message.from_user else None
    if user_id is not None and user_id in settings.parsed_admin_user_ids:
        return True

    chat_id = message.chat.id
    if not await is_module_enabled(chat_id, "admin_ops"):
        return False

    if message.bot is None:
        return False

    sender_chat_id = message.sender_chat.id if message.sender_chat else None
    return await is_chat_admin(
        message.bot,
        chat_id,
        user_id,
        sender_chat_id=sender_chat_id,
    )


@router.message(Command("health", ignore_case=True))
async def cmd_health(message: Message) -> None:
    """Shows bot uptime, DB record counts, enabled chat modules, and global RPM limit."""
    if not await _is_ops_authorized(message):
        await _reply_safely(
            message,
            "⛔ Команда /health доступна лише адміністраторам (або увімкніть модуль «Моніторинг і квоти» у /start).",
        )
        return

    uptime_str = _format_uptime(time.monotonic() - BOT_STARTED_AT)

    db_status_lines: list[str]
    try:
        async with async_session_factory() as session:
            logs_count = (await session.execute(select(func.count()).select_from(ChatLog))).scalar_one()
            digests_count = (await session.execute(select(func.count()).select_from(ChatDigest))).scalar_one()
            users_count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        db_status_lines = [
            "🗄️ База даних: OK",
            f"• Повідомлень (ChatLog): {logs_count}",
            f"• Дайджестів (ChatDigest): {digests_count}",
            f"• Користувачів (User): {users_count}",
        ]
    except Exception as e:
        logger.warning("Health check DB query failed: %s", e)
        db_status_lines = [f"🗄️ База даних: помилка ({type(e).__name__})"]

    enabled_set = await get_enabled_modules(message.chat.id)
    enabled_ordered = [mid for mid in MODULE_ORDER if mid in enabled_set]
    modules_str = ", ".join(enabled_ordered) if enabled_ordered else "немає"

    rpm_limit = getattr(settings, "global_rpm_limit", 0)
    rpm_str = f"{rpm_limit} запитів/хв" if rpm_limit and rpm_limit > 0 else "вимкнено"

    lines = [
        "🩺 Стан бота (/health)",
        "",
        f"⏱️ Аптайм: {uptime_str}",
        *db_status_lines,
        f"🧩 Активні модулі в цьому чаті ({len(enabled_ordered)}): {modules_str}",
        f"🚦 Глобальний RPM ліміт ШІ: {rpm_str}",
    ]
    await _reply_safely(message, "\n".join(lines))


@router.message(Command("quota", ignore_case=True))
async def cmd_quota(message: Message) -> None:
    """Shows per-model AI request telemetry (successes, 429, 404, other errors, avg latency)."""
    if not await _is_ops_authorized(message):
        await _reply_safely(
            message,
            "⛔ Команда /quota доступна лише адміністраторам (або увімкніть модуль «Моніторинг і квоти» у /start).",
        )
        return

    snapshot = gemini_service.get_telemetry_snapshot()
    if not snapshot:
        await _reply_safely(message, "Ще не було запитів до ШІ після запуску.")
        return

    lines = ["📊 Статистика запитів до моделей ШІ (/quota)", ""]
    for model_name, stats in snapshot.items():
        success = int(stats.get("success", 0))
        r429 = int(stats.get("rate_limited_429", 0))
        r404 = int(stats.get("not_found_404", 0))
        other = int(stats.get("other_errors", 0))
        avg_ms = float(stats.get("avg_latency_ms", 0.0))
        lines.append(
            f"• {model_name}: успішно={success}, 429={r429}, 404={r404}, "
            f"інші помилки={other}, сер. затримка={avg_ms:.1f} мс"
        )

    await _reply_safely(message, "\n".join(lines))
