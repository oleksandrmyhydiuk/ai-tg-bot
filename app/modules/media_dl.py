from __future__ import annotations

import logging
import time

from aiogram import Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import FSInputFile, Message

from app.handlers.group_events import log_text_message
from app.modules.registry import is_module_enabled
from app.services.media_downloader import (
    DownloadedMedia,
    MediaDownloadError,
    download_video,
    extract_supported_urls,
    is_supported_video_url,
)

logger = logging.getLogger(__name__)
router = Router(name="media_dl")

DOWNLOAD_COOLDOWN_SECONDS = 10.0
_user_last_download: dict[int, float] = {}

_PLATFORM_LABELS: dict[str, str] = {
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "youtube": "YouTube",
}


def _check_download_cooldown(user_id: int | None) -> float:
    """Returns 0.0 if user can download now, or remaining cooldown seconds."""
    if user_id is None:
        return 0.0
    now = time.monotonic()
    last_time = _user_last_download.get(user_id, 0.0)
    elapsed = now - last_time
    if elapsed < DOWNLOAD_COOLDOWN_SECONDS:
        return DOWNLOAD_COOLDOWN_SECONDS - elapsed
    _user_last_download[user_id] = now
    if len(_user_last_download) > 1000:
        cutoff = now - 60.0
        for uid in [k for k, v in _user_last_download.items() if v < cutoff]:
            _user_last_download.pop(uid, None)
    return 0.0


def _build_caption(media: DownloadedMedia) -> str:
    platform_label = _PLATFORM_LABELS.get(media.platform, media.platform)
    title = media.title.strip()
    if len(title) > 200:
        title = title[:197].rstrip() + "..."
    return f"🎬 {title}\n[{platform_label}]"


async def _send_video_safely(message: Message, media: DownloadedMedia) -> None:
    video_file = FSInputFile(media.file_path)
    caption = _build_caption(media)
    kwargs: dict[str, object] = {
        "video": video_file,
        "caption": caption,
        "supports_streaming": True,
    }
    if media.duration is not None:
        kwargs["duration"] = media.duration
    try:
        await message.reply_video(**kwargs)  # type: ignore[arg-type]
    except Exception:
        await message.answer_video(**kwargs)  # type: ignore[arg-type]


async def _reply_text_safely(message: Message, text: str) -> Message:
    try:
        return await message.reply(text)
    except Exception:
        return await message.answer(text)


def _extract_urls_from_message(message: Message) -> list[str]:
    """Extracts all supported video URLs from message text/caption and text_link entities."""
    raw_text = (message.text or message.caption or "").strip()
    urls = extract_supported_urls(raw_text)
    seen = set(urls)
    entities = list(message.entities or []) + list(message.caption_entities or [])
    for ent in entities:
        ent_url = getattr(ent, "url", None)
        if ent_url and ent_url not in seen and is_supported_video_url(ent_url):
            seen.add(ent_url)
            urls.append(ent_url)
    return urls


class AutoMediaUrlFilter(Filter):
    """Matches any non-command message containing a supported video URL when media_dl is enabled."""

    async def __call__(self, message: Message) -> bool:
        raw_text = (message.text or message.caption or "").strip()
        if raw_text.startswith("/"):
            return False
        if not _extract_urls_from_message(message):
            return False
        return await is_module_enabled(message.chat.id, "media_dl")


@router.message(Command("dl", "download", ignore_case=True))
async def cmd_download_video(message: Message, command: CommandObject) -> None:
    """Downloads video from TikTok, Instagram, or YouTube via /dl or /download."""
    if not await is_module_enabled(message.chat.id, "media_dl"):
        await _reply_text_safely(
            message,
            "Модуль завантаження відео вимкнено в цьому чаті. Увімкнути: /start",
        )
        return

    urls: list[str] = []
    if command.args:
        urls = extract_supported_urls(command.args)
    if not urls and message.reply_to_message:
        replied_text = (
            message.reply_to_message.text or message.reply_to_message.caption or ""
        )
        urls = extract_supported_urls(replied_text)

    if not urls:
        await _reply_text_safely(
            message,
            "💡 Використання: /dl <посилання> або Reply на повідомлення з посиланням "
            "(TikTok, Instagram, YouTube).",
        )
        return

    user_id = message.from_user.id if message.from_user else None
    wait_sec = _check_download_cooldown(user_id)
    if wait_sec > 0:
        await _reply_text_safely(
            message,
            f"⏳ Зачекайте {int(wait_sec) + 1} сек. перед наступним завантаженням.",
        )
        return

    status_msg = await _reply_text_safely(message, "⏳ Завантажую відео...")
    media: DownloadedMedia | None = None
    try:
        media = await download_video(urls[0])
        await _send_video_safely(message, media)
        try:
            await status_msg.delete()
        except Exception:
            pass
    except MediaDownloadError as exc:
        try:
            await status_msg.edit_text(f"⚠️ {exc}")
        except Exception:
            await _reply_text_safely(message, f"⚠️ {exc}")
    except Exception as exc:
        logger.exception("Unexpected error in /dl for chat %s: %s", message.chat.id, exc)
        try:
            await status_msg.edit_text("⚠️ Не вдалося завантажити відео. Спробуйте пізніше.")
        except Exception:
            await _reply_text_safely(message, "⚠️ Не вдалося завантажити відео. Спробуйте пізніше.")
    finally:
        if media is not None:
            media.cleanup()


@router.message(AutoMediaUrlFilter())
async def handle_auto_media_download(message: Message) -> None:
    """
    Automatically detects and downloads any supported TikTok, Instagram, or YouTube
    video URL posted in chat without requiring a command when media_dl is enabled.
    """
    try:
        await log_text_message(message)
    except Exception as exc:
        logger.debug("Failed to log auto-media message in chat %s: %s", message.chat.id, exc)

    urls = _extract_urls_from_message(message)
    if not urls:
        return

    user_id = message.from_user.id if message.from_user else None
    if _check_download_cooldown(user_id) > 0:
        return

    if message.bot is not None:
        try:
            await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_VIDEO)
        except Exception:
            pass

    media: DownloadedMedia | None = None
    try:
        media = await download_video(urls[0])
        await _send_video_safely(message, media)
    except MediaDownloadError as exc:
        logger.warning("Auto-download failed for %s in chat %s: %s", urls[0], message.chat.id, exc)
    except Exception as exc:
        logger.warning("Unexpected auto-download error in chat %s: %s", message.chat.id, exc)
    finally:
        if media is not None:
            media.cleanup()
