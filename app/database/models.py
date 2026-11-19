from datetime import datetime, UTC
from sqlalchemy import (
    BigInteger,
    Integer,
    String,
    Text,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    text as sql_text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    """Повертає поточний час в UTC без tzinfo для коректного порівняння в SQLite."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now
    )


class ChatLog(Base):
    __tablename__ = "chat_logs"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.user_id"), index=True, nullable=True
    )
    text: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    is_processed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sql_text("0")
    )

    # Rich context & anti-hallucination metadata
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reply_to_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reply_to_author: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reply_to_snippet: Mapped[str | None] = mapped_column(String(240), nullable=True)
    quote_text: Mapped[str | None] = mapped_column(String(240), nullable=True)
    message_thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    forward_origin: Mapped[str | None] = mapped_column(String(160), nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_bot_message: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sql_text("0")
    )
    edited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User | None] = relationship("User")

    __table_args__ = (
        Index("ix_chat_logs_chat_timestamp", "chat_id", "timestamp"),
        Index("ix_chat_logs_unprocessed", "chat_id", "is_processed", "timestamp"),
        Index("idx_chat_reply_msg", "chat_id", "reply_to_message_id"),
        Index("idx_chat_thread", "chat_id", "message_thread_id"),
    )


class ChatDigest(Base):
    __tablename__ = "chat_digests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    digest_text: Mapped[str] = mapped_column(Text)
    keywords: Mapped[str] = mapped_column(
        Text, default="[]", server_default=sql_text("'[]'")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    message_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    structured_json: Mapped[str | None] = mapped_column(Text, nullable=True)
