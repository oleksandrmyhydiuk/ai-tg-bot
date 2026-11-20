from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


def _coerce_msg_ids(value: Any) -> list[int]:
    """Coerces message ID list (strings like '#1045' or '1045') to list of int."""
    if value is None:
        return []
    if isinstance(value, (int, str)):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []

    result: list[int] = []
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            if item > 0 and item not in result:
                result.append(item)
        elif isinstance(item, str):
            matches = re.findall(r"\d+", item)
            for m in matches:
                try:
                    num = int(m)
                    if num > 0 and num not in result:
                        result.append(num)
                except ValueError:
                    continue
    return result


def _coerce_participants(value: Any) -> list[str]:
    """Normalizes participant list."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
        return parts
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in value:
        if item is None:
            continue
        s = str(item).strip()
        if s and s not in result:
            result.append(s)
    return result


def _clean_plain_text(text: str) -> str:
    """Strips Markdown asterisks and backticks while preserving #ID citations."""
    if not text:
        return ""
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    cleaned = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"\1", cleaned)
    cleaned = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = cleaned.replace("**", "").replace("`", "")
    return cleaned.strip()


class TopicItem(BaseModel):
    title: str
    summary: str
    participants: list[str] = Field(default_factory=list)
    evidence_msg_ids: list[int] = Field(default_factory=list)
    thread_id: int | None = None

    @field_validator("evidence_msg_ids", mode="before")
    @classmethod
    def validate_evidence_msg_ids(cls, v: Any) -> list[int]:
        return _coerce_msg_ids(v)

    @field_validator("participants", mode="before")
    @classmethod
    def validate_participants(cls, v: Any) -> list[str]:
        return _coerce_participants(v)

    @field_validator("thread_id", mode="before")
    @classmethod
    def validate_thread_id(cls, v: Any) -> int | None:
        if v is None or v == "":
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            m = re.search(r"\d+", v)
            if m:
                try:
                    return int(m.group(0))
                except ValueError:
                    return None
        return None


class DecisionItem(BaseModel):
    text: str
    status: Literal["confirmed", "proposed", "open_question"] = "proposed"
    participants: list[str] = Field(default_factory=list)
    evidence_msg_ids: list[int] = Field(default_factory=list)

    @field_validator("evidence_msg_ids", mode="before")
    @classmethod
    def validate_evidence_msg_ids(cls, v: Any) -> list[int]:
        return _coerce_msg_ids(v)

    @field_validator("participants", mode="before")
    @classmethod
    def validate_participants(cls, v: Any) -> list[str]:
        return _coerce_participants(v)

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, v: Any) -> Literal["confirmed", "proposed", "open_question"]:
        if not isinstance(v, str):
            return "proposed"
        norm = v.strip().lower()
        if norm in ("confirmed", "підтверджено", "домовлено", "agreed", "resolved"):
            return "confirmed"
        if norm in ("open_question", "open", "question", "відкрите питання", "питання", "unresolved"):
            return "open_question"
        return "proposed"


class ChatSummarySchema(BaseModel):
    is_low_signal: bool = False
    low_signal_reason: str | None = None
    topics: list[TopicItem] = Field(default_factory=list)
    decisions_and_outcomes: list[DecisionItem] = Field(default_factory=list)
    unverified_or_unclear_context: list[str] = Field(default_factory=list)

    @field_validator("unverified_or_unclear_context", mode="before")
    @classmethod
    def validate_unverified_context(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v.strip()] if v.strip() else []
        if not isinstance(v, (list, tuple, set)):
            return []
        return [str(x).strip() for x in v if x is not None and str(x).strip()]


def parse_summary_json(raw_text: str) -> ChatSummarySchema | None:
    """
    Parses JSON response from LLM, stripping markdown code blocks,
    and validates schema with ChatSummarySchema.
    """
    if not raw_text or not raw_text.strip():
        return None

    cleaned = raw_text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()

    candidates: list[str] = [cleaned]
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        extracted = cleaned[first_brace : last_brace + 1]
        if extracted != cleaned:
            candidates.append(extracted)

    for candidate in candidates:
        for attempt_str in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
            try:
                data = json.loads(attempt_str)
                if isinstance(data, dict):
                    return ChatSummarySchema.model_validate(data)
            except Exception:
                continue

    logger.warning("Failed to parse summary JSON from LLM response (length=%d).", len(raw_text))
    return None


def _speaker_matches(participant: str, valid_speakers: set[str]) -> bool:
    """
    Checks if participant name matches real speaker list in window
    (case-insensitive exact or token/substring match).
    """
    p_clean = participant.strip()
    if not p_clean:
        return False
    p_low = p_clean.lower()
    p_bare = p_low.lstrip("@")
    p_tokens = [t.lstrip("@") for t in re.split(r"[\s()@,#]+", p_low) if len(t.lstrip("@")) >= 2]

    for vs in valid_speakers:
        vs_clean = vs.strip()
        if not vs_clean:
            continue
        vs_low = vs_clean.lower()
        vs_bare = vs_low.lstrip("@")
        if p_low == vs_low or p_bare == vs_bare:
            return True
        if (len(p_bare) >= 2 and p_bare in vs_low) or (len(vs_bare) >= 2 and vs_bare in p_low):
            return True
        vs_tokens = {t.lstrip("@") for t in re.split(r"[\s()@,#]+", vs_low) if len(t.lstrip("@")) >= 2}
        if p_tokens and any(tok in vs_tokens for tok in p_tokens):
            return True

    return False


def verify_and_prune_summary(
    parsed: ChatSummarySchema,
    valid_msg_ids: set[int],
    valid_speakers: set[str] | None = None,
) -> ChatSummarySchema:
    """
    Verifies generated summary against ground-truth message_ids and speaker names in window.
    Removes fabricated message_ids, drops items without valid evidence_msg_ids,
    and filters out hallucinated participants.
    """
    enforce_msg_ids = bool(valid_msg_ids)
    enforce_speakers = bool(valid_speakers)

    verified_topics: list[TopicItem] = []
    for topic in parsed.topics:
        if enforce_msg_ids:
            kept_ids: list[int] = []
            for mid in topic.evidence_msg_ids:
                if mid in valid_msg_ids and mid not in kept_ids:
                    kept_ids.append(mid)
            if not kept_ids:
                logger.debug("Pruned topic without valid message_ids: %s", topic.title)
                continue
        else:
            kept_ids = list(dict.fromkeys(topic.evidence_msg_ids))

        if enforce_speakers and valid_speakers:
            kept_participants = [
                p for p in topic.participants if _speaker_matches(p, valid_speakers)
            ]
        else:
            kept_participants = list(topic.participants)

        verified_topics.append(
            TopicItem(
                title=topic.title,
                summary=topic.summary,
                participants=kept_participants,
                evidence_msg_ids=kept_ids,
                thread_id=topic.thread_id,
            )
        )

    verified_decisions: list[DecisionItem] = []
    for decision in parsed.decisions_and_outcomes:
        if enforce_msg_ids:
            kept_ids = []
            for mid in decision.evidence_msg_ids:
                if mid in valid_msg_ids and mid not in kept_ids:
                    kept_ids.append(mid)
            if not kept_ids:
                logger.debug("Pruned decision/outcome without valid message_ids: %s", decision.text)
                continue
        else:
            kept_ids = list(dict.fromkeys(decision.evidence_msg_ids))

        if enforce_speakers and valid_speakers:
            kept_participants = [
                p for p in decision.participants if _speaker_matches(p, valid_speakers)
            ]
        else:
            kept_participants = list(decision.participants)

        verified_decisions.append(
            DecisionItem(
                text=decision.text,
                status=decision.status,
                participants=kept_participants,
                evidence_msg_ids=kept_ids,
            )
        )

    is_low_signal = parsed.is_low_signal
    low_signal_reason = parsed.low_signal_reason

    if not verified_topics and not verified_decisions:
        is_low_signal = True
        if not low_signal_reason:
            low_signal_reason = (
                "Не знайдено підтверджених змістовних тем або домовленостей у межах наявних повідомлень."
            )

    return ChatSummarySchema(
        is_low_signal=is_low_signal,
        low_signal_reason=low_signal_reason,
        topics=verified_topics,
        decisions_and_outcomes=verified_decisions,
        unverified_or_unclear_context=list(parsed.unverified_or_unclear_context),
    )


def format_message_link(
    chat_id: int | None,
    message_id: int,
    chat_username: str | None = None,
) -> str:
    """
    Constructs link to specific Telegram message:
    - public chat with username -> https://t.me/<username>/<message_id>
    - private supergroup (-100...) -> https://t.me/c/<internal_id>/<message_id>
    - group without username or private chat -> #<message_id>
    """
    if chat_username and chat_username.strip():
        clean_username = chat_username.strip().lstrip("@")
        if clean_username:
            return f"https://t.me/{clean_username}/{message_id}"

    if chat_id is not None:
        chat_id_str = str(chat_id)
        if chat_id_str.startswith("-100") and len(chat_id_str) > 4:
            return f"https://t.me/c/{chat_id_str[4:]}/{message_id}"

    return f"#{message_id}"


def format_citation_badge(
    evidence_msg_ids: list[int],
    chat_id: int | None = None,
    chat_username: str | None = None,
    max_links: int = 2,
) -> str:
    """
    Constructs compact citation link badge for a report item (up to max_links citations).
    """
    if not evidence_msg_ids or max_links <= 0:
        return ""

    unique_ids = list(dict.fromkeys(evidence_msg_ids))[:max_links]
    links = [format_message_link(chat_id, mid, chat_username) for mid in unique_ids]
    if not links:
        return ""

    if links[0].startswith("https://t.me/"):
        label = "джерело" if len(links) == 1 else "джерела"
        return f" ({label}: {', '.join(links)})"

    return f" [{', '.join(links)}]"


def render_summary_text(
    verified: ChatSummarySchema,
    chat_id: int | None = None,
    chat_username: str | None = None,
    show_citations: bool = True,
    header_note: str | None = None,
) -> str:
    """
    Renders verified ChatSummarySchema into clean plain text without markdown syntax.
    """
    sections: list[str] = []

    if header_note and header_note.strip():
        sections.append(_clean_plain_text(header_note))

    if verified.is_low_signal and not verified.topics and not verified.decisions_and_outcomes:
        base_msg = (
            "За цей період у чаті не було змістовних обговорень або конкретних домовленостей "
            "(переважно короткі репліки чи загальне спілкування)."
        )
        if verified.low_signal_reason and verified.low_signal_reason.strip():
            reason_clean = _clean_plain_text(verified.low_signal_reason)
            if reason_clean.lower() not in base_msg.lower():
                base_msg = f"{base_msg}\nПримітка: {reason_clean}"
        sections.append(base_msg)
        return "\n\n".join(sections)

    if verified.topics:
        topic_lines = ["1. Головні теми обговорення:"]
        for topic in verified.topics:
            title_clean = _clean_plain_text(topic.title)
            summary_clean = _clean_plain_text(topic.summary)
            participants_clean = [_clean_plain_text(p) for p in topic.participants if _clean_plain_text(p)]
            participants_str = f" ({', '.join(participants_clean)})" if participants_clean else ""
            citation_badge = (
                format_citation_badge(topic.evidence_msg_ids, chat_id=chat_id, chat_username=chat_username)
                if show_citations
                else ""
            )
            topic_lines.append(f"- {title_clean}{participants_str}: {summary_clean}{citation_badge}")
        sections.append("\n".join(topic_lines))

    if verified.decisions_and_outcomes:
        status_map = {
            "confirmed": "[Підтверджено]",
            "proposed": "[Пропозиція]",
            "open_question": "[Відкрите питання]",
        }
        decision_lines = ["2. Висновки, рішення та відкриті питання:"]
        for decision in verified.decisions_and_outcomes:
            status_prefix = status_map.get(decision.status, "[Пропозиція]")
            text_clean = _clean_plain_text(decision.text)
            participants_clean = [_clean_plain_text(p) for p in decision.participants if _clean_plain_text(p)]
            participants_str = f" ({', '.join(participants_clean)})" if participants_clean else ""
            citation_badge = (
                format_citation_badge(decision.evidence_msg_ids, chat_id=chat_id, chat_username=chat_username)
                if show_citations
                else ""
            )
            decision_lines.append(f"- {status_prefix} {text_clean}{participants_str}{citation_badge}")
        sections.append("\n".join(decision_lines))

    if verified.unverified_or_unclear_context:
        unclear_items = [_clean_plain_text(item) for item in verified.unverified_or_unclear_context if _clean_plain_text(item)]
        if unclear_items:
            unclear_lines = ["Неоднозначний контекст:"]
            for item in unclear_items:
                unclear_lines.append(f"- {item}")
            sections.append("\n".join(unclear_lines))

    return "\n\n".join(sections).strip()
