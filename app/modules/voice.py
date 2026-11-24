from __future__ import annotations

from io import BytesIO
import logging
from pathlib import Path

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Document, Message

from app.handlers.commands import _check_rate_limit, _safe_edit_text
from app.modules.registry import is_module_enabled
from app.services.gemini_client import gemini_service, strip_markdown

logger = logging.getLogger(__name__)
router = Router(name="voice")

MAX_VOICE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB
MAX_DOC_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB
MAX_TELEGRAM_TEXT_LEN = 4000

_ALLOWED_DOC_MIME_EXACT = frozenset(
    {
        "application/pdf",
        "application/json",
        "application/xml",
        "application/x-yaml",
        "application/yaml",
        "application/javascript",
        "application/x-python-code",
    }
)

_EXT_TO_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".ts": "text/typescript",
    ".tsx": "text/typescript",
    ".jsx": "text/javascript",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".xml": "application/xml",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".toml": "text/plain",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".conf": "text/plain",
    ".sql": "text/plain",
    ".sh": "text/plain",
    ".bash": "text/plain",
    ".ps1": "text/plain",
    ".java": "text/plain",
    ".c": "text/plain",
    ".cpp": "text/plain",
    ".h": "text/plain",
    ".hpp": "text/plain",
    ".cs": "text/plain",
    ".go": "text/plain",
    ".rs": "text/plain",
    ".rb": "text/plain",
    ".php": "text/plain",
    ".env": "text/plain",
    ".dockerfile": "text/plain",
}

_ALLOWED_SPECIAL_FILENAMES = frozenset({"dockerfile", "makefile", "procfile"})


def _resolve_supported_document_mime(doc: Document) -> str | None:
    """Returns a normalized MIME type if the document is supported for /doc, else None."""
    raw_mime = (doc.mime_type or "").split(";", 1)[0].strip().lower()
    filename = (doc.file_name or "").strip()
    ext = Path(filename).suffix.lower() if filename else ""

    if raw_mime.startswith("text/") or raw_mime in _ALLOWED_DOC_MIME_EXACT:
        return raw_mime

    if ext in _EXT_TO_MIME:
        return _EXT_TO_MIME[ext]

    if filename.lower() in _ALLOWED_SPECIAL_FILENAMES:
        return "text/plain"

    return None


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


@router.message(Command("doc", ignore_case=True))
async def cmd_doc(message: Message, bot: Bot, command: CommandObject) -> None:
    """Analyzes a replied-to document (PDF, text, JSON, Markdown, CSV, code) via Gemini."""
    if not await is_module_enabled(message.chat.id, "voice"):
        await _reply_safely(
            message,
            "Модуль розшифровки голосових та аналізу документів вимкнено в цьому чаті. Увімкнути: /start",
        )
        return

    doc: Document | None = None
    if message.reply_to_message is not None and message.reply_to_message.document is not None:
        doc = message.reply_to_message.document
    elif message.document is not None:
        doc = message.document

    if doc is None:
        await _reply_safely(
            message,
            "💡 Зробіть Reply командою /doc [питання] на документ "
            "(PDF, TXT, MD, JSON, CSV або файл коду до 15 МБ).",
        )
        return

    mime_type = _resolve_supported_document_mime(doc)
    if mime_type is None:
        await _reply_safely(
            message,
            "⚠️ Непідтримуваний формат файлу. Підтримуються PDF, текстові файли, JSON, Markdown, CSV та код (до 15 МБ).",
        )
        return

    if doc.file_size is not None and doc.file_size > MAX_DOC_SIZE_BYTES:
        await _reply_safely(
            message,
            "⚠️ Файл занадто великий для аналізу (максимальний розмір — 15 МБ).",
        )
        return

    user_id = message.from_user.id if message.from_user else None
    wait_sec = _check_rate_limit(user_id)
    if wait_sec > 0:
        await _reply_safely(
            message,
            f"⏳ Зачекайте {int(wait_sec) + 1} сек. перед наступним запитом до ШІ.",
        )
        return

    filename = (doc.file_name or "document").strip() or "document"
    question = (command.args or "").strip()
    status_msg = await _reply_safely(message, f"📄 Аналізую документ «{filename}»...")

    try:
        buf = BytesIO()
        await bot.download(doc.file_id, destination=buf)
        file_bytes = buf.getvalue()
    except Exception as exc:
        logger.warning("Failed to download document in chat %s: %s", message.chat.id, exc)
        await _safe_edit_text(status_msg, "⚠️ Не вдалося завантажити документ з Telegram.")
        return

    if not file_bytes:
        await _safe_edit_text(status_msg, "⚠️ Завантажений файл виявився порожнім.")
        return

    if len(file_bytes) > MAX_DOC_SIZE_BYTES:
        await _safe_edit_text(
            status_msg,
            "⚠️ Файл занадто великий для аналізу (максимальний розмір — 15 МБ).",
        )
        return

    try:
        analysis = await gemini_service.analyze_document(
            file_bytes,
            mime_type,
            filename,
            question,
        )
    except Exception as exc:
        logger.exception("Gemini document analysis error in chat %s: %s", message.chat.id, exc)
        await _safe_edit_text(
            status_msg,
            "⚠️ Не вдалося проаналізувати документ. Спробуйте пізніше.",
        )
        return

    header = f"📄 Аналіз документа: {filename}"
    body = (analysis or "").strip() or "Не вдалося сформувати аналіз документа."
    await _safe_edit_text(status_msg, f"{header}\n\n{body}")
