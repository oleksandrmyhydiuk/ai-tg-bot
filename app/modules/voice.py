from __future__ import annotations

from io import BytesIO
import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.modules.registry import is_module_enabled
from app.services.gemini_client import gemini_service, strip_markdown

logger = logging.getLogger(__name__)
router = Router(name="voice")

MAX_VOICE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB
MAX_TELEGRAM_TEXT_LEN = 4000


def _split_chunks(text: str, max_len: int = MAX_TELEGRAM_TEXT_LEN) -> list[str]:
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


async def _reply_safely(message: Message, text: str) -> Message:
    try:
        return await message.reply(text)
    except Exception:
        return await message.answer(text)


def _extract_target_media(target: Message) -> tuple[str, str, int | None] | None:
    """Returns (file_id, mime_type, file_size) for voice, audio, or video_note."""
    if target.voice is not None:
        return (
            target.voice.file_id,
            target.voice.mime_type or "audio/ogg",
            target.voice.file_size,
        )
    if target.audio is not None:
        return (
            target.audio.file_id,
            target.audio.mime_type or "audio/mpeg",
            target.audio.file_size,
        )
    if target.video_note is not None:
        return (
            target.video_note.file_id,
            "video/mp4",
            target.video_note.file_size,
        )
    return None


@router.message(Command("transcribe", ignore_case=True))
async def cmd_transcribe(message: Message, bot: Bot) -> None:
    """Transcribes a replied-to voice, audio, or video_note message via Gemini."""
    if not await is_module_enabled(message.chat.id, "voice"):
        await _reply_safely(
            message,
            "Модуль розшифровки голосових вимкнено в цьому чаті. Увімкнути: /start",
        )
        return

    target = message.reply_to_message
    media_info = _extract_target_media(target) if target is not None else None
    if media_info is None:
        await _reply_safely(
            message,
            "💡 Зробіть Reply командою /transcribe на голосове повідомлення, аудіо або відеокружечок.",
        )
        return

    file_id, mime_type, file_size = media_info
    if file_size is not None and file_size > MAX_VOICE_SIZE_BYTES:
        await _reply_safely(
            message,
            "⚠️ Файл занадто великий для розшифровки (максимальний розмір — 20 МБ).",
        )
        return

    status_msg = await _reply_safely(message, "🎙️ Розшифровую повідомлення...")

    try:
        buf = BytesIO()
        await bot.download(file_id, destination=buf)
        media_bytes = buf.getvalue()
    except Exception as exc:
        logger.warning("Failed to download voice/audio media in chat %s: %s", message.chat.id, exc)
        try:
            await status_msg.edit_text("⚠️ Не вдалося завантажити аудіо з Telegram.")
        except Exception:
            await _reply_safely(message, "⚠️ Не вдалося завантажити аудіо з Telegram.")
        return

    if not media_bytes:
        try:
            await status_msg.edit_text("⚠️ Завантажений аудіофайл виявився порожнім.")
        except Exception:
            await _reply_safely(message, "⚠️ Завантажений аудіофайл виявився порожнім.")
        return

    if len(media_bytes) > MAX_VOICE_SIZE_BYTES:
        try:
            await status_msg.edit_text(
                "⚠️ Файл занадто великий для розшифровки (максимальний розмір — 20 МБ)."
            )
        except Exception:
            await _reply_safely(
                message,
                "⚠️ Файл занадто великий для розшифровки (максимальний розмір — 20 МБ).",
            )
        return

    try:
        raw_result = await gemini_service.transcribe_media(media_bytes, mime_type)
    except Exception as exc:
        logger.exception("Gemini transcription error in chat %s: %s", message.chat.id, exc)
        try:
            await status_msg.edit_text("⚠️ Не вдалося розшифрувати повідомлення. Спробуйте пізніше.")
        except Exception:
            await _reply_safely(message, "⚠️ Не вдалося розшифрувати повідомлення. Спробуйте пізніше.")
        return

    clean_text = strip_markdown(raw_result) or "Не вдалося розпізнати мовлення."
    chunks = _split_chunks(clean_text, MAX_TELEGRAM_TEXT_LEN)
    try:
        await status_msg.edit_text(chunks[0])
    except Exception:
        await _reply_safely(message, chunks[0])

    for extra_chunk in chunks[1:]:
        try:
            await message.answer(extra_chunk)
        except Exception as exc:
            logger.warning("Failed to send extra transcription chunk: %s", exc)
            break
