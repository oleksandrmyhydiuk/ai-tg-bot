from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.handlers.commands import _check_rate_limit, _safe_edit_text, _safe_reply
from app.modules.registry import is_module_enabled
from app.services.gemini_client import gemini_service
from app.services.link_summarizer import (
    LinkFetchError,
    extract_first_http_url,
    fetch_link_content,
)

logger = logging.getLogger(__name__)
router = Router(name="link_tldr")


def _extract_url_from_message(message: Message) -> str | None:
    """Extracts the first HTTP/HTTPS URL from message text/caption or text_link entities."""
    raw_text = (message.text or message.caption or "").strip()
    found = extract_first_http_url(raw_text)
    if found:
        return found

    entities = list(message.entities or []) + list(message.caption_entities or [])
    for ent in entities:
        ent_url = getattr(ent, "url", None)
        if isinstance(ent_url, str) and ent_url.strip():
            extracted = extract_first_http_url(ent_url)
            if extracted:
                return extracted
    return None


@router.message(Command("tldr", ignore_case=True))
async def cmd_tldr(message: Message, command: CommandObject) -> None:
    """Fetches a webpage or YouTube video and generates a concise TL;DR summary."""
    if not await is_module_enabled(message.chat.id, "link_tldr"):
        await _safe_reply(
            message,
            "Модуль саммарі посилань вимкнено в цьому чаті. Увімкнути: /start",
        )
        return

    url: str | None = None
    if command.args:
        url = extract_first_http_url(command.args)
    if not url:
        # Also check text_link entities in the /tldr command message itself
        for ent in list(message.entities or []) + list(message.caption_entities or []):
            ent_url = getattr(ent, "url", None)
            if isinstance(ent_url, str) and ent_url.strip():
                url = extract_first_http_url(ent_url)
                if url:
                    break
    if not url and message.reply_to_message is not None:
        url = _extract_url_from_message(message.reply_to_message)

    if not url:
        await _safe_reply(
            message,
            "💡 Використання: /tldr <посилання> або зробіть Reply командою /tldr "
            "на повідомлення з посиланням (стаття або YouTube).",
        )
        return

    user_id = message.from_user.id if message.from_user else None
    wait_sec = _check_rate_limit(user_id)
    if wait_sec > 0:
        await _safe_reply(
            message,
            f"⏳ Зачекайте {int(wait_sec) + 1} сек. перед наступним запитом до ШІ.",
        )
        return

    status_msg = await _safe_reply(message, "⏳ Аналізую посилання...")

    try:
        extracted = await fetch_link_content(url)
    except LinkFetchError as exc:
        await _safe_edit_text(status_msg, f"⚠️ {exc}")
        return
    except Exception as exc:
        logger.exception("Unexpected error fetching link %s in chat %s: %s", url, message.chat.id, exc)
        await _safe_edit_text(
            status_msg,
            "⚠️ Не вдалося завантажити вміст за посиланням. Спробуйте пізніше.",
        )
        return

    try:
        summary = await gemini_service.summarize_external_content(
            extracted.title,
            extracted.url,
            extracted.text,
        )
    except Exception as exc:
        logger.exception(
            "Gemini summarization failed for %s in chat %s: %s",
            extracted.url,
            message.chat.id,
            exc,
        )
        await _safe_edit_text(
            status_msg,
            "⚠️ Не вдалося створити саммарі. Спробуйте пізніше.",
        )
        return

    icon = "🎬" if extracted.source_type == "youtube" else "🔗"
    header = f"{icon} TL;DR: {extracted.title}" if extracted.title else f"{icon} TL;DR"
    final_text = f"{header}\n\n{summary.strip()}\n\nДжерело: {extracted.url}"
    await _safe_edit_text(status_msg, final_text)
