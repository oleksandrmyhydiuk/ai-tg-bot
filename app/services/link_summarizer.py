from __future__ import annotations

import asyncio
from dataclasses import dataclass
from html.parser import HTMLParser
import ipaddress
import logging
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MAX_HTML_BYTES = 2 * 1024 * 1024  # 2 MB
FETCH_TIMEOUT_SECONDS = 15
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; HybridTelegramBot/1.0; +https://core.telegram.org/bots)"
)

_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?)>]}'\"»”"

_YOUTUBE_HOSTS = frozenset({"youtube.com", "youtu.be", "m.youtube.com"})

_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "nav", "footer"})
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "ul",
        "ol",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "article",
        "section",
        "header",
        "main",
        "tr",
        "blockquote",
        "pre",
        "hr",
    }
)


class LinkFetchError(Exception):
    """Raised when a URL is invalid, blocked by SSRF policy, unreachable, or empty."""


@dataclass
class ExtractedLinkContent:
    url: str
    title: str
    text: str
    source_type: str  # "youtube" or "webpage"


def _is_ip_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Returns True only if the IP address is globally routable and not private/local/reserved."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    ):
        return False
    return True


def validate_public_http_url(url: str) -> str:
    """
    Strictly validates an HTTP/HTTPS URL against SSRF attacks:
    - Only http / https schemes allowed.
    - Rejects embedded credentials (user:pass@host).
    - Rejects non-global IP literals (loopback, private, link-local, reserved, multicast).
    - Resolves DNS via socket.getaddrinfo and rejects hosts resolving to any non-global IP.
    """
    cleaned = (url or "").strip()
    if not cleaned:
        raise LinkFetchError("Порожнє посилання.")

    parsed = urlparse(cleaned)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise LinkFetchError("Підтримуються лише HTTP та HTTPS посилання.")

    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in (parsed.netloc or "")
    ):
        raise LinkFetchError("Посилання з вбудованими обліковими даними заборонені.")

    hostname = (parsed.hostname or "").strip().rstrip(".")
    if not hostname:
        raise LinkFetchError("Некоректне посилання: відсутній домен.")

    if hostname.lower() in ("localhost", "localhost.localdomain"):
        raise LinkFetchError("Доступ до локальних адрес заборонено.")

    # Check if hostname is an IP literal
    try:
        ip_literal = ipaddress.ip_address(hostname)
    except ValueError:
        ip_literal = None

    if ip_literal is not None:
        if not _is_ip_public(ip_literal):
            raise LinkFetchError("Доступ до приватних або локальних IP-адрес заборонено.")
        return cleaned

    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise LinkFetchError("Некоректний порт у посиланні.") from exc

    try:
        addr_infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise LinkFetchError(f"Не вдалося знайти домен: {hostname}.") from exc
    except OSError as exc:
        raise LinkFetchError(f"Помилка DNS-запиту для {hostname}.") from exc

    if not addr_infos:
        raise LinkFetchError(f"Домен {hostname} не має доступних IP-адрес.")

    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        try:
            resolved_ip = ipaddress.ip_address(ip_str)
        except ValueError as exc:
            raise LinkFetchError("Некоректна IP-адреса при резолвінгу домену.") from exc
        if not _is_ip_public(resolved_ip):
            raise LinkFetchError(
                "Домен веде на приватну або локальну IP-адресу (заблоковано з міркувань безпеки)."
            )

    return cleaned


def extract_first_http_url(text: str) -> str | None:
    """Extracts the first HTTP/HTTPS URL from free text, or returns None."""
    if not text:
        return None
    match = _HTTP_URL_RE.search(text)
    if not match:
        return None
    candidate = match.group(0).rstrip(_TRAILING_PUNCT)
    return candidate or None


def _is_youtube_url(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname in _YOUTUBE_HOSTS or hostname.endswith(".youtube.com")


def _format_duration(seconds: int | float | None) -> str | None:
    if seconds is None:
        return None
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return None
    if total < 0:
        return None
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class _ReadableHTMLParser(HTMLParser):
    """Stdlib HTML parser that extracts <title> and clean readable text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_stack: list[str] = []
        self._in_title = False
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        if tag_lower in _SKIP_TAGS:
            self._skip_stack.append(tag_lower)
            return
        if self._skip_stack:
            return
        if tag_lower == "title":
            self._in_title = True
        elif tag_lower in _BLOCK_TAGS:
            self._text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if self._skip_stack and tag_lower in _SKIP_TAGS:
            # Pop matching tag if present on stack
            for idx in range(len(self._skip_stack) - 1, -1, -1):
                if self._skip_stack[idx] == tag_lower:
                    del self._skip_stack[idx:]
                    break
            return
        if tag_lower == "title":
            self._in_title = False
        elif not self._skip_stack and tag_lower in _BLOCK_TAGS:
            self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_stack:
            return
        if self._in_title:
            self._title_parts.append(data)
        else:
            self._text_parts.append(data)

    def get_title(self) -> str:
        raw = "".join(self._title_parts)
        return re.sub(r"\s+", " ", raw).strip()

    def get_text(self) -> str:
        raw = "".join(self._text_parts)
        lines: list[str] = []
        for line in raw.splitlines():
            normalized = re.sub(r"[ \t\r\f\v]+", " ", line).strip()
            if normalized:
                lines.append(normalized)
        return "\n".join(lines)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validates every redirect target against SSRF before following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_youtube_sync(url: str, max_chars: int) -> ExtractedLinkContent:
    import yt_dlp  # type: ignore[import-untyped]

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": FETCH_TIMEOUT_SECONDS,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not isinstance(info, dict):
        raise LinkFetchError("Не вдалося отримати метадані YouTube відео.")

    title = str(info.get("title") or "").strip() or "YouTube відео"
    uploader = str(info.get("uploader") or info.get("channel") or "").strip()
    duration_str = _format_duration(info.get("duration"))
    description = str(info.get("description") or "").strip()

    parts: list[str] = [f"Назва відео: {title}"]
    if uploader:
        parts.append(f"Автор / канал: {uploader}")
    if duration_str:
        parts.append(f"Тривалість: {duration_str}")

    chapters = info.get("chapters")
    if isinstance(chapters, list) and chapters:
        chapter_lines = []
        for ch in chapters[:30]:
            if not isinstance(ch, dict):
                continue
            ch_title = str(ch.get("title") or "").strip()
            ch_start = _format_duration(ch.get("start_time"))
            if ch_title:
                chapter_lines.append(f"- {ch_start + ' ' if ch_start else ''}{ch_title}")
        if chapter_lines:
            parts.append("Розділи:\n" + "\n".join(chapter_lines))

    manual_subs = info.get("subtitles")
    auto_subs = info.get("automatic_captions")
    sub_notes: list[str] = []
    if isinstance(manual_subs, dict) and manual_subs:
        langs = ", ".join(sorted(str(k) for k in list(manual_subs.keys())[:10]))
        sub_notes.append(f"ручні ({langs})")
    if isinstance(auto_subs, dict) and auto_subs:
        langs = ", ".join(sorted(str(k) for k in list(auto_subs.keys())[:10]))
        sub_notes.append(f"автоматичні ({langs})")
    if sub_notes:
        parts.append("Доступні субтитри: " + "; ".join(sub_notes))

    if description:
        parts.append(f"Опис:\n{description}")

    combined = "\n\n".join(parts).strip()
    if len(combined) > max_chars:
        combined = combined[:max_chars].rstrip() + "..."

    canonical_url = str(info.get("webpage_url") or url).strip() or url
    return ExtractedLinkContent(
        url=canonical_url,
        title=title,
        text=combined,
        source_type="youtube",
    )


def _fetch_webpage_sync(url: str, max_chars: int) -> ExtractedLinkContent:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
            "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )
    opener = urllib.request.build_opener(_SafeRedirectHandler())

    try:
        with opener.open(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            final_url = resp.geturl() or url
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if content_type and not any(
                allowed in content_type
                for allowed in (
                    "text/",
                    "application/xhtml+xml",
                    "application/xml",
                    "application/json",
                )
            ):
                raise LinkFetchError(
                    "Посилання веде на непідтримуваний тип файлу (очікується вебсторінка або текст)."
                )

            raw_bytes = resp.read(MAX_HTML_BYTES + 1)
            if len(raw_bytes) > MAX_HTML_BYTES:
                raw_bytes = raw_bytes[:MAX_HTML_BYTES]

            charset = resp.headers.get_content_charset() or "utf-8"
    except LinkFetchError:
        raise
    except urllib.error.HTTPError as exc:
        raise LinkFetchError(f"Сторінка повернула помилку HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise LinkFetchError("Не вдалося підключитися до сайту за посиланням.") from exc
    except TimeoutError as exc:
        raise LinkFetchError("Перевищено час очікування відповіді від сайту.") from exc
    except Exception as exc:
        raise LinkFetchError("Не вдалося завантажити вміст сторінки.") from exc

    try:
        decoded = raw_bytes.decode(charset, errors="replace")
    except LookupError:
        decoded = raw_bytes.decode("utf-8", errors="replace")

    if "text/plain" in content_type or "application/json" in content_type:
        title = urlparse(final_url).hostname or final_url
        cleaned_text = decoded.strip()
    else:
        parser = _ReadableHTMLParser()
        parser.feed(decoded)
        parser.close()
        title = parser.get_title() or urlparse(final_url).hostname or final_url
        cleaned_text = parser.get_text()

    if not cleaned_text:
        raise LinkFetchError(
            "На сторінці не вдалося знайти читабельний текст (можливо, сторінка вимагає JavaScript або авторизацію)."
        )

    if len(cleaned_text) > max_chars:
        cleaned_text = cleaned_text[:max_chars].rstrip() + "..."

    return ExtractedLinkContent(
        url=final_url,
        title=title,
        text=cleaned_text,
        source_type="webpage",
    )


async def fetch_link_content(url: str, max_chars: int = 18000) -> ExtractedLinkContent:
    """
    Validates URL for SSRF safety and extracts readable text from a YouTube video
    (via yt-dlp metadata with webpage fallback) or a regular webpage.
    """
    validated_url = await asyncio.to_thread(validate_public_http_url, url)

    if _is_youtube_url(validated_url):
        try:
            return await asyncio.to_thread(_fetch_youtube_sync, validated_url, max_chars)
        except ImportError:
            logger.info("yt_dlp not available; falling back to HTML fetch for %s", validated_url)
        except Exception as exc:
            logger.warning(
                "yt_dlp extraction failed for %s (%s); falling back to HTML fetch",
                validated_url,
                exc,
            )

    return await asyncio.to_thread(_fetch_webpage_sync, validated_url, max_chars)
