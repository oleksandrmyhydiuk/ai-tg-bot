from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, UTC
import json
import logging
import re
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import ChatDigest, ChatLog, User

logger = logging.getLogger(__name__)

try:
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except Exception:
    KYIV_TZ = timezone(timedelta(hours=2))

MEDIA_TYPE_LABELS: dict[str, str] = {
    "photo": "фото",
    "video": "відео",
    "voice": "голосове повідомлення",
    "video_note": "відеоповідомлення",
    "document": "документ",
    "sticker": "стікер",
    "poll": "опитування",
    "audio": "аудіо",
    "animation": "анімація",
    "location": "геолокація",
}


@dataclass
class SummaryContextBundle:
    transcript_text: str
    valid_msg_ids: set[int] = field(default_factory=set)
    valid_speakers: set[str] = field(default_factory=set)
    message_count: int = 0
    digest_count: int = 0
    was_truncated: bool = False
    total_available_messages: int = 0
    period_start: datetime | None = None
    period_end: datetime | None = None
    single_digest_fast_path: str | None = None
    raw_fallback_lines: list[str] = field(default_factory=list)


def format_kyiv_datetime(dt: datetime | None, include_date: bool = True) -> str:
    """
    Converts naive UTC (or aware) datetime to Europe/Kyiv timezone
    and formats as '%m-%d %H:%M' (if include_date=True) or '%H:%M' (if False).
    """
    if dt is None:
        return "--.-- --:--" if include_date else "--:--"
    if dt.tzinfo is None:
        aware_utc = dt.replace(tzinfo=UTC)
    else:
        aware_utc = dt
    kyiv_dt = aware_utc.astimezone(KYIV_TZ)
    return kyiv_dt.strftime("%m-%d %H:%M" if include_date else "%H:%M")


def _truncate_single_line(text: str | None, max_len: int = 60) -> str:
    """Normalizes string to a single line without linebreaks and truncates to max_len chars."""
    if not text:
        return ""
    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) <= max_len:
        return clean
    return clean[: max(1, max_len - 3)].rstrip() + "..."


def _base_speaker_name(log: ChatLog, user: User | None) -> str:
    """Determines base author name of message before namesake disambiguation."""
    if getattr(log, "is_bot_message", False):
        return "Бот [БОТ]"
    if user is not None and getattr(user, "full_name", None):
        name = user.full_name.strip()
        if name:
            return name
    sender_name = getattr(log, "sender_name", None)
    if sender_name and str(sender_name).strip():
        return str(sender_name).strip()
    uid = getattr(log, "user_id", None) or 0
    return f"User#{uid}"


def _build_speaker_resolver(
    rows: list[tuple[ChatLog, User | None]],
) -> tuple[dict[tuple[int | None, str], str], set[str]]:
    """
    Analyzes all (ChatLog, User | None) pairs in window to detect namesakes
    (different user_ids with the same full_name) and returns:
    - dict (user_id, base_name) -> disambiguated_label
    - set of all valid speaker name variants (valid_speakers)
    """
    name_to_identities: dict[str, set[int | None]] = {}
    for log, user in rows:
        if getattr(log, "is_bot_message", False):
            continue
        base_name = _base_speaker_name(log, user)
        uid = getattr(log, "user_id", None)
        name_to_identities.setdefault(base_name.lower(), set()).add(uid)

    duplicated_names = {
        norm_name for norm_name, uids in name_to_identities.items() if len(uids) > 1
    }

    label_map: dict[tuple[int | None, str], str] = {}
    valid_speakers: set[str] = set()

    for log, user in rows:
        base_name = _base_speaker_name(log, user)
        uid = getattr(log, "user_id", None)
        key = (uid, base_name)

        if getattr(log, "is_bot_message", False):
            label = "Бот [БОТ]"
            valid_speakers.update({"Бот [БОТ]", "Бот"})
        elif base_name.lower() in duplicated_names:
            username = getattr(user, "username", None) if user is not None else None
            if username and str(username).strip():
                clean_uname = str(username).strip().lstrip("@")
                label = f"{base_name} (@{clean_uname})"
                valid_speakers.add(f"@{clean_uname}")
                valid_speakers.add(clean_uname)
            else:
                label = f"{base_name} (#{uid or 0})"
        else:
            label = base_name
            username = getattr(user, "username", None) if user is not None else None
            if username and str(username).strip():
                clean_uname = str(username).strip().lstrip("@")
                valid_speakers.add(f"@{clean_uname}")
                valid_speakers.add(clean_uname)

        label_map[key] = label
        valid_speakers.add(label)
        valid_speakers.add(base_name)

    return label_map, valid_speakers


def _resolve_speaker(
    log: ChatLog,
    user: User | None,
    label_map: dict[tuple[int | None, str], str],
) -> str:
    base_name = _base_speaker_name(log, user)
    uid = getattr(log, "user_id", None)
    return label_map.get((uid, base_name), base_name)


def _format_single_message_line(
    log: ChatLog,
    user: User | None,
    label_map: dict[tuple[int | None, str], str],
    msg_lookup: dict[int, tuple[ChatLog, User | None]],
) -> str:
    """
    Formats single message line with format:
    [{MM-DD HH:MM}] #{message_id} | {speaker}{reply_part}{forward_part}{media_part}: {clean_text}
    """
    ts_str = format_kyiv_datetime(getattr(log, "timestamp", None), include_date=True)
    msg_id = getattr(log, "message_id", 0)
    speaker = _resolve_speaker(log, user, label_map)

    raw_text = (getattr(log, "text", None) or "").strip()
    clean_text = re.sub(r"\s+", " ", raw_text).strip()

    reply_to_id = getattr(log, "reply_to_message_id", None)
    reply_to_author = getattr(log, "reply_to_author", None)
    reply_to_snippet = getattr(log, "reply_to_snippet", None)
    quote_text = getattr(log, "quote_text", None)

    # Backward compatibility with legacy pre-migration rows: "(in reply to Author): text"
    is_legacy_prefixed = (
        clean_text.startswith("(у відповідь ")
        and reply_to_id is None
        and not reply_to_author
    )

    reply_part = ""
    if not is_legacy_prefixed and (reply_to_id is not None or reply_to_author):
        parent_author: str | None = None
        parent_snippet: str | None = None

        if reply_to_id is not None and reply_to_id in msg_lookup:
            p_log, p_user = msg_lookup[reply_to_id]
            parent_author = _resolve_speaker(p_log, p_user, label_map)
            parent_snippet = getattr(p_log, "text", None)

        if not parent_author:
            parent_author = (str(reply_to_author).strip() if reply_to_author else "учасник")
        if not parent_snippet and reply_to_snippet:
            parent_snippet = str(reply_to_snippet)

        snippet_60 = _truncate_single_line(parent_snippet, 60)
        quote_60 = _truncate_single_line(quote_text, 60)
        quote_suffix = f', цитата: "{quote_60}"' if quote_60 else ""

        if reply_to_id is not None:
            if snippet_60:
                reply_part = f' (↩ #{reply_to_id} {parent_author}: "{snippet_60}"{quote_suffix})'
            else:
                reply_part = f" (↩ #{reply_to_id} {parent_author}{quote_suffix})"
        else:
            if snippet_60:
                reply_part = f' (↩ {parent_author}: "{snippet_60}"{quote_suffix})'
            else:
                reply_part = f" (↩ {parent_author}{quote_suffix})"

    forward_origin = getattr(log, "forward_origin", None)
    forward_part = f" [переслано: {_truncate_single_line(str(forward_origin), 80)}]" if forward_origin else ""

    media_type = getattr(log, "media_type", None)
    media_part = ""
    if media_type:
        media_key = str(media_type).strip().lower()
        media_label = MEDIA_TYPE_LABELS.get(media_key, media_key)
        text_lower = clean_text.lower()
        if f"[{media_label.lower()}" not in text_lower and f"[{media_key}" not in text_lower:
            media_part = f" [{media_label}]"

    if not clean_text and media_part:
        clean_text = media_part.strip()
        media_part = ""

    return f"[{ts_str}] #{msg_id} | {speaker}{reply_part}{forward_part}{media_part}: {clean_text}"


def _format_rows_with_threads(
    rows: list[tuple[ChatLog, User | None]],
    label_map: dict[tuple[int | None, str], str],
    msg_lookup: dict[int, tuple[ChatLog, User | None]],
) -> list[str]:
    """
    Formats list of messages adding headers [Тема/Топік #{thread_id}]
    if multiple distinct non-null message_thread_ids exist.
    """
    distinct_threads = {
        getattr(log, "message_thread_id", None)
        for log, _ in rows
        if getattr(log, "message_thread_id", None) is not None
    }
    has_multiple_threads = len(distinct_threads) > 1

    lines: list[str] = []
    current_thread: int | None | object = object()

    for log, user in rows:
        if has_multiple_threads:
            thread_id = getattr(log, "message_thread_id", None)
            if thread_id != current_thread:
                current_thread = thread_id
                if thread_id is not None:
                    lines.append(f"[Тема/Топік #{thread_id}]")
                else:
                    lines.append("[Загальний потік чату]")
        lines.append(_format_single_message_line(log, user, label_map, msg_lookup))

    return lines


async def hydrate_orphan_parents(
    session: AsyncSession,
    chat_id: int,
    logs: list[ChatLog],
) -> dict[int, tuple[ChatLog, User | None]]:
    """
    Finds all reply_to_message_id in logs not present in current window
    and fetches them from ChatLog with User in a single batch query.
    """
    if not logs:
        return {}

    present_ids = {getattr(log, "message_id", None) for log in logs}
    missing_ids = {
        getattr(log, "reply_to_message_id", None)
        for log in logs
        if getattr(log, "reply_to_message_id", None) is not None
        and getattr(log, "reply_to_message_id", None) not in present_ids
    }
    missing_int_ids = [int(mid) for mid in missing_ids if mid is not None]
    if not missing_int_ids:
        return {}

    stmt = (
        select(ChatLog, User)
        .outerjoin(User, ChatLog.user_id == User.user_id)
        .where(
            ChatLog.chat_id == chat_id,
            ChatLog.message_id.in_(missing_int_ids),
        )
        .order_by(ChatLog.timestamp.asc(), ChatLog.message_id.asc())
    )
    fetched = (await session.execute(stmt)).all()
    return {row[0].message_id: (row[0], row[1]) for row in fetched}


def format_messages_for_digest(
    rows: list[tuple[ChatLog, User | None]],
    orphan_parents: dict[int, tuple[ChatLog, User | None]] | None = None,
) -> SummaryContextBundle:
    """
    Formats a batch of messages (ChatLog, User | None) for hourly digest generation.
    Includes context section for parent messages outside window
    and main message section inside <chat_transcript>...</chat_transcript>.
    """
    normalized_rows: list[tuple[ChatLog, User | None]] = [
        (r[0], r[1]) for r in rows
    ]
    orphan_dict = orphan_parents or {}
    orphan_list = sorted(
        orphan_dict.values(),
        key=lambda pair: (
            getattr(pair[0], "timestamp", None) or datetime.min,
            getattr(pair[0], "message_id", 0),
        ),
    )

    all_pairs = [*orphan_list, *normalized_rows]
    label_map, valid_speakers = _build_speaker_resolver(all_pairs)

    msg_lookup: dict[int, tuple[ChatLog, User | None]] = {}
    for log, user in orphan_list:
        msg_lookup[log.message_id] = (log, user)
    for log, user in normalized_rows:
        msg_lookup[log.message_id] = (log, user)
        if getattr(log, "reply_to_author", None):
            valid_speakers.add(str(log.reply_to_author).strip())

    transcript_parts: list[str] = ["<chat_transcript>"]

    if orphan_list:
        transcript_parts.append(
            "[КОНТЕКСТ ДО ПЕРІОДУ (лише для розуміння відповідей, не включати як нові події)]"
        )
        for log, user in orphan_list:
            transcript_parts.append(_format_single_message_line(log, user, label_map, msg_lookup))
        transcript_parts.append("")

    transcript_parts.append("[ПОВІДОМЛЕННЯ ЗА ПЕРІОД]")
    main_lines = _format_rows_with_threads(normalized_rows, label_map, msg_lookup)
    transcript_parts.extend(main_lines)
    transcript_parts.append("</chat_transcript>")

    valid_msg_ids = {log.message_id for log, _ in normalized_rows}
    valid_msg_ids.update(orphan_dict.keys())

    timestamps = [
        getattr(log, "timestamp", None)
        for log, _ in normalized_rows
        if getattr(log, "timestamp", None) is not None
    ]
    period_start = min(timestamps) if timestamps else None
    period_end = max(timestamps) if timestamps else None

    fallback_lines = [
        f"• [{format_kyiv_datetime(getattr(log, 'timestamp', None), include_date=True)}] "
        f"{_resolve_speaker(log, user, label_map)}: {getattr(log, 'text', '')}"
        for log, user in normalized_rows
    ]

    return SummaryContextBundle(
        transcript_text="\n".join(transcript_parts),
        valid_msg_ids=valid_msg_ids,
        valid_speakers=valid_speakers,
        message_count=len(normalized_rows),
        digest_count=0,
        was_truncated=False,
        total_available_messages=len(normalized_rows),
        period_start=period_start,
        period_end=period_end,
        single_digest_fast_path=None,
        raw_fallback_lines=fallback_lines,
    )


def _extract_metadata_from_digest(
    digest: ChatDigest,
    valid_msg_ids: set[int],
    valid_speakers: set[str],
) -> None:
    """
    Extracts stored evidence_msg_ids and participants from ChatDigest.structured_json
    (as well as #ID mentions in digest text) to preserve valid citations.
    """
    structured_raw = getattr(digest, "structured_json", None)
    if structured_raw:
        try:
            data = json.loads(structured_raw)
            if isinstance(data, dict):
                for item in data.get("topics", []) or []:
                    if isinstance(item, dict):
                        for mid in item.get("evidence_msg_ids", []) or []:
                            if isinstance(mid, int) and mid > 0:
                                valid_msg_ids.add(mid)
                        for p in item.get("participants", []) or []:
                            if isinstance(p, str) and p.strip():
                                valid_speakers.add(p.strip())
                for item in data.get("decisions_and_outcomes", []) or []:
                    if isinstance(item, dict):
                        for mid in item.get("evidence_msg_ids", []) or []:
                            if isinstance(mid, int) and mid > 0:
                                valid_msg_ids.add(mid)
                        for p in item.get("participants", []) or []:
                            if isinstance(p, str) and p.strip():
                                valid_speakers.add(p.strip())
        except Exception:
            pass

    digest_text = getattr(digest, "digest_text", None) or ""
    for match in re.findall(r"#(\d+)", digest_text):
        try:
            mid = int(match)
            if mid > 0:
                valid_msg_ids.add(mid)
        except ValueError:
            continue


async def build_summary_context(
    session: AsyncSession,
    chat_id: int,
    cutoff_utc: datetime,
    hours: float,
) -> SummaryContextBundle:
    """
    Builds context for /summary using a Raw-First strategy:
    1) If all raw messages in period exist in ChatLog and fit within limits
       (summary_raw_message_limit and summary_raw_char_limit), uses purely raw
       messages with hydrated orphan replies (Pure Raw Mode), without lossy intermediate digests.
    2) If message count exceeds limit or older raw logs were pruned,
       combines hourly ChatDigest records for the period with fresh raw ChatLog messages.
    """
    raw_msg_limit = int(getattr(settings, "summary_raw_message_limit", 400))
    raw_char_limit = int(getattr(settings, "summary_raw_char_limit", 60000))

    count_stmt = select(func.count(ChatLog.message_id)).where(
        ChatLog.chat_id == chat_id,
        ChatLog.timestamp >= cutoff_utc,
    )
    total_available_messages = (await session.execute(count_stmt)).scalar_one() or 0

    raw_stmt = (
        select(ChatLog, User)
        .outerjoin(User, ChatLog.user_id == User.user_id)
        .where(
            ChatLog.chat_id == chat_id,
            ChatLog.timestamp >= cutoff_utc,
        )
        .order_by(ChatLog.timestamp.desc(), ChatLog.message_id.desc())
        .limit(raw_msg_limit)
    )
    raw_rows_fetched = list(reversed((await session.execute(raw_stmt)).all()))
    raw_rows: list[tuple[ChatLog, User | None]] = [(r[0], r[1]) for r in raw_rows_fetched]

    # Check Pure Raw Mode eligibility
    if 0 < total_available_messages <= raw_msg_limit and raw_rows:
        orphan_parents = await hydrate_orphan_parents(
            session, chat_id, [log for log, _ in raw_rows]
        )
        bundle = format_messages_for_digest(raw_rows, orphan_parents=orphan_parents)
        if len(bundle.transcript_text) <= raw_char_limit:
            return bundle

    # Combine ChatDigest + fresh/tail ChatLog mode (or truncated raw messages)
    digest_time_expr = func.coalesce(ChatDigest.period_end, ChatDigest.created_at)
    digest_order_expr = func.coalesce(ChatDigest.period_start, ChatDigest.created_at)
    d_stmt = (
        select(ChatDigest)
        .where(
            ChatDigest.chat_id == chat_id,
            digest_time_expr >= cutoff_utc,
        )
        .order_by(digest_order_expr.asc(), ChatDigest.id.asc())
        .limit(200)
    )
    digests = list((await session.execute(d_stmt)).scalars().all())

    if not digests and not raw_rows:
        return SummaryContextBundle(
            transcript_text="<chat_transcript>\n</chat_transcript>",
            valid_msg_ids=set(),
            valid_speakers=set(),
            message_count=0,
            digest_count=0,
            was_truncated=False,
            total_available_messages=0,
            period_start=None,
            period_end=None,
            single_digest_fast_path=None,
            raw_fallback_lines=[],
        )

    # Determine which raw messages to append after digests
    selected_raw_rows: list[tuple[ChatLog, User | None]]
    if digests:
        max_digest_end: datetime | None = None
        for d in digests:
            d_end = getattr(d, "period_end", None) or getattr(d, "created_at", None)
            if d_end is not None and (max_digest_end is None or d_end > max_digest_end):
                max_digest_end = d_end

        tail_rows = [
            (log, user)
            for log, user in raw_rows
            if not getattr(log, "is_processed", False)
            or (max_digest_end is not None and getattr(log, "timestamp", None) is not None and log.timestamp > max_digest_end)
        ]
        selected_raw_rows = tail_rows
    else:
        selected_raw_rows = raw_rows

    orphan_parents = await hydrate_orphan_parents(
        session, chat_id, [log for log, _ in selected_raw_rows]
    )
    orphan_list = sorted(
        orphan_parents.values(),
        key=lambda pair: (
            getattr(pair[0], "timestamp", None) or datetime.min,
            getattr(pair[0], "message_id", 0),
        ),
    )

    all_pairs = [*orphan_list, *selected_raw_rows]
    label_map, valid_speakers = _build_speaker_resolver(all_pairs)

    msg_lookup: dict[int, tuple[ChatLog, User | None]] = {}
    for log, user in orphan_list:
        msg_lookup[log.message_id] = (log, user)
    for log, user in selected_raw_rows:
        msg_lookup[log.message_id] = (log, user)
        if getattr(log, "reply_to_author", None):
            valid_speakers.add(str(log.reply_to_author).strip())

    valid_msg_ids: set[int] = {log.message_id for log, _ in selected_raw_rows}
    valid_msg_ids.update(orphan_parents.keys())

    digest_blocks: list[str] = []
    fallback_lines: list[str] = []

    for d in digests:
        _extract_metadata_from_digest(d, valid_msg_ids, valid_speakers)
        p_start = getattr(d, "period_start", None)
        p_end = getattr(d, "period_end", None) or getattr(d, "created_at", None)
        if p_start and p_end:
            time_header = f"{format_kyiv_datetime(p_start)} — {format_kyiv_datetime(p_end)}"
        else:
            time_header = format_kyiv_datetime(p_end)
        msg_cnt = getattr(d, "message_count", None)
        cnt_str = f" ({msg_cnt} повід.)" if msg_cnt else ""
        block = f"[Дайджест за період {time_header}{cnt_str}]\n{d.digest_text}"
        digest_blocks.append(block)
        fallback_lines.append(block)

    formatted_raw_lines = _format_rows_with_threads(selected_raw_rows, label_map, msg_lookup)

    was_truncated = total_available_messages > raw_msg_limit and not digests

    # Control character limit raw_char_limit (retaining newest messages)
    digests_section_text = "\n\n".join(digest_blocks)
    reserved_chars = len(digests_section_text) + 500
    available_for_raw = max(4000, raw_char_limit - reserved_chars)

    kept_raw_lines: list[str] = []
    running_chars = 0
    for line in reversed(formatted_raw_lines):
        line_len = len(line) + 1
        if running_chars + line_len > available_for_raw and kept_raw_lines:
            was_truncated = True
            break
        kept_raw_lines.append(line)
        running_chars += line_len
    kept_raw_lines.reverse()

    for log, user in selected_raw_rows[-len(kept_raw_lines) if kept_raw_lines else 0 :]:
        fallback_lines.append(
            f"• [{format_kyiv_datetime(getattr(log, 'timestamp', None), include_date=True)}] "
            f"{_resolve_speaker(log, user, label_map)}: {getattr(log, 'text', '')}"
        )

    transcript_parts: list[str] = ["<chat_transcript>"]
    if digest_blocks:
        transcript_parts.append("[ПОПЕРЕДНІ ПОГОДИННІ ДАЙДЖЕСТИ ЗА ПЕРІОД]")
        transcript_parts.append(digests_section_text)
        transcript_parts.append("")

    if orphan_list and kept_raw_lines:
        transcript_parts.append(
            "[КОНТЕКСТ ДО ПЕРІОДУ (лише для розуміння відповідей, не включати як нові події)]"
        )
        for log, user in orphan_list:
            transcript_parts.append(_format_single_message_line(log, user, label_map, msg_lookup))
        transcript_parts.append("")

    if kept_raw_lines:
        section_title = (
            "[СВІЖІ ПОВІДОМЛЕННЯ ЧАТУ]" if digest_blocks else "[ПОВІДОМЛЕННЯ ЗА ПЕРІОД]"
        )
        transcript_parts.append(section_title)
        transcript_parts.extend(kept_raw_lines)

    transcript_parts.append("</chat_transcript>")

    all_timestamps: list[datetime] = []
    for d in digests:
        for dt_attr in ("period_start", "period_end", "created_at"):
            dt_val = getattr(d, dt_attr, None)
            if dt_val is not None:
                all_timestamps.append(dt_val)
    for log, _ in selected_raw_rows:
        dt_val = getattr(log, "timestamp", None)
        if dt_val is not None:
            all_timestamps.append(dt_val)

    period_start = min(all_timestamps) if all_timestamps else None
    period_end = max(all_timestamps) if all_timestamps else None

    single_digest_fast_path: str | None = None
    if len(digests) == 1 and not selected_raw_rows:
        single_digest_fast_path = digests[0].digest_text

    # Effective message count calculation
    if digests:
        digest_msg_sum = sum((getattr(d, "message_count", None) or 0) for d in digests)
        effective_msg_count = digest_msg_sum + len(selected_raw_rows)
        if effective_msg_count == 0:
            effective_msg_count = total_available_messages or len(digests)
    else:
        effective_msg_count = len([
            line for line in kept_raw_lines
            if not line.startswith("[Тема/Топік") and not line.startswith("[Загальний потік")
        ])

    return SummaryContextBundle(
        transcript_text="\n".join(transcript_parts),
        valid_msg_ids=valid_msg_ids,
        valid_speakers=valid_speakers,
        message_count=effective_msg_count,
        digest_count=len(digests),
        was_truncated=was_truncated,
        total_available_messages=max(total_available_messages, effective_msg_count),
        period_start=period_start,
        period_end=period_end,
        single_digest_fast_path=single_digest_fast_path,
        raw_fallback_lines=fallback_lines,
    )
