from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import logging
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

TELEGRAM_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB Telegram Bot API limit

TIKTOK_DOMAINS: frozenset[str] = frozenset(
    {
        "tiktok.com",
        "www.tiktok.com",
        "vm.tiktok.com",
        "vt.tiktok.com",
        "m.tiktok.com",
    }
)

INSTAGRAM_DOMAINS: frozenset[str] = frozenset(
    {
        "instagram.com",
        "www.instagram.com",
        "m.instagram.com",
    }
)

YOUTUBE_DOMAINS: frozenset[str] = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
    }
)

ALLOWED_DOMAINS: frozenset[str] = TIKTOK_DOMAINS | INSTAGRAM_DOMAINS | YOUTUBE_DOMAINS

_URL_REGEX = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,!?;:»\"'”’"


class MediaDownloadError(Exception):
    """Raised when media URL validation or video download fails."""


@dataclass
class DownloadedMedia:
    file_path: Path
    temp_dir: Path
    title: str
    duration: int | None
    platform: str
    file_size_bytes: int

    def cleanup(self) -> None:
        """Safely deletes the temporary download directory."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)


def _is_ip_literal(host: str) -> bool:
    """Returns True if host is an IPv4 or IPv6 literal (SSRF protection)."""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def detect_platform(url: str) -> str | None:
    """
    Strictly validates a URL against SSRF vectors and the platform allowlist.
    Returns 'tiktok', 'instagram', 'youtube', or None if unsupported/rejected.
    """
    if not url or not isinstance(url, str):
        return None

    raw = url.strip()
    if not raw or any(ch.isspace() for ch in raw):
        return None

    try:
        parsed = urlparse(raw)
    except ValueError:
        return None

    if parsed.scheme.lower() not in ("http", "https"):
        return None

    # Reject embedded credentials / @ in netloc
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        return None

    # Reject non-standard ports
    try:
        port = parsed.port
    except ValueError:
        return None
    if port not in (None, 80, 443):
        return None

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or _is_ip_literal(host) or host not in ALLOWED_DOMAINS:
        return None

    path_lower = (parsed.path or "").lower()

    if host in TIKTOK_DOMAINS:
        return "tiktok"

    if host in INSTAGRAM_DOMAINS:
        if path_lower.startswith(("/reel/", "/reels/", "/p/", "/tv/")):
            return "instagram"
        return None

    if host in YOUTUBE_DOMAINS:
        if host == "youtu.be":
            if path_lower.startswith("/") and len(path_lower.strip("/")) > 0:
                return "youtube"
            return None
        if (
            path_lower.startswith("/shorts/")
            or path_lower == "/watch"
            or path_lower.startswith("/watch/")
        ):
            return "youtube"
        return None

    return None


def is_supported_video_url(url: str) -> bool:
    """Returns True if URL passes strict allowlist & platform path checks."""
    return detect_platform(url) is not None


def _is_auto_download_url(url: str) -> bool:
    """
    Returns True only for short-form video URLs suitable for passive auto-download:
    TikTok (with video/short path), Instagram Reels, and YouTube Shorts.
    """
    platform = detect_platform(url)
    if platform is None:
        return False

    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    path_lower = (parsed.path or "").lower()

    if platform == "tiktok":
        return path_lower not in ("", "/")

    if platform == "instagram":
        return path_lower.startswith(("/reel/", "/reels/"))

    if platform == "youtube":
        return host != "youtu.be" and path_lower.startswith("/shorts/")

    return False


def _extract_raw_urls(text: str) -> list[str]:
    if not text:
        return []
    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_REGEX.findall(text):
        cleaned = match.rstrip(_TRAILING_PUNCT)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            urls.append(cleaned)
    return urls


def extract_supported_urls(text: str) -> list[str]:
    """Finds all URLs in text that pass is_supported_video_url."""
    return [u for u in _extract_raw_urls(text) if is_supported_video_url(u)]


def extract_auto_download_urls(text: str) -> list[str]:
    """
    Finds short-form video URLs (TikTok, Instagram Reels, YouTube Shorts)
    for passive auto-detection in groups without downloading long YouTube videos.
    """
    return [u for u in _extract_raw_urls(text) if _is_auto_download_url(u)]


def _find_downloaded_file(temp_dir: Path, expected_path: Path | None) -> Path | None:
    if expected_path is not None and expected_path.is_file():
        return expected_path
    if expected_path is not None:
        mp4_candidate = expected_path.with_suffix(".mp4")
        if mp4_candidate.is_file():
            return mp4_candidate

    candidates = [
        p
        for p in temp_dir.iterdir()
        if p.is_file() and not p.name.endswith((".part", ".ytdl", ".temp"))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def _download_sync(url: str, temp_dir: Path, max_size_mb: int, platform: str) -> DownloadedMedia:
    try:
        import yt_dlp  # type: ignore[import-untyped]
    except ImportError as exc:
        raise MediaDownloadError("Модуль yt-dlp не встановлено на сервері.") from exc

    max_bytes = max_size_mb * 1024 * 1024
    video_max_mb = max(4, max_size_mb - 6)

    ydl_opts = {
        "outtmpl": str(temp_dir / "%(id)s.%(ext)s"),
        "format": (
            f"best[ext=mp4][filesize<{max_size_mb}M]/"
            f"bestvideo[ext=mp4][filesize<{video_max_mb}M]+bestaudio[ext=m4a]/"
            f"best[filesize<{max_size_mb}M]/best"
        ),
        "merge_output_format": "mp4",
        "max_filesize": max_bytes,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 2,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not isinstance(info, dict):
                raise MediaDownloadError("Не вдалося отримати інформацію про відео.")
            expected_path = Path(ydl.prepare_filename(info))
    except MediaDownloadError:
        raise
    except Exception as exc:
        logger.warning("yt-dlp failed for %s: %s", url, exc)
        raise MediaDownloadError(f"Не вдалося завантажити відео: {exc}") from exc

    file_path = _find_downloaded_file(temp_dir, expected_path)
    if file_path is None or not file_path.is_file():
        raise MediaDownloadError("Файл відео не знайдено після завантаження (можливо, перевищено ліміт розміру).")

    file_size = file_path.stat().st_size
    if file_size <= 0:
        raise MediaDownloadError("Завантажений файл порожній.")

    if file_size > TELEGRAM_MAX_FILE_BYTES:
        size_mb = file_size / (1024 * 1024)
        raise MediaDownloadError(
            f"Відео занадто велике ({size_mb:.1f} МБ). Ліміт Telegram — 50 МБ."
        )

    raw_title = str(info.get("title") or info.get("description") or "Відео").strip()
    clean_title = re.sub(r"\s+", " ", raw_title)[:180].strip() or "Відео"

    raw_duration = info.get("duration")
    duration: int | None = None
    if isinstance(raw_duration, (int, float)) and raw_duration > 0:
        duration = int(raw_duration)

    return DownloadedMedia(
        file_path=file_path,
        temp_dir=temp_dir,
        title=clean_title,
        duration=duration,
        platform=platform,
        file_size_bytes=file_size,
    )


async def download_video(url: str, max_size_mb: int = 48) -> DownloadedMedia:
    """
    Validates URL and downloads video into a fresh temporary directory using yt-dlp
    in a background thread. Cleans up temp_dir automatically on any error.
    """
    platform = detect_platform(url)
    if platform is None:
        raise MediaDownloadError(
            "Непідтримуване посилання. Підтримуються лише TikTok, Instagram та YouTube."
        )

    # Fail fast before creating temp_dir if yt-dlp is missing
    try:
        import yt_dlp  # noqa: F401
    except ImportError as exc:
        raise MediaDownloadError("Модуль yt-dlp не встановлено на сервері.") from exc

    temp_dir = Path(tempfile.mkdtemp(prefix="bot_media_"))
    try:
        return await asyncio.to_thread(_download_sync, url, temp_dir, max_size_mb, platform)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
